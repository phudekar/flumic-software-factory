"""Project-owned source triggers; ingress tokens never become action grants."""

import json
import secrets
import time
from dataclasses import dataclass
from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.database import (
    RepositoryConnection,
    RepositoryRevision,
    RepositoryTrigger,
    TriggerToken,
    TriggerReceipt,
    WorkflowDefinitionRecord,
    WorkflowRevision,
    identity,
)
from app.project_context import encrypt, decrypt
from app.projects import (
    request_scope,
    require_owned,
    project_parameter,
    select_repository,
)
from app.workflow_state import transition_lock


class TriggerConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    require_ready_label: bool = True
    input_defaults: dict = Field(default_factory=dict)
    followups: dict[str, dict[Literal["issue", "feedback", "verify"], str]] = Field(
        default_factory=dict
    )


class TriggerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository_id: str = Field(min_length=1, max_length=36)
    definition_id: str = Field(min_length=1, max_length=36)
    event_kind: Literal["issue-ready", "feedback", "verify"]
    enabled: bool = True
    configuration: TriggerConfiguration = Field(default_factory=TriggerConfiguration)


class TriggerEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_id: str = Field(min_length=1, max_length=36)
    definition_id: str = Field(min_length=1, max_length=36)
    enabled: bool = True
    configuration: TriggerConfiguration = Field(default_factory=TriggerConfiguration)


class TokenEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_id: str | None = Field(default=None, max_length=36)


def public(item, settings):
    return {
        "id": item.id,
        "project_id": item.project_id,
        "repository_id": item.repository_id,
        "definition_id": item.definition_id,
        "event_kind": item.event_kind,
        "enabled": item.enabled,
        "revision_id": item.revision_id,
        "token_version_id": item.current_token_id,
        "configuration": decrypt(item.configuration_ciphertext, settings),
        "webhook_url": settings.public_origin.rstrip("/")
        + "/api/v1/hooks/gitlab/"
        + item.id,
        "last_error": item.last_error,
        "created_at": item.created_at,
    }


def validate_definition(db, scope, repository, definition_id, settings):
    definition = require_owned(db, scope, WorkflowDefinitionRecord, definition_id)
    revision = require_owned(db, scope, WorkflowRevision, definition.revision_id)
    graph = decrypt(revision.graph_ciphertext, settings)
    root = select_repository(db, scope, graph["default_repository"])
    if root.id != repository.id:
        raise HTTPException(
            422, "Trigger repository must be the workflow default repository"
        )

    return graph


def validate_configuration(
    db, scope, repository, definition_id, configuration, settings
):
    graph = validate_definition(db, scope, repository, definition_id, settings)
    value = configuration.model_dump(mode="json")
    encoded = json.dumps(value, allow_nan=False).encode()
    if len(encoded) > 65536:
        raise HTTPException(422, "Trigger configuration exceeds the size limit")
    from factory.workflows.references import json_bytes
    from factory.domain.safety import find_sensitive_categories

    # The same outbound guard used for operator-authored action data prevents
    # credentials from being stored as public trigger defaults.
    json_bytes(value)
    if find_sensitive_categories(encoded.decode()):
        raise HTTPException(422, "Sensitive values are not allowed in trigger defaults")
    if len(configuration.followups) > 64:
        raise HTTPException(422, "At most 64 follow-up repositories are allowed")
    allowed = {
        select_repository(db, scope, alias).id for alias in graph["repositories"]
    }
    for repository_id, definitions in configuration.followups.items():
        if set(definitions) != {"issue", "feedback", "verify"}:
            raise HTTPException(
                422,
                "Follow-up bindings require issue, feedback and verification workflows",
            )
        if repository_id not in allowed:
            raise HTTPException(
                422, "Follow-up repository must be declared in the workflow"
            )
        followup_repo = require_owned(db, scope, RepositoryConnection, repository_id)
        for selected in definitions.values():
            validate_definition(db, scope, followup_repo, selected, settings)
    return value


