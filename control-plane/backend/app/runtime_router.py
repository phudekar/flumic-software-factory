"""Runtime composition: operator authority stays out of browser and project data."""

import re
from app.ports import managed_runtime
import time
from sqlalchemy import select
from app.database import Worker, RuntimeResourceBinding
from app.runtime_targets import TargetRegistry, TargetSnapshot
from app.runtime_resources import RuntimeResources
from app.runtime_claims import require_runtime_claim


class RuntimeRouter:
    def __init__(self, settings, config_service, sessions, *, factories=None):
        self.settings, self.config, self.sessions = settings, config_service, sessions
        self.targets = TargetRegistry(settings)
        self.factories = factories or {}
        self.clock = time.time
        self.warnings = []
        self._docker = {}
        if not self.factories and any(
            item["runtime"] == "kubernetes" for item in self.targets.public_targets()
        ):
            self._require_postgresql()

    def _require_postgresql(self):
        with self.sessions() as db:
            if db.get_bind().dialect.name != "postgresql":
                raise ValueError("Kubernetes runtime composition requires PostgreSQL")

    def _worker_pin(self, worker):
        return self.targets.resolve_worker(worker)

    def _workers(self, snapshot, *, worker=None, claim=None):
        resources = RuntimeResources(
            self.sessions,
            snapshot,
            worker_id=worker.id if worker else None,
            clock=self.clock,
        )
        key = snapshot.kind + "_workers"
        if key in self.factories:
            return self.factories[key](snapshot, resources, claim)
        if snapshot.kind == "docker":
            if key not in self._docker:
                from app.adapters.docker import DockerRuntime

                self._docker[key] = DockerRuntime(self.settings, self.config)
            return self._docker[key]
        self._require_postgresql()
        from app.adapters.kubernetes_api import KubernetesApi
        from app.adapters.kubernetes_workers import KubernetesWorkerRuntime

        guard = (
            (lambda: require_runtime_claim(self.sessions, claim, self.clock()))
            if claim
            else None
        )
        api = KubernetesApi(snapshot, effect_guard=guard)
        return KubernetesWorkerRuntime(
            self.settings, self.config, snapshot, resources, api=api, claim=claim
        )

    def for_worker(self, worker, claim):
        return self._workers(self._worker_pin(worker), worker=worker, claim=claim)

    def for_job(self, ref, *, claim):
        if ref.target_snapshot:
            snapshot = self.targets.require_snapshot(
                TargetSnapshot.model_validate(ref.target_snapshot)
            )
            if (snapshot.id, snapshot.revision) != (ref.target_id, ref.target_revision):
                raise ValueError("Job target pin mismatch")
        else:
            if ref.target_id != "local-docker" or ref.target_revision:
                raise ValueError("Job target pin is missing")
            snapshot = self.targets.snapshot("local-docker")
        resources = RuntimeResources(
            self.sessions, snapshot, project_id=ref.project_id, clock=self.clock
        )
        key = snapshot.kind + "_jobs"
        if key in self.factories:
            return self.factories[key](snapshot, resources, claim)
        if snapshot.kind == "docker":
            if key not in self._docker:
                from app.adapters.docker_jobs import DockerJobRuntime

                root = self.settings.snapshot_root / "jobs"
                host = self.settings.host_root / root.relative_to(self.settings.root)
                self._docker[key] = DockerJobRuntime(
                    root,
                    host,
                    snapshot.configuration["deployment_id"].removeprefix("docker-"),
                    bridge=self.settings.job_bridge,
                )
            return self._docker[key]
        self._require_postgresql()
        from app.adapters.kubernetes_api import KubernetesApi
        from app.adapters.kubernetes_jobs import KubernetesJobRuntime

        api = KubernetesApi(
            snapshot,
            effect_guard=lambda: require_runtime_claim(
                self.sessions, claim, self.clock()
            ),
        )
        return KubernetesJobRuntime(
            snapshot, resources, api=api, claim=claim, clock=self.clock
        )

    def list(self):
        result = []
        self.warnings = []
        for public in self.targets.public_targets():
            snapshot = self.targets.snapshot(public["id"])
            try:
                with managed_runtime(self._workers(snapshot)) as adapter:
                    entries = adapter.list()
                for entry in entries:
                    if snapshot.kind == "kubernetes":
                        identifier = entry["id"]
                        entry = {
                            **entry,
                            "id": "kubernetes:" + snapshot.id + ":" + identifier,
                            "instance_id": entry["worker_id"],
                            "managed": True,
                            "target_id": snapshot.id,
                            "runtime_kind": "kubernetes",
                        }
                    else:
                        entry = {
                            **entry,
                            "target_id": snapshot.id,
                            "runtime_kind": "docker",
                        }
                    result.append(entry)
            except Exception:
                self.warnings.append(
                    "Runtime target "
                    + snapshot.id
                    + " unavailable; observed state is unknown."
                )
        return result

    def create(self, worker, document, token):
        with managed_runtime(
            self._workers(self._worker_pin(worker), worker=worker)
        ) as adapter:
            return adapter.create(worker, document, token)

    def configure(self, worker, document, token):
        with managed_runtime(
            self._workers(self._worker_pin(worker), worker=worker)
        ) as adapter:
            return adapter.configure(worker, document, token)

    def action(self, worker_id, action):
        with self.sessions() as db:
            worker = db.get(Worker, worker_id)
        if worker is None:
            raise ValueError("Worker not found")
        with managed_runtime(
            self._workers(self._worker_pin(worker), worker=worker)
        ) as adapter:
            return adapter.action(worker_id, action)

    def logs(self, handle, limit):
        if handle.startswith(("kubernetes:", "kubernetes/")):
            pieces = handle.split(":")
            if (
                len(pieces) != 3
                or pieces[0] != "kubernetes"
                or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", pieces[2])
            ):
                raise ValueError("Invalid Kubernetes runtime log handle")
            snapshot = self.targets.snapshot(pieces[1])
            if snapshot.kind != "kubernetes":
                raise ValueError("Runtime log target mismatch")
            with self.sessions() as db:
                binding = db.scalar(
                    select(RuntimeResourceBinding).where(
                        RuntimeResourceBinding.target_id == snapshot.id,
                        RuntimeResourceBinding.target_revision == snapshot.revision,
                        RuntimeResourceBinding.kind == "Deployment",
                        RuntimeResourceBinding.uid == pieces[2],
                        RuntimeResourceBinding.worker_id.is_not(None),
                    )
                )
                worker = db.get(Worker, binding.worker_id) if binding else None
            if worker is None or self._worker_pin(worker) != snapshot:
                raise ValueError("Unknown Kubernetes runtime log handle")
            with managed_runtime(self._workers(snapshot, worker=worker)) as adapter:
                return adapter.logs(pieces[2], limit)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", handle):
            raise ValueError("Invalid Docker runtime log handle")
        return self._workers(self.targets.snapshot("local-docker")).logs(handle, limit)
