"""Administrator-owned gateway revisions and credential-purpose checks."""
from dataclasses import asdict
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.ports import GatewayAdministration, GatewayVerificationError
from app.credential_store import read_credential
from app.database import GatewayConnectionRevision
from app.project_context import require_secret
from app.projects import project_parameter, request_scope, require_owned
from factory.adapters.gateway_inference import LLMConnectionRevision

INFERENCE_PURPOSE = "gateway-inference"
MANAGEMENT_PURPOSE = "gateway-management"
Identifier = Annotated[str, Field(min_length=1, max_length=36)]
Alias = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_./:-]{0,119}$")]


class GatewayConnectionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    endpoint: str = Field(max_length=500)
    inference_credential_id: Identifier
    management_credential_id: Identifier | None = None
    parent_id: Identifier | None = None
    aliases: dict[Alias, Alias] = Field(min_length=1, max_length=64)
    timeout_seconds: float = Field(default=90, gt=0, le=300, allow_inf_nan=False)


def require_gateway_credential(db, scope, credential_id, purpose):
    secret = require_secret(db, scope, credential_id, purpose)
    if secret.openbao_version is None or secret.openbao_version < 1:
        raise HTTPException(409, "Gateway credentials require an active OpenBao version")
    return secret


def inference_descriptor(row):
    try:
        return LLMConnectionRevision(
            revision_id=row.id,
            endpoint=row.endpoint,
            credential_ref=row.inference_credential_id,
            aliases=row.aliases,
            timeout_seconds=row.timeout_seconds,
        )
    except ValueError:
        raise HTTPException(422, "Invalid gateway endpoint or tokenizer mapping") from None


def public(row):
    return {
        key: getattr(row, key) for key in (
            "id", "project_id", "parent_id", "endpoint", "aliases",
            "inference_credential_id", "management_credential_id", "timeout_seconds",
        )
    }


def install(app, sessions, settings, administration: GatewayAdministration):
    path = "/api/v1/projects/{project_id}/gateway-connections"
    project = [Depends(project_parameter)]

    @app.get(path, dependencies=project)
    def list_connections(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return [public(row) for row in db.scalars(select(GatewayConnectionRevision).where(
                GatewayConnectionRevision.project_id == scope.project_id,
            ).order_by(GatewayConnectionRevision.created_at.desc(), GatewayConnectionRevision.id))]

    @app.get(path + "/{revision_id}", dependencies=project)
    def read_connection(revision_id: str, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return public(require_owned(db, scope, GatewayConnectionRevision, revision_id))

    @app.post(path, dependencies=project, status_code=201)
    def create_connection(body: GatewayConnectionEdit, request: Request):
        from app.workflow_state import transition_lock

        with sessions.begin() as db:
            transition_lock(db)
            scope = request_scope(request, db)
            if body.parent_id is not None:
                require_owned(db, scope, GatewayConnectionRevision, body.parent_id)
            require_gateway_credential(db, scope, body.inference_credential_id, INFERENCE_PURPOSE)
            if body.management_credential_id is not None:
                require_gateway_credential(db, scope, body.management_credential_id, MANAGEMENT_PURPOSE)
            row = GatewayConnectionRevision(project_id=scope.project_id, **body.model_dump())
            db.add(row)
            db.flush()
            inference_descriptor(row)
            return public(row)

    @app.post(path + "/{revision_id}/verify", dependencies=project)
    def verify_connection(revision_id: str, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            row = require_owned(db, scope, GatewayConnectionRevision, revision_id)
            if row.management_credential_id is None:
                raise HTTPException(409, "Gateway management credential is required")
            credential = require_gateway_credential(db, scope, row.management_credential_id, MANAGEMENT_PURPOSE)
            connection = inference_descriptor(row)
            # Keep the approved metadata available after closing the read transaction.
            db.expunge(credential)
        try:
            return asdict(administration.verify(
                connection.endpoint, tuple(connection.aliases), read_credential(credential, settings)
            ))
        except GatewayVerificationError:
            raise HTTPException(502, "Gateway model discovery failed") from None
