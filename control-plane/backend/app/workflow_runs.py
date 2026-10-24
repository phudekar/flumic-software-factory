"""Project-owned invocation receipts and a recoverable ID-only engine outbox."""

import time
from fastapi import Depends, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from sqlalchemy import select, and_
from app.database import (
    WorkflowRun,
    StageAttempt,
    DynamicDispatch,
    RuntimeJob,
    ProjectFollowup,
)
from app.projects import ProjectScope, require_owned, request_scope, project_parameter
from app.workflow_state import WorkflowState, transition_lock
from app.workflow_definitions import read_definition_pin

TERMINAL = {"completed", "failed", "cancelled"}


class StartRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(min_length=1, max_length=120)
    input: dict = Field(default_factory=dict)
    bindings: dict[str, dict[str, str]] | None = None


class SeedBuiltins(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(min_length=1, max_length=100)
    simple_pool: str = Field(min_length=1, max_length=120)
    developer_pool: str = Field(min_length=1, max_length=120)
    secrets: dict[Literal["openai", "anthropic"], str] = Field(default_factory=dict)


class ResolveExecution(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["failed", "cancelled"]
    confirmation: Literal["external-effects-reconciled"]


class Empty(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DynamicRuntime:
    def __init__(self, sessions, settings, config, *, clock=time.time):
        self.sessions, self.settings, self.config, self.clock = (
            sessions,
            settings,
            config,
            clock,
        )
        self.store = WorkflowState(sessions, settings, config, clock=clock)
        from app.job_broker import JobBroker

        self.broker = JobBroker(sessions, settings, clock=clock)

    def enqueue(self, db, scope, kind, subject, payload):
        existing = db.scalar(
            select(DynamicDispatch).where(
                DynamicDispatch.project_id == scope.project_id,
                DynamicDispatch.kind == kind,
                DynamicDispatch.subject_id == subject,
            )
        )
        if existing is None:
            existing = DynamicDispatch(
                project_id=scope.project_id,
                kind=kind,
                subject_id=subject,
                payload=payload,
            )
            db.add(existing)
            db.flush()
        return existing

    def launch(self, scope, body, key):
        run = self.store.start_run(
            scope, body.revision_id, body.input, key=key, bindings=body.bindings
        )
        # A crash between pinning and this transaction is repaired by the outbox
        # scan. Retrying the project-qualified request returns the original pin.
        with self.sessions.begin() as db:
            transition_lock(db)
            self.enqueue(
                db,
                scope,
                "run",
                run.id,
                {"project_id": scope.project_id, "run_id": run.id},
            )
        return run

    def reconcile_followups(self):
        from app.followups import reconcile

        return reconcile(self)

    def check_wait(self, identifier):
        from app.followups import check

        return check(self, identifier)

    def resume_wait(self, identifier):
        from app.followups import resume

        return resume(self, identifier)

    def settle_jobs(self, scope, run_id):
        from app.project_context import decrypt
        from factory.workflows.contracts import ActionResult

        with self.sessions.begin() as db:
            transition_lock(db)
            run = require_owned(db, scope, WorkflowRun, run_id)
            jobs = db.scalars(
                select(RuntimeJob)
                .join(StageAttempt, RuntimeJob.execution_id == StageAttempt.id)
                .where(
                    RuntimeJob.project_id == scope.project_id,
                    StageAttempt.run_id == run.id,
                    StageAttempt.state.in_(["ready", "running"]),
                    RuntimeJob.state.in_(
                        ["completed", "failed", "cancelled", "timed-out"]
                    ),
                )
            ).all()
            for job in jobs:
                capture = decrypt(job.result_ciphertext, self.settings)
                failed = job.state != "completed"
                try:
                    result = ActionResult(
                        output=capture.get("output", {}), usage=capture.get("usage", [])
                    )
                except ValueError:
                    result, failed = (
                        ActionResult(
                            output={"error": "Job result or usage is invalid"}
                        ),
                        True,
                    )
                from app.history import record_job_usage

                record_job_usage(db, self.settings, job, result)
                self.store.finish_execution(
                    scope,
                    job.execution_id,
                    result=result,
                    failed=failed,
                    cancelled=job.state == "cancelled" or run.state == "cancelling",
                    confirmed_terminal=True,
                    db=db,
                )

    def reconcile_background(self):
        from app.database import Assignment, WorkflowRun
        from factory.workflows.contracts import ActionResult

        with self.sessions() as db:
            finished = list(
                db.execute(
                    select(StageAttempt.project_id, StageAttempt.run_id)
                    .join(RuntimeJob, RuntimeJob.execution_id == StageAttempt.id)
                    .where(
                        StageAttempt.state.in_(["ready", "running"]),
                        RuntimeJob.state.in_(
                            ["completed", "failed", "cancelled", "timed-out"]
                        ),
                    )
                    .distinct()
                    .limit(32)
                )
            )
        for project, run in finished:
            self.settle_jobs(ProjectScope(project), run)
        with self.sessions.begin() as db:
            transition_lock(db)
            # Context start is fenced before credentials leave the server; Docker
            # launch requires a durable job row. Their absence proves no launch,
            # unlike expiry of an already-started action or accepted job.
            unstarted = list(
                db.scalars(
                    select(Assignment)
                    .join(StageAttempt, StageAttempt.assignment_id == Assignment.id)
                    .outerjoin(RuntimeJob, RuntimeJob.assignment_id == Assignment.id)
                    .where(
                        Assignment.status == "running",
                        Assignment.started_at.is_(None),
                        Assignment.expires_at <= self.clock(),
                        StageAttempt.state.in_(["ready", "running"]),
                        RuntimeJob.id.is_(None),
                    )
                    .order_by(Assignment.expires_at)
                    .limit(32)
                )
            )
            for row in unstarted:
                run = require_owned(
                    db, ProjectScope(row.project_id), WorkflowRun, row.run_id
                )
                self.store.finish_execution(
                    ProjectScope(row.project_id),
                    row.execution_id,
                    result=ActionResult(
                        output={"error": "Worker lease ended before action launch"}
                    ),
                    failed=True,
                    cancelled=run.state == "cancelling",
                    confirmed_terminal=True,
                    db=db,
                )
        return bool(finished or unstarted)

    def advance(self, invocation):
        self.settle_jobs(ProjectScope(invocation.project_id), invocation.run_id)
        return self.store.advance_run(
            ProjectScope(invocation.project_id), invocation.run_id
        )

    def cancel(self, scope, run_id):
        return self.store.cancel_run(scope, run_id)

    def reconcile_outbox(self, engine):
        with self.sessions.begin() as db:
            transition_lock(db)
            # Repair accepted run -> enqueue crash window, without re-pinning.
            for run in db.scalars(
                select(WorkflowRun)
                .outerjoin(
                    DynamicDispatch,
                    and_(
                        DynamicDispatch.project_id == WorkflowRun.project_id,
                        DynamicDispatch.kind == "run",
                        DynamicDispatch.subject_id == WorkflowRun.id,
                    ),
                )
                .where(WorkflowRun.state.not_in(TERMINAL), DynamicDispatch.id.is_(None))
                .order_by(WorkflowRun.created_at)
                .limit(100)
            ):
                self.enqueue(
                    db,
                    ProjectScope(run.project_id),
                    "run",
                    run.id,
                    {"project_id": run.project_id, "run_id": run.id},
                )
            row = db.scalar(
                select(DynamicDispatch)
                .where(
                    DynamicDispatch.state == "pending",
                    DynamicDispatch.available_at <= self.clock(),
                )
                .order_by(DynamicDispatch.available_at, DynamicDispatch.id)
                .limit(1)
            )
            if row is None:
                return False
            if (
                row.kind == "run"
                and require_owned(
                    db, ProjectScope(row.project_id), WorkflowRun, row.subject_id
                ).state
                in TERMINAL
            ):
                row.state = "cancelled"
                return True
            row.available_at = self.clock() + 60
            identifier, kind, payload = row.id, row.kind, dict(row.payload)
        try:
            result = engine.submit_dynamic(kind, payload, identifier)
            engine_id = result["run_id"]
            if not isinstance(engine_id, str) or not 1 <= len(engine_id) <= 120:
                raise ValueError("Missing engine run identity")
        except Exception:
            with self.sessions.begin() as db:
                row = db.get(DynamicDispatch, identifier)
                row.error = "Engine dispatch unavailable; durable retry pending"
                row.available_at = self.clock() + 5
            return True
        with self.sessions.begin() as db:
            row = db.get(DynamicDispatch, identifier)
            row.state, row.engine_id, row.error = "submitted", engine_id, None
        return True

    def public(self, db, scope, identifier, *, detail=False):
        run = require_owned(db, scope, WorkflowRun, identifier)
        pin = read_definition_pin(db, scope, run.snapshot_id, self.settings)
        result = {
            key: getattr(run, key)
            for key in ("id", "project_id", "snapshot_id", "state", "created_at")
        }
        result["revision_id"] = pin["workflow_revision_id"]
        dispatch = db.scalar(
            select(DynamicDispatch).where(
                DynamicDispatch.project_id == scope.project_id,
                DynamicDispatch.kind == "run",
                DynamicDispatch.subject_id == run.id,
            )
        )
        result["dispatch"] = (
            {
                "state": dispatch.state,
                "engine_id": dispatch.engine_id,
                "error": dispatch.error,
            }
            if dispatch
            else {"state": "pending", "engine_id": None, "error": None}
        )
        if detail:
            result["followups"] = []
            for item, waiting in db.execute(
                select(ProjectFollowup, DynamicDispatch)
                .outerjoin(
                    DynamicDispatch,
                    and_(
                        DynamicDispatch.project_id == ProjectFollowup.project_id,
                        DynamicDispatch.kind == "wait",
                        DynamicDispatch.subject_id == ProjectFollowup.id,
                    ),
                )
                .where(
                    ProjectFollowup.project_id == scope.project_id,
                    ProjectFollowup.source_run_id == run.id,
                )
                .order_by(ProjectFollowup.id)
            ):
                entry = {
                    key: getattr(item, key)
                    for key in ("id", "kind", "state", "result_run_id", "error")
                }
                entry["dispatch"] = (
                    {
                        key: getattr(waiting, key)
                        for key in ("state", "engine_id", "error")
                    }
                    if waiting
                    else None
                )
                result["followups"].append(entry)
            result["attempts"] = [
                {
                    key: getattr(item, key)
                    for key in (
                        "id",
                        "stage_id",
                        "generation",
                        "state",
                        "assignment_id",
                    )
                }
                for item in db.scalars(
                    select(StageAttempt)
                    .where(
                        StageAttempt.project_id == scope.project_id,
                        StageAttempt.run_id == run.id,
                    )
                    .order_by(StageAttempt.stage_id, StageAttempt.generation)
                )
            ]
            result["repositories"] = {
                alias: {key: value[key] for key in ("id", "revision_id")}
                for alias, value in pin["repositories"].items()
            }
            result["factory_revision_id"] = pin["factory_revision_id"]
        return result


def install(app, sessions, settings, config):
    runtime = DynamicRuntime(sessions, settings, config)
    app.state.dynamic_runtime = runtime
    from app.worker_runtime import install as install_worker_runtime

    install_worker_runtime(app, sessions, settings, runtime)

    @app.post(
        "/api/v1/projects/{project_id}/repositories/{identifier}/dynamic-mode",
        dependencies=[Depends(project_parameter)],
    )
    def mode(identifier: str, body: Empty, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
        runtime.store.enable_dynamic_repository(scope, identifier)
        return {"repository_id": identifier, "mode": "dynamic"}

    @app.post(
        "/api/v1/projects/{project_id}/workflow-definitions/seed-builtins",
        dependencies=[Depends(project_parameter)],
    )
    def seed(body: SeedBuiltins, request: Request):
        from app.builtin_workflows import seed_project_templates

        with sessions() as db:
            scope = request_scope(request, db)
        return seed_project_templates(
            sessions, scope, config, settings, **body.model_dump()
        )

    @app.post(
        "/api/v1/projects/{project_id}/workflow-runs/{identifier}/reconcile",
        status_code=202,
        dependencies=[Depends(project_parameter)],
    )
    def reconcile(identifier: str, body: Empty, request: Request):
        with sessions.begin() as db:
            transition_lock(db)
            scope = request_scope(request, db)
            run = require_owned(db, scope, WorkflowRun, identifier)
            row = runtime.enqueue(
                db,
                scope,
                "run",
                run.id,
                {"project_id": scope.project_id, "run_id": run.id},
            )
            if run.state not in TERMINAL:
                row.state, row.available_at, row.error = "pending", 0, None
            if run.state not in {"cancelling", "cancelled"}:
                for followup in db.scalars(
                    select(ProjectFollowup).where(
                        ProjectFollowup.project_id == scope.project_id,
                        ProjectFollowup.source_run_id == run.id,
                        ProjectFollowup.state == "waiting",
                    )
                ):
                    waiting = runtime.enqueue(
                        db, scope, "wait", followup.id, {"wait_id": followup.id}
                    )
                    waiting.state, waiting.available_at, waiting.error = (
                        "pending",
                        0,
                        None,
                    )
        with sessions() as db:
            return runtime.public(db, scope, identifier)

    @app.post(
        "/api/v1/projects/{project_id}/workflow-runs/{identifier}/executions/{execution_id}/resolve",
        dependencies=[Depends(project_parameter)],
    )
    def resolve(
        identifier: str, execution_id: str, body: ResolveExecution, request: Request
    ):
        from app.database import Assignment, Audit

        with sessions.begin() as db:
            transition_lock(db)
            scope = request_scope(request, db)
            run = require_owned(db, scope, WorkflowRun, identifier)
            attempt = require_owned(db, scope, StageAttempt, execution_id)
            if attempt.run_id != run.id:
                raise HTTPException(404, "Execution does not belong to run")
            assignment = require_owned(
                db, scope, Assignment, attempt.assignment_id or ""
            )
            if assignment.executor != "builtin@1":
                raise HTTPException(
                    409, "Use broker terminal reconciliation for external jobs"
                )
            prior = attempt.state
            receipt = runtime.store.finish_execution(
                scope,
                attempt.id,
                output={"error": "Operator reconciled external effects"},
                failed=body.state == "failed",
                cancelled=body.state == "cancelled" or run.state == "cancelling",
                confirmed_terminal=True,
                db=db,
            )
            if prior not in {"completed", "failed", "cancelled", "blocked", "skipped"}:
                db.add(
                    Audit(
                        action="resolve-dynamic-execution",
                        resource=f"{scope.project_id}/{run.id}/{attempt.id}",
                    )
                )
            return receipt.model_dump()

    @app.get(
        "/api/v1/projects/{project_id}/jobs/{identifier}",
        dependencies=[Depends(project_parameter)],
    )
    def job(identifier: str, request: Request):
        with sessions() as db:
            return runtime.broker._receipt(
                require_owned(db, request_scope(request, db), RuntimeJob, identifier)
            ).model_dump()

    @app.post(
        "/api/v1/projects/{project_id}/workflow-runs",
        status_code=202,
        dependencies=[Depends(project_parameter)],
    )
    def launch(body: StartRun, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
        run = runtime.launch(scope, body, request.headers.get("idempotency-key", ""))
        with sessions() as db:
            return runtime.public(db, scope, run.id)

    @app.get(
        "/api/v1/projects/{project_id}/workflow-runs",
        dependencies=[Depends(project_parameter)],
    )
    def runs(
        request: Request,
        limit: int = Query(50, ge=1, le=100),
        offset: int = Query(0, ge=0, le=10000),
    ):
        with sessions() as db:
            scope = request_scope(request, db)
            return [
                runtime.public(db, scope, identifier)
                for identifier in db.scalars(
                    select(WorkflowRun.id)
                    .where(WorkflowRun.project_id == scope.project_id)
                    .order_by(WorkflowRun.created_at.desc(), WorkflowRun.id)
                    .offset(offset)
                    .limit(limit)
                )
            ]

    @app.get(
        "/api/v1/projects/{project_id}/workflow-runs/{identifier}",
        dependencies=[Depends(project_parameter)],
    )
    def run(identifier: str, request: Request):
        with sessions() as db:
            return runtime.public(
                db, request_scope(request, db), identifier, detail=True
            )

    @app.post(
        "/api/v1/projects/{project_id}/workflow-runs/{identifier}/cancel",
        status_code=202,
        dependencies=[Depends(project_parameter)],
    )
    def cancel(identifier: str, body: Empty, request: Request):
        with sessions() as db:
            scope = request_scope(request, db)
        runtime.cancel(scope, identifier)
        with sessions() as db:
            return runtime.public(db, scope, identifier, detail=True)
