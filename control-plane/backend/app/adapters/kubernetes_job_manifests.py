"""Pure bounded Kubernetes manifests. This module has no cluster authority."""

import base64
import copy
import hashlib
from decimal import Decimal

from app.adapters.kubernetes_api import NAMESPACE_FENCE
from app.runtime_targets import TargetSnapshot, digest
from factory.workflows.references import json_bytes

CHUNK_BYTES = 196608
HELPER_COMMAND = ["python", "-I", "-B", "/opt/flumic/flumic_job_helper.py"]
JOB_ID = "flumic.io/job-id"


def pinned(launch, target):
    target = TargetSnapshot.model_validate(target.model_dump())
    if (
        target.kind != "kubernetes"
        or launch.target_snapshot != target.model_dump()
        or launch.target_id != target.id
        or launch.target_revision != target.revision
    ):
        raise ValueError("Job target authority mismatch")
    return target.configuration


def job_name(launch, target):
    return (
        "flumic-job-" + digest({"target": target.revision, "job": launch.job_id})[:40]
    )


def labels(launch, config):
    return {
        "flumic.io/deployment": hashlib.sha256(
            config["deployment_id"].encode()
        ).hexdigest()[:32],
        "flumic.io/job": launch.job_id[:40],
        "flumic.io/project": hashlib.sha256(launch.project_id.encode()).hexdigest()[
            :32
        ],
        "flumic.io/action-network": launch.network,
    }


def metadata(launch, target, name):
    config = pinned(launch, target)
    return {
        "name": name,
        "namespace": config["job_namespace"],
        "labels": labels(launch, config),
        "annotations": {
            NAMESPACE_FENCE: config["job_namespace_uid"],
            JOB_ID: launch.job_id,
            "flumic.io/project-id": launch.project_id,
            "flumic.io/execution-id": launch.execution_id,
            "flumic.io/spec-digest": launch.spec_digest,
            "flumic.io/target-revision": target.revision,
        },
    }


def payload_resources(launch, owner_uid, target):
    if not isinstance(owner_uid, str) or not 1 <= len(owner_uid) <= 128:
        raise ValueError("Verified Job UID is required for payloads")
    name = job_name(launch, target)
    raw = json_bytes(launch.inputs)
    context = json_bytes(launch.context)
    if len(raw) > 1048576 or len(context) > 16384:
        raise ValueError("Job input transport exceeds bounds")
    chunks = [
        raw[index : index + CHUNK_BYTES] for index in range(0, len(raw), CHUNK_BYTES)
    ]
    if not 1 <= len(chunks) <= 8:
        raise ValueError("Job input chunk count exceeds bounds")
    owner = [
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "name": name,
            "uid": owner_uid,
            "controller": True,
            "blockOwnerDeletion": False,
        }
    ]
    resources = []
    for index, chunk in enumerate(chunks):
        resources.append(
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": metadata(launch, target, name + "-chunk-" + str(index)),
                "immutable": True,
                "binaryData": {"chunk-" + str(index): base64.b64encode(chunk).decode()},
            }
        )
    proof = {
        "version": 1,
        "count": len(chunks),
        "length": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "chunks": [
            {
                "index": i,
                "length": len(chunk),
                "sha256": hashlib.sha256(chunk).hexdigest(),
            }
            for i, chunk in enumerate(chunks)
        ],
    }
    if launch.context:
        proof["context"] = launch.context
    resources.append(
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": metadata(launch, target, name + "-manifest"),
            "immutable": True,
            "data": {"manifest.json": json_bytes(proof).decode()},
        }
    )
    resources.append(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": metadata(launch, target, name + "-grants"),
            "type": "Opaque",
            "immutable": True,
            "data": {
                "credentials.json": base64.b64encode(
                    json_bytes(launch.credentials)
                ).decode()
            },
        }
    )
    for resource in resources:
        resource["metadata"]["ownerReferences"] = copy.deepcopy(owner)
    return resources


