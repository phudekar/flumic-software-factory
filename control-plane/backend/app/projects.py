"""Ownership checks and project/repository operator APIs.

A missing and a foreign identity deliberately have the same response. Workers
and pools are deployment infrastructure; they do not use ProjectOwned.
"""
from dataclasses import dataclass
from copy import deepcopy
from fastapi import HTTPException, Request, Depends, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from app.database import Project, RepositoryConnection, RepositoryRevision, Revision, DEFAULT_PROJECT_ID


@dataclass(frozen=True)
class ProjectScope:
    project_id: str


def require_owned(db, scope, model, identity):
    item = db.get(model, str(identity))
    if item is None or item.project_id != scope.project_id:
        raise HTTPException(404, "Resource not found")
    return item


def project_parameter(project_id: str = Path(description="Project UUID; legacy unscoped routes always use the default project")):
    return project_id


def request_scope(request, db):
    project_id = request.path_params.get("project_id", DEFAULT_PROJECT_ID)
    if db.get(Project, project_id) is None:
        raise HTTPException(404, "Project not found")
    return ProjectScope(project_id)


class ProjectEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    slug: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")
    name: str = Field(min_length=1, max_length=100)


class RepositoryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    alias: str = Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")
    provider: str = Field(default="github", pattern=r"^github$")
    host: str = Field(default="github.com", pattern=r"^github\.com$")
    owner: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    name: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,100}$")
    overrides: dict = Field(default_factory=dict)
    credential_id: str | None = None


class RepositoryUpdate(RepositoryEdit):
    parent_id: str


class EffectivePreview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str
    stage_repository: str | None = None
    profile: str = "simple"
    workflow: dict = Field(default_factory=dict)
    stage: dict = Field(default_factory=dict)