def issue_token(db, item, settings):
    previous = (
        db.get(TriggerToken, item.current_token_id) if item.current_token_id else None
    )
    if previous:
        previous.revoked_at = time.time()
    plaintext = secrets.token_urlsafe(32)
    token = TriggerToken(
        id=identity(),
        project_id=item.project_id,
        binding_id=item.id,
        parent_id=previous.id if previous else None,
        ciphertext=encrypt(plaintext, settings),
    )
    db.add(token)
    db.flush()
    item.current_token_id = token.id
    return plaintext


@dataclass(frozen=True)
class TriggerProof:
    binding_id: str
    project_id: str
    token_version_id: str
    configuration_revision_id: str
    repository_revision_id: str


def authenticate(db, binding_id, token, settings):
    """Validate the current ingress credential without disclosing its owner.

    The returned version proof must be checked again inside acceptance after
    remote reads. A concurrent rotation must not authorize a new pinned run.
    """
    denied = HTTPException(401, "Invalid trigger credential")
    if not isinstance(token, str) or not token.isascii() or not 1 <= len(token) <= 256:
        raise denied
    item = db.get(RepositoryTrigger, binding_id)
    version = (
        db.get(TriggerToken, item.current_token_id)
        if item and item.current_token_id
        else None
    )
    expected = (
        decrypt(version.ciphertext, settings)
        if version
        else "unavailable-trigger-credential"
    )
    matches = secrets.compare_digest(token, expected)
    if (
        not item
        or not item.enabled
        or not version
        or version.revoked_at is not None
        or not matches
    ):
        raise denied
    if version.project_id != item.project_id or version.binding_id != item.id:
        raise denied
    repository = db.get(RepositoryConnection, item.repository_id)
    if not repository or repository.project_id != item.project_id:
        raise denied
    return TriggerProof(
        item.id, item.project_id, version.id, item.revision_id, repository.revision_id
    )


