"""Scoped immutable graph publication and launch-time revision snapshots."""

import hashlib
import json
from fastapi import Depends, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy import select, desc, update as sql_update
from app.database import (
    WorkflowDefinitionRecord,
    WorkflowRevision,
    WorkflowLaunchSnapshot,
    Revision,
    SecretVersion,
)
from app.projects import (
    request_scope,
    require_owned,
    project_parameter,
    select_repository,
    effective_configuration,
)
from app.project_context import encrypt, decrypt, require_secret
from app.worker_pools import pool_record, membership
from factory.workflows.definitions import (
    WorkflowDefinition,
    DockerStage,
    stage_capability,
)


class DefinitionEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: dict
    parent_id: str | None = None


def parse_definition(document):
    try:
        return WorkflowDefinition.model_validate(document)
    except ValidationError as error:
        raise HTTPException(
            422,
            {
                "message": "Invalid workflow schema, graph or bindings",
                "fields": [
                    {"path": list(item["loc"][:8]), "type": item["type"]}
                    for item in error.errors(include_input=False, include_url=False)[
                        :20
                    ]
                ],
            },
        ) from None


def prepare_definition(db, scope, config, document, *, allow_bridge=False, frozen=None):
    graph = parse_definition(document)
    factory = (
        require_owned(db, scope, Revision, frozen["factory_revision_id"])
        if frozen
        else db.scalar(
            select(Revision)
            .where(Revision.project_id == scope.project_id, Revision.scope == "factory")
            .order_by(desc(Revision.created_at))
            .limit(1)
        )
    )
    if factory is None:
        raise HTTPException(404, "Configuration not found")
    repositories = {}
    for alias in graph.repositories:
        from app.database import RepositoryRevision, RepositoryConnection

        if frozen is not None:
            entry = frozen["repositories"].get(alias)
            if entry is None:
                raise HTTPException(
                    422, "Follow-up repository was not declared by the launch"
                )
            connection = require_owned(db, scope, RepositoryConnection, entry["id"])
            revision = require_owned(
                db, scope, RepositoryRevision, entry["revision_id"]
            )
            if revision.repository_id != connection.id:
                raise HTTPException(404, "Resource not found")
        else:
            connection = select_repository(db, scope, alias)
            revision = require_owned(
                db, scope, RepositoryRevision, connection.revision_id
            )
        repositories[alias] = {
            "id": connection.id,
            "revision_id": revision.id,
            "document": revision.document,
        }
    for purpose, identity in graph.secrets.items():
        version = require_owned(db, scope, SecretVersion, identity)
        if version.repository_id is not None:
            raise HTTPException(
                422, "Repository-scoped secrets belong on a matching stage"
            )
        require_secret(db, scope, identity, purpose)
    stages = {}
    for stage in graph.stages:
        selected = stage.repository or graph.default_repository
        repository = repositories[selected]
        pool = pool_record(db, stage.pool_id)
        membership(db, scope.project_id, pool.id)
        executor, action = stage_capability(stage)
        if (
            stage.role != pool.profile["role"]
            or executor not in pool.profile["executors"]
            or action not in pool.profile["actions"]
        ):
            raise HTTPException(409, "Pool does not support stage requirements")
        if (
            isinstance(stage, DockerStage)
            and stage.network == "bridge"
            and not allow_bridge
        ):
            raise HTTPException(
                422, "Bridge networking is not enabled by deployment policy"
            )
        profile = stage.profile or stage.role
        try:
            settings, _, _ = effective_configuration(
                db,
                scope,
                config,
                selected,
                profile=profile,
                workflow=graph.defaults,
                stage=stage.overrides,
                factory_revision_id=factory.id,
                repository_revision_id=repository["revision_id"],
            )
        except ValueError:
            raise HTTPException(422, "Effective stage settings are invalid") from None
        if settings.worker_type != stage.role:
            raise HTTPException(
                422, "Project behavior profile does not match stage role"
            )
        grants = {**graph.secrets, **stage.secrets}
        github = repository["document"].get("credential_id")
        if github:
            if "github" in grants and grants["github"] != github:
                raise HTTPException(
                    422, "GitHub grant must match the selected repository revision"
                )
            grants["github"] = github
        elif "github" in grants:
            raise HTTPException(
                422, "Configure the GitHub grant on the repository connection"
            )
        for purpose, identity in grants.items():
            require_secret(db, scope, identity, purpose, repository["id"])
        stages[stage.id] = {
            "repository_id": repository["id"],
            "repository_alias": selected,
            "repository_revision_id": repository["revision_id"],
            "config_revision_id": factory.id,
            "pool_id": pool.id,
            "profile": profile,
            "role": stage.role,
            "executor": executor,
            "action": action,
            "settings_json": json.dumps(jsonable_encoder(settings), allow_nan=False),
            "secret_versions": grants,
        }
    return graph, {
        "factory_revision_id": factory.id,
        "repositories": repositories,
        "stages": stages,
    }


