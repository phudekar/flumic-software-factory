"""Infrastructure pools and worker-authenticated, fenced execution assignments."""

import hashlib
import hmac
import json
import time
from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, func
from app.database import (
    WorkerPool,
    ProjectPool,
    Worker,
    Assignment,
    StageAttempt,
    RepositoryConnection,
    Operation,
)
from app.projects import ProjectScope, require_owned, request_scope, project_parameter
from app.project_context import encrypt, decrypt, resolve_pinned_context
from app.reconciler import worker_token
from app.security import digest
from factory.workflows.contracts import ActionInvocation, ActionResult

PROTOCOL = "shared-v1"
LEASE_SECONDS = 120
RENEW_SECONDS = 20


class PoolEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")
    role: str = Field(pattern=r"^(simple|developer)$")
    normal_slots: int = Field(default=2, ge=1, le=8, strict=True)
    durable_slots: int = Field(default=16, ge=1, le=16, strict=True)
    executors: list[str] = Field(
        default_factory=lambda: ["builtin@1"], min_length=1, max_length=8
    )
    actions: list[str] = Field(
        default_factory=lambda: ["core.echo@1"], min_length=1, max_length=64
    )
    hatchet_tls_strategy: str = Field(default="tls", pattern=r"^(tls|none|mtls)$")


class SharedWorkerEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")


class MembershipEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


class Registration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol: str
    pool_id: str
    role: str
    executors: list[str] = Field(max_length=8)
    actions: list[str] = Field(max_length=64)
    normal_slots: int = Field(ge=1, le=8, strict=True)
    durable_slots: int = Field(ge=1, le=16, strict=True)


class Claim(ActionInvocation):
    claim_key: str = Field(min_length=16, max_length=120)


class LeaseProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lease_token: str = Field(min_length=1, max_length=120, repr=False)


class Completion(LeaseProof):
    result: ActionResult
    failed: bool = False


def pool_record(db, identity):
    item = db.get(WorkerPool, identity)
    if item is None:
        raise HTTPException(404, "Pool not found")
    return item


def shared_profile(worker, pool):
    return {
        "version": 1,
        "protocol": PROTOCOL,
        "pool_id": pool.id,
        "worker_id": worker.id,
        "worker_name": worker.name,
        **{k: v for k, v in pool.profile.items() if k != "name"},
    }


def authenticate(db, request):
    worker = db.get(Worker, request.headers.get("x-worker-id", ""))
    authorization = request.headers.get("authorization", "")
    if (
        worker is None
        or not authorization.startswith("Bearer ")
        or not hmac.compare_digest(worker.credential_hash, digest(authorization[7:]))
    ):
        raise HTTPException(401, "Invalid worker credentials")
    if (
        worker.protocol != PROTOCOL
        or not worker.pool_id
        or worker.desired_state in {"removed", "stopped"}
    ):
        raise HTTPException(
            409, "Worker requires explicit shared protocol registration"
        )
    return worker


def membership(db, project_id, pool_id):
    if db.get(ProjectPool, (project_id, pool_id)) is None:
        raise HTTPException(409, "Project is not authorized for pool")


def authorize_execution(
    db,
    scope,
    settings,
    *,
    repository_id,
    pool_id,
    run_id,
    stage_id,
    execution_id,
    role,
    executor,
    action,
    input,
):
    """Trusted scheduler boundary. Run/graph admission adds its own checks in #7.

    No operator/worker API creates this authority from caller-supplied run labels.
    The persisted requirements are the only eligibility source for claim/renew.
    """
    require_owned(db, scope, RepositoryConnection, repository_id)
    pool = pool_record(db, pool_id)
    membership(db, scope.project_id, pool.id)
    if (
        role != pool.profile["role"]
        or executor not in pool.profile["executors"]
        or action not in pool.profile["actions"]
    ):
        raise HTTPException(409, "Pool does not support execution requirements")
    from factory.workflows.references import json_bytes

    if len(json_bytes(input)) > 1048576:
        raise HTTPException(422, "Action input exceeds 1 MiB")
    row = Assignment(
        project_id=scope.project_id,
        repository_id=repository_id,
        pool_id=pool.id,
        run_id=run_id,
        stage_id=stage_id,
        execution_id=execution_id,
        role=role,
        executor=executor,
        action=action,
        input_ciphertext=encrypt(input, settings),
    )
    db.add(row)
    db.flush()
    return row


