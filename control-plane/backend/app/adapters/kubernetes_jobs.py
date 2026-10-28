"""Durable suspended Job intents, fenced admission and repeatable collection."""

import copy
import math
import time

from sqlalchemy import select
from app.adapters.kubernetes_api import KubernetesApi, KubernetesError
from app.adapters.kubernetes_job_manifests import (
    job_manifest,
    job_name,
    payload_resources,
    manifest_fingerprint,
    normalized_manifest,
)
from app.database import RuntimeJob, RuntimeResourceBinding, StageAttempt, WorkflowRun
from app.job_contracts import RuntimeJobRef, RuntimeJobState, JobCapture
from app.ports import RuntimePending
from app.runtime_claims import require_runtime_claim
from app.runtime_targets import TargetSnapshot, digest

TERMINAL = {"completed", "failed", "timed-out", "cancelled"}


class KubernetesJobRuntime:
    def close(self):
        self.api.close()

    def __init__(
        self,
        target,
        resources,
        *,
        api=None,
        effect_guard=None,
        claim=None,
        clock=time.time,
    ):
        self.target = TargetSnapshot.model_validate(target.model_dump())
        if (
            self.target.kind != "kubernetes"
            or resources.snapshot != self.target
            or resources.project_id is None
        ):
            raise ValueError("Project-scoped Kubernetes job authority required")
        self.resources, self.claim, self.clock = resources, claim, clock
        self.api = api or KubernetesApi(self.target)
        if self.api.target != self.target:
            raise ValueError("Kubernetes client target mismatch")
        self.launching = False
        previous = effect_guard or self.api.effect_guard

        def guard():
            if previous is not None:
                previous()
            if self.claim is None:
                raise ValueError("Kubernetes Job claim is required")
            require_runtime_claim(resources.sessions, self.claim, self.clock())
            if self.launching:
                self._launch_guard(self.current_ref)

        self.api.effect_guard = guard

    @staticmethod
    def reference(launch):
        return RuntimeJobRef(
            **{key: getattr(launch, key) for key in RuntimeJobRef.model_fields}
        )

    def _job(self, ref):
        ref = RuntimeJobRef.model_validate(ref)
        if (
            ref.target_snapshot != self.target.model_dump()
            or ref.target_id != self.target.id
            or ref.target_revision != self.target.revision
            or ref.project_id != self.resources.project_id
        ):
            raise ValueError("Job target/project authority mismatch")
        with self.resources.sessions() as db:
            row = db.get(RuntimeJob, ref.job_id)
            if (
                row is None
                or row.project_id != ref.project_id
                or row.execution_id != ref.execution_id
                or row.spec_digest != ref.spec_digest
                or row.target_snapshot != ref.target_snapshot
                or row.deadline != ref.deadline
            ):
                raise ValueError("Job reference does not match canonical authority")
            return row

    def _launch_guard(self, ref):
        row = self._job(ref)
        if (
            row.state in TERMINAL
            or row.cancel_requested
            or self.clock() >= row.deadline
        ):
            raise ValueError("Job admission is cancelled or past its deadline")
        with self.resources.sessions() as db:
            attempt = db.get(StageAttempt, row.execution_id)
            run = db.get(WorkflowRun, attempt.run_id) if attempt else None
            if (
                run is None
                or run.project_id != ref.project_id
                or run.state in {"cancelling", "cancelled"}
            ):
                raise ValueError("Workflow no longer permits Job admission")

    def _call(self, group, operation, resource, **kwargs):
        return self.api.call(
            group, operation + "_namespaced_" + resource, namespace_kind="job", **kwargs
        )

    def _read(self, group, resource, name):
        try:
            return self._call(group, "read", resource, name=name)
        except KubernetesError as exc:
            if exc.status != 404:
                raise
            return None

    def _manifest(self, root):
        document = root.details.get("specs", {}).get(root.spec_digest)
        if document is None or digest(document) != root.spec_digest:
            raise ValueError("Prepared Job manifest is unavailable")
        return document

    def _remote(self, ref):
        self._job(ref)
        root = self.resources.get("Job", ref.job_id)
        name = root.name if root else job_name(ref, self.target)
        value = self._read("batch", "job", name)
        if root is None:
            if value is not None:
                raise ValueError("Unreserved Job resource is not authority")
            return None, None
        if value is None:
            return root, None
        if root.uid is not None and value["metadata"]["uid"] != root.uid:
            raise ValueError("Kubernetes Job UID changed")
        expected = (
            (root.details.get("stop_manifest") or self._manifest(root))
            if value["spec"].get("suspend", False)
            else root.details.get("start_manifest")
        )
        if expected is None or manifest_fingerprint(value) != manifest_fingerprint(
            expected
        ):
            raise ValueError("Kubernetes Job spec does not match prepared intent")
        if root.uid is None:
            root = self.resources.bind(
                "Job",
                ref.job_id,
                namespace=root.namespace,
                name=root.name,
                uid=value["metadata"]["uid"],
                spec_digest=root.spec_digest,
                project_id=ref.project_id,
                job_id=ref.job_id,
                claim=self.claim,
            )
        return root, value

    def _patch(self, value, operations):
        tests = [
            {"op": "test", "path": "/metadata/" + key, "value": value["metadata"][key]}
            for key in ("uid", "resourceVersion")
        ]
        return self._call(
            "batch",
            "patch",
            "job",
            name=value["metadata"]["name"],
            body=tests + operations,
        )

    def _payloads(self, launch, root):
        for body in payload_resources(launch, root.uid, self.target):
            kind = body["kind"]
            resource = "secret" if kind == "Secret" else "config_map"
            logical = launch.job_id + ":" + body["metadata"]["name"]
            binding = self.resources.get(kind, logical)
            value = self._read("core", resource, body["metadata"]["name"])
            if binding is not None and (
                value is None
                or value["metadata"]["uid"] != binding.uid
                or binding.spec_digest != digest(body)
            ):
                raise ValueError("Job payload identity changed")
            if value is None:
                value = self._call("core", "create", resource, body=body)
            if manifest_fingerprint(value) != manifest_fingerprint(body):
                raise ValueError("Job payload does not match prepared owner/spec")
            self.resources.bind(
                kind,
                logical,
                namespace=root.namespace,
                name=body["metadata"]["name"],
                uid=value["metadata"]["uid"],
                spec_digest=digest(body),
                owner_uid=root.uid,
                project_id=launch.project_id,
                job_id=launch.job_id,
                claim=self.claim,
            )

    def ensure(self, launch):
        ref = self.reference(launch)
        self.current_ref, self.launching = ref, True
        try:
            self._launch_guard(ref)
            initial = job_manifest(launch, self.target)
            root = self.resources.prepare_job_root(
                ref.job_id, initial, claim=self.claim
            )
            root, value = self._remote(ref)
            if root.details.get("stop_manifest"):
                raise ValueError("Stopped Job cannot be readmitted")
            if value is None:
                if root.uid is not None:
                    raise ValueError("Observed Job cannot be recreated")
                value = self._call("batch", "create", "job", body=initial)
                root, value = self._remote(ref)
            if not value["spec"].get("suspend", False):
                if root.phase == "payload-ready":
                    self.resources.record_phase(
                        "Job",
                        ref.job_id,
                        phase="running",
                        expected_phase=root.phase,
                        claim=self.claim,
                    )
                return ref
            self._payloads(launch, root)
            if root.phase == "prepared":
                root = self.resources.record_phase(
                    "Job",
                    ref.job_id,
                    phase="payload-ready",
                    expected_phase=root.phase,
                    claim=self.claim,
                )
            if root.details.get("start_manifest"):
                # Increment RV even for a no-op suspension, invalidating any
                # previously delayed unsuspend before selecting a smaller budget.
                value = self._patch(
                    value, [{"op": "replace", "path": "/spec/suspend", "value": True}]
                )
            budget = math.floor(ref.deadline - self.clock())
            if budget < 1:
                raise ValueError("Job has no whole-second execution budget")
            started = copy.deepcopy(initial)
            started["spec"].update(suspend=False, activeDeadlineSeconds=budget)
            self.resources.prepare_job_start(ref.job_id, started, claim=self.claim)
            value = self._patch(
                value,
                [
                    {
                        "op": "add",
                        "path": "/spec/activeDeadlineSeconds",
                        "value": budget,
                    },
                    {"op": "replace", "path": "/spec/suspend", "value": False},
                ],
            )
            if manifest_fingerprint(value) != manifest_fingerprint(started):
                raise ValueError("Started Job spec changed")
            self.resources.record_phase(
                "Job",
                ref.job_id,
                phase="running",
                expected_phase="payload-ready",
                claim=self.claim,
            )
            return ref
        finally:
            self.launching = False

    def _pods(self, ref, root):
        items = self._call(
            "core",
            "list",
            "pod",
            label_selector="flumic.io/job=" + ref.job_id[:40],
            limit=100,
        )["items"]
        for pod in items:
            owners = pod["metadata"].get("ownerReferences", [])
            if (
                len(owners) != 1
                or owners[0].get("kind") != "Job"
                or not owners[0].get("controller")
                or owners[0].get("uid") != root.uid
            ):
                raise ValueError("Action Pod owner is not the recorded Job UID")
        return items

    def _verified_pod(self, ref, root, pod):
        expected = (root.details.get("start_manifest") or self._manifest(root))["spec"][
            "template"
        ]
        for key, value in expected["metadata"].get("annotations", {}).items():
            if pod["metadata"].get("annotations", {}).get(key) != value:
                raise ValueError("Action Pod annotations changed")
        for key, value in expected["metadata"].get("labels", {}).items():
            if pod["metadata"].get("labels", {}).get(key) != value:
                raise ValueError("Action Pod labels changed")
        spec = copy.deepcopy(pod["spec"])
        spec.pop("nodeName", None)
        if spec.get("priority") == 0:
            spec.pop("priority")
        if spec.get("preemptionPolicy") == "PreemptLowerPriority":
            spec.pop("preemptionPolicy")
        original = expected["spec"].get("tolerations", [])
        automatic = [
            {
                "key": key,
                "operator": "Exists",
                "effect": "NoExecute",
                "tolerationSeconds": 300,
            }
            for key in (
                "node.kubernetes.io/not-ready",
                "node.kubernetes.io/unreachable",
            )
        ]
        if "tolerations" in spec:
            spec["tolerations"] = [
                item
                for item in spec["tolerations"]
                if item in original or item not in automatic
            ]

        def normalize(spec):
            return normalized_manifest({"spec": {"template": {"spec": spec}}})["spec"][
                "template"
            ]["spec"]

        if normalize(spec) != normalize(expected["spec"]):
            raise ValueError("Action Pod execution spec changed")
        self.resources.bind(
            "Pod",
            ref.job_id + ":pod",
            namespace=root.namespace,
            name=pod["metadata"]["name"],
            uid=pod["metadata"]["uid"],
            spec_digest=digest(expected),
            owner_uid=root.uid,
            project_id=ref.project_id,
            job_id=ref.job_id,
            claim=self.claim,
        )
        return pod

    @staticmethod
    def _action(pod):
        return next(
            (
                value.get("state", {})
                for value in pod.get("status", {}).get("containerStatuses", [])
                if value.get("name") == "action"
            ),
            {},
        )

    def inspect(self, ref):
        root, value = self._remote(ref)
        if root is None:
            return RuntimeJobState(state="missing", creation_inert=True)
        if root.uid is None:
            try:
                self._launch_guard(ref)
            except ValueError:
                return RuntimeJobState(state="missing", creation_inert=True)
            return RuntimeJobState(state="intent")
        pods = self._pods(ref, root)
        if value is None and pods:
            return (
                RuntimeJobState(state="dead", exit_code=1)
                if all(
                    pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}
                    for pod in pods
                )
                else RuntimeJobState(state="removing")
            )
        if not pods:
            if value is None:
                return RuntimeJobState(state="missing")
            if root.details.get("stop_manifest") and not value["metadata"].get(
                "deletionTimestamp"
            ):
                return RuntimeJobState(state="dead", exit_code=1)
            if any(
                item.get("type") in {"Failed", "Complete"}
                and item.get("status") == "True"
                for item in value.get("status", {}).get("conditions", [])
            ):
                return RuntimeJobState(state="dead", exit_code=1)
            return RuntimeJobState(
                state="removing"
                if value["metadata"].get("deletionTimestamp")
                else "created"
                if value["spec"].get("suspend", False)
                else "running"
            )
        if len(pods) != 1:
            return RuntimeJobState(state="dead", exit_code=1)
        pod = self._verified_pod(ref, root, pods[0])
        action = self._action(pod)
        if "terminated" in action:
            result = action["terminated"]
            return RuntimeJobState(
                state="exited",
                exit_code=result.get("exitCode", 1),
                oom=result.get("reason") == "OOMKilled",
            )
        if pod.get("status", {}).get("phase") in {"Failed", "Succeeded"}:
            return RuntimeJobState(state="dead", exit_code=1)
        return RuntimeJobState(
            state="removing"
            if value is None or value["metadata"].get("deletionTimestamp")
            else "running"
        )

    def collect(self, ref):
        row = self._job(ref)
        base = dict(output={}, log_stream="combined")
        if self.clock() >= row.deadline:
            return JobCapture(
                **base, error="Result unavailable after the pinned Job deadline"
            )
        root, value = self._remote(ref)
        if root is None or not root.uid or value is None:
            return JobCapture(**base, error="Job result is unavailable")
        pods = self._pods(ref, root)
        if len(pods) != 1:
            return JobCapture(**base, error="A single owned action Pod is unavailable")
        pod = self._verified_pod(ref, root, pods[0])
        action = self._action(pod).get("terminated")
        if action is None:
            return JobCapture(**base, error="Action termination has not been observed")
        exit_code = action.get("exitCode", 1)
        oom = action.get("reason") == "OOMKilled"
        try:
            stdout, truncated = self.api.pod_logs(
                pod["metadata"]["name"],
                namespace_kind="job",
                container="action",
                expected_uid=pod["metadata"]["uid"],
            )
        except KubernetesError:
            stdout, truncated = "", True
        base.update(
            exit_code=exit_code, oom=oom, stdout=stdout, stdout_truncated=truncated
        )
        if exit_code != 0 or oom:
            return JobCapture(
                **base, error="Action exited nonzero or exceeded memory limits"
            )
        collector = next(
            (
                value.get("state", {})
                for value in pod.get("status", {}).get("containerStatuses", [])
                if value.get("name") == "collector"
            ),
            {},
        )
        if "running" not in collector:
            return JobCapture(
                **base, error="Collector is unavailable; result could not be captured"
            )
        try:
            envelope = self.api.collector_exec(
                pod["metadata"]["name"],
                expected_uid=pod["metadata"]["uid"],
                mode="capture",
            )
            from factory.workflows.references import json_bytes
            from factory.workflows.contracts import UsageRecord

            if (
                not isinstance(envelope, dict)
                or set(envelope) - {"output", "usage"}
                or not isinstance(envelope.get("output"), dict)
                or len(json_bytes(envelope)) > 1048576
            ):
                raise ValueError("Invalid collector result")
            usage = envelope.get("usage", [])
            if not isinstance(usage, list) or len(usage) > 256:
                raise ValueError("Invalid usage")
            usage = [
                UsageRecord.model_validate(record).model_dump(mode="json")
                for record in usage
            ]
            if self.clock() >= row.deadline:
                return JobCapture(
                    **base, error="Result unavailable after the pinned Job deadline"
                )
            return JobCapture(**{**base, "output": envelope["output"]}, usage=usage)
        except (ValueError, TypeError, RecursionError):
            return JobCapture(
                **base, error="Collector returned an invalid bounded result"
            )

    def _delete(self, group, resource, value):
        return self._call(
            group,
            "delete",
            resource,
            name=value["metadata"]["name"],
            body={
                "preconditions": {
                    "uid": value["metadata"]["uid"],
                    "resourceVersion": value["metadata"]["resourceVersion"],
                },
                "propagationPolicy": "Foreground",
                "gracePeriodSeconds": 5,
            },
        )

    def _payload_bindings(self, ref):
        with self.resources.sessions() as db:
            return db.scalars(
                select(RuntimeResourceBinding).where(
                    RuntimeResourceBinding.job_id == ref.job_id,
                    RuntimeResourceBinding.project_id == ref.project_id,
                    RuntimeResourceBinding.target_id == self.target.id,
                    RuntimeResourceBinding.target_revision == self.target.revision,
                    RuntimeResourceBinding.kind.in_(("Secret", "ConfigMap")),
                )
            ).all()

    def _payloads_intact(self, ref, root):
        for binding in self._payload_bindings(ref):
            resource = "secret" if binding.kind == "Secret" else "config_map"
            remote = self._read("core", resource, binding.name)
            if remote is None:
                continue
            if (
                remote["metadata"]["uid"] != binding.uid
                or manifest_fingerprint(remote) != binding.spec_digest
            ):
                return False
        return True

    def terminate(self, ref, grace_seconds=5):
        root, value = self._remote(ref)
        if root is None:
            return
        if value is None:
            if not root.uid:
                return
            primary = self.resources.get("Pod", ref.job_id + ":pod")
            template = (root.details.get("start_manifest") or self._manifest(root))[
                "spec"
            ]["template"]
            for pod in self._pods(ref, root):
                logical = (
                    ref.job_id + ":pod"
                    if primary is None or primary.uid == pod["metadata"]["uid"]
                    else ref.job_id + ":late-" + digest(pod["metadata"]["uid"])[:24]
                )
                binding = self.resources.bind(
                    "Pod",
                    logical,
                    namespace=root.namespace,
                    name=pod["metadata"]["name"],
                    uid=pod["metadata"]["uid"],
                    spec_digest=digest(template),
                    owner_uid=root.uid,
                    project_id=ref.project_id,
                    job_id=ref.job_id,
                    claim=self.claim,
                )
                if binding.phase != "terminating":
                    self.resources.record_phase(
                        "Pod",
                        logical,
                        phase="terminating",
                        expected_phase=binding.phase,
                        claim=self.claim,
                    )
                self._delete("core", "pod", pod)
            return
        row = self._job(ref)
        if row.state == "completed" and row.runtime_phase == "captured":
            state = self.inspect(ref)
            if state.state in {"exited", "dead", "missing"}:
                return
        stopped = copy.deepcopy(
            root.details.get("start_manifest")
            if not value["spec"].get("suspend", False)
            else (root.details.get("stop_manifest") or self._manifest(root))
        )
        stopped["spec"]["suspend"] = True
        self.resources.prepare_job_stop(ref.job_id, stopped, claim=self.claim)
        if not value["metadata"].get("deletionTimestamp"):
            value = self._patch(
                value, [{"op": "replace", "path": "/spec/suspend", "value": True}]
            )
        # Suspension requests action termination without cascading through an
        # object whose persisted UID/owner has been replaced.
        if not self._payloads_intact(ref, root):
            return
        self._delete("batch", "job", value)

    def cleanup(self, ref):
        row = self._job(ref)
        if row.state not in TERMINAL or row.runtime_phase not in {
            "captured",
            "removed",
        }:
            raise ValueError("Canonical capture is required before Job cleanup")
        root, value = self._remote(ref)
        if root is None:
            return
        if root.uid is None:
            return
        pods = self._pods(ref, root)
        if not self._payloads_intact(ref, root):
            raise ValueError("Cleanup payload identity/owner changed")
        if value is not None:
            if len(pods) == 1:
                self._verified_pod(ref, root, pods[0])
                try:
                    self.api.collector_exec(
                        pods[0]["metadata"]["name"],
                        expected_uid=pods[0]["metadata"]["uid"],
                        mode="release",
                    )
                except (KubernetesError, ValueError):
                    pass
            self._delete("batch", "job", value)
            if self._read("batch", "job", root.name) is not None or self._pods(
                ref, root
            ):
                raise RuntimePending("Waiting for owned Job and Pods to terminate")
        elif pods:
            raise RuntimePending("Owned action Pods have not terminated")
        with self.resources.sessions() as db:
            rows = db.scalars(
                select(RuntimeResourceBinding).where(
                    RuntimeResourceBinding.job_id == ref.job_id,
                    RuntimeResourceBinding.project_id == ref.project_id,
                    RuntimeResourceBinding.target_revision == self.target.revision,
                )
            ).all()
        for binding in rows:
            if binding.kind not in {"Secret", "ConfigMap"}:
                continue
            resource = "secret" if binding.kind == "Secret" else "config_map"
            remote = self._read("core", resource, binding.name)
            if remote is not None:
                owners = remote["metadata"].get("ownerReferences", [])
                if (
                    remote["metadata"]["uid"] != binding.uid
                    or len(owners) != 1
                    or owners[0].get("uid") != root.uid
                ):
                    raise ValueError("Cleanup resource identity/owner changed")
                self._delete("core", resource, remote)
            if binding.phase != "removed":
                self.resources.record_phase(
                    binding.kind,
                    binding.logical_id,
                    phase="removed",
                    expected_phase=binding.phase,
                    claim=self.claim,
                )
        current = self.resources.get("Job", ref.job_id)
        if current.phase != "removed":
            self.resources.record_phase(
                "Job",
                ref.job_id,
                phase="removed",
                expected_phase=current.phase,
                claim=self.claim,
            )
