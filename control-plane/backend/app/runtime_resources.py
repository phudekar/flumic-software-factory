"""Scoped durable remote identities; names and labels are never deletion authority."""

from contextlib import contextmanager
import copy
import re
import time
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.database import RuntimeResourceBinding, RuntimeJob, Worker
from app.runtime_claims import require_runtime_claim
from app.runtime_targets import TargetSnapshot, digest

KINDS = {
    "Deployment",
    "Job",
    "Pod",
    "Secret",
    "ConfigMap",
    "PersistentVolumeClaim",
    "NetworkPolicy",
}
PHASES = {
    "intent",
    "prepared",
    "payload-ready",
    "running",
    "captured",
    "terminating",
    "removed",
}


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
        if (
            row is None
            or row.uid is None
            or row.uid != uid
            or row.spec_digest != spec_digest
        ):
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
        document=None,
    ):
        if document is not None and (
            kind not in {"Deployment", "Job"} or digest(document) != spec_digest
        ):
            raise ValueError("Runtime document does not match prepared spec")
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
                    if parent is None or not parent.uid or parent.uid != owner_uid:
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
                    unobserved = (
                        row.uid is None and row.phase == "intent" and kind == "Job"
                    )
                    if any(
                        getattr(row, key) != value
                        for key, value in values.items()
                        if key != "uid" or not unobserved
                    ):
                        raise ValueError("Remote runtime binding cannot be replaced")
                    if unobserved:
                        row.uid, row.phase = uid, "prepared"
                        connection.flush()
                    if kind == "Job":
                        connection.get(RuntimeJob, job_id).runtime_observed = True
                        connection.flush()
                    return row
                row = RuntimeResourceBinding(**values)
                if document is not None:
                    row.details = {"specs": {spec_digest: copy.deepcopy(document)}}
                connection.add(row)
                if kind == "Job":
                    connection.get(RuntimeJob, job_id).runtime_observed = True
                connection.flush()
                return row
        except IntegrityError:
            raise ValueError("Runtime resource identity is already bound") from None

    def prepare_job_root(self, job_id, document, *, claim):
        """Reserve a suspended, nonsecret manifest before any external create."""
        if (
            document.get("kind") != "Job"
            or document.get("spec", {}).get("suspend") is not True
            or document.get("metadata", {}).get("namespace")
            != self.snapshot.configuration.get("job_namespace")
        ):
            raise ValueError("A pinned suspended Job manifest is required")
        name = document["metadata"].get("name")
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9-]{0,62}", name
        ):
            raise ValueError("Invalid Job resource name")
        spec = digest(document)
        try:
            with self.sessions.begin() as db:
                self._owner(
                    db,
                    project_id=self.project_id,
                    worker_id=None,
                    job_id=job_id,
                    claim=claim,
                )
                row = self.get("Job", job_id, db=db)
                if row is not None:
                    if (
                        row.name != name
                        or row.spec_digest != spec
                        or row.details.get("specs", {}).get(spec) != document
                    ):
                        raise ValueError("Prepared Job intent cannot change")
                    return row
                row = RuntimeResourceBinding(
                    target_id=self.snapshot.id,
                    target_revision=self.snapshot.revision,
                    kind="Job",
                    logical_id=job_id,
                    namespace=document["metadata"]["namespace"],
                    name=name,
                    uid=None,
                    spec_digest=spec,
                    phase="intent",
                    project_id=self.project_id,
                    job_id=job_id,
                    details={"specs": {spec: copy.deepcopy(document)}},
                )
                db.add(row)
                db.flush()
                return row
        except IntegrityError:
            raise ValueError("Job intent is already reserved") from None

    def prepare_job_fence(self, job_id, nonce, *, claim):
        """Approve both sides of a metadata write before invalidating old RVs."""
        if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
            raise ValueError("Invalid Job fence")
        with self.sessions.begin() as db:
            row = self._owned_row(db, "Job", job_id, claim)
            if not row.uid or row.phase not in {"prepared", "payload-ready"}:
                raise ValueError("Only a prepared suspended Job may be fenced")
            initial = row.details["specs"][row.spec_digest]
            document = copy.deepcopy(initial)
            document["metadata"]["annotations"]["flumic.io/start-fence"] = nonce
            previous = row.details.get("fence_manifest", initial)
            row.details = {
                **row.details,
                "previous_fence_manifest": previous,
                "fence_manifest": document,
            }
            db.flush()
            return document

    def prepare_job_start(self, job_id, document, *, claim):
        """Only a still-suspended Job may select a new, fenced remaining budget."""
        with self.sessions.begin() as db:
            row = self._owned_row(db, "Job", job_id, claim)
            budget = document.get("spec", {}).get("activeDeadlineSeconds")
            if not row.uid or type(budget) is not int or not 1 <= budget <= 3600:
                raise ValueError("Observed Job and bounded remaining budget required")
            initial = row.details.get("specs", {}).get(row.spec_digest)
            expected = copy.deepcopy(row.details.get("fence_manifest", initial))
            if expected is None:
                raise ValueError("Prepared Job document is missing")
            expected["spec"].update(suspend=False, activeDeadlineSeconds=budget)
            if document != expected or row.phase not in {"prepared", "payload-ready"}:
                raise ValueError("Running Job intent cannot change")
            row.details = {**row.details, "start_manifest": copy.deepcopy(document)}
            db.flush()
            return row

    def prepare_job_stop(self, job_id, document, *, claim):
        with self.sessions.begin() as db:
            row = self._owned_row(db, "Job", job_id, claim)
            initial = row.details.get("specs", {}).get(row.spec_digest)
            candidates = [initial] + [
                row.details.get(key)
                for key in (
                    "start_manifest",
                    "fence_manifest",
                    "previous_fence_manifest",
                    "stop_manifest",
                    "previous_stop_manifest",
                )
            ]
            nonce = (
                document.get("metadata", {})
                .get("annotations", {})
                .get("flumic.io/start-fence")
            )
            if not isinstance(nonce, str) or not re.fullmatch(r"[0-9a-f]{32}", nonce):
                raise ValueError("Job stop requires a fresh metadata fence")
            allowed = []
            for candidate in candidates:
                if candidate is not None:
                    stopped = copy.deepcopy(candidate)
                    stopped["spec"]["suspend"] = True
                    stopped["metadata"]["annotations"]["flumic.io/start-fence"] = nonce
                    allowed.append(stopped)
            if not row.uid or document not in allowed:
                raise ValueError("Job stop must preserve prepared execution spec")
            row.details = {
                **row.details,
                "previous_stop_manifest": row.details.get("stop_manifest"),
                "stop_manifest": copy.deepcopy(document),
            }
            row.phase = "terminating"
            db.flush()
            return row

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
            if row.uid is None and phase != "intent":
                raise ValueError("Runtime resource UID has not been observed")
            if row.phase != expected_phase:
                raise ValueError("Runtime resource phase changed")
            row.phase = phase
            connection.flush()
            return row

    def prepare_change(
        self, kind, logical_id, *, expected_spec, new_spec, claim, document=None
    ):
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
            if document is not None:
                if digest(document) != new_spec:
                    raise ValueError("Runtime document does not match prepared spec")
                documents = {
                    k: v
                    for k, v in row.details.get("specs", {}).items()
                    if k == expected_spec
                }
                documents[new_spec] = copy.deepcopy(document)
                row.details = {"specs": documents}
            db.flush()
            return row

    def commit_change(self, kind, logical_id, *, new_spec, claim):
        with self.sessions.begin() as db:
            row = self._owned_row(db, kind, logical_id, claim)
            if kind != "Deployment" or row.pending_spec_digest != new_spec:
                raise ValueError("Runtime spec change was not prepared")
            row.spec_digest = new_spec
            row.pending_spec_digest = None
            if row.details.get("specs"):
                row.details = {"specs": {new_spec: row.details["specs"][new_spec]}}
            db.flush()
            return row
