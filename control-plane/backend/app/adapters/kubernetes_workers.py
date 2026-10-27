"""Persistent, UID-bound shared workers; configuration stays in PostgreSQL."""

import base64
import copy
import hashlib
import json

from app.adapters.kubernetes_api import KubernetesApi, KubernetesError, NAMESPACE_FENCE
from app.adapters.kubernetes_job_manifests import manifest_fingerprint
from app.ports import RuntimePending
from app.runtime_targets import TargetSnapshot, digest
from app.security import redact

OWNER = "flumic.io/worker-id"
INTENT = "flumic.io/intent"


def matches_manifest(expected, actual):
    """Compare exact prepared authority after known Kubernetes API defaults."""
    return manifest_fingerprint(expected) == manifest_fingerprint(actual)


class KubernetesWorkerRuntime:
    def __init__(
        self, settings, config_service, target, resources, *, api=None, claim=None
    ):
        self.settings, self.config_service = settings, config_service
        self.target = TargetSnapshot.model_validate(target.model_dump())
        self.config, self.resources, self.claim = (
            self.target.configuration,
            resources,
            claim,
        )
        if self.target.kind != "kubernetes" or resources.snapshot != self.target:
            raise ValueError("Worker target authority mismatch")
        self.api = api or KubernetesApi(self.target)
        self._tokens = set()

    def _call(self, group, operation, resource, **kwargs):
        return self.api.call(
            group,
            operation + "_namespaced_" + resource,
            namespace_kind="worker",
            **kwargs,
        )

    def _read(self, group, resource, name):
        try:
            return self._call(group, "read", resource, name=name)
        except KubernetesError as exc:
            if exc.status != 404:
                raise
            return None

    def _name(self, worker_id):
        return (
            "flumic-worker-"
            + hashlib.sha256(
                (self.target.revision + ":" + worker_id).encode()
            ).hexdigest()[:24]
        )

    def _labels(self, worker_id):
        return {
            "flumic.io/deployment": hashlib.sha256(
                self.config["deployment_id"].encode()
            ).hexdigest()[:32],
            "flumic.io/worker": hashlib.sha256(worker_id.encode()).hexdigest()[:32],
        }

    def _metadata(self, name, worker_id, intent):
        return {
            "name": name,
            "namespace": self.config["worker_namespace"],
            "labels": self._labels(worker_id),
            "annotations": {
                OWNER: worker_id,
                INTENT: intent,
                NAMESPACE_FENCE: self.config["worker_namespace_uid"],
            },
        }

    def _ensure(
        self, group, resource, kind, logical_id, body, worker_id, owner_uid=None
    ):
        binding = self.resources.get(kind, logical_id)
        value = self._read(group, resource, body["metadata"]["name"])
        spec = digest(body)
        if binding is not None:
            if (
                value is None
                or value["metadata"]["uid"] != binding.uid
                or binding.spec_digest != spec
            ):
                raise ValueError("Worker resource identity changed")
        elif value is None:
            value = self._call(group, "create", resource, body=body)
        if not matches_manifest(body, value) or value["metadata"].get(
            "ownerReferences", []
        ) != body["metadata"].get("ownerReferences", []):
            raise ValueError("Worker resource does not match prepared intent")
        return self.resources.bind(
            kind,
            logical_id,
            namespace=self.config["worker_namespace"],
            name=body["metadata"]["name"],
            uid=value["metadata"]["uid"],
            spec_digest=spec,
            owner_uid=owner_uid,
            worker_id=worker_id,
            claim=self.claim,
            document=body if kind == "Deployment" else None,
        )

    def _bodies(self, worker, document, token):
        self._tokens.add(token)
        if (
            worker.protocol != "shared-v1"
            or document.get("protocol") != "shared-v1"
            or document.get("worker_id") != worker.id
            or document.get("pool_id") != worker.pool_id
        ):
            raise ValueError("Kubernetes workers require the shared protocol")
        if (
            worker.target_id != self.target.id
            or worker.target_snapshot != self.target.model_dump()
        ):
            raise ValueError("Worker target pin mismatch")
        role = document.get("role")
        if role not in ("simple", "developer"):
            raise ValueError("Unknown shared worker role")
        pricing_path = self.settings.root / "observability/model-pricing.json"
        pricing = pricing_path.read_text() if pricing_path.exists() else "{}"
        if len(pricing.encode()) > 512 * 1024:
            raise ValueError("Worker pricing document exceeds transport limit")
        json.loads(pricing)
        name = self._name(worker.id)
        version = digest(
            {
                "profile": document,
                "token": token,
                "pricing": pricing,
                "image": self.config["images"][role],
            }
        )
        payload_name = name + "-" + version[:12]
        secret = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": self._metadata(payload_name, worker.id, version),
            "immutable": True,
            "type": "Opaque",
            "data": {"control-token": base64.b64encode(token.encode()).decode()},
        }
        config = {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": self._metadata(payload_name, worker.id, version),
            "immutable": True,
            "data": {"model-pricing.json": pricing},
        }
        pvc = {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "metadata": self._metadata(name, worker.id, "retained-spool"),
            "spec": {
                "accessModes": ["ReadWriteOnce"],
                "storageClassName": self.config["storage_class"],
                "resources": {
                    "requests": {"storage": str(self.config["storage_mib"]) + "Mi"}
                },
            },
        }
        env = [
            {"name": k, "value": v}
            for k, v in {
                "FACTORY_CONTROL_ENDPOINT": self.config["worker_endpoint"],
                "FACTORY_CONTROL_WORKER_ID": worker.id,
                "FACTORY_WORKER_PROTOCOL": "shared-v1",
                "FACTORY_WORKER_POOL_ID": worker.pool_id,
                "FACTORY_SHARED_HISTORY_ROOT": "/data/control-spool",
            }.items()
        ]
        env += [
            {
                "name": "FACTORY_CONTROL_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {"name": payload_name, "key": "control-token"}
                },
            },
            {
                "name": "HATCHET_CLIENT_TOKEN",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": self.config["hatchet_secret_name"],
                        "key": "HATCHET_CLIENT_TOKEN",
                    }
                },
            },
        ]
        for key in (
            "HATCHET_CLIENT_HOST_PORT",
            "HATCHET_CLIENT_SERVER_URL",
            "HATCHET_CLIENT_TLS_STRATEGY",
        ):
            env.append(
                {
                    "name": key,
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": self.config["hatchet_secret_name"],
                            "key": key,
                            "optional": True,
                        }
                    },
                }
            )
        pod = {
            "automountServiceAccountToken": False,
            "terminationGracePeriodSeconds": 30,
            "containers": [
                {
                    "name": "worker",
                    "image": self.config["images"][role],
                    "env": env,
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "2", "memory": "2Gi"},
                    },
                    "securityContext": {
                        "allowPrivilegeEscalation": False,
                        "capabilities": {"drop": ["ALL"]},
                    },
                    "volumeMounts": [
                        {
                            "name": "spool",
                            "mountPath": "/data/control-spool/" + worker.id,
                        },
                        {
                            "name": "pricing",
                            "mountPath": "/observability",
                            "readOnly": True,
                        },
                    ],
                }
            ],
            "volumes": [
                {"name": "spool", "persistentVolumeClaim": {"claimName": name}},
                {"name": "pricing", "configMap": {"name": payload_name}},
            ],
            "nodeSelector": self.config["node_selector"],
            "tolerations": self.config["tolerations"],
            "imagePullSecrets": [
                {"name": n} for n in self.config["image_pull_secrets"]
            ],
        }
        deployment = {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": self._metadata(name, worker.id, version),
            "spec": {
                "replicas": 0,
                "strategy": {"type": "Recreate"},
                "selector": {"matchLabels": self._labels(worker.id)},
                "template": {
                    "metadata": {
                        "labels": self._labels(worker.id),
                        "annotations": {
                            NAMESPACE_FENCE: self.config["worker_namespace_uid"],
                            OWNER: worker.id,
                            INTENT: version,
                        },
                    },
                    "spec": pod,
                },
            },
        }
        return deployment, pvc, config, secret

    def create(self, worker, document, token):
        bodies = self._bodies(worker, document, token)
        deployment = bodies[0]
        # Existing replicas are lifecycle state, not a replacement configuration.
        binding = self.resources.get("Deployment", worker.id)
        if binding is not None:
            value = self._deployment(worker.id)
            check = copy.deepcopy(value)
            check["spec"]["replicas"] = 0
            if (
                not matches_manifest(deployment, check)
                or digest(deployment) != binding.spec_digest
            ):
                raise ValueError(
                    "Worker configuration requires a fenced configure operation"
                )
            root = binding
        else:
            root = self._ensure(
                "apps", "deployment", "Deployment", worker.id, deployment, worker.id
            )
        self._payloads(worker.id, root, bodies)
        if root.phase == "prepared":
            self.resources.record_phase(
                "Deployment",
                worker.id,
                expected_phase="prepared",
                phase="payload-ready",
                claim=self.claim,
            )
        return root.uid

    def _payloads(self, worker_id, root, bodies):
        for group, resource, kind, body in (
            ("core", "persistent_volume_claim", "PersistentVolumeClaim", bodies[1]),
            ("core", "config_map", "ConfigMap", bodies[2]),
            ("core", "secret", "Secret", bodies[3]),
        ):
            owner = None
            if kind != "PersistentVolumeClaim":
                owner = root.uid
                body["metadata"]["ownerReferences"] = [
                    {
                        "apiVersion": "apps/v1",
                        "kind": "Deployment",
                        "name": root.name,
                        "uid": root.uid,
                        "controller": True,
                        "blockOwnerDeletion": False,
                    }
                ]
            logical_id = (
                worker_id
                if kind == "PersistentVolumeClaim"
                else body["metadata"]["name"]
            )
            self._ensure(group, resource, kind, logical_id, body, worker_id, owner)

    def _validate_pod(self, value):
        pod = value["spec"]["template"]["spec"]
        if any(
            pod.get(k)
            for k in (
                "hostNetwork",
                "hostPID",
                "hostIPC",
                "initContainers",
                "ephemeralContainers",
            )
        ):
            raise ValueError("Worker Pod has unexpected authority")
        for container in pod["containers"]:
            if any(
                container.get(k) for k in ("envFrom", "command", "args")
            ) or container.get("securityContext", {}).get("privileged"):
                raise ValueError("Worker Pod has unexpected credentials or privilege")

    def _deployment(self, worker_id, missing_ok=False):
        binding = self.resources.get("Deployment", worker_id)
        if binding is None:
            raise ValueError("Worker Deployment has no durable binding")
        value = self._read("apps", "deployment", binding.name)
        if value is None and missing_ok:
            return None
        if (
            value is None
            or value["metadata"]["uid"] != binding.uid
            or value["metadata"].get("ownerReferences")
            or value["metadata"].get("annotations", {}).get(OWNER) != worker_id
        ):
            raise ValueError("Worker Deployment identity changed")
        documents = binding.details.get("specs", {})
        actual = copy.deepcopy(value)
        actual["spec"]["replicas"] = 0
        if not any(
            key in (binding.spec_digest, binding.pending_spec_digest)
            and digest(document) == key
            and matches_manifest(document, actual)
            for key, document in documents.items()
        ):
            raise ValueError("Worker Deployment spec does not match prepared authority")
        self._validate_pod(value)
        return value

    def _pods(self, worker_id, deployment):
        items = self._call(
            "core",
            "list",
            "pod",
            label_selector=",".join(
                k + "=" + v for k, v in self._labels(worker_id).items()
            ),
            limit=100,
        )["items"]
        for pod in items:
            owners = [
                r
                for r in pod["metadata"].get("ownerReferences", [])
                if r.get("controller") and r.get("kind") == "ReplicaSet"
            ]
            if len(owners) != 1:
                raise ValueError("Worker Pod controller is not verified")
            rs = self._read("apps", "replica_set", owners[0]["name"])
            if (
                rs is None
                or rs["metadata"]["uid"] != owners[0]["uid"]
                or not any(
                    r.get("controller")
                    and r.get("kind") == "Deployment"
                    and r.get("uid") == deployment["metadata"]["uid"]
                    for r in rs["metadata"].get("ownerReferences", [])
                )
            ):
                raise ValueError("Worker Pod belongs to another Deployment")
        return items

    def _patch(self, value, operations):
        tests = [
            {"op": "test", "path": "/metadata/" + key, "value": value["metadata"][key]}
            for key in ("uid", "resourceVersion")
        ]
        return self._call(
            "apps",
            "patch",
            "deployment",
            name=value["metadata"]["name"],
            body=tests + operations,
        )

    def _scale(self, worker_id, replicas):
        value = self._deployment(worker_id)
        binding = self.resources.get("Deployment", worker_id)
        if replicas and binding.phase not in ("payload-ready", "running"):
            raise ValueError("Worker payload has not been prepared")
        if replicas:
            self._dependencies(worker_id, value)
        if value["spec"]["replicas"] != replicas:
            value = self._patch(
                value, [{"op": "replace", "path": "/spec/replicas", "value": replicas}]
            )
        status = value.get("status", {})
        if status.get("observedGeneration", 0) < value["metadata"]["generation"] or (
            not replicas and (status.get("replicas", 0) or self._pods(worker_id, value))
        ):
            raise RuntimePending("Waiting for worker Deployment observation")
        return value

    def _dependencies(self, worker_id, value, *, spool_only=False):
        pod = value["spec"]["template"]["spec"]
        references = [("PersistentVolumeClaim", "persistent_volume_claim", worker_id)]
        if not spool_only:
            references.extend(
                ("ConfigMap", "config_map", volume["configMap"]["name"])
                for volume in pod["volumes"]
                if "configMap" in volume
            )
            references.extend(
                ("Secret", "secret", env["valueFrom"]["secretKeyRef"]["name"])
                for env in pod["containers"][0]["env"]
                if env["name"] == "FACTORY_CONTROL_TOKEN"
            )
        for kind, resource, logical_id in references:
            binding = self.resources.get(kind, logical_id)
            if binding is None:
                raise ValueError("Worker dependency has no durable authority")
            remote = self._read("core", resource, binding.name)
            if remote is None or remote["metadata"]["uid"] != binding.uid:
                raise ValueError("Worker dependency identity changed")
            owners = remote["metadata"].get("ownerReferences", [])
            if kind == "PersistentVolumeClaim":
                if owners:
                    raise ValueError("Retained worker spool has an unexpected owner")
            elif not (
                len(owners) == 1
                and owners[0].get("kind") == "Deployment"
                and owners[0].get("uid") == value["metadata"]["uid"]
                and owners[0].get("controller")
            ):
                raise ValueError("Worker dependency owner changed")

    def action(self, worker_id, action):
        if action in ("start", "stop"):
            self._scale(worker_id, int(action == "start"))
        elif action == "remove":
            value = self._deployment(worker_id, missing_ok=True)
            if value is None:
                self._removed(worker_id)
                return
            value = self._scale(worker_id, 0)
            self._dependencies(worker_id, value, spool_only=True)
            self._call(
                "apps",
                "delete",
                "deployment",
                name=value["metadata"]["name"],
                body={
                    "preconditions": {"uid": value["metadata"]["uid"]},
                    "propagationPolicy": "Foreground",
                },
            )
            if self._deployment(worker_id, missing_ok=True) is not None:
                raise RuntimePending(
                    "Waiting for worker Deployment deletion; spool retained"
                )
            self._removed(worker_id)
        else:
            raise ValueError("Unsupported Kubernetes worker action")

    def _removed(self, worker_id):
        root = self.resources.get("Deployment", worker_id)
        if root.phase != "removed":
            self.resources.record_phase(
                "Deployment",
                worker_id,
                phase="removed",
                expected_phase=root.phase,
                claim=self.claim,
            )

    def configure(self, worker, document, token):
        value = self._scale(worker.id, 0)
        bodies = self._bodies(worker, document, token)
        root = self.resources.get("Deployment", worker.id)
        intended = digest(bodies[0])
        self.resources.prepare_change(
            "Deployment",
            worker.id,
            expected_spec=root.spec_digest,
            new_spec=intended,
            claim=self.claim,
            document=bodies[0],
        )
        self._payloads(worker.id, root, bodies)
        if (
            value["metadata"].get("annotations", {}).get(INTENT)
            != bodies[0]["metadata"]["annotations"][INTENT]
        ):
            value = self._patch(
                value,
                [
                    {"op": "replace", "path": "/spec", "value": bodies[0]["spec"]},
                    {
                        "op": "replace",
                        "path": "/metadata/annotations",
                        "value": bodies[0]["metadata"]["annotations"],
                    },
                ],
            )
        if not matches_manifest(bodies[0], value):
            raise ValueError("Worker configuration observation does not match intent")
        if (
            value.get("status", {}).get("observedGeneration", 0)
            < value["metadata"]["generation"]
        ):
            raise RuntimePending("Waiting for configured worker Deployment")
        self.resources.commit_change(
            "Deployment", worker.id, new_spec=intended, claim=self.claim
        )
        return root.uid

    def list(self):
        from app.database import RuntimeResourceBinding
        from sqlalchemy import select

        with self.resources.sessions() as db:
            rows = db.scalars(
                select(RuntimeResourceBinding).where(
                    RuntimeResourceBinding.target_id == self.target.id,
                    RuntimeResourceBinding.target_revision == self.target.revision,
                    RuntimeResourceBinding.kind == "Deployment",
                    *(
                        [RuntimeResourceBinding.worker_id == self.resources.worker_id]
                        if self.resources.worker_id is not None
                        else []
                    ),
                )
            ).all()
        result = []
        for row in rows:
            value = self._deployment(row.worker_id, missing_ok=True)
            if value is None:
                if row.phase == "removed":
                    spool = self.resources.get("PersistentVolumeClaim", row.worker_id)
                    result.append(
                        {
                            "id": row.uid,
                            "worker_id": row.worker_id,
                            "target_id": self.target.id,
                            "namespace": row.namespace,
                            "status": "removed",
                            "spool_retained": spool is not None,
                            "spool_uid": spool.uid if spool else None,
                        }
                    )
                continue
            pods = self._pods(row.worker_id, value)
            status = (
                "stopped" if value["spec"]["replicas"] == 0 and not pods else "pending"
            )
            for pod in pods:
                if (
                    value["spec"]["replicas"] == 1
                    and not pod["metadata"].get("deletionTimestamp")
                    and pod.get("status", {}).get("phase") == "Running"
                    and any(
                        container.get("name") == "worker"
                        and container.get("ready") is True
                        and "running" in container.get("state", {})
                        for container in pod.get("status", {}).get(
                            "containerStatuses", []
                        )
                    )
                ):
                    status = "running"
                conditions = pod.get("status", {}).get("conditions", [])
                if any(
                    c.get("type") == "PodScheduled" and c.get("status") == "False"
                    for c in conditions
                ):
                    status = "unschedulable"
                for container in pod.get("status", {}).get("containerStatuses", []):
                    reason = container.get("state", {}).get("waiting", {}).get("reason")
                    if reason in ("ImagePullBackOff", "ErrImagePull"):
                        status = "image-pull-backoff"
                    elif reason == "CrashLoopBackOff":
                        status = "crash-loop-backoff"
            result.append(
                {
                    "id": row.uid,
                    "worker_id": row.worker_id,
                    "target_id": self.target.id,
                    "namespace": row.namespace,
                    "deployment_uid": row.uid,
                    "pod_uids": [p["metadata"]["uid"] for p in pods],
                    "image": value["spec"]["template"]["spec"]["containers"][0][
                        "image"
                    ],
                    "status": status,
                }
            )
        return result

    def logs(self, runtime_id, limit):
        for value in self.list():
            if runtime_id not in (value["id"], value["worker_id"]):
                continue
            deployment = self._deployment(value["worker_id"])
            logs = []
            for pod in self._pods(value["worker_id"], deployment):
                text, truncated = self.api.pod_logs(
                    pod["metadata"]["name"],
                    expected_uid=pod["metadata"]["uid"],
                    namespace_kind="worker",
                    container="worker",
                    limit=65536,
                    tail_lines=min(max(limit, 0), 500),
                )
                from app.reconciler import worker_token

                tokens = self._tokens | {
                    worker_token(self.settings, value["worker_id"])
                }
                if truncated:
                    from app.security import redact_truncated_secret_tail

                    text = redact_truncated_secret_tail(text, tokens)
                for secret in tokens:
                    if secret:
                        text = text.replace(secret, "[REDACTED]")
                logs.append(redact(text) + ("\n[TRUNCATED]" if truncated else ""))
            return "\n".join(logs).encode()[:65536].decode("utf-8", errors="ignore")
        raise ValueError("Unknown worker runtime identity")