def job_manifest(launch, target, *, suspended=True):
    config = pinned(launch, target)
    if (
        not config["network_qualified"]
        or not config["network_evidence"]
        or launch.network not in config["network_policy_names"]
        or not config["pid_qualified"]
        or not config["pid_evidence"]
        or launch.limits.pids != config["pid_limit"]
    ):
        raise ValueError("Kubernetes action runtime prerequisites are unqualified")
    name = job_name(launch, target)
    count = max(1, (len(json_bytes(launch.inputs)) + CHUNK_BYTES - 1) // CHUNK_BYTES)
    sources = [
        {
            "configMap": {
                "name": name + "-chunk-" + str(i),
                "items": [{"key": "chunk-" + str(i), "path": "chunk-" + str(i)}],
            }
        }
        for i in range(count)
    ]
    sources.append(
        {
            "configMap": {
                "name": name + "-manifest",
                "items": [{"key": "manifest.json", "path": "manifest.json"}],
            }
        }
    )
    security = {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "readOnlyRootFilesystem": True,
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    action_mounts = [
        {
            "name": "stage",
            "mountPath": "/flumic/input.json",
            "subPath": "input.json",
            "readOnly": True,
        },
        {
            "name": "credentials",
            "mountPath": "/flumic/credentials.json",
            "subPath": "credentials.json",
            "readOnly": True,
        },
        {"name": "output", "mountPath": "/flumic/output"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    if launch.context:
        action_mounts.append(
            {
                "name": "stage",
                "mountPath": "/flumic/context.json",
                "subPath": "context.json",
                "readOnly": True,
            }
        )

    def helper(mode, mounts, memory):
        return {
            "name": "prepare" if mode == "init" else "collector",
            "image": config["images"]["helper"],
            "command": HELPER_COMMAND + [mode],
            "securityContext": copy.deepcopy(security),
            "resources": {
                "requests": {
                    "cpu": str(config["helper_cpu_millicores"]) + "m",
                    "memory": str(memory) + "Mi",
                },
                "limits": {
                    "cpu": str(config["helper_cpu_millicores"]) + "m",
                    "memory": str(memory) + "Mi",
                },
            },
            "volumeMounts": mounts,
        }

    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": metadata(launch, target, name),
        "spec": {
            "suspend": bool(suspended),
            "backoffLimit": 0,
            "podReplacementPolicy": "Failed",
            "template": {
                "metadata": {
                    "labels": labels(launch, config),
                    "annotations": {
                        NAMESPACE_FENCE: config["job_namespace_uid"],
                        JOB_ID: launch.job_id,
                    },
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "shareProcessNamespace": False,
                    "terminationGracePeriodSeconds": 5,
                    "securityContext": {
                        "fsGroup": 10001,
                        "fsGroupChangePolicy": "OnRootMismatch",
                    },
                    "initContainers": [
                        helper(
                            "init",
                            [
                                {
                                    "name": "projection",
                                    "mountPath": "/projection",
                                    "readOnly": True,
                                },
                                {"name": "stage", "mountPath": "/stage"},
                            ],
                            config["init_memory_mib"],
                        )
                    ],
                    "containers": [
                        {
                            "name": "action",
                            "image": launch.image,
                            "command": list(launch.command),
                            "env": [],
                            "securityContext": copy.deepcopy(security),
                            "resources": {
                                "requests": {
                                    "cpu": str(launch.limits.cpu),
                                    "memory": str(launch.limits.memory_mib) + "Mi",
                                },
                                "limits": {
                                    "cpu": str(launch.limits.cpu),
                                    "memory": str(launch.limits.memory_mib) + "Mi",
                                },
                            },
                            "volumeMounts": action_mounts,
                        },
                        helper(
                            "serve",
                            [
                                {
                                    "name": "output",
                                    "mountPath": "/output",
                                    "readOnly": True,
                                },
                                {"name": "control", "mountPath": "/control"},
                            ],
                            config["collector_memory_mib"],
                        ),
                    ],
                    "volumes": [
                        {
                            "name": "projection",
                            "projected": {"defaultMode": 0o444, "sources": sources},
                        },
                        {
                            "name": "credentials",
                            "secret": {
                                "secretName": name + "-grants",
                                "defaultMode": 0o444,
                            },
                        },
                        {
                            "name": "stage",
                            "emptyDir": {"medium": "Memory", "sizeLimit": "2Mi"},
                        },
                        {
                            "name": "output",
                            "emptyDir": {"medium": "Memory", "sizeLimit": "2Mi"},
                        },
                        {
                            "name": "control",
                            "emptyDir": {"medium": "Memory", "sizeLimit": "1Mi"},
                        },
                        {
                            "name": "tmp",
                            "emptyDir": {
                                "medium": "Memory",
                                "sizeLimit": str(launch.limits.tmpfs_mib) + "Mi",
                            },
                        },
                    ],
                    "nodeSelector": config["node_selector"],
                    "tolerations": config["tolerations"],
                    "imagePullSecrets": [
                        {"name": n} for n in config["image_pull_secrets"]
                    ],
                },
            },
        },
    }


def _quantity(value, *, cpu=False):
    """Compare API-canonical quantities, without accepting new resource keys."""
    value = str(value)
    factors = (
        {"m": Decimal("0.001")}
        if cpu
        else {
            "Ki": 1024,
            "Mi": 1024**2,
            "Gi": 1024**3,
            "Ti": 1024**4,
            "K": 1000,
            "M": 1000**2,
            "G": 1000**3,
        }
    )
    for suffix, multiplier in factors.items():
        if value.endswith(suffix):
            return str((Decimal(value[: -len(suffix)]) * multiplier).normalize())
    return str(Decimal(value).normalize())


def normalized_manifest(manifest):
    """Remove known API defaults only; retain unexpected containers/env/owners."""

    def nonnull(item):
        if isinstance(item, dict):
            return {k: nonnull(v) for k, v in item.items() if v is not None}
        if isinstance(item, list):
            return [nonnull(v) for v in item]
        return item

    value = nonnull(copy.deepcopy(manifest))
    value.pop("status", None)
    meta = value.get("metadata", {})
    uid, name = meta.get("uid"), meta.get("name")
    deleting = bool(meta.get("deletionTimestamp"))
    if deleting and "finalizers" in meta:
        meta["finalizers"] = [
            item for item in meta["finalizers"] if item != "foregroundDeletion"
        ]
        if not meta["finalizers"]:
            meta.pop("finalizers")
    for key in (
        "uid",
        "resourceVersion",
        "generation",
        "creationTimestamp",
        "managedFields",
        "deletionTimestamp",
        "deletionGracePeriodSeconds",
        "selfLink",
    ):
        meta.pop(key, None)
    spec = value.get("spec", {})
    template = spec.get("template", {})
    if value.get("kind") == "Job":
        auto = {
            "batch.kubernetes.io/controller-uid": uid,
            "controller-uid": uid,
            "batch.kubernetes.io/job-name": name,
            "job-name": name,
        }
        for metadata in (meta, template.get("metadata", {})):
            for key, expected in auto.items():
                if key in metadata.get("labels", {}):
                    if expected is None or metadata["labels"][key] != expected:
                        raise ValueError("Job controller identity does not match UID")
                    metadata["labels"].pop(key)
        selector = spec.get("selector")
        if selector is not None:
            matches = selector.get("matchLabels", {})
            if (
                set(selector) != {"matchLabels"}
                or not matches
                or any(
                    k not in ("batch.kubernetes.io/controller-uid", "controller-uid")
                    or v != uid
                    for k, v in matches.items()
                )
                or uid is None
            ):
                raise ValueError("Job selector does not match controller UID")
            spec.pop("selector")
        for key, default in {
            "parallelism": 1,
            "completions": 1,
            "completionMode": "NonIndexed",
            "manualSelector": False,
        }.items():
            if spec.get(key) == default:
                spec.pop(key)
    if value.get("kind") == "Deployment":
        for key, default in {
            "revisionHistoryLimit": 10,
            "progressDeadlineSeconds": 600,
        }.items():
            if spec.get(key) == default:
                spec.pop(key)
        meta.get("annotations", {}).pop("deployment.kubernetes.io/revision", None)
    pod = template.get("spec", {})
    for key, default in {
        "dnsPolicy": "ClusterFirst",
        "schedulerName": "default-scheduler",
        "serviceAccountName": "default",
        "serviceAccount": "default",
        "enableServiceLinks": True,
        "nodeSelector": {},
        "tolerations": [],
        "imagePullSecrets": [],
        "securityContext": {},
        "restartPolicy": "Always",
    }.items():
        if pod.get(key) == default:
            pod.pop(key)
    for container in pod.get("containers", []) + pod.get("initContainers", []):
        for key, default in {
            "imagePullPolicy": "IfNotPresent",
            "terminationMessagePath": "/dev/termination-log",
            "terminationMessagePolicy": "File",
            "stdin": False,
            "stdinOnce": False,
            "tty": False,
            "env": [],
        }.items():
            if container.get(key) == default:
                container.pop(key)
        for resources in container.get("resources", {}).values():
            for key in ("cpu", "memory", "ephemeral-storage"):
                if key in resources:
                    resources[key] = _quantity(resources[key], cpu=key == "cpu")
    for volume in pod.get("volumes", []):
        for kind in ("configMap", "secret", "projected"):
            if volume.get(kind, {}).get("defaultMode") == 420:
                volume[kind].pop("defaultMode")
        if "sizeLimit" in volume.get("emptyDir", {}):
            volume["emptyDir"]["sizeLimit"] = _quantity(volume["emptyDir"]["sizeLimit"])
    if value.get("kind") == "PersistentVolumeClaim":
        spec.pop("volumeName", None)
        if spec.get("volumeMode") == "Filesystem":
            spec.pop("volumeMode")
        annotations = meta.get("annotations", {})
        for key in (
            "pv.kubernetes.io/bind-completed",
            "pv.kubernetes.io/bound-by-controller",
            "volume.beta.kubernetes.io/storage-provisioner",
            "volume.kubernetes.io/storage-provisioner",
            "volume.kubernetes.io/selected-node",
        ):
            annotations.pop(key, None)
        if meta.get("finalizers") == ["kubernetes.io/pvc-protection"]:
            meta.pop("finalizers")
        for resources in spec.get("resources", {}).values():
            if "storage" in resources:
                resources["storage"] = _quantity(resources["storage"])
    return value


def manifest_fingerprint(manifest):
    return digest(normalized_manifest(manifest))
