"""Explicitly owned local Kubernetes qualification; never reads ambient context.

This fixture cannot bootstrap/delete clusters or namespaces. An operator must
prepare and pin those first. Checkpoints contain fingerprints, not payloads.
"""

import copy
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import unittest
from urllib.parse import urlsplit

MARKER = "flumic.io/qualification"
UUID = re.compile(r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$")
NAMESPACE = re.compile(r"^flumic-i17-(?:worker|action)-[a-z0-9-]{1,24}$")
KINDS = {
    "ConfigMap",
    "Secret",
    "PersistentVolumeClaim",
    "Pod",
    "Deployment",
    "Job",
    "NetworkPolicy",
    "Service",
    "ServiceAccount",
    "Role",
    "RoleBinding",
}


@dataclass(frozen=True)
class LiveKubernetesConfig:
    kubeconfig: str = field(repr=False)
    context: str
    server: str
    cluster_uid: str
    namespace_uids: dict[str, str]
    fixture_id: str

    @classmethod
    def from_env(cls, values=None):
        values = os.environ if values is None else values
        if values.get("FLUMIC_LIVE_KUBERNETES") != "1":
            raise unittest.SkipTest(
                "Local Kubernetes qualification is explicitly opt-in"
            )
        prefix = "FLUMIC_KUBERNETES_"
        keys = (
            "KUBECONFIG",
            "CONTEXT",
            "SERVER",
            "CLUSTER_UID",
            "WORKER_NAMESPACE",
            "WORKER_NAMESPACE_UID",
            "JOB_NAMESPACE",
            "JOB_NAMESPACE_UID",
            "FIXTURE_ID",
        )
        if values.get(prefix + "ALLOW_WRITES") != "1" or any(
            not values.get(prefix + key) for key in keys
        ):
            raise ValueError(
                "Complete pinned local Kubernetes scope and write opt-in are required"
            )
        data = {key: values[prefix + key] for key in keys}
        endpoint = urlsplit(data["SERVER"])
        if (
            not Path(data["KUBECONFIG"]).is_absolute()
            or data["CONTEXT"] != "k3d-flumic-issue17"
            or endpoint.scheme != "https"
            or endpoint.hostname != "127.0.0.1"
            or not endpoint.port
            or endpoint.username
            or endpoint.password
            or endpoint.path not in ("", "/")
            or endpoint.query
            or endpoint.fragment
            or not re.fullmatch(r"issue17-[a-z0-9-]{1,32}", data["FIXTURE_ID"])
            or any(
                not UUID.fullmatch(data[key])
                for key in ("CLUSTER_UID", "WORKER_NAMESPACE_UID", "JOB_NAMESPACE_UID")
            )
            or any(
                not NAMESPACE.fullmatch(data[key])
                for key in ("WORKER_NAMESPACE", "JOB_NAMESPACE")
            )
            or data["WORKER_NAMESPACE"] == data["JOB_NAMESPACE"]
            or len(
                {
                    data[key]
                    for key in (
                        "CLUSTER_UID",
                        "WORKER_NAMESPACE_UID",
                        "JOB_NAMESPACE_UID",
                    )
                }
            )
            != 3
            or any(any(ord(c) < 32 for c in value) for value in data.values())
        ):
            raise ValueError("Invalid pinned local Kubernetes scope")
        return cls(
            data["KUBECONFIG"],
            data["CONTEXT"],
            data["SERVER"],
            data["CLUSTER_UID"],
            {
                data["WORKER_NAMESPACE"]: data["WORKER_NAMESPACE_UID"],
                data["JOB_NAMESPACE"]: data["JOB_NAMESPACE_UID"],
            },
            data["FIXTURE_ID"],
        )


def fingerprint(value):
    value = copy.deepcopy(value)
    value.pop("status", None)
    if value.get("kind") == "Pod":
        spec = value.get("spec", {})
        selected = spec.get("nodeSelector", {}).get("kubernetes.io/hostname")
        if selected and spec.get("nodeName") == selected:
            spec.pop("nodeName")
    metadata = value.get("metadata", {})
    for key in (
        "resourceVersion",
        "creationTimestamp",
        "managedFields",
        "generation",
        "deletionTimestamp",
        "deletionGracePeriodSeconds",
    ):
        metadata.pop(key, None)
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


class KubernetesLiveFixture:
    def __init__(self, config, client, *, inventory=None, checkpoint_path=None):
        self.config, self.client = config, client
        self.checkpoint_path = Path(checkpoint_path) if checkpoint_path else None
        if self.checkpoint_path and self.checkpoint_path.exists():
            if inventory is not None or self.checkpoint_path.stat().st_size > 65536:
                raise ValueError("Invalid fixture checkpoint source")
            inventory = json.loads(self.checkpoint_path.read_text())
        self.inventory = copy.deepcopy(inventory or [])
        if not isinstance(self.inventory, list) or len(self.inventory) > 64:
            raise ValueError("Fixture resource budget exceeded")
        for entry in self.inventory:
            if (
                not isinstance(entry, dict)
                or not all(isinstance(value, str) for value in entry.values())
                or set(entry)
                != {
                    "kind",
                    "namespace",
                    "name",
                    "uid",
                    "resource_version",
                    "fingerprint",
                }
                or entry["kind"] not in KINDS
                or entry["namespace"] not in config.namespace_uids
                or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", entry["name"])
                or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", entry["uid"])
                or not re.fullmatch(r"[0-9]{1,64}", entry["resource_version"])
                or not re.fullmatch(r"[a-f0-9]{64}", entry["fingerprint"])
            ):
                raise ValueError("Invalid fixture checkpoint")

    def save(self):
        if self.checkpoint_path:
            temporary = self.checkpoint_path.with_suffix(".pending")
            with open(
                temporary, "w", opener=lambda path, flags: os.open(path, flags, 0o600)
            ) as out:
                json.dump(self.inventory, out, allow_nan=False)
                out.flush()
                os.fsync(out.fileno())
            temporary.chmod(0o600)
            temporary.replace(self.checkpoint_path)

    def guard(self):
        expected = {
            "context": self.config.context,
            "server": self.config.server,
            "cluster_uid": self.config.cluster_uid,
        }
        if self.client.identity() != expected:
            raise ValueError("Pinned local cluster identity changed")
        for namespace, uid in self.config.namespace_uids.items():
            metadata = self.client.namespace(namespace).get("metadata", {})
            if (
                metadata.get("uid") != uid
                or metadata.get("labels", {}).get(MARKER) != self.config.fixture_id
            ):
                raise ValueError(
                    "Pinned fixture namespace identity or ownership changed"
                )

    def owned(self, value):
        metadata = value.get("metadata", {})
        if (
            value.get("kind") not in KINDS
            or metadata.get("namespace") not in self.config.namespace_uids
            or metadata.get("labels", {}).get(MARKER) != self.config.fixture_id
            or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,62}", metadata.get("name", ""))
        ):
            raise ValueError("Resource is outside fixture ownership")

    def bounded_pod(self, manifest):
        kind = manifest["kind"]
        if kind not in {"Pod", "Job", "Deployment"}:
            return
        spec = manifest.get("spec", {})
        if kind != "Pod":
            spec = spec.get("template", {}).get("spec", {})
        containers = spec.get("containers", []) + spec.get("initContainers", [])
        if (
            not containers
            or len(containers) > 4
            or any(spec.get(key) for key in ("hostNetwork", "hostPID", "hostIPC"))
            or any("hostPath" in volume for volume in spec.get("volumes", []))
        ):
            raise ValueError("Unbounded or host-privileged fixture Pod")
        for container in containers:
            limits = container.get("resources", {}).get("limits", {})
            security = container.get("securityContext", {})
            cpu = str(limits.get("cpu", ""))
            memory = str(limits.get("memory", ""))
            if (
                not re.fullmatch(r"[1-9][0-9]{0,3}m", cpu)
                or int(cpu[:-1]) > 1000
                or not re.fullmatch(r"[1-9][0-9]{0,3}Mi", memory)
                or int(memory[:-2]) > 512
                or security.get("privileged")
                or security.get("capabilities", {}).get("add")
            ):
                raise ValueError(
                    "Fixture Pod requires bounded CPU/memory without elevated privileges"
                )

    def record(self, value):
        self.owned(value)
        metadata = value["metadata"]
        if not metadata.get("uid") or not metadata.get("resourceVersion"):
            raise ValueError("Resource UID/version observation is required")
        return {
            "kind": value["kind"],
            "namespace": metadata["namespace"],
            "name": metadata["name"],
            "uid": metadata["uid"],
            "resource_version": metadata["resourceVersion"],
            "fingerprint": fingerprint(value),
        }

    def create(self, manifest):
        self.owned(manifest)
        self.bounded_pod(manifest)
        if (
            len(self.inventory) >= 64
            or len(json.dumps(manifest, allow_nan=False).encode()) > 1048576
        ):
            raise ValueError("Fixture resource budget exceeded")
        key = (
            manifest["kind"],
            manifest["metadata"]["namespace"],
            manifest["metadata"]["name"],
        )
        if any(
            (entry["kind"], entry["namespace"], entry["name"]) == key
            for entry in self.inventory
        ):
            raise ValueError("Fixture resource already recorded")
        self.guard()
        value = self.client.create(copy.deepcopy(manifest))
        entry = self.record(value)
        if (entry["kind"], entry["namespace"], entry["name"]) != key:
            raise ValueError("Created fixture identity mismatch")
        self.inventory.append(entry)
        self.save()
        return value

    def checkpoint(self):
        return copy.deepcopy(self.inventory)

    def observe(self, kind, namespace, name):
        """Explicit safe refresh after a status transition; never adopts spec drift."""
        self.guard()
        entry = next(
            item
            for item in self.inventory
            if (item["kind"], item["namespace"], item["name"])
            == (kind, namespace, name)
        )
        value = self.client.get(kind, namespace, name)
        current = self.record(value)
        if any(
            current[key] != entry[key]
            for key in ("kind", "namespace", "name", "uid", "fingerprint")
        ):
            raise ValueError("Fixture ownership or spec changed")
        entry["resource_version"] = current["resource_version"]
        self.save()
        return value

    def cleanup(self):
        for entry in list(reversed(self.inventory)):
            self.guard()
            value = self.client.get(entry["kind"], entry["namespace"], entry["name"])
            if self.record(value) != entry:
                raise ValueError(
                    "Fixture cleanup refused changed identity, spec, owner or version"
                )
            self.client.delete(
                entry["kind"],
                entry["namespace"],
                entry["name"],
                uid=entry["uid"],
                resource_version=entry["resource_version"],
            )
            self.inventory.remove(entry)
            self.save()


