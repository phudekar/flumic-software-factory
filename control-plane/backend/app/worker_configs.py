"""Scoped configuration API and immutable snapshot compilation."""

import uuid
from fastapi import HTTPException, Request, APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from factory.configuration import read_configuration, profile_fragment
from sqlalchemy import select, desc, text
from app.projects import project_parameter, request_scope, require_owned
from app.database import Revision, WorkerConfiguration, Worker, Audit, DEFAULT_PROJECT_ID


def bootstrap(db, config, project_id=DEFAULT_PROJECT_ID):
    if db.bind.dialect.name == "postgresql":
        db.execute(text("SELECT pg_advisory_xact_lock(6423401)"))
    latest = db.scalar(
        select(Revision)
        .where(Revision.scope == "factory", Revision.project_id == project_id)
        .order_by(desc(Revision.created_at))
        .limit(1)
    )
    if not db.scalar(select(WorkerConfiguration.id).where(WorkerConfiguration.project_id == project_id).limit(1)):
        paths = sorted((config.root / "config/worker-profiles").glob("*.yaml"))
        if not paths:
            raise ValueError("Bootstrap worker profile files are missing")
        names = set()
        for path in paths:
            value = read_configuration(path)
            if (
                not isinstance(value, dict)
                or set(value) != {"version", "name", "profile", "worker", "document"}
                or value["version"] != 1
            ):
                raise ValueError("Invalid bootstrap worker profile file: " + path.name)
            profile_fragment(value)
            # Reuse the operator API schema and full worker validation so bootstrap
            # files cannot introduce credentials, factory settings or unknown fields.
            parsed = WorkerConfigEdit.model_validate(
                {
                    "name": value["name"],
                    "profile": value["profile"],
                    "factory_revision_id": latest.id,
                    "document": value["document"],
                }
            )
            if parsed.profile not in latest.document["workers"]:
                raise ValueError(
                    "Bootstrap profile references an unknown factory worker type"
                )
            if parsed.name in names:
                raise ValueError("Duplicate bootstrap worker profile name")
            names.add(parsed.name)
            resolved = config.compose(latest.document, parsed.profile, parsed.document)
            db.add(
                WorkerConfiguration(
                    project_id=project_id,
                    id=str(uuid.uuid4()),
                    family_id=str(uuid.uuid4()),
                    name=parsed.name,
                    profile=parsed.profile,
                    factory_revision_id=latest.id,
                    document=config.split(resolved, parsed.profile)[1],
                )
            )
    if project_id != DEFAULT_PROJECT_ID:
        return
    for worker in db.scalars(
        select(Worker).where(
            Worker.config_id.is_(None), Worker.desired_state != "removed"
        )
    ):
        attach_imported_profile(db, config, worker)
        # Deliberately retain worker.revision_id and runtime; metadata migration only.


def attach_imported_profile(db, config, worker):
    source = db.get(Revision, worker.revision_id)
    document = config.split(source.document, worker.profile)[1]
    candidates = db.scalars(
        select(WorkerConfiguration).where(
            WorkerConfiguration.profile == worker.profile,
            WorkerConfiguration.factory_revision_id == source.id,
        )
    ).all()
    cfg = next((item for item in candidates if item.document == document), None)
    if cfg is None:
        cfg = WorkerConfiguration(
            id=str(uuid.uuid4()),
            family_id=str(uuid.uuid4()),
            name=worker.name + " imported",
            profile=worker.profile,
            factory_revision_id=source.id,
            document=document,
        )
        db.add(cfg)
    worker.config_id = cfg.id
    return cfg


def compile_revision(db, config, cfg):
    factory = db.get(Revision, cfg.factory_revision_id)
    document = config.compose(factory.document, cfg.profile, cfg.document)
    revision = Revision(
        id=str(uuid.uuid4()),
        scope="worker_snapshot",
        project_id=cfg.project_id,
        summary=cfg.name,
        document=document,
        digest=config.hash(document),
    )
    db.add(revision)
    return revision


class WorkerConfigEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=100)
    profile: str = Field(min_length=1, max_length=100)
    factory_revision_id: str
    document: dict
    parent_id: str | None = None
    cloned_from: str | None = None


class FactoryEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: dict
    parent_id: str
    summary: str = Field(
        default="Factory settings update", min_length=1, max_length=240
    )


