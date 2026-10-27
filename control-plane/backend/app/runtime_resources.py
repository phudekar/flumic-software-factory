"""Scoped durable remote identities; names and labels are never deletion authority."""

from contextlib import contextmanager
import re
import time
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import RuntimeResourceBinding, RuntimeJob, Worker
from app.runtime_claims import require_runtime_claim
from app.runtime_targets import TargetSnapshot

KINDS = {
    "Deployment",
    "Job",
    "Pod",
    "Secret",
    "ConfigMap",
    "PersistentVolumeClaim",
    "NetworkPolicy",
}
PHASES = {"prepared", "payload-ready", "running", "captured", "terminating", "removed"}


class RuntimeResources:
    def __init__(
        self, sessions, snapshot, *, project_id=None, worker_id=None, clock=time.time
    ):
        self.sessions = sessions
        self.snapshot = TargetSnapshot.model_validate(snapshot.model_dump())
        self.project_id, self.worker_id, self.clock = project_id, worker_id, clock
        if project_id is not None and worker_id is not None:
            raise ValueError("Runtime resource authority is ambiguous")

    @contextmanager
    def _transaction(self, db):
        if db is not None:
            yield db
        else:
            with self.sessions.begin() as connection:
                yield connection

    def _query(self, kind, logical_id):
        query = select(RuntimeResourceBinding).where(
            RuntimeResourceBinding.target_id == self.snapshot.id,
            RuntimeResourceBinding.target_revision == self.snapshot.revision,
            RuntimeResourceBinding.kind == kind,
            RuntimeResourceBinding.logical_id == logical_id,
        )
        if self.project_id is not None:
            query = query.where(RuntimeResourceBinding.project_id == self.project_id)
        elif self.worker_id is not None:
            query = query.where(RuntimeResourceBinding.worker_id == self.worker_id)
        else:
            # An unscoped store cannot read job identities. Its only use is the
            # trusted infrastructure worker view, which still has no project data.
            query = query.where(RuntimeResourceBinding.project_id.is_(None))
        return query

    def get(self, kind, logical_id, *, db=None):
        with self._transaction(db) as connection:
            return connection.scalar(
                self._query(kind, logical_id).execution_options(populate_existing=True)
            )

    def require(self, kind, logical_id, *, uid, spec_digest, db=None):
        row = self.get(kind, logical_id, db=db)
        if row is None or row.uid != uid or row.spec_digest != spec_digest:
            raise ValueError("Remote runtime identity does not match authority")
        return row

    def _owner(self, db, *, project_id, worker_id, job_id, claim):
        require_runtime_claim(self.sessions, claim, self.clock(), db=db)
        if job_id is not None and project_id is not None and worker_id is None:
            job = db.get(RuntimeJob, job_id)
            if (
                self.project_id != project_id
                or job is None
                or job.project_id != project_id
                or claim.resource_key != "job:" + job_id
            ):
                raise ValueError("Runtime job ownership mismatch")
            snapshot = job.target_snapshot
            target_id, revision = job.target_id, job.target_revision
        elif worker_id is not None and job_id is None and project_id is None:
            worker = db.get(Worker, worker_id)
            if (
                self.project_id is not None
                or (self.worker_id is not None and self.worker_id != worker_id)
                or worker is None
                or claim.resource_key != "worker:" + worker_id
            ):
                raise ValueError("Runtime worker ownership mismatch")
            snapshot = worker.target_snapshot
            target_id = worker.target_id
            revision = snapshot.get("revision") if isinstance(snapshot, dict) else None
        else:
            raise ValueError("Runtime resource requires one owner")
        if (
            target_id != self.snapshot.id
            or revision != self.snapshot.revision
            or snapshot != self.snapshot.model_dump()
        ):
            raise ValueError("Runtime resource target mismatch")

    def bind(
        self,
        kind,
        logical_id,
        *,
        namespace,
        name,
        uid,
        spec_digest,
        owner_uid=None,
        project_id=None,
        worker_id=None,
        job_id=None,
        claim,
        db=None,
    ):
        if (
            kind not in KINDS
            or not isinstance(logical_id, str)
            or not 1 <= len(logical_id) <= 160
            or not isinstance(name, str)
            or not 1 <= len(name) <= 253
            or not isinstance(uid, str)
            or not 1 <= len(uid) <= 128
            or not re.fullmatch(r"[a-f0-9]{64}", spec_digest)
        ):
            raise ValueError("Invalid runtime resource identity")
        expected_namespace = self.snapshot.configuration.get(
            "job_namespace" if job_id else "worker_namespace"
        )
        if namespace != expected_namespace or (
            owner_uid is not None
            and (not isinstance(owner_uid, str) or not 1 <= len(owner_uid) <= 128)
        ):
            raise ValueError("Runtime resource namespace/owner mismatch")
        try:
            with self._transaction(db) as connection:
                self._owner(
                    connection,
                    project_id=project_id,
                    worker_id=worker_id,
                    job_id=job_id,
                    claim=claim,
                )
                if job_id is not None:
                    root_kind, root_id = "Job", job_id
                    independent = kind == "Job"
                else:
                    root_kind, root_id = "Deployment", worker_id
                    independent = kind in {"Deployment", "PersistentVolumeClaim"}
                    if kind not in {
                        "Deployment",
                        "PersistentVolumeClaim",
                        "ConfigMap",
                        "Secret",
                    }:
                        raise ValueError("Unsupported worker resource ownership chain")
                if independent:
                    if owner_uid is not None:
                        raise ValueError(
                            "Runtime root/retained volume cannot have another owner"
                        )
                else:
                    parent = self.get(root_kind, root_id, db=connection)
                    if parent is None or parent.uid != owner_uid:
                        raise ValueError("Runtime child resource owner is not pinned")
                row = self.get(kind, logical_id, db=connection)
                values = dict(
                    target_id=self.snapshot.id,
                    target_revision=self.snapshot.revision,
                    kind=kind,
                    logical_id=logical_id,
                    namespace=namespace,
                    name=name,
                    uid=uid,
                    spec_digest=spec_digest,
                    owner_uid=owner_uid,
                    project_id=project_id,
                    worker_id=worker_id,
                    job_id=job_id,
                )
                if row is not None:
                    if any(getattr(row, key) != value for key, value in values.items()):
                        raise ValueError("Remote runtime binding cannot be replaced")
                    return row
                row = RuntimeResourceBinding(**values)
                connection.add(row)
                connection.flush()
                return row
        except IntegrityError:
            raise ValueError("Runtime resource identity is already bound") from None

    def _owned_row(self, db, kind, logical_id, claim):
        row = self.get(kind, logical_id, db=db)
        if row is None:
            raise ValueError("Runtime resource binding is missing")
        self._owner(
            db,
            project_id=row.project_id,
            worker_id=row.worker_id,
            job_id=row.job_id,
            claim=claim,
        )
        db.refresh(row)
        return row

    def record_phase(self, kind, logical_id, *, phase, expected_phase, claim, db=None):
        if phase not in PHASES or expected_phase not in PHASES:
            raise ValueError("Invalid runtime resource phase")
        with self._transaction(db) as connection:
            row = self._owned_row(connection, kind, logical_id, claim)
            if row.phase != expected_phase:
                raise ValueError("Runtime resource phase changed")
            row.phase = phase
            connection.flush()
            return row

    def prepare_change(self, kind, logical_id, *, expected_spec, new_spec, claim):
        if kind != "Deployment" or not re.fullmatch(r"[a-f0-9]{64}", new_spec):
            raise ValueError("Only a worker Deployment may change its prepared spec")
        with self.sessions.begin() as db:
            row = self._owned_row(db, kind, logical_id, claim)
            if (
                row.worker_id is None
                or row.spec_digest != expected_spec
                or row.pending_spec_digest not in (None, new_spec)
            ):
                raise ValueError("Runtime spec changed before preparation")
            row.pending_spec_digest = new_spec
            db.flush()
            return row

    def commit_change(self, kind, logical_id, *, new_spec, claim):
        with self.sessions.begin() as db:
            row = self._owned_row(db, kind, logical_id, claim)
            if kind != "Deployment" or row.pending_spec_digest != new_spec:
                raise ValueError("Runtime spec change was not prepared")
            row.spec_digest = new_spec
            row.pending_spec_digest = None
            db.flush()
            return row
