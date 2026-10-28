"""Durable assignment-scoped job intent, recovery and capture before cleanup.

submit is trusted composition: HTTP callers must authenticate their live assignment
lease before invoking it (#11). No supplied image, argv, mounts, inputs or credentials
are trusted: all are recovered from the owned immutable launch and assignment.
"""

import hashlib
from pathlib import Path
import time
from app.ports import managed_runtime
from fastapi import HTTPException
from sqlalchemy import select
from app.database import (
    RuntimeJob,
    Assignment,
    StageAttempt,
    WorkflowRun,
    SecretVersion,
    Worker,
    RuntimeResourceBinding,
)
from app.projects import require_owned, ProjectScope
from app.project_context import encrypt, decrypt, resolve_secret
from app.workflow_state import transition_lock, hashed
from app.workflow_definitions import read_definition_pin
from app.job_contracts import JobLaunch, JobReceipt, RuntimeJobRef
from factory.workflows.definitions import WorkflowDefinition, DockerStage
from factory.workflows.redaction import secret_scope
from app.job_contracts import sanitize_capture
from app.runtime_targets import TargetRegistry, TargetSnapshot
from app.runtime_claims import (
    claim_runtime,
    require_runtime_claim,
    release_runtime_claim,
    runtime_heartbeat,
)

TERMINAL = {"completed", "failed", "timed-out", "cancelled"}