def install(app, sessions, config, row):
    router = APIRouter()
    @router.get("/worker-profiles")
    @router.get("/worker-configs")
    def configurations(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            items = db.scalars(
                select(WorkerConfiguration).where(WorkerConfiguration.project_id == scope.project_id).order_by(
                    desc(WorkerConfiguration.created_at)
                )
            ).all()
            return [row(item) for item in items]

    @router.get("/worker-configs/template/{profile}")
    def template(profile: str, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            factory = db.scalar(
                select(Revision)
                .where(Revision.scope == "factory", Revision.project_id == scope.project_id)
                .order_by(desc(Revision.created_at))
                .limit(1)
            )
            if profile not in factory.document["workers"]:
                raise HTTPException(404, "Profile not found")
            return {
                "name": profile + " configuration",
                "profile": profile,
                "factory_revision_id": factory.id,
                "document": config.split(factory.document, profile)[1],
            }

    @router.post("/worker-profiles/validate")
    def validate_profile(body: WorkerConfigEdit, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            for identity in (body.parent_id, body.cloned_from):
                if identity is not None:
                    require_owned(db, scope, WorkerConfiguration, identity)
            factory = require_owned(db, scope, Revision, body.factory_revision_id)
            if factory is None or factory.scope != "factory":
                raise HTTPException(404, "Factory configuration not found")
            if body.profile not in factory.document["workers"]:
                raise HTTPException(422, "Unknown factory worker profile")
            config.compose(factory.document, body.profile, body.document)
        return {"valid": True}

    @router.post("/worker-profiles", status_code=201)
    @router.post("/worker-configs", status_code=201)
    def save(body: WorkerConfigEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            if body.parent_id and body.cloned_from:
                raise HTTPException(422, "Choose revision or clone")
            factory = require_owned(db, scope, Revision, body.factory_revision_id)
            if factory is None or factory.scope != "factory":
                raise HTTPException(404, "Factory revision not found")
            if body.profile not in factory.document["workers"]:
                raise HTTPException(422, "Unknown profile")
            config.compose(factory.document, body.profile, body.document)
            parent = db.get(
                WorkerConfiguration, body.parent_id or body.cloned_from or ""
            )
            if (body.parent_id or body.cloned_from) and (parent is None or parent.project_id != scope.project_id):
                raise HTTPException(404, "Source configuration not found")
            if body.parent_id:
                if db.bind.dialect.name == "postgresql":
                    db.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": int(config.hash(parent.family_id)[:15], 16)},
                    )
                latest = db.scalar(
                    select(WorkerConfiguration)
                    .where(WorkerConfiguration.family_id == parent.family_id, WorkerConfiguration.project_id == scope.project_id)
                    .order_by(desc(WorkerConfiguration.created_at))
                    .limit(1)
                )
                if latest.id != parent.id:
                    raise HTTPException(
                        409, "Worker configuration changed; reload before saving"
                    )
                if parent.profile != body.profile:
                    raise HTTPException(422, "Clone to change the worker profile")
            item = WorkerConfiguration(
                project_id=scope.project_id,
                id=str(uuid.uuid4()),
                family_id=parent.family_id if body.parent_id else str(uuid.uuid4()),
                **body.model_dump(),
            )
            db.add(item)
            db.add(Audit(action="worker-config.publish", resource=item.id))
            db.flush()
            return row(item)

    @router.get("/factory-config")
    def factory_config(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            revision = db.scalar(
                select(Revision)
                .where(Revision.scope == "factory", Revision.project_id == scope.project_id)
                .order_by(desc(Revision.created_at))
                .limit(1)
            )
            return {
                "id": revision.id,
                "document": config.factory_view(revision.document),
            }

    @router.post("/factory-config", status_code=201)
    def save_factory(body: FactoryEdit, request: Request):
        with sessions.begin() as db:
            scope = request_scope(request, db)
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(6423400)"))
            require_owned(db, scope, Revision, body.parent_id)
            latest = db.scalar(
                select(Revision)
                .where(Revision.scope == "factory", Revision.project_id == scope.project_id)
                .order_by(desc(Revision.created_at))
                .with_for_update()
                .limit(1)
            )
            if latest.id != body.parent_id:
                raise HTTPException(
                    409, "Factory configuration changed; reload before saving"
                )
            document = config.update_factory(latest.document, body.document)
            revision = Revision(
                project_id=scope.project_id,
                id=str(uuid.uuid4()),
                parent_id=latest.id,
                summary=body.summary,
                document=document,
                digest=config.hash(document),
            )
            db.add(revision)
            db.add(Audit(action="factory-config.publish", resource=revision.id))
            db.flush()
            return {"id": revision.id, "document": config.factory_view(document)}

    app.include_router(router, prefix="/api/v1")
    app.include_router(router, prefix="/api/v1/projects/{project_id}", dependencies=[Depends(project_parameter)])