def graph_digest(document):
    return hashlib.sha256(
        json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def pin_definition(db, scope, revision_id, config, settings, *, frozen=None):
    """Trusted run-launch transaction calls this once, then persists its pin ID.

    No latest revisions are resolved on read/replay. Stage grant references are
    pinned here; the action catalog selects minimum purposes before delivery.
    """
    revision = require_owned(db, scope, WorkflowRevision, revision_id)
    graph, metadata = prepare_definition(
        db,
        scope,
        config,
        decrypt(revision.graph_ciphertext, settings),
        allow_bridge=settings.job_bridge is not None,
        frozen=frozen,
    )
    value = {
        "workflow_revision_id": revision.id,
        "definition_id": revision.definition_id,
        "definition": graph.model_dump(mode="json"),
        **metadata,
    }
    item = WorkflowLaunchSnapshot(
        project_id=scope.project_id,
        workflow_revision_id=revision.id,
        ciphertext=encrypt(value, settings),
    )
    db.add(item)
    db.flush()
    return item


def read_definition_pin(db, scope, identifier, settings):
    item = require_owned(db, scope, WorkflowLaunchSnapshot, identifier)
    return decrypt(item.ciphertext, settings)


def install(app, sessions, config, settings):
    def public(item):
        return {
            key: getattr(item, key)
            for key in ("id", "project_id", "name", "revision_id", "created_at")
        }

    def revision_metadata(item):
        return {
            key: getattr(item, key)
            for key in (
                "id",
                "project_id",
                "definition_id",
                "parent_id",
                "digest",
                "created_at",
            )
        }

    @app.get(
        "/api/v1/projects/{project_id}/workflow-catalog",
        dependencies=[Depends(project_parameter)],
    )
    def catalog(request: Request):
        from factory.workflows.action_schemas import CATALOG

        with sessions() as db:
            request_scope(request, db)
        return {
            "definition_schema": WorkflowDefinition.model_json_schema(),
            "actions": [
                {
                    "id": action.id,
                    "role": action.role,
                    "input_schema": action.arguments.model_json_schema()
                    if action.arguments
                    else {"type": "object"},
                    "output_schema": action.output.model_json_schema()
                    if action.output
                    else {"type": "object"},
                }
                for action in CATALOG.values()
            ],
            "executors": [
                {"id": "builtin", "available": True, "version": "builtin@1"},
                {"id": "docker", "available": True, "version": "docker@1"},
                {"id": "kubernetes", "available": False},
                {"id": "function", "available": False},
            ],
        }

    @app.post(
        "/api/v1/projects/{project_id}/workflow-definitions/validate",
        dependencies=[Depends(project_parameter)],
    )
    def validate(body: DefinitionEdit, request: Request):
        with sessions() as db:
            graph, _ = prepare_definition(
                db,
                request_scope(request, db),
                config,
                body.document,
                allow_bridge=settings.job_bridge is not None,
            )
            return {
                "digest": graph_digest(graph.model_dump(mode="json")),
                "stages": len(graph.stages),
            }

    @app.get(
        "/api/v1/projects/{project_id}/workflow-definitions",
        dependencies=[Depends(project_parameter)],
    )
    def definitions(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return [
                public(item)
                for item in db.scalars(
                    select(WorkflowDefinitionRecord)
                    .where(WorkflowDefinitionRecord.project_id == scope.project_id)
                    .order_by(WorkflowDefinitionRecord.name)
                )
            ]

    @app.post(
        "/api/v1/projects/{project_id}/workflow-definitions",
        status_code=201,
        dependencies=[Depends(project_parameter)],
    )
    def create(body: DefinitionEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            if body.parent_id:
                raise HTTPException(
                    422, "A new definition cannot select an existing parent"
                )
            graph, _ = prepare_definition(
                db,
                scope,
                config,
                body.document,
                allow_bridge=settings.job_bridge is not None,
            )
            if db.scalar(
                select(WorkflowDefinitionRecord.id).where(
                    WorkflowDefinitionRecord.project_id == scope.project_id,
                    WorkflowDefinitionRecord.name == graph.name,
                )
            ):
                raise HTTPException(409, "Definition name already exists")
            item = WorkflowDefinitionRecord(
                project_id=scope.project_id, name=graph.name
            )
            db.add(item)
            db.flush()
            document = graph.model_dump(mode="json")
            revision = WorkflowRevision(
                project_id=scope.project_id,
                definition_id=item.id,
                graph_ciphertext=encrypt(document, settings),
                digest=graph_digest(document),
            )
            db.add(revision)
            db.flush()
            item.revision_id = revision.id
            return public(item)

    @app.get(
        "/api/v1/projects/{project_id}/workflow-definitions/{identifier}",
        dependencies=[Depends(project_parameter)],
    )
    def definition(identifier: str, request: Request):
        with sessions() as db:
            return public(
                require_owned(
                    db, request_scope(request, db), WorkflowDefinitionRecord, identifier
                )
            )

    @app.put(
        "/api/v1/projects/{project_id}/workflow-definitions/{identifier}",
        dependencies=[Depends(project_parameter)],
    )
    def update(identifier: str, body: DefinitionEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            item = db.scalar(
                select(WorkflowDefinitionRecord)
                .where(
                    WorkflowDefinitionRecord.project_id == scope.project_id,
                    WorkflowDefinitionRecord.id == identifier,
                )
                .with_for_update()
            )
            if item is None:
                raise HTTPException(404, "Definition not found")
            parent = require_owned(db, scope, WorkflowRevision, body.parent_id or "")
            if parent.definition_id != item.id:
                raise HTTPException(404, "Resource not found")
            if item.revision_id != parent.id:
                raise HTTPException(409, "Definition changed; reload before saving")
            graph, _ = prepare_definition(
                db,
                scope,
                config,
                body.document,
                allow_bridge=settings.job_bridge is not None,
            )
            if db.scalar(
                select(WorkflowDefinitionRecord.id).where(
                    WorkflowDefinitionRecord.project_id == scope.project_id,
                    WorkflowDefinitionRecord.name == graph.name,
                    WorkflowDefinitionRecord.id != item.id,
                )
            ):
                raise HTTPException(409, "Definition name already exists")
            document = graph.model_dump(mode="json")
            revision = WorkflowRevision(
                project_id=scope.project_id,
                definition_id=item.id,
                parent_id=parent.id,
                graph_ciphertext=encrypt(document, settings),
                digest=graph_digest(document),
            )
            db.add(revision)
            db.flush()
            # PostgreSQL locks serialize readers; SQLite ignores FOR UPDATE.
            # Compare-and-swap the head on every backend so a stale writer rolls
            # back its inserted revision rather than creating a second branch.
            changed = db.execute(
                sql_update(WorkflowDefinitionRecord)
                .where(
                    WorkflowDefinitionRecord.id == item.id,
                    WorkflowDefinitionRecord.project_id == scope.project_id,
                    WorkflowDefinitionRecord.revision_id == parent.id,
                )
                .values(revision_id=revision.id, name=graph.name)
                .execution_options(synchronize_session=False)
            )
            if changed.rowcount != 1:
                raise HTTPException(409, "Definition changed; reload before saving")
            db.refresh(item)
            return public(item)

    @app.get(
        "/api/v1/projects/{project_id}/workflow-definitions/{identifier}/revisions",
        dependencies=[Depends(project_parameter)],
    )
    def revisions(identifier: str, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            require_owned(db, scope, WorkflowDefinitionRecord, identifier)
            return [
                revision_metadata(item)
                for item in db.scalars(
                    select(WorkflowRevision)
                    .where(
                        WorkflowRevision.project_id == scope.project_id,
                        WorkflowRevision.definition_id == identifier,
                    )
                    .order_by(desc(WorkflowRevision.created_at))
                )
            ]

    @app.get(
        "/api/v1/projects/{project_id}/workflow-definition-revisions/{identifier}",
        dependencies=[Depends(project_parameter)],
    )
    def revision(identifier: str, request: Request):
        with sessions() as db:
            item = require_owned(
                db, request_scope(request, db), WorkflowRevision, identifier
            )
            return {
                **revision_metadata(item),
                "document": decrypt(item.graph_ciphertext, settings),
            }
