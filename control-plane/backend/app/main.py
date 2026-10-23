"""Same-origin operator API. Vendor credentials never enter browser responses."""

from contextlib import asynccontextmanager
from collections import defaultdict
import json
import secrets
import time
import uuid
import yaml
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Request, Response, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, desc, or_, text, func
from app.database import (
    database,
    Project, DEFAULT_PROJECT_ID,
    Revision,
    Worker,
    Operation,
    Event,
    Conversation,
    Session,
    Audit,
    WorkerConfiguration,
)
from app.projects import project_parameter, ProjectScope, request_scope, require_owned
from app.services import Configurations
from app.security import digest, redact
from app.reconciler import worker_token


class Login(BaseModel):
    password: str = Field(max_length=1024)


class ConfigEdit(BaseModel):
    document: dict
    parent_id: str | None = None
    summary: str = Field(default="Configuration update", min_length=1, max_length=240)


class CreateWorker(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,59}$")
    profile: str = Field(default="simple", max_length=100)
    revision_id: str | None = None
    config_id: str | None = None
    worker_profile_id: str | None = None
    target_id: str = "local-docker"


class Action(BaseModel):
    config_id: str | None = None
    action: str = Field(pattern=r"^(start|restart|stop|remove|configure)$")
    revision_id: str | None = None


