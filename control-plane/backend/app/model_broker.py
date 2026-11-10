"""Trusted inference endpoint for a stage's pinned project gateway."""
from dataclasses import asdict, replace
from hashlib import sha256
import json
from typing import Annotated, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from app import broker_capabilities as ledger
from app.credential_store import read_credential
from app.database import GatewayConnectionRevision, WorkflowRun
from app.worker_pools import LeaseProof
from app.execution_admission import read_execution_pin
from app.gateway_connections import INFERENCE_PURPOSE, inference_descriptor, require_gateway_credential
from app.projects import ProjectScope, require_owned
from app.workflow_definitions import read_definition_pin
from factory.adapters.gateway_inference import GatewayTransport, assess_gateway_tokens
from factory.adapters.token_budget import MAX_COMPLETION_TOKENS, MAX_CONTEXT_TOKENS
from factory.application.model_sessions import ModelRequest, ModelResponse, ModelTransportError
from factory.domain.broker import CapabilityScope, MAX_ALLOWANCE, SessionLimits
from factory.workflows.redaction import redact, secret_scope

INFERENCE_PATH = "/api/v1/model-broker/inference"
MAX_REQUEST_BYTES = 256 * 1024
MODEL_OPERATION = "model.infer"


class Message(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    role: Literal["system", "user", "assistant"]
    content: str


class InferenceBody(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    model: str = Field(min_length=1, max_length=120)
    messages: list[Message] = Field(min_length=1, max_length=64)
    max_completion_tokens: Annotated[int, Field(gt=0, le=MAX_COMPLETION_TOKENS)]
    max_context_tokens: Annotated[int, Field(gt=0, le=MAX_CONTEXT_TOKENS)]
    request_id: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,120}$")


def _route(db, scope, alias, settings):
    project = ProjectScope(scope.project_id)
    run = require_owned(db, project, WorkflowRun, scope.run_id)
    pin = read_execution_pin(read_definition_pin(db, project, run.snapshot_id, settings))
    if not pin.route_revision_id:
        raise HTTPException(409, "Run has no pinned model gateway")
    if pin.gateway_alias is not None and pin.gateway_alias != alias:
        raise HTTPException(422, "Model alias does not match the pinned gateway route")
    revision = require_owned(db, project, GatewayConnectionRevision, pin.route_revision_id)
    return project, inference_descriptor(revision)


def execute_model_request(sessions, bearer: str, body: InferenceBody, settings) -> ModelResponse:
    request = ModelRequest(**body.model_dump())
    logical_id = sha256(body.request_id.encode()).hexdigest()
    request_digest = sha256(body.model_dump_json(exclude={"request_id"}).encode()).hexdigest()
    try:
        with sessions.begin() as db:
            scope = ledger.authorize_operation(db, bearer, "model", MODEL_OPERATION, settings=settings)
            project, connection = _route(db, scope, body.model, settings)
            credential = require_gateway_credential(db, project, connection.credential_ref, INFERENCE_PURPOSE)

        try:
            assessment = assess_gateway_tokens(connection, request)
        except (ValueError, ModelTransportError):
            raise HTTPException(422, "Model alias or request limits are invalid") from None

        # Resolve outside the workflow transaction. Recheck live credential and
        # bearer authority inside the fence before claiming dispatch.
        token = read_credential(credential, settings)
        transport = GatewayTransport(connection, token)
        with sessions.begin() as db:
            reservation = ledger.reserve_request(
                db, bearer, "model", logical_id, assessment.input_tokens,
                request.max_completion_tokens, operation=MODEL_OPERATION,
                request_digest=request_digest, settings=settings,
            )
            require_gateway_credential(db, project, connection.credential_ref, INFERENCE_PURPOSE)
            if not ledger.mark_dispatched(db, bearer, "model", reservation.id, settings=settings):
                raise HTTPException(409, "Logical model request was already dispatched")
    except ledger.CapabilityDenied:
        raise HTTPException(403, "Model capability or allowance denied") from None

    # A sandbox can choose its local request ID but cannot impersonate
    # another stage's correlation ID at the gateway.
    request = replace(request, request_id=reservation.id)
    try:
        response = transport.infer(request)
    except ModelTransportError as error:
        outcome = "uncertain" if error.uncertain else "rejected"
        with sessions.begin() as db:
            ledger.settle_request(db, reservation.id, None, outcome)
        if error.status_code == 429:
            raise HTTPException(429, "Model gateway rate or budget limit reached") from None
        raise HTTPException(502, "Model gateway request failed") from None

    usage = response.usage
    actual = None
    if (
        usage is not None and usage.input_tokens is not None and usage.output_tokens is not None
        and max(usage.input_tokens, usage.output_tokens) <= MAX_ALLOWANCE
    ):
        actual = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens}
    outcome = "completed" if actual is not None else "uncertain"
    exceeded = actual is not None and (
        actual["input_tokens"] > reservation.input_tokens
        or actual["output_tokens"] > reservation.output_tokens
    )
    with sessions.begin() as db:
        ledger.settle_request(db, reservation.id, actual, outcome)
        if exceeded:
            ledger.revoke_assignment(db, reservation.scope.assignment_id)
    if exceeded:
        raise HTTPException(502, "Gateway reported usage outside the reserved allowance")

    with secret_scope((token, bearer, *(message["content"] for message in request.messages))):
        content = json.dumps(redact(json.loads(response.content))) if response.content is not None else None
        return replace(
            response,
            content=content,
            response_model=redact(response.response_model),
            response_id=redact(response.response_id),
            finish_reason=redact(response.finish_reason),
            usage=replace(usage, response_model=redact(usage.response_model)) if usage else None,
        )