class KubectlFixtureClient:
    """Bounded subprocess adapter; JSON never goes through a shell or debug logs."""

    def __init__(self, config):
        self.config = config
        self.base = [
            "kubectl",
            "--kubeconfig",
            config.kubeconfig,
            "--context",
            config.context,
            "--request-timeout=15s",
        ]

    def run(self, args, body=None):
        result = subprocess.run(
            self.base + args,
            input=None if body is None else json.dumps(body),
            text=True,
            capture_output=True,
            timeout=25,
        )
        if result.returncode or len(result.stdout.encode()) > 8 * 1048576:
            raise ValueError("Scoped Kubernetes fixture request failed")
        try:
            return json.loads(result.stdout)
        except ValueError:
            raise ValueError("Invalid Kubernetes fixture response") from None

    def identity(self):
        config = self.run(["config", "view", "--minify", "-o", "json"])
        return {
            "context": config.get("current-context"),
            "server": config["clusters"][0]["cluster"]["server"],
            "cluster_uid": self.namespace("kube-system")["metadata"]["uid"],
        }

    def namespace(self, name):
        return self.run(["get", "namespace", name, "-o", "json"])

    def get(self, kind, namespace, name):
        return self.run(["get", kind, name, "-n", namespace, "-o", "json"])

    def create(self, manifest):
        return self.run(["create", "-f", "-", "-o", "json"], manifest)

    def delete(self, kind, namespace, name, *, uid, resource_version):
        # Native API preconditions, never an unchecked kubectl delete race.
        groups = {
            "Deployment": ("apps/v1", "deployments"),
            "Job": ("batch/v1", "jobs"),
            "NetworkPolicy": ("networking.k8s.io/v1", "networkpolicies"),
            "Role": ("rbac.authorization.k8s.io/v1", "roles"),
            "RoleBinding": ("rbac.authorization.k8s.io/v1", "rolebindings"),
        }
        version, resource = groups.get(
            kind,
            (
                "v1",
                {
                    "ConfigMap": "configmaps",
                    "Secret": "secrets",
                    "PersistentVolumeClaim": "persistentvolumeclaims",
                    "Pod": "pods",
                    "Service": "services",
                    "ServiceAccount": "serviceaccounts",
                }[kind]
                if kind not in groups
                else "",
            ),
        )
        prefix = "/api/" if version == "v1" else "/apis/"
        return self.run(
            [
                "delete",
                "--raw",
                prefix
                + version
                + "/namespaces/"
                + namespace
                + "/"
                + resource
                + "/"
                + name,
                "-f",
                "-",
            ],
            {
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "gracePeriodSeconds": 0,
                "preconditions": {"uid": uid, "resourceVersion": resource_version},
            },
        )


def acquire_fixture_lock(path):
    """Serialize local qualification processes sharing one pinned inventory.

    This is only a test-harness lock. Product controller authority remains DB leases.
    """
    import fcntl

    path = Path(path)
    if not path.is_absolute():
        raise ValueError("An absolute private fixture lock path is required")
    handle = open(
        path, "a", opener=lambda filename, flags: os.open(filename, flags, 0o600)
    )
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise ValueError(
            "Another live qualification process owns this fixture"
        ) from None
    return handle
