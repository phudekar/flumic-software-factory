"""Workflow control receipts keep transport retries from duplicating triggers."""

import json
import time
import uuid
from typing import Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, desc, text

from app.database import WorkflowControlOperation, Audit
from app.security import digest
from factory.domain.safety import find_sensitive_categories


class WorkflowAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["trigger", "pause", "resume"]
    input: dict = Field(default_factory=dict)
    queue_ttl_seconds: int = Field(default=86400, ge=60, le=2592000, strict=True)
    cron_behavior: Literal["DROP", "QUEUE"] = "DROP"
    scheduled_behavior: Literal["DROP", "QUEUE"] = "DROP"


def public(operation):
    return {
        key: getattr(operation, key)
        for key in (
            "id",
            "workflow_id",
            "workflow_name",
            "action",
            "status",
            "result",
            "error",
            "created_at",
            "updated_at",
        )
    }


def install(app, sessions, engine):
    @app.get("/api/v1/workflows")
    def workflows():
        try:
            return {"items": engine().definitions(), "warnings": []}
        except Exception:
            return {
                "items": [],
                "warnings": ["Workflow engine unavailable; workflow state is unknown."],
            }

    @app.get("/api/v1/workflows/{identifier}/actions")
    def actions(identifier: uuid.UUID):
        with sessions() as db:
            operations = db.scalars(
                select(WorkflowControlOperation)
                .where(WorkflowControlOperation.workflow_id == str(identifier))
                .order_by(desc(WorkflowControlOperation.created_at))
                .limit(25)
            ).all()
            if operations:
                return [public(operation) for operation in operations]
        engine().definition(str(identifier))
        return []

    @app.post("/api/v1/workflows/{identifier}/actions")
    def action(identifier: uuid.UUID, body: WorkflowAction, request: Request):
        identifier = str(identifier)
        key = request.headers.get("idempotency-key", "")
        if not 8 <= len(key) <= 100:
            raise HTTPException(
                422, "Idempotency-Key must be between 8 and 100 characters"
            )
        encoded = json.dumps(body.model_dump(), sort_keys=True)
        if len(encoded.encode()) > 65536:
            raise HTTPException(413, "Workflow action exceeds 64 KiB")
        if find_sensitive_categories(encoded):
            raise HTTPException(422, "Workflow input must not contain credentials")
        request_digest = digest(identifier + encoded)
        with sessions.begin() as db:
            if db.bind.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": int(digest("workflow:" + key)[:15], 16)},
                )
            existing = db.scalar(
                select(WorkflowControlOperation).where(
                    WorkflowControlOperation.key == key
                )
            )
            if existing:
                if existing.request_digest != request_digest:
                    raise HTTPException(
                        409,
                        "Idempotency key was already used for a different workflow action",
                    )
                return public(existing)
            definition = engine().definition(identifier)
            if body.action in {"pause", "resume"} and not definition["pause_supported"]:
                raise HTTPException(
                    422, "Native workflow pause support is unavailable on this server"
                )
            payload = (
                engine().validate_input(definition["name"], body.input)
                if body.action == "trigger"
                else {}
            )
            operation = WorkflowControlOperation(
                id=str(uuid.uuid4()),
                key=key,
                request_digest=request_digest,
                workflow_id=identifier,
                workflow_name=definition["name"],
                action=body.action,
                status="running",
                result={},
            )
            db.add(operation)
            db.add(
                Audit(
                    action="workflow." + body.action + ".requested",
                    resource=operation.id,
                )
            )
            db.flush()
            operation_id = operation.id
        try:
            result = engine().control(
                identifier,
                body.action,
                payload,
                body.queue_ttl_seconds,
                body.cron_behavior,
                body.scheduled_behavior,
                operation_id=operation_id,
            )
            status, error = "completed", None
        except Exception:
            # The engine may have accepted the action before transport failure.
            # Never retry a trigger automatically or expose a vendor response body.
            result, status = {}, "uncertain"
            error = "Engine outcome is uncertain. Inspect workflow state and runs before issuing another action."
        with sessions.begin() as db:
            operation = db.get(WorkflowControlOperation, operation_id)
            operation.status, operation.result, operation.error = status, result, error
            operation.updated_at = time.time()
            db.add(
                Audit(
                    action="workflow." + body.action + "." + status,
                    resource=operation_id,
                )
            )
            db.flush()
            return public(operation)