class WorkerInferenceBody(LeaseProof):
    request: InferenceBody


async def _read_body(request, schema):
    payload = bytearray()
    async for chunk in request.stream():
        if len(payload) + len(chunk) > MAX_REQUEST_BYTES:
            raise HTTPException(413, "Model request exceeds the size limit")
        payload.extend(chunk)
    try:
        return schema.model_validate_json(payload)
    except ValidationError:
        raise HTTPException(422, "Invalid model request") from None


def _worker_inference(sessions, identifier, body, request, settings):
    from app.worker_runtime import authorize

    with sessions.begin() as db:
        _, assignment = authorize(db, request, identifier, body.lease_token, settings)
        if not assignment.gateway_model:
            raise HTTPException(409, "Assignment has no Direct gateway route")
        scope = CapabilityScope(
            project_id=assignment.project_id, run_id=assignment.run_id,
            stage_id=assignment.stage_id, attempt_id=assignment.execution_id,
            assignment_id=assignment.id, audience="model", operations=(MODEL_OPERATION,),
        )
        _, connection = _route(db, scope, body.request.model, settings)
        if body.request.model not in connection.aliases:
            raise HTTPException(422, "Model alias does not match the pinned gateway route")
        launch = read_definition_pin(
            db, ProjectScope(scope.project_id), db.get(WorkflowRun, scope.run_id).snapshot_id, settings
        )
        limits = json.loads(launch["stages"][scope.stage_id]["settings_json"])
        context_limit = min(limits["max_context_tokens"], MAX_CONTEXT_TOKENS)
        output_limit = min(limits["max_completion_tokens"], MAX_COMPLETION_TOKENS)
        if (body.request.max_context_tokens > context_limit
                or body.request.max_completion_tokens > output_limit):
            raise HTTPException(422, "Request exceeds the pinned stage token limits")
        try:
            bearer = ledger.worker_model_capability(
                db, scope, SessionLimits(
                    request_count=32, concurrent_requests=1, input_tokens=32 * context_limit,
                    output_tokens=32 * output_limit, ttl_seconds=120,
                ), settings=settings,
            )
        except ledger.CapabilityDenied:
            raise HTTPException(403, "Model capability denied") from None

    return execute_model_request(sessions, bearer, body.request, settings)


def install(app, sessions, settings):
    @app.post("/api/v1/model-broker/inference")
    async def inference(request: Request):
        scheme, _, bearer = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not bearer:
            raise HTTPException(401, "Model capability required")
        body = await _read_body(request, InferenceBody)
        response = await run_in_threadpool(execute_model_request, sessions, bearer, body, settings)
        return asdict(response)

    @app.post("/api/v1/shared-worker/assignments/{identifier}/model-inference")
    async def worker_inference(identifier: str, request: Request):
        body = await _read_body(request, WorkerInferenceBody)
        response = await run_in_threadpool(_worker_inference, sessions, identifier, body, request, settings)
        return asdict(response)