class Incoming(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    kind: str = Field(
        pattern=r"^(heartbeat|attempt.started|attempt.completed|model.completed|log)$"
    )
    occurred_at: float = Field(ge=0, allow_inf_nan=False)
    run_id: str | None = Field(default=None, max_length=120)
    attempt_id: str | None = Field(default=None, max_length=120)
    stage: str | None = Field(default=None, max_length=100)
    payload: dict = Field(default_factory=dict)


def row(value):
    return {
        column.name: getattr(value, column.name) for column in value.__table__.columns
    }


def _lifecycle_marker(db, worker):
    operations = db.execute(select(func.count(Operation.id), func.max(Operation.updated_at)).where(Operation.worker_id == worker.id)).one()
    return (worker.revision_id, worker.config_id, worker.desired_state, *operations)


def create_app(settings=None, sessions=None, runtime=None, workflow=None):
    from app.settings import AppSettings

    settings = settings or AppSettings.load()
    if sessions is None:
        _, sessions = database(settings.database_url)
    config = Configurations(settings.root)
    cipher = Fernet(settings.encryption_key.encode())
    hasher = PasswordHasher()
    password_hash = hasher.hash(settings.admin_password)
    failures = defaultdict(list)

    @asynccontextmanager
    async def lifespan(app):
        with sessions.begin() as db:
            if db.get(Project, DEFAULT_PROJECT_ID) is None:
                db.add(Project(id=DEFAULT_PROJECT_ID, slug="default", name="Default project"))
                db.flush()
            if db.scalar(select(Revision.id).where(Revision.project_id == DEFAULT_PROJECT_ID).limit(1)) is None:
                config.validate(config.approved)
                db.add(
                    Revision(
                        summary="Imported repository configuration",
                        document=config.approved,
                        digest=config.hash(config.approved),
                    )
                )
            db.flush()
            from app.worker_configs import bootstrap

            bootstrap(db, config)
        yield

    app = FastAPI(
        title="Flumic AI Control Plane",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.sessions = sessions
    app.state.settings = settings
    from app.worker_configs import install, compile_revision, attach_imported_profile

    install(app, sessions, config, row)
    from app.projects import install as install_projects
    install_projects(app, sessions, config, row)
    from app.project_context import install as install_project_context
    install_project_context(app, sessions, settings)
    from app.worker_pools import install as install_worker_pools
    install_worker_pools(app, sessions, settings)
    from app.workflow_definitions import install as install_workflow_definitions
    install_workflow_definitions(app, sessions, config, settings)

    def runtime_adapter():
        nonlocal runtime
        if runtime is None:
            from app.adapters.docker import DockerRuntime

            runtime = DockerRuntime(settings, config)
        return runtime

    workflow_clients = {}

    def workflow_adapter():
        if workflow is not None:
            return workflow
        from app.adapters.hatchet import HatchetEngine, create_client

        with sessions() as db:
            revision = db.scalar(
                select(Revision)
                .where(Revision.scope == "factory", Revision.project_id == DEFAULT_PROJECT_ID)
                .order_by(desc(Revision.created_at))
                .limit(1)
            )
            if revision is None or revision.project_id != DEFAULT_PROJECT_ID:
                raise ValueError("No saved factory configuration is available")
            resolved = config.resolve(revision.document, "simple")
        # Repository policy is refreshed per call; transport clients only depend
        # on TLS strategy and process-level credentials.
        strategy = resolved.hatchet_tls_strategy
        if strategy not in workflow_clients:
            workflow_clients[strategy] = create_client(resolved)
        return HatchetEngine(resolved, workflow_clients[strategy])

    from app.workflow_controls import install as install_workflow_controls

    install_workflow_controls(app, sessions, workflow_adapter)

    @app.middleware("http")
    async def security(request, call_next):
        if request.url.path.startswith("/api/") and not request.url.path.startswith("/api/v1/shared-worker/") and request.url.path not in {
            "/api/v1/auth/login",
            "/api/v1/health",
            "/api/v1/ingest",
            "/api/v1/worker-bootstrap",
        }:
            with sessions() as db:
                session = db.get(
                    Session, digest(request.cookies.get("factory_session", ""))
                )
                if session is None or session.expires_at < time.time():
                    return Response(status_code=401)
                if request.method not in {"GET", "HEAD", "OPTIONS"}:
                    if not secrets.compare_digest(
                        request.headers.get("x-csrf-token", ""), session.csrf
                    ):
                        return Response(status_code=403)
        if (
            request.method not in {"GET", "HEAD", "OPTIONS"}
            and request.url.path != "/api/v1/ingest"
        ):
            origin = request.headers.get("origin")
            if origin and origin != settings.public_origin:
                return Response(status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=422,
            content={
                "detail": "Invalid request fields; check the form values and limits."
            },
        )

    @app.exception_handler(ValueError)
    async def value_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=422, content={"detail": redact(str(exc))})

    @app.exception_handler(Exception)
    async def integration_error(request, exc):
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=503,
            content={
                "detail": "Integration unavailable. Check target connectivity and server configuration."
            },
        )

    @app.get("/api/v1/health")
    def health():
        with sessions() as db:
            db.scalar(select(Revision.id).limit(1))
        return {"status": "ready"}

    @app.post("/api/v1/auth/login")
    def login(body: Login, request: Request, response: Response):
        host = request.client.host if request.client else "unknown"
        failures[host] = [when for when in failures[host] if when > time.time() - 60]
        if len(failures[host]) >= 8:
            raise HTTPException(429, "Too many login attempts; wait one minute")
        try:
            hasher.verify(password_hash, body.password)
        except VerificationError:
            failures[host].append(time.time())
            raise HTTPException(401, "Invalid credentials") from None
        token, csrf = secrets.token_urlsafe(40), secrets.token_urlsafe(32)
        with sessions.begin() as db:
            db.add(Session(id=digest(token), csrf=csrf, expires_at=time.time() + 28800))
            db.add(Audit(action="login", resource="operator"))
        response.set_cookie(
            "factory_session",
            token,
            httponly=True,
            secure=settings.cookie_secure,
            samesite="strict",
            max_age=28800,
        )
        return {"csrf": csrf, "role": "administrator"}

    @app.get("/api/v1/auth/session")
    def session_info(request: Request):
        with sessions() as db:
            session = db.get(Session, digest(request.cookies["factory_session"]))
            return {"csrf": session.csrf, "role": "administrator"}

    @app.post("/api/v1/auth/logout")
    def logout(request: Request, response: Response):
        with sessions.begin() as db:
            session = db.get(
                Session, digest(request.cookies.get("factory_session", ""))
            )
            if session:
                db.delete(session)
        response.delete_cookie("factory_session")
        return {"ok": True}

    @app.get("/api/v1/targets")
    def targets():
        return [
            {
                "id": "local-docker",
                "name": "Local Docker",
                "runtime": "docker",
                "capabilities": {
                    "create": True,
                    "start": True,
                    "stop": True,
                    "drain": False,
                    "kubernetes": False,
                },
                "drain_reason": "Drain requires assignment and pause-race verification against this Hatchet server.",
            }
        ]

    @app.get("/api/v1/workers")
    def workers():
        warnings = []
        try:
            actual = runtime_adapter().list()
        except Exception:
            actual = []
            warnings.append("Docker unavailable; observed runtime state is unknown.")
        try:
            engine_workers = workflow_adapter().workers()
        except Exception:
            engine_workers = []
            warnings.append("Hatchet unavailable; engine registration is unknown.")
        with sessions() as db:
            specs = db.scalars(select(Worker).order_by(desc(Worker.created_at))).all()
            result = []
            for worker in specs:
                item = {
                    key: value
                    for key, value in row(worker).items()
                    if key != "credential_hash"
                }
                current = (
                    db.get(WorkerConfiguration, worker.config_id)
                    if worker.config_id
                    else None
                )
                latest = (
                    db.scalar(
                        select(WorkerConfiguration)
                        .where(WorkerConfiguration.family_id == current.family_id)
                        .order_by(desc(WorkerConfiguration.created_at))
                        .limit(1)
                    )
                    if current
                    else None
                )
                item["worker_profile_id"] = current.family_id if current else None
                item["worker_profile_name"] = latest.name if latest else None
                item["profile_update_pending"] = bool(
                    latest and latest.id != current.id
                )
                item["runtime"] = next(
                    (entry for entry in actual if entry["instance_id"] == worker.id),
                    None,
                )
                if (
                    item["runtime"]
                    and item["runtime"].get("revision_id") != worker.revision_id
                ):
                    item["profile_update_pending"] = True
                item["engine"] = next(
                    (entry for entry in engine_workers if entry["name"] == worker.name),
                    None,
                )
                from app.adapters.image_builder import BuildStore

                build = BuildStore(settings.snapshot_root.parent / "builds").read(
                    worker.id
                )
                item["build"] = {
                    key: value for key, value in build.items() if key != "content"
                }
                item["operations"] = [
                    row(op)
                    for op in db.scalars(
                        select(Operation)
                        .where(Operation.worker_id == worker.id)
                        .order_by(desc(Operation.created_at))
                        .limit(5)
                    )
                ]
                result.append(item)
        return {
            "managed": result,
            "discovered": [entry for entry in actual if not entry["managed"]],
            "warnings": warnings,
            "observed_at": time.time(),
        }

    @app.post("/api/v1/workers", status_code=202)
    def create_worker(body: CreateWorker, request: Request):
        if body.target_id != "local-docker":
            raise HTTPException(422, "Unsupported deployment target")
        with sessions.begin() as db:
            from app.workflow_state import transition_lock, legacy_guard
            transition_lock(db)
            for field, model in (("config_id", WorkerConfiguration), ("revision_id", Revision)):
                value = getattr(body, field)
                if value is not None:
                    require_owned(db, ProjectScope(DEFAULT_PROJECT_ID), model, value)
            cfg = db.get(WorkerConfiguration, body.config_id or "")
            if body.worker_profile_id:
                cfg = db.scalar(
                    select(WorkerConfiguration)
                    .where(WorkerConfiguration.family_id == body.worker_profile_id)
                    .order_by(desc(WorkerConfiguration.created_at))
                    .limit(1)
                )
                if cfg is None or cfg.project_id != DEFAULT_PROJECT_ID:
                    raise HTTPException(404, "Worker profile not found")
            if body.config_id and (cfg is None or cfg.project_id != DEFAULT_PROJECT_ID):
                raise HTTPException(404, "Worker configuration not found")
            revision = db.get(
                Revision, cfg.factory_revision_id if cfg else body.revision_id or ""
            )
            profile = cfg.profile if cfg else body.profile
            if revision is None or revision.project_id != DEFAULT_PROJECT_ID:
                raise HTTPException(404, "Revision not found")
            profiles = config.validate(revision.document)
            if profile not in profiles:
                raise HTTPException(422, "Unknown worker profile")
            legacy_guard(db, config, revision.document, profile)
            worker = Worker(
                id=str(uuid.uuid4()),
                name=body.name,
                profile=profile,
                revision_id=revision.id,
                config_id=cfg.id if cfg else None,
                credential_hash="",
            )
            worker.credential_hash = digest(worker_token(settings, worker.id))
            key = request.headers.get("idempotency-key")
            if not key or len(key) > 100:
                raise HTTPException(422, "Idempotency-Key is required")
            existing = db.scalar(
                select(Operation).where(Operation.key == "create:" + key)
            )
            if existing:
                return row(existing)
            if db.scalar(select(Worker.id).where(Worker.name == body.name)):
                raise HTTPException(409, "Worker name already exists")
            if cfg:
                worker.revision_id = compile_revision(db, config, cfg).id
            else:
                attach_imported_profile(db, config, worker)
            db.add(worker)
            operation = Operation(
                id=str(uuid.uuid4()),
                worker_id=worker.id,
                action="create",
                key="create:" + key,
            )
            db.add(operation)
            db.add(Audit(action="worker.create", resource=worker.id))
            db.flush()
            return row(operation)

    @app.post("/api/v1/workers/{identifier}/actions", status_code=202)
    def worker_action(identifier: str, body: Action, request: Request):
        key = request.headers.get("idempotency-key")
        if not key or len(key) > 100:
            raise HTTPException(422, "Idempotency-Key is required")
        # Docker can be slow. Observe it without holding any deployment/worker
        # transaction fence, then reject a stale observation under that fence.
        observed_runtime, observation_marker = [], None
        if body.action in {"configure", "start", "restart"}:
            with sessions() as observation_db:
                candidate = observation_db.get(Worker, identifier)
                if candidate is not None:
                    observation_marker = _lifecycle_marker(observation_db, candidate)
                    needs_observation = candidate.protocol == "legacy" and (body.action == "configure" or candidate.config_id is not None)
                else:
                    needs_observation = False
            if needs_observation:
                observed_runtime = runtime_adapter().list()
        with sessions.begin() as db:
            from app.workflow_state import transition_lock, legacy_guard
            transition_lock(db)
            for field, model in (("config_id", WorkerConfiguration), ("revision_id", Revision)):
                value = getattr(body, field)
                if value is not None:
                    require_owned(db, ProjectScope(DEFAULT_PROJECT_ID), model, value)
            worker = db.scalar(
                select(Worker).where(Worker.id == identifier).with_for_update()
            )
            if worker is None:
                raise HTTPException(404, "Managed worker not found")
            if worker.protocol == 'shared-v1' and (body.action == 'configure' or body.config_id or body.revision_id):
                raise HTTPException(422, 'Shared workers use infrastructure pool profiles, not project behavior profiles')
            existing = db.scalar(
                select(Operation).where(Operation.key == f"{identifier}:{key}")
            )
            if existing:
                return row(existing)
            if observation_marker is not None and _lifecycle_marker(db, worker) != observation_marker:
                raise HTTPException(409, "Worker changed during runtime observation; retry the action")
            if db.scalar(
                select(Operation.id).where(
                    Operation.worker_id == identifier,
                    Operation.status.in_(["pending", "running"]),
                )
            ):
                raise HTTPException(409, "Worker has an operation in progress")
            if worker.desired_state == "removed" and body.action != "remove":
                raise HTTPException(
                    409, "Worker was removed; create a new worker to run it again"
                )
            if body.action == "configure":
                cfg = db.get(WorkerConfiguration, body.config_id or "")
                if body.config_id and (cfg is None or cfg.project_id != DEFAULT_PROJECT_ID):
                    raise HTTPException(404, "Worker configuration not found")
                if cfg and cfg.profile != worker.profile:
                    raise HTTPException(422, "Configuration profile must match worker")
                revision = db.get(
                    Revision, cfg.factory_revision_id if cfg else body.revision_id or ""
                )
                if revision is None or revision.project_id != DEFAULT_PROJECT_ID:
                    raise HTTPException(404, "Configuration revision not found")
                profiles = config.validate(revision.document)
                if worker.profile not in profiles:
                    raise HTTPException(
                        422, "Worker profile is absent from this revision"
                    )
                actual = next(
                    (
                        item
                        for item in observed_runtime
                        if item.get("instance_id") == identifier
                    ),
                    None,
                )
                if actual and actual["status"] == "running":
                    raise HTTPException(
                        409, "Stop the worker before applying a configuration revision"
                    )
                worker.revision_id = (
                    compile_revision(db, config, cfg).id if cfg else revision.id
                )
                worker.config_id = cfg.id if cfg else None
                if cfg is None or cfg.project_id != DEFAULT_PROJECT_ID:
                    attach_imported_profile(db, config, worker)
            action = body.action
            if body.action in {"start", "restart"} and worker.config_id:
                current = db.get(WorkerConfiguration, worker.config_id)
                latest = db.scalar(
                    select(WorkerConfiguration)
                    .where(WorkerConfiguration.family_id == current.family_id)
                    .order_by(desc(WorkerConfiguration.created_at))
                    .limit(1)
                )
                actual = next(
                    (
                        item
                        for item in observed_runtime
                        if item.get("instance_id") == identifier
                    ),
                    None,
                )
                needs_configuration = (
                    latest.id != current.id
                    or not actual
                    or actual.get("revision_id") != worker.revision_id
                )
                if needs_configuration:
                    if (
                        body.action == "start"
                        and actual
                        and actual["status"] == "running"
                    ):
                        raise HTTPException(
                            409,
                            "Worker is running; restart to apply the latest profile",
                        )
                    if latest.id != current.id:
                        worker.config_id = latest.id
                        worker.revision_id = compile_revision(db, config, latest).id
                    action = (
                        "restart"
                        if body.action == "restart" and actual
                        else "configure_start"
                    )
            if worker.protocol == "legacy" and body.action in {"start", "restart", "configure"}:
                selected = db.get(Revision, worker.revision_id)
                legacy_guard(db, config, selected.document, worker.profile)
            operation = Operation(
                id=str(uuid.uuid4()),
                worker_id=identifier,
                action=action,
                key=f"{identifier}:{key}",
            )
            db.add(operation)
            db.add(Audit(action="worker." + body.action, resource=identifier))
            db.flush()
            return row(operation)

    @app.get("/api/v1/workers/{identifier}/build")
    def worker_build(identifier: uuid.UUID):
        with sessions() as db:
            if db.get(Worker, str(identifier)) is None:
                raise HTTPException(404, "Worker not found")
        from app.adapters.image_builder import BuildStore

        return redact(
            BuildStore(settings.snapshot_root.parent / "builds").read(str(identifier))
        )

    @app.get("/api/v1/runtime/{identifier}/logs")
    def runtime_logs(identifier: str):
        return {
            "source": "runtime stdout",
            "content": redact(runtime_adapter().logs(identifier, 200)),
        }

    @app.get("/api/v1/runs")
    def runs(offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        try:
            return {
                "items": workflow_adapter().runs(offset, limit),
                "source": "hatchet",
                "warnings": [],
            }
        except Exception:
            return {
                "items": [],
                "source": "hatchet",
                "warnings": [
                    "Hatchet unavailable. No execution state can be inferred."
                ],
            }

    @app.get("/api/v1/runs/{identifier}")
    def run_detail(identifier: uuid.UUID):
        return redact(workflow_adapter().run(str(identifier)))

    @app.get("/api/v1/engine/tasks/{identifier}/logs")
    def engine_logs(identifier: uuid.UUID):
        return {
            "items": redact(workflow_adapter().logs(str(identifier), 200)),
            "source": "hatchet",
            "note": "Only logs explicitly sent to Hatchet appear here.",
        }

    @app.get("/api/v1/config/revisions")
    @app.get("/api/v1/projects/{project_id}/config/revisions", dependencies=[Depends(project_parameter)])
    def revisions(request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            return [
                {key: value for key, value in row(rev).items() if key != "document"}
                for rev in db.scalars(
                    select(Revision)
                    .where(Revision.scope == "factory", Revision.project_id == scope.project_id)
                    .order_by(desc(Revision.created_at))
                    .limit(100)
                )
            ]

    @app.get("/api/v1/config/revisions/{identifier}")
    @app.get("/api/v1/projects/{project_id}/config/revisions/{identifier}", dependencies=[Depends(project_parameter)])
    def revision(identifier: str, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            rev = require_owned(db, scope, Revision, identifier)
            if rev is None:
                raise HTTPException(404, "Revision not found")
            return {**row(rev), "profiles": config.validate(rev.document)}

    @app.post("/api/v1/config/validate")
    @app.post("/api/v1/projects/{project_id}/config/validate", dependencies=[Depends(project_parameter)])
    def validate(body: ConfigEdit, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
            if body.parent_id is not None:
                require_owned(db, scope, Revision, body.parent_id)
        return {
            "profiles": config.validate(body.document),
            "digest": config.hash(body.document),
        }

    @app.post("/api/v1/config/revisions", status_code=201)
    @app.post("/api/v1/projects/{project_id}/config/revisions", status_code=201, dependencies=[Depends(project_parameter)])
    def save_config(body: ConfigEdit, request: Request):
        config.validate(body.document)
        with sessions.begin() as db:
            scope = request_scope(request, db)
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(6423400)"))
            latest = db.scalar(
                select(Revision)
                .where(Revision.scope == "factory", Revision.project_id == scope.project_id)
                .order_by(desc(Revision.created_at))
                .with_for_update()
                .limit(1)
            )
            if body.parent_id:
                require_owned(db, scope, Revision, body.parent_id)
            if latest and body.parent_id != latest.id:
                raise HTTPException(409, "Configuration changed; reload before saving")
            revision = Revision(
                project_id=scope.project_id,
                id=str(uuid.uuid4()),
                parent_id=body.parent_id,
                summary=body.summary,
                document=body.document,
                digest=config.hash(body.document),
            )
            db.add(revision)
            db.add(Audit(action="config.publish", resource=revision.id))
            db.flush()
            return row(revision)

    @app.get("/api/v1/config/revisions/{identifier}/export")
    @app.get("/api/v1/projects/{project_id}/config/revisions/{identifier}/export", dependencies=[Depends(project_parameter)])
    def export(identifier: str, request: Request):
        with sessions() as db:
            revision = require_owned(db, request_scope(request, db), Revision, identifier)
            if revision is None:
                raise HTTPException(404, "Revision not found")
            return PlainTextResponse(
                yaml.safe_dump(revision.document, sort_keys=False),
                headers={
                    "Content-Disposition": "attachment; filename=worker-config.yaml"
                },
            )

    @app.get("/api/v1/worker-bootstrap")
    def worker_bootstrap(request: Request):
        identifier = request.headers.get("x-worker-id", "")
        authorization = request.headers.get("authorization", "")
        with sessions.begin() as db:
            worker = db.scalar(
                select(Worker).where(Worker.id == identifier).with_for_update()
            )
            if (
                worker is None
                or not authorization.startswith("Bearer ")
                or not secrets.compare_digest(
                    worker.credential_hash, digest(authorization[7:])
                )
            ):
                raise HTTPException(401, "Invalid worker credentials")
            if worker.protocol == 'shared-v1':
                from app.worker_pools import pool_record, shared_profile
                return shared_profile(worker, pool_record(db, worker.pool_id))
            # The container label identifies the installed image's configuration.
            # Desired DB state may already point at a replacement whose build failed.
            actual = next(
                (
                    item
                    for item in runtime_adapter().list()
                    if item.get("instance_id") == identifier
                ),
                None,
            )
            applied_id = actual.get("revision_id") if actual else worker.revision_id
            applied = db.get(Revision, applied_id or worker.revision_id)
            if applied is None:
                raise HTTPException(
                    409,
                    "Worker image configuration is unavailable; recreate the worker",
                )
            snapshot = config.snapshot(
                applied.document, worker.profile, worker.name, applied.id
            )
            if worker.config_id:
                current = db.get(WorkerConfiguration, worker.config_id)
                latest = db.scalar(
                    select(WorkerConfiguration)
                    .where(WorkerConfiguration.family_id == current.family_id)
                    .order_by(desc(WorkerConfiguration.created_at))
                    .limit(1)
                )
                candidate = config.compose(
                    db.get(Revision, latest.factory_revision_id).document,
                    latest.profile,
                    latest.document,
                )
                candidate_snapshot = config.snapshot(
                    candidate, worker.profile, worker.name, "candidate"
                )
                build_fields = config.BUILD_FIELDS | {
                    "worker_type",
                    "developer_agent",
                    "coding_factory",
                }
                same_build = all(
                    snapshot["defaults"].get(field)
                    == candidate_snapshot["defaults"].get(field)
                    for field in build_fields
                )
                if same_build:
                    if latest.id != current.id:
                        worker.config_id = latest.id
                        worker.revision_id = compile_revision(db, config, latest).id
                        db.add(
                            Audit(action="worker.profile-bootstrap", resource=worker.id)
                        )
                    desired = db.get(Revision, worker.revision_id)
                    snapshot = config.snapshot(
                        desired.document, worker.profile, worker.name, desired.id
                    )
                # Otherwise keep the installed image's configuration. A lifecycle
                # Start/Restart will prepare and replace its image before applying it.
            # Build settings belong to the runtime adapter, not the process.
            # Omitting them also preserves compatibility with protocol-3 images
            # built before profile-driven image assembly was introduced.
            for field in config.BUILD_FIELDS:
                snapshot["defaults"].pop(field, None)
            return snapshot

    @app.post("/api/v1/ingest", status_code=202)
    def ingest(body: list[Incoming], request: Request):
        if (
            len(body) > 50
            or len(json.dumps([item.model_dump() for item in body]).encode()) > 524288
        ):
            raise HTTPException(413, "Event batch too large")
        identifier = request.headers.get("x-worker-id", "")
        with sessions.begin() as db:
            worker = db.get(Worker, identifier)
            authorization = request.headers.get("authorization", "")
            if (
                worker is None
                or not authorization.startswith("Bearer ")
                or not secrets.compare_digest(
                    worker.credential_hash, digest(authorization[7:])
                )
            ):
                raise HTTPException(401, "Invalid worker credentials")
            if worker.protocol == 'shared-v1':
                raise HTTPException(409, 'Shared workers require assignment-scoped history ingestion')
            if db.bind.dialect.name == "postgresql":
                db.execute(
                    text("SELECT pg_advisory_xact_lock(:key)"),
                    {"key": int(digest(identifier)[:15], 16)},
                )
            for item in body:
                if db.get(Event, item.id):
                    continue
                if item.kind == "model.completed":
                    from factory.application.telemetry import ModelUsage

                    usage_fields = {
                        key: item.payload[key]
                        for key in ModelUsage.__dataclass_fields__
                        if key in item.payload
                    }
                    ModelUsage(**usage_fields)
                    import math

                    for key in ("estimated_usd", "reported_cost_usd"):
                        amount = item.payload.get(key)
                        if amount is not None and (
                            isinstance(amount, bool)
                            or not isinstance(amount, (int, float))
                            or not math.isfinite(amount)
                            or amount < 0
                        ):
                            raise ValueError("Invalid monetary usage")
                payload = redact(item.payload)
                conversation = payload.pop("conversation", None)
                if conversation:
                    db.add(
                        Conversation(
                            id=item.id,
                            ciphertext=cipher.encrypt(
                                json.dumps(conversation).encode()
                            ).decode(),
                            expires_at=time.time() + 604800,
                        )
                    )
                    payload["conversation_available"] = True
                db.add(
                    Event(
                        id=item.id,
                        kind=item.kind,
                        worker_id=identifier,
                        run_id=item.run_id,
                        attempt_id=item.attempt_id,
                        stage=item.stage,
                        occurred_at=item.occurred_at,
                        payload=payload,
                    )
                )
        return {"accepted": len(body)}

    def scoped_events(
        query,
        run_id=None,
        attempt_id=None,
        worker_id=None,
        engine_task_id=None,
        stage=None,
    ):
        if run_id:
            query = query.where(
                or_(
                    Event.run_id == run_id,
                    Event.payload["engine_workflow_run_id"].as_string() == run_id,
                )
            )
        if attempt_id:
            query = query.where(Event.attempt_id == attempt_id)
        if worker_id:
            query = query.where(Event.worker_id == worker_id)
        if engine_task_id:
            query = query.where(
                Event.payload["engine_task_run_id"].as_string() == engine_task_id
            )
        if stage:
            query = query.where(Event.stage == stage)
        return query

    @app.get("/api/v1/events")
    @app.get("/api/v1/projects/{project_id}/events", dependencies=[Depends(project_parameter)])
    def events(
        request: Request,
        run_id: str | None = None,
        attempt_id: str | None = None,
        kind: str | None = None,
        worker_id: str | None = None,
        engine_task_id: str | None = None,
        stage: str | None = None,
        offset: int = Query(0, ge=0),
        limit: int = Query(100, ge=1, le=500),
    ):
        query = scoped_events(
            select(Event), run_id, attempt_id, worker_id, engine_task_id, stage
        )
        if kind == "attempt":
            query = query.where(
                Event.kind.in_(["attempt.started", "attempt.completed"])
            )
        elif kind:
            query = query.where(Event.kind == kind)
        with sessions() as db:
            scope = request_scope(request, db)
            query = query.where(Event.project_id == scope.project_id)
            return [
                row(item)
                for item in db.scalars(
                    query.order_by(desc(Event.occurred_at)).offset(offset).limit(limit)
                )
            ]

    @app.get("/api/v1/model-calls/{identifier}/conversation")
    @app.get("/api/v1/projects/{project_id}/model-calls/{identifier}/conversation", dependencies=[Depends(project_parameter)])
    def conversation(identifier: str, request: Request):
        with sessions.begin() as db:
            content = require_owned(db, request_scope(request, db), Conversation, identifier)
            if content is None or content.expires_at < time.time():
                raise HTTPException(
                    404, "Conversation not captured or retention expired"
                )
            db.add(Audit(action="conversation.read", resource=identifier))
            return json.loads(cipher.decrypt(content.ciphertext.encode()))

    @app.get("/api/v1/usage")
    @app.get("/api/v1/projects/{project_id}/usage", dependencies=[Depends(project_parameter)])
    def usage(
        request: Request,
        since: float = Query(default=0, ge=0),
        run_id: str | None = None,
        attempt_id: str | None = None,
        worker_id: str | None = None,
        engine_task_id: str | None = None,
        stage: str | None = None,
    ):
        with sessions() as db:
            scope = request_scope(request, db)
            calls = db.scalars(
                scoped_events(
                    select(Event), run_id, attempt_id, worker_id, engine_task_id, stage
                )
                .where(Event.project_id == scope.project_id, Event.kind == "model.completed", Event.occurred_at >= since)
                .order_by(desc(Event.occurred_at))
                .limit(10000)
            ).all()
            leaves = [
                call for call in calls if call.payload.get("accounting_leaf", True)
            ]

            def total(key):
                values = [call.payload.get(key) for call in leaves]
                known = [
                    value
                    for value in values
                    if isinstance(value, (float, int)) and not isinstance(value, bool)
                ]
                return sum(known) if known else None

            return {
                "calls": len(leaves),
                "input_tokens": total("input_tokens"),
                "output_tokens": total("output_tokens"),
                "estimated_usd": total("estimated_usd"),
                "reported_usd": total("reported_cost_usd"),
                "unpriced_calls": sum(
                    call.payload.get("estimated_usd") is None for call in leaves
                ),
                "missing_usage_calls": sum(
                    call.payload.get("input_tokens") is None
                    or call.payload.get("output_tokens") is None
                    for call in leaves
                ),
                "limit": 10000,
                "truncated": len(calls) == 10000,
                "note": "Known subtotal. Estimated and reported USD are separate; provider invoices may differ.",
            }

    if settings.frontend.exists():
        assets = settings.frontend / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            return FileResponse(settings.frontend / "index.html")

    return app