def install(app, sessions, settings, config):
    from app.trigger_runtime import TriggerRuntime
    from factory.adapters.gitlab_events import GitLabWebhook, normalize_webhook
    from starlette.concurrency import run_in_threadpool
    from factory.domain.models import RepositoryError

    runtime = TriggerRuntime(sessions, settings, config)
    app.state.repository_triggers = runtime

    @app.post("/api/v1/hooks/gitlab/{binding_id}", status_code=202)
    async def webhook(binding_id: str, request: Request):
        def authorize():
            with sessions() as db:
                return authenticate(
                    db, binding_id, request.headers.get("x-gitlab-token", ""), settings
                )

        proof = await run_in_threadpool(authorize)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 1048576:
                raise HTTPException(413, "Webhook body exceeds the size limit")
            raw.extend(chunk)
        try:
            payload = GitLabWebhook.model_validate_json(bytes(raw))
            event = normalize_webhook(
                payload, delivery_id=request.headers.get("x-gitlab-event-uuid")
            )
        except (ValueError, TypeError, RecursionError):
            raise HTTPException(422, "Invalid GitLab webhook payload") from None
        try:
            return await run_in_threadpool(
                runtime.accept_event, binding_id, event, proof=proof
            )
        except RepositoryError:
            raise HTTPException(
                503, "GitLab source state is unavailable; retry delivery"
            ) from None

    dependencies = [Depends(project_parameter)]

    @app.get(
        "/api/v1/projects/{project_id}/repository-triggers", dependencies=dependencies
    )
    def listing(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return [
                public(item, settings)
                for item in db.scalars(
                    select(RepositoryTrigger)
                    .where(RepositoryTrigger.project_id == scope.project_id)
                    .order_by(RepositoryTrigger.created_at)
                )
            ]

    @app.post(
        "/api/v1/projects/{project_id}/repository-triggers",
        status_code=201,
        dependencies=dependencies,
    )
    def create(body: TriggerCreate, request: Request):
        with sessions.begin() as db:
            transition_lock(db)
            scope = request_scope(request, db)
            repository = require_owned(
                db, scope, RepositoryConnection, body.repository_id
            )
            value = validate_configuration(
                db, scope, repository, body.definition_id, body.configuration, settings
            )
            revision = require_owned(
                db, scope, RepositoryRevision, repository.revision_id
            )
            if repository.provider != "gitlab" or not revision.document.get("identity"):
                raise HTTPException(
                    409, "Validate a GitLab.com repository before creating its trigger"
                )
            item = RepositoryTrigger(
                id=identity(),
                project_id=scope.project_id,
                repository_id=repository.id,
                definition_id=body.definition_id,
                event_kind=body.event_kind,
                enabled=body.enabled,
                configuration_ciphertext=encrypt(value, settings),
            )
            db.add(item)
            db.flush()
            token = issue_token(db, item, settings)
            return {**public(item, settings), "webhook_token": token}

    @app.get(
        "/api/v1/projects/{project_id}/repository-triggers/{identifier}",
        dependencies=dependencies,
    )
    def get(identifier: str, request: Request):
        with sessions() as db:
            return public(
                require_owned(
                    db, request_scope(request, db), RepositoryTrigger, identifier
                ),
                settings,
            )

    @app.put(
        "/api/v1/projects/{project_id}/repository-triggers/{identifier}",
        dependencies=dependencies,
    )
    def update(identifier: str, body: TriggerEdit, request: Request):
        with sessions.begin() as db:
            transition_lock(db)
            scope = request_scope(request, db)
            item = require_owned(db, scope, RepositoryTrigger, identifier)
            if item.revision_id != body.parent_id:
                raise HTTPException(409, "Trigger changed; reload before editing")
            repository = require_owned(
                db, scope, RepositoryConnection, item.repository_id
            )
            value = validate_configuration(
                db, scope, repository, body.definition_id, body.configuration, settings
            )
            item.definition_id = body.definition_id
            item.enabled = body.enabled
            item.configuration_ciphertext = encrypt(value, settings)
            item.revision_id = identity()
            item.updated_at = time.time()
            item.next_poll_at = 0
            return public(item, settings)

    @app.post(
        "/api/v1/projects/{project_id}/repository-triggers/{identifier}/rotate-token",
        dependencies=dependencies,
    )
    def rotate(identifier: str, body: TokenEdit, request: Request):
        with sessions.begin() as db:
            transition_lock(db)
            item = require_owned(
                db, request_scope(request, db), RepositoryTrigger, identifier
            )
            if item.current_token_id != body.parent_id:
                raise HTTPException(
                    409, "Trigger token changed; reload before rotation"
                )
            token = issue_token(db, item, settings)
            return {**public(item, settings), "webhook_token": token}

    @app.post(
        "/api/v1/projects/{project_id}/repository-triggers/{identifier}/revoke-token",
        dependencies=dependencies,
    )
    def revoke(identifier: str, body: TokenEdit, request: Request):
        with sessions.begin() as db:
            transition_lock(db)
            item = require_owned(
                db, request_scope(request, db), RepositoryTrigger, identifier
            )
            if item.current_token_id != body.parent_id:
                raise HTTPException(
                    409, "Trigger token changed; reload before revocation"
                )
            if item.current_token_id:
                db.get(TriggerToken, item.current_token_id).revoked_at = time.time()
                item.current_token_id = None
            return public(item, settings)

    @app.get(
        "/api/v1/projects/{project_id}/repository-triggers/{identifier}/receipts",
        dependencies=dependencies,
    )
    def receipts(identifier: str, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            require_owned(db, scope, RepositoryTrigger, identifier)
            return [
                {
                    key: getattr(item, key)
                    for key in (
                        "id",
                        "event_id",
                        "status",
                        "source_key",
                        "run_id",
                        "workflow_revision_id",
                        "repository_revision_id",
                        "config_revision_id",
                        "received_at",
                    )
                }
                for item in db.scalars(
                    select(TriggerReceipt)
                    .where(
                        TriggerReceipt.project_id == scope.project_id,
                        TriggerReceipt.binding_id == identifier,
                    )
                    .order_by(TriggerReceipt.received_at.desc())
                    .limit(100)
                )
            ]
