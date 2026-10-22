"""Ownership checks and project/repository operator APIs.

A missing and a foreign identity deliberately have the same response. Workers
and pools are deployment infrastructure; they do not use ProjectOwned.
"""
from dataclasses import dataclass
from copy import deepcopy
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import Project, RepositoryConnection, Revision, DEFAULT_PROJECT_ID


@dataclass(frozen=True)
class ProjectScope:
    project_id: str


def require_owned(db, scope, model, identity):
    item = db.get(model, str(identity))
    if item is None or item.project_id != scope.project_id:
        raise HTTPException(404, "Resource not found")
    return item


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

    @app.get("/api/v1/projects/{project_id}")
    def project(project_id: str):
        with sessions() as db:
            item = db.get(Project, project_id)
            if item is None:
                raise HTTPException(404, "Project not found")
            return row(item)

    @app.get("/api/v1/projects/{project_id}/repositories")
    def repositories(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return [row(p) for p in db.scalars(select(RepositoryConnection).where(
                RepositoryConnection.project_id == scope.project_id).order_by(RepositoryConnection.alias))]

    @app.get("/api/v1/projects/{project_id}/repositories/{identifier}")
    def repository(identifier: str, request: Request):
        with sessions() as db:
            return row(require_owned(db, request_scope(request, db), RepositoryConnection, identifier))

    @app.post("/api/v1/projects/{project_id}/repositories", status_code=201)
    def save_repository(body: RepositoryEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            if body.credential_id:
                # Secret grants are installed with the credential service; never
                # allow unchecked UUIDs in the meantime.
                from app.database import SecretVersion
                require_owned(db, scope, SecretVersion, body.credential_id)
            if db.scalar(select(RepositoryConnection.id).where(
                RepositoryConnection.project_id == scope.project_id, RepositoryConnection.alias == body.alias)):
                raise HTTPException(409, "Repository alias already exists")
            validate_overrides(body.overrides)
            item = RepositoryConnection(project_id=scope.project_id, **body.model_dump())
            db.add(item)
            db.flush()
            return row(item)


# Identity/credential/adapter selection is not a behavior override. Task schemas
# and all remaining Settings fields are validated through Configurations.resolve.
RESERVED = frozenset({"repository", "worker_name", "worker_type", "worker_labels",
    "control_revision", "state_path", "workspace", "hatchet_namespace", "hatchet_host_port",
    "hatchet_server_url", "hatchet_tls_strategy"})


def validate_overrides(value):
    from dataclasses import fields
    from factory.settings import Settings
    allowed = {f.name for f in fields(Settings)} - RESERVED
    if not isinstance(value, dict) or set(value) - allowed or any(k.endswith("factory") for k in value):
        raise HTTPException(422, "Unsupported behavior override")
    from factory.domain.safety import find_sensitive_categories
    import json
    if find_sensitive_categories(json.dumps(value)):
        raise HTTPException(422, "Use credential references instead of inline secrets")
