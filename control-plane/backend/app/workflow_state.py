"""Trusted durable state boundary. All transitions use one short database fence.

No provider calls run in these transactions. A lease expiry is not terminal proof.
The coordinator/API/catalog use this service; workers cannot supply trusted proofs.
"""

from dataclasses import asdict, dataclass
import hashlib
import json
import re
import time
from fastapi import HTTPException
from sqlalchemy import select, update, func, delete
from app.database import (
    WorkflowAdmission,
    WorkflowRun,
    StageAttempt,
    ProjectBusinessState,
    ExternalResourceClaim,
    RepositoryExecutionMode,
    RepositoryConnection,
    Worker,
    WorkerPool,
    ProjectPool,
    Assignment,
    ActionContextSnapshot,
    Revision,
    Operation,
)
from app.projects import require_owned, ProjectScope
from app.project_context import encrypt, decrypt
from app.workflow_definitions import pin_definition, read_definition_pin
from app.worker_pools import authorize_execution, LEASE_SECONDS
from factory.workflows.definitions import WorkflowDefinition, BuiltinStage
from factory.workflows.bindings import resolve_bindings, condition_matches, ParentOutput
from factory.workflows.references import json_bytes
from factory.workflows.resources import (
    canonical_repository,
    canonical_resource as canonical_resource,
)
from factory.workflows.contracts import (
    ActionInvocation,
    RunMetadata,
    ActionRoute,
    ActionContext,
    RunAdvance,
    StageReceipt,
    ActionResult,
)

ACTIVE = ("ready", "running")
TERMINAL = ("completed", "failed", "skipped", "blocked", "cancelled")


def hashed(value):
    json_bytes(value)
    encoded = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def bounded_origins(values):
    """Deduplicate ancestry before persistence; never let a DAG amplify metadata.

    The canonical identity retains path and producing stage, not just repository,
    so provenance remains useful to later consumers. Charge each unique record
    while collecting it, rather than constructing an unbounded ancestry list.
    """
    unique = {}
    remaining = 262144 - 2
    for value in values:
        json_bytes(value)
        encoded = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if encoded in unique:
            continue
        remaining -= len(encoded.encode()) + (1 if unique else 0)
        if remaining < 0:
            raise ValueError("Action provenance exceeds 256 KiB")
        unique[encoded] = value
    return list(unique.values())


def transition_lock(db):
    """One durable mutex: row UPDATE locks on PostgreSQL and SQLite alike.

    Always acquire before assignment/worker/resource locks. A no-op write also
    prevents SQLite's ignored FOR UPDATE from allowing lost admissions.
    """
    dialect = db.bind.dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise RuntimeError("Workflow state requires PostgreSQL or SQLite")
    db.execute(
        insert(WorkflowAdmission)
        .values(id="global", tick=0, last_project="")
        .on_conflict_do_nothing()
    )
    db.execute(
        update(WorkflowAdmission)
        .where(WorkflowAdmission.id == "global")
        .values(tick=WorkflowAdmission.tick + 1)
    )
    return db.scalar(
        select(WorkflowAdmission)
        .where(WorkflowAdmission.id == "global")
        .execution_options(populate_existing=True)
    )


def repository_key(document):
    return canonical_repository(
        document.get("host", "github.com"), document["owner"], document["name"]
    )


def legacy_guard(db, config, document, profile):
    value = config.resolve(document, profile).repository
    owner, name = value.split("/", 1)
    key = canonical_repository("github.com", owner, name)
    mode = db.get(RepositoryExecutionMode, key)
    if mode and mode.mode == "dynamic":
        raise HTTPException(
            409, "Repository uses dynamic claims; legacy execution is disabled"
        )