def install(app, sessions, config, row):
    @app.get("/api/v1/projects")
    def projects():
        with sessions() as db:
            return [row(p) for p in db.scalars(select(Project).order_by(Project.slug))]

    @app.post("/api/v1/projects", status_code=201)
    def create_project(body: ProjectEdit):
        from app.worker_configs import bootstrap
        with sessions.begin() as db:
            if db.scalar(select(Project.id).where(Project.slug == body.slug)):
                raise HTTPException(409, "Project slug already exists")
            project = Project(**body.model_dump())
            db.add(project)
            db.flush()
            # A new project's validated template is independent of other projects'
            # operator edits. No worker or external repository is started here.
            document = deepcopy(config.approved)
            db.add(Revision(project_id=project.id, summary="Initial project configuration",
                            document=document, digest=config.hash(document)))
            db.flush()
            bootstrap(db, config, project_id=project.id)
            return row(project)

    @app.get("/api/v1/projects/{project_id}", dependencies=[Depends(project_parameter)])
    def project(project_id: str):
        with sessions() as db:
            item = db.get(Project, project_id)
            if item is None:
                raise HTTPException(404, "Project not found")
            return row(item)

    @app.get("/api/v1/projects/{project_id}/repositories", dependencies=[Depends(project_parameter)])
    def repositories(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return [row(p) for p in db.scalars(select(RepositoryConnection).where(
                RepositoryConnection.project_id == scope.project_id).order_by(RepositoryConnection.alias))]

    @app.get("/api/v1/projects/{project_id}/repositories/{identifier}", dependencies=[Depends(project_parameter)])
    def repository(identifier: str, request: Request):
        with sessions() as db:
            return row(require_owned(db, request_scope(request, db), RepositoryConnection, identifier))

    @app.post("/api/v1/projects/{project_id}/repositories", status_code=201, dependencies=[Depends(project_parameter)])
    def save_repository(body: RepositoryEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            if body.credential_id:
                # Credential management is installed in issue #4. An arbitrary
                # UUID must never be persisted as an unchecked grant.
                raise HTTPException(404, "Credential not found")
            if db.scalar(select(RepositoryConnection.id).where(
                RepositoryConnection.project_id == scope.project_id, RepositoryConnection.alias == body.alias)):
                raise HTTPException(409, "Repository alias already exists")
            validate_overrides(body.overrides)
            item = RepositoryConnection(project_id=scope.project_id, **body.model_dump())
            db.add(item)
            db.flush()
            revision = RepositoryRevision(project_id=scope.project_id, repository_id=item.id, document=body.model_dump())
            db.add(revision)
            db.flush()
            item.revision_id = revision.id
            # Validate the full resolved Settings, not only override keys.
            validate_repository_configuration(db, scope, config, body.alias)
            return row(item)

    @app.put("/api/v1/projects/{project_id}/repositories/{identifier}", dependencies=[Depends(project_parameter)])
    def update_repository(identifier: str, body: RepositoryUpdate, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            item = db.scalar(select(RepositoryConnection).where(
                RepositoryConnection.project_id == scope.project_id, RepositoryConnection.id == identifier).with_for_update())
            if item is None:
                raise HTTPException(404, "Resource not found")
            parent = require_owned(db, scope, RepositoryRevision, body.parent_id)
            if parent.repository_id != item.id:
                raise HTTPException(404, "Resource not found")
            if item.revision_id != parent.id:
                raise HTTPException(409, "Repository changed; reload before saving")
            if body.alias != item.alias:
                raise HTTPException(422, "Repository aliases are stable; create a new connection")
            if body.credential_id:
                raise HTTPException(404, "Credential not found")
            validate_overrides(body.overrides)
            document = body.model_dump(exclude={"parent_id"})
            revision = RepositoryRevision(project_id=scope.project_id, repository_id=item.id,
                                          parent_id=body.parent_id, document=document)
            db.add(revision)
            db.flush()
            for key, value in document.items():
                setattr(item, key, value)
            item.revision_id = revision.id
            db.flush()
            validate_repository_configuration(db, scope, config, item.alias)
            return row(item)

    @app.post("/api/v1/projects/{project_id}/config/effective", dependencies=[Depends(project_parameter)])
    def effective(body: EffectivePreview, request: Request):
        from fastapi.encoders import jsonable_encoder
        with sessions() as db:
            scope = request_scope(request, db)
            resolved, factory, repository = effective_configuration(db, scope, config,
                body.repository, stage_repository=body.stage_repository, profile=body.profile,
                workflow=body.workflow, stage=body.stage)
            return {"project_id":scope.project_id, "factory_revision_id":factory.id,
                    "repository_id":repository.id, "repository_revision_id":repository.revision_id,
                    "settings":jsonable_encoder(resolved)}


# Identity/credential/adapter selection is not a behavior override. Task schemas
# and all remaining Settings fields are validated through Configurations.resolve.
BEHAVIOR_FIELDS = frozenset({"developer_agent", "max_patch_iterations", "path_policy",
    "openai_model", "codex_model", "claude_model", "max_files_per_iteration", "max_file_bytes",
    "max_context_tokens", "max_completion_tokens", "api_max_retries", "api_timeout_seconds",
    "cli_timeout_seconds", "discussion_poll_seconds", "labels", "tasks", "conversation_capture"})


def validate_overrides(value):
    allowed = BEHAVIOR_FIELDS
    if not isinstance(value, dict) or set(value) - allowed or any(k.endswith("factory") for k in value):
        raise HTTPException(422, "Unsupported behavior override")
    tasks = value.get("tasks", {})
    if not isinstance(tasks, dict) or any(not isinstance(task, dict) or "worker_type" in task for task in tasks.values()):
        raise HTTPException(422, "Task role is not a behavior override")
    from factory.domain.safety import find_sensitive_categories
    import json
    if find_sensitive_categories(json.dumps(value)):
        raise HTTPException(422, "Use credential references instead of inline secrets")


def select_repository(db, scope, run_alias, stage_alias=None):
    """Resolve an explicit run/stage alias only inside its owning project."""
    run = db.scalar(select(RepositoryConnection).where(
        RepositoryConnection.project_id == scope.project_id, RepositoryConnection.alias == run_alias))
    if run is None:
        raise HTTPException(404, "Repository not found")
    if stage_alias is None:
        return run
    stage = db.scalar(select(RepositoryConnection).where(
        RepositoryConnection.project_id == scope.project_id, RepositoryConnection.alias == stage_alias))
    if stage is None:
        raise HTTPException(404, "Repository not found")
    return stage


def effective_configuration(db, scope, config, repository, *, stage_repository=None,
                            profile="simple", workflow=None, stage=None, limits=None,
                            factory_revision_id=None, repository_revision_id=None):
    from sqlalchemy import desc
    connection = select_repository(db, scope, repository, stage_repository)
    revision = require_owned(db, scope, RepositoryRevision, repository_revision_id or connection.revision_id)
    if revision.repository_id != connection.id:
        raise HTTPException(404, "Resource not found")
    if factory_revision_id:
        factory = require_owned(db, scope, Revision, factory_revision_id)
    else:
        factory = db.scalar(select(Revision).where(Revision.project_id == scope.project_id,
            Revision.scope == "factory").order_by(desc(Revision.created_at)).limit(1))
    if factory is None or factory.scope != "factory" or profile not in factory.document["workers"]:
        raise HTTPException(404, "Configuration profile not found")
    value = config.effective(factory.document, profile, revision.document,
                             workflow or {}, stage or {}, limits=limits)
    return value, factory, connection


def validate_repository_configuration(db, scope, config, alias):
    """A connection must resolve for the project's actual configured profiles."""
    from sqlalchemy import desc
    factory = db.scalar(select(Revision).where(Revision.project_id == scope.project_id,
        Revision.scope == "factory").order_by(desc(Revision.created_at)).limit(1))
    if factory is None:
        raise HTTPException(404, "Configuration not found")
    for profile in factory.document["workers"]:
        effective_configuration(db, scope, config, alias, profile=profile,
                                factory_revision_id=factory.id)