class JobBroker:
    def __init__(self, sessions, settings, *, clock=time.time, root=None, targets=None):
        self.sessions, self.settings, self.clock = sessions, settings, clock
        self.root = Path(root or settings.snapshot_root / "jobs")
        self.targets = targets or TargetRegistry(settings)

    def _receipt(self, row):
        return JobReceipt(
            job_id=row.id,
            execution_id=row.execution_id,
            state=row.state,
            deadline=row.deadline,
            result=decrypt(row.result_ciphertext, self.settings)
            if row.result_ciphertext
            else None,
        )

    def submit(self, scope, assignment_id, spec_digest):
        with self.sessions.begin() as db:
            transition_lock(db)
            assignment = require_owned(db, scope, Assignment, assignment_id)
            existing = db.scalar(
                select(RuntimeJob).where(
                    RuntimeJob.project_id == scope.project_id,
                    RuntimeJob.assignment_id == assignment.id,
                )
            )
            if existing:
                if existing.spec_digest != spec_digest:
                    raise HTTPException(409, "Logical job already has another spec")
                return self._receipt(existing)
            item = require_owned(db, scope, StageAttempt, assignment.execution_id)
            run = require_owned(db, scope, WorkflowRun, item.run_id)
            if (
                item.assignment_id != assignment.id
                or item.stage_id != assignment.stage_id
                or item.run_id != assignment.run_id
                or item.state not in {"ready", "running"}
                or assignment.status != "running"
                or not assignment.worker_id
                or assignment.expires_at is None
                or assignment.expires_at <= self.clock()
                or run.state != "running"
                or item.spec_digest != spec_digest
                or assignment.executor != "docker@1"
                or assignment.action != "docker.run@1"
            ):
                raise HTTPException(
                    409, "Job requires an active authorized Docker assignment"
                )
            pin = read_definition_pin(db, scope, run.snapshot_id, self.settings)
            graph = WorkflowDefinition.model_validate(pin["definition"])
            stage = next((s for s in graph.stages if s.id == item.stage_id), None)
            if (
                not isinstance(stage, DockerStage)
                or hashed(stage.model_dump(mode="json")) != spec_digest
            ):
                raise HTTPException(409, "Job spec does not match pinned stage")
            worker = db.get(Worker, assignment.worker_id)
            if worker is None or worker.protocol != "shared-v1":
                raise HTTPException(409, "Job requires a shared worker runtime target")
            try:
                target = self.targets.resolve_worker(worker)
                self.targets.require_stage(target, stage, protocol=worker.protocol)
            except ValueError:
                raise HTTPException(
                    409, "Worker runtime target is not available for this stage"
                ) from None
            metadata = pin["stages"][stage.id]
            if metadata["repository_id"] != assignment.repository_id:
                raise HTTPException(409, "Job repository does not match pinned stage")
            inputs = decrypt(assignment.input_ciphertext, self.settings)
            if hashed(inputs) != item.input_digest:
                raise HTTPException(409, "Job inputs do not match admitted execution")
            # Docker gets only purposes explicitly declared on the graph/stage;
            # it never inherits the repository's ambient source-host credential.
            requested = {**graph.secrets, **stage.secrets}
            grants = {
                purpose: metadata["secret_versions"][purpose] for purpose in requested
            }
            resolved = {
                purpose: resolve_secret(
                    db, scope, version, purpose, self.settings, assignment.repository_id
                )
                for purpose, version in grants.items()
            }
            job_id = hashlib.sha256(
                f"{scope.project_id}:{run.id}:{stage.id}:{item.generation}".encode()
            ).hexdigest()
            from app.history import attribution
            from app.database import ActionContextSnapshot
            from factory.workflows.redaction import secret_scope, redact

            public_context = attribution(db, assignment, self.settings, job_id=job_id)
            if assignment.context_id:
                source = decrypt(
                    require_owned(
                        db, scope, ActionContextSnapshot, assignment.context_id
                    ).ciphertext,
                    self.settings,
                )["context"]
                with secret_scope(resolved.values()):
                    public_context["trace_context"] = {
                        key: redact(value)
                        for key, value in source.get("trace_context", {}).items()
                        if key in {"traceparent", "tracestate"}
                    }
                    correlation = redact(source.get("factory_run_id", run.id))
                    public_context["factory_run_id"] = (
                        correlation if len(correlation) <= 120 else run.id
                    )
            deadline = self.clock() + stage.timeout_seconds
            launch = JobLaunch(
                target_id=target.id,
                target_revision=target.revision,
                target_snapshot=target.model_dump(mode="json"),
                deadline=deadline,
                job_id=job_id,
                project_id=scope.project_id,
                execution_id=item.id,
                spec_digest=spec_digest,
                image=stage.image,
                command=stage.command,
                inputs=inputs,
                context=public_context,
                network=stage.network,
                limits=stage.limits,
            )
            row = RuntimeJob(
                id=job_id,
                project_id=scope.project_id,
                execution_id=item.id,
                assignment_id=assignment.id,
                spec_digest=spec_digest,
                launch_ciphertext=encrypt(
                    {
                        "launch": launch.model_dump(mode="json"),
                        "grants": grants,
                        "repository_id": assignment.repository_id,
                    },
                    self.settings,
                ),
                state="pending",
                launch_started=False,
                cancel_requested=False,
                deadline=deadline,
                target_id=target.id,
                target_revision=target.revision,
                target_snapshot=target.model_dump(mode="json"),
                updated_at=self.clock(),
            )
            db.add(row)
            db.flush()
            return self._receipt(row)

    def cancel(self, scope, job_id):
        with self.sessions.begin() as db:
            transition_lock(db)
            row = require_owned(db, scope, RuntimeJob, job_id)
            if row.state not in TERMINAL:
                row.cancel_requested = True
            return self._receipt(row)

    def _authority(self, db, row):
        document = decrypt(row.launch_ciphertext, self.settings)
        launch = JobLaunch.model_validate(document["launch"])
        if (
            launch.job_id,
            launch.project_id,
            launch.execution_id,
            launch.spec_digest,
        ) != (row.id, row.project_id, row.execution_id, row.spec_digest):
            raise ValueError("Stored job authority is inconsistent")
        if (launch.target_id, launch.target_revision, launch.target_snapshot) != (
            row.target_id,
            row.target_revision,
            row.target_snapshot,
        ):
            raise ValueError("Stored job target authority is inconsistent")
        if (
            launch.deadline is not None or launch.target_revision
        ) and launch.deadline != row.deadline:
            raise ValueError("Stored job deadline is inconsistent")
        if launch.target_snapshot:
            self.targets.require_snapshot(
                TargetSnapshot.model_validate(launch.target_snapshot)
            )
        elif launch.target_id != "local-docker":
            raise ValueError("Stored job target pin is missing")
        item = require_owned(
            db, ProjectScope(row.project_id), StageAttempt, row.execution_id
        )
        run = require_owned(db, ProjectScope(row.project_id), WorkflowRun, item.run_id)
        return document, launch, run

    def reconcile_once(self, runtime):
        # Shared database leases serialize replicas without a shared host path.
        with self.sessions() as db:
            # Reserve an independent budget for active effects; historical cleanup
            # must never consume their launch/cancel/deadline service capacity.
            candidates = list(
                db.scalars(
                    select(RuntimeJob.id)
                    .where(RuntimeJob.state.not_in(TERMINAL))
                    .order_by(RuntimeJob.updated_at, RuntimeJob.id)
                    .limit(8)
                )
            )
            candidates += list(
                db.scalars(
                    select(RuntimeJob.id)
                    .where(
                        RuntimeJob.state.in_(TERMINAL),
                        RuntimeJob.next_cleanup_at <= self.clock(),
                    )
                    .order_by(RuntimeJob.updated_at, RuntimeJob.id)
                    .limit(1)
                )
            )
        progressed = False
        for job_id in candidates:
            claim = claim_runtime(self.sessions, "job:" + job_id, self.clock())
            if claim is None:
                continue
            try:
                with runtime_heartbeat(self.sessions, claim, clock=self.clock):
                    self._reconcile(runtime, job_id, claim)
            except Exception:
                with self.sessions.begin() as db:
                    transition_lock(db)
                    try:
                        require_runtime_claim(self.sessions, claim, self.clock(), db=db)
                    except ValueError:
                        pass
                    else:
                        row = db.get(RuntimeJob, job_id)
                        row.error = (
                            "Job reconciliation unavailable; retrying inspection"
                        )
                        row.updated_at = self.clock()
            finally:
                release_runtime_claim(self.sessions, claim)
            progressed = True
        return progressed

    def _unknown(self, job_id, claim):
        with self.sessions.begin() as db:
            transition_lock(db)
            require_runtime_claim(self.sessions, claim, self.clock(), db=db)
            row = db.get(RuntimeJob, job_id)
            row.state = "unknown"
            row.error = "Job launch/execution outcome unknown; awaiting reconciliation"

    def _reconcile(self, runtime, job_id, claim):
        with self.sessions.begin() as db:
            transition_lock(db)
            require_runtime_claim(self.sessions, claim, self.clock(), db=db)
            row = db.get(RuntimeJob, job_id)
            document, launch, run = self._authority(db, row)
            terminal, started = row.state in TERMINAL, row.launch_started
            observed = row.runtime_observed
            stop = (
                row.cancel_requested
                or run.state in {"cancelling", "cancelled"}
                or self.clock() >= row.deadline
            )
            stop_state = (
                "cancelled"
                if row.cancel_requested or run.state in {"cancelling", "cancelled"}
                else "timed-out"
            )
            row.updated_at = self.clock()
            ref = RuntimeJobRef(
                **{k: getattr(launch, k) for k in RuntimeJobRef.model_fields}
            )
            if not terminal and not stop and not started:
                # Commit launch intent before calling Docker; retries inspect first.
                row.launch_started = True
            credentials = {}
            if not terminal:
                for purpose, version in document["grants"].items():
                    secret = require_owned(
                        db, ProjectScope(row.project_id), SecretVersion, version
                    )
                    credentials[purpose] = decrypt(secret.ciphertext, self.settings)
                revoked = any(
                    db.get(SecretVersion, version).revoked_at is not None
                    for version in document["grants"].values()
                )
                if revoked:
                    stop, stop_state = True, "failed"
        if callable(getattr(type(runtime), "for_job", None)):
            runtime = runtime.for_job(ref, claim=claim)
        with managed_runtime(runtime):
            if terminal:
                # Keep tombstones and inspect them on later ticks too: a delayed Docker
                # create response must never leave a cancelled job running indefinitely.
                require_runtime_claim(self.sessions, claim, self.clock())
                runtime.terminate(ref, grace_seconds=5)
                require_runtime_claim(self.sessions, claim, self.clock())
                runtime.cleanup(ref)
                with self.sessions.begin() as db:
                    transition_lock(db)
                    require_runtime_claim(self.sessions, claim, self.clock(), db=db)
                    row = db.get(RuntimeJob, job_id)
                    row.next_cleanup_at = self.clock() + 60
                    row.cleanup_pending = False
                    row.runtime_phase = "removed"
                    row.error = None
                return
            with secret_scope(credentials.values()):
                state = runtime.inspect(ref)
                if state.state not in {"missing", "intent"}:
                    observed = True
                    with self.sessions.begin() as db:
                        transition_lock(db)
                        require_runtime_claim(self.sessions, claim, self.clock(), db=db)
                        db.get(RuntimeJob, job_id).runtime_observed = True
                if (
                    state.state == "missing"
                    and started
                    and not observed
                    and not state.creation_inert
                ):
                    self._unknown(job_id, claim)
                    return
                if stop:
                    require_runtime_claim(self.sessions, claim, self.clock())
                    runtime.terminate(ref, grace_seconds=5)
                    state = runtime.inspect(ref)
                    if state.state not in {"missing", "exited", "dead"}:
                        return
                    result = (
                        sanitize_capture(runtime.collect(ref), credentials.values())[0]
                        if state.state in {"exited", "dead"}
                        else {}
                    )
                    result, final = (
                        {**result, "output": {}, "error": "Job " + stop_state},
                        stop_state,
                    )
                else:
                    if (
                        not started
                        or state.state in {"created", "intent"}
                        or (
                            state.state == "missing"
                            and state.creation_inert
                            and not observed
                        )
                    ):
                        require_runtime_claim(self.sessions, claim, self.clock())
                        runtime.ensure(
                            launch.model_copy(update={"credentials": credentials})
                        )
                        state = runtime.inspect(ref)
                    if state.state == "missing":
                        self._unknown(job_id, claim)
                        return  # Unknown external effects; do not recreate a lost job.
                    with self.sessions.begin() as db:
                        transition_lock(db)
                        require_runtime_claim(self.sessions, claim, self.clock(), db=db)
                        db.get(RuntimeJob, job_id).runtime_observed = True
                    if state.state not in {"exited", "dead"}:
                        with self.sessions.begin() as db:
                            transition_lock(db)
                            require_runtime_claim(
                                self.sessions, claim, self.clock(), db=db
                            )
                            row = db.get(RuntimeJob, job_id)
                            row.state, row.error = "running", None
                        return
                    result, failed = sanitize_capture(
                        runtime.collect(ref), credentials.values()
                    )
                    final = "failed" if failed else "completed"
                with self.sessions.begin() as db:
                    transition_lock(db)
                    require_runtime_claim(self.sessions, claim, self.clock(), db=db)
                    row = db.get(RuntimeJob, job_id)
                    _, _, run = self._authority(db, row)
                    if row.state in TERMINAL:
                        return
                    # Cancellation is linearized at terminal commit, never at inspect.
                    if row.cancel_requested or run.state in {"cancelling", "cancelled"}:
                        result, final = (
                            {**result, "output": {}, "error": "Job cancelled"},
                            "cancelled",
                        )
                    elif self.clock() >= row.deadline:
                        result, final = (
                            {**result, "output": {}, "error": "Job timed-out"},
                            "timed-out",
                        )
                    row.state, row.error = final, None
                    row.runtime_phase = "captured"
                    row.cleanup_pending = True
                    binding = db.scalar(
                        select(RuntimeResourceBinding).where(
                            RuntimeResourceBinding.job_id == row.id,
                            RuntimeResourceBinding.project_id == row.project_id,
                            RuntimeResourceBinding.target_id == row.target_id,
                            RuntimeResourceBinding.target_revision
                            == row.target_revision,
                            RuntimeResourceBinding.kind == "Job",
                            RuntimeResourceBinding.logical_id == row.id,
                        )
                    )
                    if binding is not None and binding.uid is not None:
                        binding.phase = "captured"
                    row.result_ciphertext = encrypt(result, self.settings)
                    row.updated_at = self.clock()