@dataclass(frozen=True)
class AdmissionLimits:
    runs_per_project: int = 2
    actions_per_project: int = 4
    actions_per_deployment: int = 8

    def __post_init__(self):
        for value, maximum in (
            (self.runs_per_project, 32),
            (self.actions_per_project, 64),
            (self.actions_per_deployment, 256),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError("Admission limits must be bounded positive integers")


class WorkflowState:
    def __init__(
        self, sessions, settings, config, *, clock=time.time, limits=None, purposes=None
    ):
        self.sessions, self.settings, self.config = sessions, settings, config
        self.clock, self.limits = clock, limits or AdmissionLimits()
        # Catalog integration supplies exact minimum purposes. Fail closed until
        # it is installed, rather than delivering every declared grant.
        self.purposes = purposes or self._purposes

    @staticmethod
    def _purposes(stage, settings):
        from factory.workflows.actions import stage_purposes

        return stage_purposes(stage, settings)

    def _snapshot(self, db, run):
        return read_definition_pin(
            db, ProjectScope(run.project_id), run.snapshot_id, self.settings
        )

    def start_run(self, scope, revision_id, run_input, *, key, bindings=None):
        if not isinstance(run_input, dict) or len(json_bytes(run_input)) > 65536:
            raise HTTPException(
                422, "Run input must be a JSON object of at most 64 KiB"
            )
        if not isinstance(key, str) or not 1 <= len(key) <= 120:
            raise HTTPException(422, "A bounded run request key is required")
        digest = hashed(
            {
                "revision": revision_id,
                "input": run_input,
                **({"bindings": bindings} if bindings is not None else {}),
            }
        )
        with self.sessions.begin() as db:
            cursor = transition_lock(db)
            existing = db.scalar(
                select(WorkflowRun).where(
                    WorkflowRun.project_id == scope.project_id,
                    WorkflowRun.request_key == key,
                )
            )
            if existing:
                if existing.request_digest != digest:
                    raise HTTPException(
                        409, "Run request key has another definition or input"
                    )
                return existing
            if bindings is None:
                pin = pin_definition(db, scope, revision_id, self.config, self.settings)
            else:
                from app.builtin_workflows import pin_project_bundle

                pin = pin_project_bundle(
                    db, scope, revision_id, bindings, self.config, self.settings
                )
            return self._create_run(db, scope, pin, run_input, key, digest, cursor)

    def start_pinned_run(self, scope, snapshot_id, run_input, *, key, db=None):
        if (
            not isinstance(run_input, dict)
            or len(json_bytes(run_input)) > 65536
            or not isinstance(key, str)
            or not 1 <= len(key) <= 120
        ):
            raise HTTPException(422, "Invalid pinned run request")
        digest = hashed({"snapshot": snapshot_id, "input": run_input})
        if db is None:
            with self.sessions.begin() as transaction:
                return self.start_pinned_run(
                    scope, snapshot_id, run_input, key=key, db=transaction
                )
        cursor = transition_lock(db)
        from app.database import WorkflowLaunchSnapshot

        pin = require_owned(db, scope, WorkflowLaunchSnapshot, snapshot_id)
        existing = db.scalar(
            select(WorkflowRun).where(
                WorkflowRun.project_id == scope.project_id,
                WorkflowRun.request_key == key,
            )
        )
        if existing:
            if existing.request_digest != digest:
                raise HTTPException(409, "Run key has another pinned context or input")
            return existing
        return self._create_run(db, scope, pin, run_input, key, digest, cursor)

    def _create_run(self, db, scope, pin, run_input, key, digest, cursor):
        try:
            RunMetadata.model_validate(
                {
                    key: run_input[key]
                    for key in ("factory_run_id", "trace_context")
                    if key in run_input
                }
            )
        except ValueError:
            raise HTTPException(422, "Invalid run correlation metadata") from None
        snapshot = read_definition_pin(db, scope, pin.id, self.settings)
        graph = WorkflowDefinition.model_validate(snapshot["definition"])
        for stage in graph.stages:
            if isinstance(stage, BuiltinStage) and stage.action == "core.echo@1":
                continue
            key_repo = repository_key(
                snapshot["repositories"][stage.repository or graph.default_repository][
                    "document"
                ]
            )
            mode = db.get(RepositoryExecutionMode, key_repo)
            if mode is None or mode.mode != "dynamic":
                raise HTTPException(
                    409,
                    "Enable dynamic repository mode after draining legacy workers",
                )
        run = WorkflowRun(
            project_id=scope.project_id,
            snapshot_id=pin.id,
            sequence=cursor.tick,
            request_key=key,
            request_digest=digest,
            input_ciphertext=encrypt(run_input, self.settings),
            created_at=self.clock(),
        )
        db.add(run)
        db.flush()
        db.add_all(
            StageAttempt(
                project_id=scope.project_id,
                run_id=run.id,
                stage_id=stage.id,
                generation=1,
                available_at=self.clock(),
                created_at=self.clock(),
            )
            for stage in graph.stages
        )
        db.flush()
        return run

    def _latest(self, db, run):
        attempts = list(
            db.scalars(
                select(StageAttempt)
                .where(
                    StageAttempt.project_id == run.project_id,
                    StageAttempt.run_id == run.id,
                )
                .order_by(StageAttempt.generation)
            )
        )
        return {item.stage_id: item for item in attempts}

    def _context(
        self,
        run,
        stage,
        attempt,
        metadata,
        revision,
        assignment_id="pending",
        definition_id="",
    ):
        raw_input = decrypt(run.input_ciphertext, self.settings)
        try:
            correlation = RunMetadata.model_validate(
                {
                    key: raw_input[key]
                    for key in ("factory_run_id", "trace_context")
                    if key in raw_input
                }
            )
        except ValueError:
            # Older echo runs accepted arbitrary business keys with these names.
            # Do not let their metadata poison other projects' admission rounds.
            correlation = RunMetadata()
        return ActionContext(
            project_id=run.project_id,
            repository_id=metadata["repository_id"],
            repository_revision_id=metadata["repository_revision_id"],
            config_revision_id=metadata["config_revision_id"],
            workflow_revision_id=revision,
            definition_id=definition_id,
            executor=metadata["executor"],
            run_id=run.id,
            stage_id=stage.id,
            execution_id=attempt.id,
            attempt=attempt.generation,
            assignment_id=assignment_id,
            namespace=f"{run.project_id}/{metadata['repository_id']}",
            settings_json=metadata["settings_json"],
            factory_run_id=correlation.factory_run_id or run.id,
            trace_context=correlation.trace_context,
            followup_bindings_json=metadata.get("followup_bindings_json", "{}"),
        )

    def _ready(self, db, run):
        snapshot = self._snapshot(db, run)
        graph = WorkflowDefinition.model_validate(snapshot["definition"])
        latest = self._latest(db, run)
        run_input = decrypt(run.input_ciphertext, self.settings)
        ready = []
        # Iterate to propagate skipped/blocked branches regardless of stage order.
        for _ in range(len(graph.stages) + 1):
            changed = False
            for stage in sorted(graph.stages, key=lambda s: s.id):
                item = latest[stage.id]
                if item.state != "pending" or item.available_at > self.clock():
                    continue
                parents = [latest[name] for name in stage.after]
                if not all(parent.state in TERMINAL for parent in parents):
                    continue
                if any(parent.state != "completed" for parent in parents):
                    item.state = (
                        "blocked"
                        if any(
                            p.state in {"failed", "blocked", "cancelled"}
                            for p in parents
                        )
                        else "skipped"
                    )
                    changed = True
                    continue
                results = {}
                for parent in parents:
                    parent_metadata = snapshot["stages"][parent.stage_id]
                    value = decrypt(parent.result_ciphertext, self.settings)
                    results[parent.stage_id] = ParentOutput(
                        run.project_id,
                        parent_metadata["repository_id"],
                        value["output"],
                    )
                metadata = snapshot["stages"][stage.id]
                context = self._context(
                    run,
                    stage,
                    item,
                    metadata,
                    snapshot["workflow_revision_id"],
                    definition_id=snapshot["definition_id"],
                )
                try:
                    if not condition_matches(stage.when, run_input, results, context):
                        item.state = "skipped"
                        changed = True
                        continue
                    bound = resolve_bindings(stage, run_input, results, context)
                    # Preserve inherited origin metadata through generic stages.
                    used_parents = {origin.stage_id for origin in bound.origins}

                    def origin_values():
                        for origin in bound.origins:
                            yield {**asdict(origin), "path": list(origin.path)}
                        for parent in parents:
                            if (
                                parent.stage_id in used_parents
                                and parent.origins_ciphertext
                            ):
                                yield from decrypt(
                                    parent.origins_ciphertext, self.settings
                                )

                    origins = bounded_origins(origin_values())
                    if (
                        isinstance(stage, BuiltinStage)
                        and stage.action != "core.echo@1"
                        and any(
                            o["repository_id"] != metadata["repository_id"]
                            for o in origins
                        )
                    ):
                        raise ValueError(
                            "Repository-affine input has another repository origin"
                        )
                    ready.append(
                        (
                            stage,
                            item,
                            bound.data,
                            origins,
                            metadata,
                            snapshot["workflow_revision_id"],
                        )
                    )
                except ValueError:
                    item.state = "failed"
                    item.result_digest = hashed(
                        {"validation": "invalid bindings or action input"}
                    )
                    item.result_ciphertext = encrypt(
                        {"validation": "invalid bindings or action input"},
                        self.settings,
                    )
                    changed = True
            if not changed:
                break
        db.flush()
        return ready

    def _capacity(self, db, stage, project_id):
        if db.get(ProjectPool, (project_id, stage.pool_id)) is None:
            return False
        pool = db.get(WorkerPool, stage.pool_id)
        if pool is None:
            return False
        workers = list(
            db.scalars(
                select(Worker).where(
                    Worker.pool_id == pool.id,
                    Worker.protocol == "shared-v1",
                    Worker.registered_at.is_not(None),
                    Worker.desired_state.not_in(["stopped", "removed"]),
                )
            )
        )
        action = stage.action if isinstance(stage, BuiltinStage) else "docker.run@1"
        capacity = sum(
            pool.profile["normal_slots"]
            for worker in workers
            if worker.capabilities.get("role") == stage.role
            and action in worker.capabilities.get("actions", [])
            and stage.executor + "@1" in worker.capabilities.get("executors", [])
        )
        used = db.scalar(
            select(func.count())
            .select_from(Assignment)
            .where(Assignment.pool_id == pool.id, Assignment.status.in_(ACTIVE))
        )
        return used < capacity

    def _admit(self, db, stage, item, inputs, origins, metadata, revision):
        scope = ProjectScope(item.project_id)
        row = authorize_execution(
            db,
            scope,
            self.settings,
            repository_id=metadata["repository_id"],
            pool_id=stage.pool_id,
            run_id=item.run_id,
            stage_id=stage.id,
            execution_id=item.id,
            role=stage.role,
            executor=metadata["executor"],
            action=metadata["action"],
            input=inputs,
        )
        run = db.get(WorkflowRun, item.run_id)
        context = self._context(
            run,
            stage,
            item,
            metadata,
            revision,
            row.id,
            self._snapshot(db, run)["definition_id"],
        )
        if isinstance(stage, BuiltinStage) and stage.action != "core.echo@1":
            from factory.workflows.project_dispatch import ProjectBindings

            bindings = ProjectBindings.model_validate_json(
                context.followup_bindings_json
            )
            if (bindings.project_id, bindings.repository_id) != (
                scope.project_id,
                context.repository_id,
            ):
                raise ValueError("Pinned follow-up bindings cross repository scope")
        purposes = set(self.purposes(stage, context.settings))
        grants = {
            purpose: metadata["secret_versions"].get(purpose) for purpose in purposes
        }
        from app.project_context import require_secret

        for purpose, version in grants.items():
            if not version:
                raise ValueError("Required credential grant is unavailable")
            require_secret(db, scope, version, purpose, metadata["repository_id"])
        pin = ActionContextSnapshot(
            project_id=item.project_id,
            execution_id=item.id,
            ciphertext=encrypt(
                {
                    "context": {
                        **context.model_dump(mode="json"),
                        "settings_json": context.settings_json,
                        "followup_bindings_json": context.followup_bindings_json,
                        "secret_versions": list(grants.values()),
                    },
                    "grants": grants,
                },
                self.settings,
            ),
        )
        db.add(pin)
        db.flush()
        row.context_id = pin.id
        item.assignment_id = row.id
        item.state = "ready"
        item.spec_digest = hashed(stage.model_dump(mode="json"))
        item.input_digest = hashed(inputs)
        item.origins_ciphertext = encrypt(origins, self.settings)
        db.flush()

    def _schedule(self, db, cursor):
        runs = list(
            db.scalars(
                select(WorkflowRun)
                .where(WorkflowRun.state.in_(["queued", "running", "cancelling"]))
                .order_by(WorkflowRun.sequence, WorkflowRun.id)
            )
        )
        active = {}
        for run in runs:
            if run.state in {"running", "cancelling"}:
                active[run.project_id] = active.get(run.project_id, 0) + 1
        for run in runs:
            if (
                run.state == "queued"
                and active.get(run.project_id, 0) < self.limits.runs_per_project
            ):
                run.state = "running"
                active[run.project_id] = active.get(run.project_id, 0) + 1
        db.flush()
        projects = sorted({run.project_id for run in runs if run.state == "running"})
        projects = [p for p in projects if p > cursor.last_project] + [
            p for p in projects if p <= cursor.last_project
        ]
        while projects:
            progress = False
            for project in projects:
                global_active = db.scalar(
                    select(func.count())
                    .select_from(Assignment)
                    .where(Assignment.status.in_(ACTIVE))
                )
                if global_active >= self.limits.actions_per_deployment:
                    return
                project_active = db.scalar(
                    select(func.count())
                    .select_from(Assignment)
                    .where(
                        Assignment.project_id == project, Assignment.status.in_(ACTIVE)
                    )
                )
                if project_active >= self.limits.actions_per_project:
                    continue
                candidate = None
                for run in runs:
                    if run.project_id != project or run.state != "running":
                        continue
                    for ready in self._ready(db, run):
                        if self._capacity(db, ready[0], project):
                            candidate = ready
                            break
                    if candidate:
                        break
                if candidate:
                    try:
                        with db.begin_nested():
                            self._admit(db, *candidate)
                    except (ValueError, HTTPException):
                        # Policy/credential changes must fail this stage without
                        # rolling back another project's admission or leaving a
                        # partially authorized assignment behind.
                        item = candidate[1]
                        item.state = "failed"
                        error = {
                            "error": "Stage admission rejected by execution policy"
                        }
                        item.result_digest = hashed(error)
                        item.result_ciphertext = encrypt(error, self.settings)
                        db.flush()
                    cursor.last_project = project
                    progress = True
            if not progress:
                break
        for run in runs:
            if run.state != "running":
                continue
            latest = self._latest(db, run)
            if all(item.state in TERMINAL for item in latest.values()):
                run.state = (
                    "failed"
                    if any(
                        item.state in {"failed", "blocked", "cancelled"}
                        for item in latest.values()
                    )
                    else "completed"
                )
        db.flush()

    def advance_run(self, scope, run_id):
        with self.sessions.begin() as db:
            cursor = transition_lock(db)
            run = require_owned(db, scope, WorkflowRun, run_id)
            self._schedule(db, cursor)
            ready = [
                ActionInvocation(
                    project_id=scope.project_id, run_id=run.id, execution_id=item.id
                )
                for item in sorted(
                    self._latest(db, run).values(), key=lambda item: item.stage_id
                )
                if item.state in ACTIVE
            ]
            routes = {}
            for invocation in ready:
                item = db.get(StageAttempt, invocation.execution_id)
                assignment = db.get(Assignment, item.assignment_id)
                routes[item.id] = ActionRoute(
                    stage_id=item.stage_id,
                    attempt=item.generation,
                    pool_id=assignment.pool_id,
                    role=assignment.role,
                )
            if run.state == "cancelling":
                ready, routes = [], {}
            return RunAdvance(state=run.state, ready=ready, routes=routes)

    def cancel_run(self, scope, run_id):
        """Trusted cancellation request. Claimed actions retain fences until settled.

        Runtime/API cancellation signaling comes through the authenticated adapter;
        cancelling a Hatchet parent alone is never proof external jobs stopped.
        """
        with self.sessions.begin() as db:
            transition_lock(db)
            run = require_owned(db, scope, WorkflowRun, run_id)
            if run.state in {"completed", "failed", "cancelled"}:
                return RunAdvance(state=run.state)
            run.state = "cancelling"
            for item in self._latest(db, run).values():
                if item.state in TERMINAL:
                    continue
                row = (
                    db.get(Assignment, item.assignment_id)
                    if item.assignment_id
                    else None
                )
                if (
                    row is not None
                    and row.status == "running"
                    and not (
                        row.executor == "builtin@1"
                        and row.action != "core.echo@1"
                        and row.started_at is None
                    )
                ):
                    continue
                item.state = "cancelled"
                if row is not None:
                    row.status = "cancelled"
                    db.execute(
                        delete(ExternalResourceClaim).where(
                            ExternalResourceClaim.execution_id == item.id
                        )
                    )
            db.flush()
            if all(item.state in TERMINAL for item in self._latest(db, run).values()):
                run.state = "cancelled"
            return RunAdvance(state=run.state)

    def assert_execution(self, scope, execution_id, *, spec_digest, input_digest):
        with self.sessions() as db:
            item = require_owned(db, scope, StageAttempt, execution_id)
            if (item.spec_digest, item.input_digest) != (spec_digest, input_digest):
                raise HTTPException(409, "Execution identity has another spec or input")
            return item

    def finish_execution(
        self,
        scope,
        execution_id,
        *,
        output=None,
        result=None,
        failed=False,
        confirmed_terminal=False,
        cancelled=False,
        db=None,
    ):
        if not confirmed_terminal:
            raise HTTPException(
                409, "Reconcile external effects before finishing an execution"
            )
        if db is None:
            with self.sessions.begin() as transaction:
                transition_lock(transaction)
                return self.finish_execution(
                    scope,
                    execution_id,
                    output=output,
                    result=result,
                    failed=failed,
                    confirmed_terminal=True,
                    cancelled=cancelled,
                    db=transaction,
                )
        item = require_owned(db, scope, StageAttempt, execution_id)
        if output is None:
            output = {}
        # Validate both success/failure payloads with finite JSON and the same bounds.
        json_bytes(output if result is None else result.output)
        result = (result or ActionResult(output=output)).model_dump(mode="json")
        digest = hashed(
            {
                "failed": failed,
                "result": result,
                **({"cancelled": True} if cancelled else {}),
            }
        )
        if item.state in TERMINAL:
            if item.result_digest != digest:
                raise HTTPException(
                    409, "Execution result differs from terminal receipt"
                )
            return StageReceipt(execution_id=item.id, state=item.state)
        if item.state not in ACTIVE:
            raise HTTPException(409, "Execution is not admitted")
        item.state = "cancelled" if cancelled else "failed" if failed else "completed"
        item.result_digest = digest
        item.result_ciphertext = encrypt(result, self.settings)
        row = db.get(Assignment, item.assignment_id)
        row.status = item.state
        row.result_ciphertext = encrypt(result, self.settings)
        row.result_digest = hashlib.sha256(
            json.dumps(
                result, sort_keys=True, allow_nan=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
        db.execute(
            delete(ExternalResourceClaim).where(
                ExternalResourceClaim.execution_id == item.id
            )
        )
        run = db.get(WorkflowRun, item.run_id)
        snapshot = self._snapshot(db, run)
        stage = next(
            s
            for s in WorkflowDefinition.model_validate(snapshot["definition"]).stages
            if s.id == item.stage_id
        )
        if (
            failed
            and not cancelled
            and run.state != "cancelling"
            and item.generation <= stage.retries
        ):
            # Allocate once, only after a known terminal previous attempt.
            db.add(
                StageAttempt(
                    project_id=item.project_id,
                    run_id=item.run_id,
                    stage_id=item.stage_id,
                    generation=item.generation + 1,
                    available_at=self.clock() + stage.retry_delay_seconds,
                    created_at=self.clock(),
                )
            )
        db.flush()
        latest = self._latest(db, run)
        if all(attempt.state in TERMINAL for attempt in latest.values()):
            run.state = (
                "cancelled"
                if run.state == "cancelling"
                else "failed"
                if any(
                    attempt.state in {"failed", "blocked", "cancelled"}
                    for attempt in latest.values()
                )
                else "completed"
            )
        return StageReceipt(execution_id=item.id, state=item.state)

    def _state(self, scope, repository_id, key, operation, value=None, *, db=None):
        if not isinstance(key, str) or not 1 <= len(key) <= 512:
            raise HTTPException(422, "State key is required and bounded")
        if operation == "put" and len(json_bytes(value)) > 1048576:
            raise HTTPException(422, "State value exceeds 1 MiB")
        if db is None:
            with self.sessions.begin() as transaction:
                transition_lock(transaction)
                return self._state(
                    scope, repository_id, key, operation, value, db=transaction
                )
        require_owned(db, scope, RepositoryConnection, repository_id)
        row = db.get(ProjectBusinessState, (repository_id, key))
        if row and row.project_id != scope.project_id:
            raise HTTPException(404, "Resource not found")
        if operation == "get":
            return decrypt(row.ciphertext, self.settings) if row else None
        if operation == "delete":
            if row:
                db.delete(row)
        elif row:
            row.ciphertext = encrypt(value, self.settings)
        else:
            db.add(
                ProjectBusinessState(
                    project_id=scope.project_id,
                    repository_id=repository_id,
                    key=key,
                    ciphertext=encrypt(value, self.settings),
                )
            )

    def get_state(self, scope, repository_id, key):
        return self._state(scope, repository_id, key, "get")

    def put_state(self, scope, repository_id, key, value):
        return self._state(scope, repository_id, key, "put", value)

    def delete_state(self, scope, repository_id, key):
        return self._state(scope, repository_id, key, "delete")

    def claim_resource(self, scope, execution_id, resource, *, db=None):
        if db is None:
            with self.sessions.begin() as transaction:
                transition_lock(transaction)
                return self.claim_resource(
                    scope, execution_id, resource, db=transaction
                )
        item = require_owned(db, scope, StageAttempt, execution_id)
        if item.state not in ACTIVE:
            raise HTTPException(409, "Execution is not admitted")
        run = db.get(WorkflowRun, item.run_id)
        snapshot = self._snapshot(db, run)
        metadata = snapshot["stages"][item.stage_id]
        repository = next(
            value["document"]
            for value in snapshot["repositories"].values()
            if value["id"] == metadata["repository_id"]
        )
        root = repository_key(repository)
        if resource != root and not re.fullmatch(
            re.escape(root) + r"/issue/[1-9][0-9]*", resource
        ):
            raise HTTPException(404, "Resource is outside the pinned repository")
        for existing in db.scalars(select(ExternalResourceClaim)):
            if (
                existing.resource == resource
                or existing.resource == root
                or (resource == root and existing.resource.startswith(root + "/"))
            ):
                if existing.execution_id != item.id:
                    raise HTTPException(
                        409,
                        "Another execution owns the external resource; reconcile before retry",
                    )
                if existing.resource == resource:
                    return existing
        row = ExternalResourceClaim(
            resource=resource,
            project_id=scope.project_id,
            execution_id=item.id,
            expires_at=self.clock() + LEASE_SECONDS,
        )
        db.add(row)
        db.flush()
        return row

    def enable_dynamic_repository(self, scope, repository_id):
        with self.sessions.begin() as db:
            transition_lock(db)
            repository = require_owned(db, scope, RepositoryConnection, repository_id)
            root = canonical_repository(
                repository.host, repository.owner, repository.name
            )
            for worker in db.scalars(select(Worker).where(Worker.protocol == "legacy")):
                revision = db.get(Revision, worker.revision_id)
                if revision is None:
                    raise HTTPException(
                        409, "Legacy worker configuration is unavailable"
                    )
                settings = self.config.resolve(revision.document, worker.profile)
                owner, name = settings.repository.split("/", 1)
                if canonical_repository("github.com", owner, name) != root:
                    continue
                pending = db.scalar(
                    select(Operation.id).where(
                        Operation.worker_id == worker.id,
                        Operation.status.in_(["pending", "running"]),
                    )
                )
                if worker.desired_state not in {"stopped", "removed"} or pending:
                    raise HTTPException(
                        409,
                        "Drain and stop legacy workers before enabling dynamic mode",
                    )
            mode = db.get(RepositoryExecutionMode, root)
            if not mode:
                db.add(RepositoryExecutionMode(repository=root, mode="dynamic"))