def eligibility(db, worker, assignment):
    membership(db, assignment.project_id, assignment.pool_id)
    pool = pool_record(db, assignment.pool_id)
    if (
        worker.pool_id != pool.id
        or worker.protocol != PROTOCOL
        or worker.registered_at is None
        or worker.desired_state in {"removed", "stopped"}
    ):
        raise HTTPException(409, "Worker is not eligible for assignment")
    caps = worker.capabilities
    if (
        caps.get("role") != assignment.role
        or assignment.role != pool.profile["role"]
        or assignment.executor not in caps.get("executors", [])
        or assignment.action not in caps.get("actions", [])
    ):
        raise HTTPException(409, "Worker capabilities do not match assignment")


def lease_token(settings, row):
    return hmac.new(
        settings.encryption_key.encode(),
        f"{row.id}:{row.worker_id}:{row.claim_hash}".encode(),
        "sha256",
    ).hexdigest()


def claim_execution(db, invocation, worker_id, settings, *, claim_key, now=None):
    from app.workflow_state import transition_lock

    transition_lock(db)
    now = time.time() if now is None else now
    row = db.scalar(
        select(Assignment).where(
            Assignment.project_id == invocation.project_id,
            Assignment.execution_id == invocation.execution_id,
        )
    )
    if row is None or row.run_id != invocation.run_id:
        raise HTTPException(404, "Execution not found")
    # Consistent lock order: pool → worker → assignment. The worker lock fences
    # slot admission; expired effects still occupy capacity until reconciled.
    db.scalar(select(WorkerPool).where(WorkerPool.id == row.pool_id).with_for_update())
    worker = db.scalar(
        select(Worker)
        .where(Worker.id == worker_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    row = db.scalar(
        select(Assignment)
        .where(Assignment.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if worker is None:
        raise HTTPException(401, "Invalid worker credentials")
    eligibility(db, worker, row)
    proof = digest(claim_key)
    if row.status != "ready":
        if (
            row.worker_id == worker.id
            and row.status == "running"
            and row.claim_hash == proof
            and row.expires_at > now
        ):
            return row, True
        raise HTTPException(409, "Execution is already claimed or terminal")
    active = db.scalar(
        select(func.count())
        .select_from(Assignment)
        .where(Assignment.worker_id == worker.id, Assignment.status == "running")
    )
    if active >= pool_record(db, row.pool_id).profile["normal_slots"]:
        raise HTTPException(409, "Worker action slots are full")
    row.worker_id = worker.id
    row.claim_hash = proof
    row.status = "running"
    row.expires_at = now + LEASE_SECONDS
    from app.database import StageAttempt

    attempt = db.get(StageAttempt, row.execution_id)
    if attempt is not None:
        if (attempt.project_id, attempt.assignment_id) != (row.project_id, row.id):
            raise HTTPException(409, "Assignment does not match logical execution")
        attempt.state = "running"
        if row.action == "core.echo@1":
            row.started_at = now
    db.flush()
    return row, False


def assignment_deadline(db, row, settings):
    if row.started_at is None or row.executor != "builtin@1":
        return None
    from app.database import WorkflowRun
    from app.workflow_definitions import read_definition_pin

    attempt = db.get(StageAttempt, row.execution_id)
    if attempt is None:
        return None
    run = require_owned(db, ProjectScope(row.project_id), WorkflowRun, attempt.run_id)
    document = read_definition_pin(
        db, ProjectScope(row.project_id), run.snapshot_id, settings
    )
    stage = next(
        stage
        for stage in document["definition"]["stages"]
        if stage["id"] == attempt.stage_id
    )
    return row.started_at + stage["timeout_seconds"]


def require_lease(db, worker, identifier, proof, settings, *, now=None, terminal=False):
    row = db.scalar(
        select(Assignment).where(Assignment.id == identifier).with_for_update()
    )
    if row is None or row.worker_id != worker.id:
        raise HTTPException(404, "Assignment not found")
    eligibility(db, worker, row)
    if not hmac.compare_digest(proof, lease_token(settings, row)):
        raise HTTPException(409, "Invalid assignment lease")
    if terminal and (
        row.status in {"completed", "failed", "cancelled"}
        or (
            row.status == "running"
            and db.get(StageAttempt, row.execution_id) is not None
        )
    ):
        # The claim is never reassigned on expiry. A stopped builtin may submit
        # its terminal acknowledgement with that same proof, without new grants.
        return row
    if row.status != "running" or row.expires_at <= (
        time.time() if now is None else now
    ):
        raise HTTPException(
            409, "Assignment lease is not active; reconcile before retry"
        )
    from app.database import WorkflowRun

    run = db.get(WorkflowRun, row.run_id)
    if run is not None and run.state != "running":
        raise HTTPException(409, "Run is not active; only terminal proof is accepted")
    deadline = assignment_deadline(db, row, settings)
    if deadline is not None and (time.time() if now is None else now) >= deadline:
        raise HTTPException(
            409, "Execution deadline expired; reconcile before continuing"
        )
    return row


def renew_execution(db, worker, identifier, proof, settings, *, now=None):
    from app.workflow_state import transition_lock
    from app.database import ExternalResourceClaim

    transition_lock(db)
    now = time.time() if now is None else now
    row = require_lease(db, worker, identifier, proof, settings, now=now)
    row.expires_at = now + LEASE_SECONDS
    for resource in db.scalars(
        select(ExternalResourceClaim).where(
            ExternalResourceClaim.execution_id == row.execution_id
        )
    ):
        resource.expires_at = row.expires_at
    db.flush()
    return row


def resolve_context(
    db, scope, execution_id, worker_id, settings, *, lease_proof, now=None
):
    row = db.scalar(
        select(Assignment).where(
            Assignment.project_id == scope.project_id,
            Assignment.execution_id == execution_id,
        )
    )
    if row is None:
        raise HTTPException(404, "Execution not found")
    worker = db.get(Worker, worker_id)
    if worker is None:
        raise HTTPException(401, "Invalid worker credentials")
    row = require_lease(db, worker, row.id, lease_proof, settings, now=now)
    if not row.context_id:
        raise HTTPException(409, "Execution context is not pinned")
    context = resolve_pinned_context(
        db, scope, row.context_id, settings, assignment_id=row.id
    )
    if (
        context.project_id,
        context.repository_id,
        context.run_id,
        context.stage_id,
        context.execution_id,
    ) != (
        row.project_id,
        row.repository_id,
        row.run_id,
        row.stage_id,
        row.execution_id,
    ):
        raise HTTPException(409, "Pinned context does not match execution authority")
    if db.get(StageAttempt, row.execution_id) is not None:
        from app.database import WorkflowRun
        from app.workflow_definitions import read_definition_pin

        run = require_owned(db, scope, WorkflowRun, row.run_id)
        pin = read_definition_pin(db, scope, run.snapshot_id, settings)
        context = context.model_copy(
            update={
                "definition_id": pin["definition_id"],
                "workflow_revision_id": pin["workflow_revision_id"],
                "executor": row.executor,
                "worker_id": worker.id,
            }
        )
    return context


def install(app, sessions, settings):
    def public(pool):
        return {"id": pool.id, **pool.profile}

    @app.get("/api/v1/worker-pools")
    def pools():
        with sessions() as db:
            return [
                public(p)
                for p in db.scalars(select(WorkerPool).order_by(WorkerPool.name))
            ]

    @app.get(
        "/api/v1/projects/{project_id}/worker-pools",
        dependencies=[Depends(project_parameter)],
    )
    def project_pools(request: Request):
        """Policy/registration view; loaded capabilities do not prove live capacity."""
        with sessions() as db:
            scope = request_scope(request, db)
            result = []
            for pool in db.scalars(select(WorkerPool).order_by(WorkerPool.name)):
                workers = []
                for worker in db.scalars(
                    select(Worker).where(Worker.pool_id == pool.id)
                ):
                    state = (
                        "unsupported"
                        if worker.protocol != PROTOCOL
                        else "unloaded"
                        if worker.registered_at is None
                        else "loaded"
                    )
                    missing_actions = sorted(
                        set(pool.profile["actions"])
                        - set(worker.capabilities.get("actions", []))
                    )
                    missing_executors = sorted(
                        set(pool.profile["executors"])
                        - set(worker.capabilities.get("executors", []))
                    )
                    if state == "loaded" and (missing_actions or missing_executors):
                        state = "partial"
                    workers.append(
                        {
                            "id": worker.id,
                            "name": worker.name,
                            "protocol": worker.protocol,
                            "desired_state": worker.desired_state,
                            "capability_state": state,
                            "capabilities": worker.capabilities,
                            "missing_actions": missing_actions,
                            "missing_executors": missing_executors,
                            "registered_at": worker.registered_at,
                            "last_heartbeat_at": worker.last_heartbeat_at,
                        }
                    )
                result.append(
                    {
                        **public(pool),
                        "authorized": db.get(ProjectPool, (scope.project_id, pool.id))
                        is not None,
                        "workers": workers,
                    }
                )
            return result

    @app.post("/api/v1/worker-pools", status_code=201)
    def create_pool(body: PoolEdit):
        import re

        if any(
            not re.fullmatch(r"[a-z][a-z0-9_.-]{0,99}@[1-9][0-9]{0,3}", v)
            for v in [*body.executors, *body.actions]
        ):
            raise HTTPException(422, "Capabilities require explicit versions")
        with sessions.begin() as db:
            if db.scalar(select(WorkerPool.id).where(WorkerPool.name == body.name)):
                raise HTTPException(409, "Pool name already exists")
            item = WorkerPool(name=body.name, profile=body.model_dump())
            db.add(item)
            db.flush()
            return public(item)

    @app.put(
        "/api/v1/projects/{project_id}/worker-pools/{pool_id}",
        dependencies=[Depends(project_parameter)],
    )
    def authorize_pool(pool_id: str, body: MembershipEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            pool_record(db, pool_id)
            item = db.get(ProjectPool, (scope.project_id, pool_id))
            if body.enabled and item is None:
                db.add(ProjectPool(project_id=scope.project_id, pool_id=pool_id))
            if not body.enabled and item is not None:
                db.delete(item)
            return {
                "project_id": scope.project_id,
                "pool_id": pool_id,
                "enabled": body.enabled,
            }

    @app.post("/api/v1/worker-pools/{pool_id}/workers", status_code=201)
    def create_worker(pool_id: str, body: SharedWorkerEdit):
        with sessions.begin() as db:
            pool = pool_record(db, pool_id)
            if db.scalar(select(Worker.id).where(Worker.name == body.name)):
                raise HTTPException(409, "Worker name already exists")
            worker = Worker(
                name=body.name,
                profile=pool.profile["role"],
                revision_id=pool.id,
                protocol=PROTOCOL,
                pool_id=pool.id,
                credential_hash="pending",
            )
            db.add(worker)
            db.flush()
            worker.credential_hash = digest(worker_token(settings, worker.id))
            db.add(
                Operation(
                    worker_id=worker.id,
                    action="create",
                    key="shared-create:" + worker.id,
                )
            )
            return {
                "id": worker.id,
                "protocol": worker.protocol,
                "pool_id": pool.id,
                "name": worker.name,
            }

    @app.post("/api/v1/shared-worker/register")
    def register(body: Registration, request: Request):
        with sessions.begin() as db:
            worker = authenticate(db, request)
            pool = pool_record(db, worker.pool_id)
            expected = shared_profile(worker, pool)
            if (
                body.protocol != PROTOCOL
                or body.pool_id != pool.id
                or body.role != expected["role"]
                or body.normal_slots != expected["normal_slots"]
                or body.durable_slots != expected["durable_slots"]
                or not body.actions
                or not body.executors
                or set(body.actions) - set(expected["actions"])
                or set(body.executors) - set(expected["executors"])
            ):
                raise HTTPException(
                    409, "Worker registration does not match pool policy"
                )
            worker.capabilities = body.model_dump(exclude={"protocol", "pool_id"})
            worker.registered_at = time.time()
            return {
                "worker_id": worker.id,
                "lease_seconds": LEASE_SECONDS,
                "renew_seconds": RENEW_SECONDS,
            }

    @app.post("/api/v1/shared-worker/claim")
    def claim(body: Claim, request: Request):
        with sessions.begin() as db:
            worker = authenticate(db, request)
            row, reused = claim_execution(
                db, body, worker.id, settings, claim_key=body.claim_key
            )
            attempt = db.get(StageAttempt, row.execution_id)
            return {
                "assignment_id": row.id,
                "execution_id": row.execution_id,
                "project_id": row.project_id,
                "action": row.action,
                "executor": row.executor,
                "spec_digest": attempt.spec_digest if attempt else None,
                "input": decrypt(row.input_ciphertext, settings),
                "lease_token": lease_token(settings, row),
                "expires_at": row.expires_at,
                "reused": reused,
            }

    @app.post("/api/v1/shared-worker/assignments/{identifier}/renew")
    def renew(identifier: str, body: LeaseProof, request: Request):
        with sessions.begin() as db:
            row = renew_execution(
                db, authenticate(db, request), identifier, body.lease_token, settings
            )
            return {"assignment_id": row.id, "expires_at": row.expires_at}

    @app.post("/api/v1/shared-worker/assignments/{identifier}/complete")
    def complete(identifier: str, body: Completion, request: Request):
        with sessions.begin() as db:
            from app.workflow_state import transition_lock, WorkflowState
            from app.database import StageAttempt

            transition_lock(db)
            row = require_lease(
                db,
                authenticate(db, request),
                identifier,
                body.lease_token,
                settings,
                terminal=True,
            )
            if row.executor != "builtin@1":
                raise HTTPException(
                    409, "External jobs require a broker terminal receipt"
                )
            if body.failed:
                from factory.workflows.action_schemas import READ_ONLY_FAILURE_ACTIONS

                if row.action not in READ_ONLY_FAILURE_ACTIONS:
                    raise HTTPException(
                        409, "Uncertain effects require operator reconciliation"
                    )
            attempt = db.get(StageAttempt, row.execution_id)
            if attempt is not None:
                if (attempt.project_id, attempt.assignment_id) != (
                    row.project_id,
                    row.id,
                ):
                    raise HTTPException(
                        409, "Assignment does not match logical execution"
                    )
                from app.database import (
                    ActionContextSnapshot,
                    SecretVersion,
                    WorkflowRun,
                )
                from factory.workflows.redaction import secret_scope

                credentials = []
                if row.context_id:
                    pin = require_owned(
                        db,
                        ProjectScope(row.project_id),
                        ActionContextSnapshot,
                        row.context_id,
                    )
                    for version in decrypt(pin.ciphertext, settings)["grants"].values():
                        credentials.append(
                            decrypt(
                                require_owned(
                                    db,
                                    ProjectScope(row.project_id),
                                    SecretVersion,
                                    version,
                                ).ciphertext,
                                settings,
                            )
                        )
                with secret_scope(credentials):
                    result = ActionResult(
                        output=body.result.output, usage=body.result.usage
                    )
                run = require_owned(
                    db, ProjectScope(row.project_id), WorkflowRun, row.run_id
                )
                deadline = assignment_deadline(db, row, settings)
                failed = (
                    body.failed
                    or attempt.state == "failed"
                    or (
                        attempt.state not in {"completed", "cancelled"}
                        and deadline is not None
                        and time.time() >= deadline
                    )
                )
                cancelled = attempt.state == "cancelled" or run.state in {
                    "cancelling",
                    "cancelled",
                }
                return (
                    WorkflowState(sessions, settings, None)
                    .finish_execution(
                        ProjectScope(row.project_id),
                        row.execution_id,
                        result=result,
                        failed=failed,
                        cancelled=cancelled,
                        confirmed_terminal=True,
                        db=db,
                    )
                    .model_dump()
                )
            value = body.result.model_dump(mode="json")
            encoded = json.dumps(
                value, sort_keys=True, allow_nan=False, separators=(",", ":")
            )
            hashed = hashlib.sha256(encoded.encode()).hexdigest()
            if row.status == "completed" and row.result_digest != hashed:
                raise HTTPException(
                    409, "Execution result differs from completed result"
                )
            if row.status != "completed":
                row.result_digest = hashed
                row.result_ciphertext = encrypt(value, settings)
                row.status = "completed"
            return {"execution_id": row.execution_id, "state": "completed"}

    @app.post("/api/v1/shared-worker/assignments/{identifier}/context")
    def context(identifier: str, body: LeaseProof, request: Request):
        with sessions.begin() as db:
            from app.workflow_state import transition_lock
            from app.database import WorkflowRun

            transition_lock(db)
            worker = authenticate(db, request)
            row = require_lease(db, worker, identifier, body.lease_token, settings)
            attempt = db.get(StageAttempt, row.execution_id)
            if attempt is not None:
                run = require_owned(
                    db, ProjectScope(row.project_id), WorkflowRun, row.run_id
                )
                if run.state != "running":
                    raise HTTPException(409, "Run is not accepting action starts")
            value = resolve_context(
                db,
                ProjectScope(row.project_id),
                row.execution_id,
                worker.id,
                settings,
                lease_proof=body.lease_token,
            )
            if row.started_at is None:
                row.started_at = time.time()
            # Deliberate authorized delivery only; never use generic row/query APIs.
            attempt = db.get(StageAttempt, row.execution_id)
            return {
                "deadline": assignment_deadline(db, row, settings),
                "origins": decrypt(attempt.origins_ciphertext, settings)
                if attempt and attempt.origins_ciphertext
                else [],
                "context": {
                    **value.model_dump(mode="json"),
                    "settings_json": value.settings_json,
                    "followup_bindings_json": value.followup_bindings_json,
                },
                "credentials": dict(value.credentials),
            }
