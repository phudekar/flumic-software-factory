"""Trusted action pinning and encrypted, revocable project credential grants.

No worker context endpoint is exposed here. Assignment eligibility/lease checks
belong to the assignment service; it must authorize before calling these helpers.
"""

import json
import time
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Request, HTTPException, Depends
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select, desc
from app.database import (
    SecretVersion,
    ActionContextSnapshot,
    RepositoryConnection,
    RepositoryRevision,
)
from app.projects import (
    request_scope,
    require_owned,
    project_parameter,
    effective_configuration,
)
from factory.workflows.contracts import ActionContext
from factory.settings import compatible_settings


class SecretEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")
    purpose: str = Field(pattern=r"^(github|gitlab|openai|anthropic)$")
    value: SecretStr = Field(min_length=1, max_length=16384)
    repository_id: str | None = None
    parent_id: str | None = None


def encrypt(value, settings):
    return (
        Fernet(settings.encryption_key.encode())
        .encrypt(json.dumps(value, allow_nan=False).encode())
        .decode()
    )


def decrypt(value, settings):
    try:
        return json.loads(
            Fernet(settings.encryption_key.encode()).decrypt(value.encode())
        )
    except (InvalidToken, ValueError):
        raise HTTPException(409, "Encrypted context is unavailable") from None


def require_secret(db, scope, identity, purpose, repository_id=None):
    secret = require_owned(db, scope, SecretVersion, identity)
    if secret.repository_id is not None and secret.repository_id != repository_id:
        raise HTTPException(404, "Resource not found")
    if secret.purpose != purpose or secret.revoked_at is not None:
        raise HTTPException(
            409, "Pinned credential is revoked or has the wrong purpose"
        )
    return secret


def resolve_secret(db, scope, identity, purpose, settings, repository_id=None):
    secret = require_secret(db, scope, identity, purpose, repository_id)
    return decrypt(secret.ciphertext, settings)


def pin_context(
    db,
    scope,
    config,
    settings,
    *,
    repository,
    workflow_revision_id,
    run_id,
    stage_id,
    execution_id,
    attempt,
    assignment_id,
    purposes=(),
    grants=None,
    profile="simple",
    workflow=None,
    stage=None,
    factory_revision_id=None,
    repository_revision_id=None,
    limits=None,
):
    """Called once by trusted admission with owned definition/assignment identities.

    Provider grants are explicit version IDs; GitHub inherits its pinned repository
    revision's reference. Later run admission supplies its already pinned revisions.
    Repeated pinning is rejected rather than resolving a newer revision on replay.
    """
    if db.scalar(
        select(ActionContextSnapshot.id).where(
            ActionContextSnapshot.project_id == scope.project_id,
            ActionContextSnapshot.execution_id == execution_id,
        )
    ):
        raise HTTPException(409, "Execution context is already pinned")
    resolved, factory, connection = effective_configuration(
        db,
        scope,
        config,
        repository,
        profile=profile,
        workflow=workflow,
        stage=stage,
        limits=limits,
        factory_revision_id=factory_revision_id,
        repository_revision_id=repository_revision_id,
    )
    repo_revision = require_owned(
        db, scope, RepositoryRevision, repository_revision_id or connection.revision_id
    )
    selected = dict(grants or {})
    source_purpose = repo_revision.document.get("provider", "github")
    if source_purpose in purposes:
        selected[source_purpose] = repo_revision.document.get("credential_id")
    # Refuse both missing grants and excessive access, even if a caller asks for it.
    if set(selected) != set(purposes) or set(selected) - {
        "github",
        "gitlab",
        "openai",
        "anthropic",
    }:
        raise HTTPException(422, "Credential grants must match required purposes")
    for purpose, identity in selected.items():
        if not identity:
            raise HTTPException(409, "Required credential grant is unavailable")
        require_secret(db, scope, identity, purpose, connection.id)
    context = ActionContext(
        project_id=scope.project_id,
        repository_id=connection.id,
        repository_revision_id=repo_revision.id,
        config_revision_id=factory.id,
        workflow_revision_id=workflow_revision_id,
        run_id=run_id,
        stage_id=stage_id,
        execution_id=execution_id,
        attempt=attempt,
        assignment_id=assignment_id,
        namespace=f"{scope.project_id}/{connection.id}",
        settings_json=json.dumps(
            compatible_settings(jsonable_encoder(resolved)), allow_nan=False
        ),
        **(
            {
                "repository_identity_json": json.dumps(
                    repo_revision.document["identity"], allow_nan=False
                ),
                "repository_check_policy_json": json.dumps(
                    repo_revision.document.get("check_policy", {}), allow_nan=False
                ),
            }
            if source_purpose == "gitlab"
            else {}
        ),
        secret_versions=tuple(selected.values()),
    )
    item = ActionContextSnapshot(
        project_id=scope.project_id,
        execution_id=execution_id,
        ciphertext=encrypt(
            {
                "context": {
                    **context.model_dump(mode="json"),
                    "settings_json": context.settings_json,
                },
                "grants": selected,
            },
            settings,
        ),
    )
    db.add(item)
    db.flush()
    return item


def resolve_pinned_context(db, scope, identity, settings, *, assignment_id):
    """Internal only: caller must first authenticate an active eligible assignment."""
    item = require_owned(db, scope, ActionContextSnapshot, identity)
    document = decrypt(item.ciphertext, settings)
    context = ActionContext.model_validate_json(json.dumps(document["context"]))
    if context.assignment_id != assignment_id:
        raise HTTPException(404, "Resource not found")
    credentials = {
        purpose: resolve_secret(
            db, scope, version, purpose, settings, context.repository_id
        )
        for purpose, version in document["grants"].items()
    }
    return context.with_credentials(credentials)


def install(app, sessions, settings):
    def public(item):
        return {
            key: getattr(item, key)
            for key in (
                "id",
                "project_id",
                "repository_id",
                "name",
                "purpose",
                "parent_id",
                "created_at",
                "revoked_at",
            )
        }

    @app.get(
        "/api/v1/projects/{project_id}/secrets",
        dependencies=[Depends(project_parameter)],
    )
    def secrets(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return [
                public(s)
                for s in db.scalars(
                    select(SecretVersion)
                    .where(SecretVersion.project_id == scope.project_id)
                    .order_by(desc(SecretVersion.created_at))
                )
            ]

    @app.post(
        "/api/v1/projects/{project_id}/secrets",
        status_code=201,
        dependencies=[Depends(project_parameter)],
    )
    def save(body: SecretEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            if body.repository_id:
                require_owned(db, scope, RepositoryConnection, body.repository_id)
            if body.parent_id:
                parent = require_owned(db, scope, SecretVersion, body.parent_id)
                if (parent.name, parent.purpose, parent.repository_id) != (
                    body.name,
                    body.purpose,
                    body.repository_id,
                ):
                    raise HTTPException(
                        422, "Rotation must retain name, purpose and repository scope"
                    )
            item = SecretVersion(
                project_id=scope.project_id,
                name=body.name,
                purpose=body.purpose,
                repository_id=body.repository_id,
                parent_id=body.parent_id,
                ciphertext=encrypt(body.value.get_secret_value(), settings),
            )
            db.add(item)
            db.flush()
            return public(item)

    @app.post(
        "/api/v1/projects/{project_id}/secrets/{identifier}/revoke",
        dependencies=[Depends(project_parameter)],
    )
    def revoke(identifier: str, request: Request):
        with sessions.begin() as db:
            item = require_owned(
                db, request_scope(request, db), SecretVersion, identifier
            )
            if item.revoked_at is None:
                item.revoked_at = time.time()
            return public(item)
