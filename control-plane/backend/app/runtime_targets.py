"""Operator-owned runtime authority. This boundary never loads a cluster client."""

import hashlib
import json
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator, field_validator
from factory.workflows.contracts import Contract
from factory.workflows.definitions import DockerStage

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,79}$")]
Namespace = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")]
UID = Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]
Reference = Annotated[str, Field(pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")]
Image = Annotated[
    str,
    Field(
        max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$"
    ),
]


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


class TargetAuth(Contract):
    mode: Literal["kubeconfig", "incluster"]
    kubeconfig: str | None = Field(
        default=None, min_length=1, max_length=4096, repr=False
    )
    context: str | None = Field(default=None, min_length=1, max_length=253, repr=False)

    @model_validator(mode="after")
    def explicit(self):
        if self.mode == "kubeconfig":
            if (
                not self.kubeconfig
                or not Path(self.kubeconfig).is_absolute()
                or not self.context
            ):
                raise ValueError("Explicit kubeconfig and context are required")
        elif self.kubeconfig is not None or self.context is not None:
            raise ValueError("In-cluster authentication cannot use a context")
        if any(c in (self.kubeconfig or "") + (self.context or "") for c in "\r\n\0"):
            raise ValueError("Invalid authentication reference")
        return self


class TargetImages(Contract):
    simple: Image
    developer: Image
    helper: Image

    @field_validator("simple", "developer", "helper")
    @classmethod
    def registry_reference(cls, value):
        if "://" in value:
            raise ValueError("An OCI image reference cannot be a URL")
        return value


class Toleration(Contract):
    key: str = Field(default="", max_length=253)
    operator: Literal["Exists", "Equal"] = "Equal"
    value: str = Field(default="", max_length=63)
    effect: Literal["NoSchedule", "PreferNoSchedule", "NoExecute"] | None = None
    tolerationSeconds: int | None = Field(default=None, ge=0, le=86400)

    @model_validator(mode="after")
    def policy(self):
        if self.operator == "Exists" and self.value:
            raise ValueError("Exists tolerations cannot have a value")
        if self.tolerationSeconds is not None and self.effect != "NoExecute":
            raise ValueError("Toleration seconds require NoExecute")
        return self


class KubernetesTarget(Contract):
    id: Slug
    name: str = Field(min_length=1, max_length=120)
    kind: Literal["kubernetes"]
    deployment_id: Slug
    auth: TargetAuth = Field(repr=False)
    worker_namespace: Namespace
    worker_namespace_uid: UID
    job_namespace: Namespace
    job_namespace_uid: UID
    images: TargetImages
    storage_class: Reference
    worker_endpoint: str = Field(min_length=1, max_length=2048)
    image_pull_secrets: list[Namespace] = Field(default_factory=list, max_length=16)
    node_selector: dict[str, str] = Field(default_factory=dict, max_length=32)
    tolerations: list[Toleration] = Field(default_factory=list, max_length=32)
    network_policy_names: dict[Literal["none", "bridge"], Namespace] = Field(
        default_factory=dict
    )
    network_qualified: bool = False
    network_evidence: str = Field(default="", max_length=120)
    pid_limit: int = Field(default=128, ge=16, le=512)
    pid_qualified: bool = False
    pid_evidence: str = Field(default="", max_length=120)
    storage_mib: int = Field(default=256, ge=64, le=1048576)
    init_memory_mib: int = Field(default=64, ge=32, le=512)
    collector_memory_mib: int = Field(default=96, ge=32, le=512)
    helper_cpu_millicores: int = Field(default=100, ge=10, le=1000)
    hatchet_secret_name: Namespace = "flumic-hatchet-transport"

    @model_validator(mode="after")
    def authority(self):
        if self.id == "local-docker":
            raise ValueError("The Docker target ID is reserved")
        if (
            self.worker_namespace == self.job_namespace
            or self.worker_namespace_uid == self.job_namespace_uid
        ):
            raise ValueError("Workers and action jobs require separate namespaces")
        url = urlsplit(self.worker_endpoint)
        if (
            url.scheme not in {"http", "https"}
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
        ):
            raise ValueError("Invalid Control Pane endpoint")
        if any(c in self.worker_endpoint for c in "\r\n\0"):
            raise ValueError("Invalid Control Pane endpoint")
        for key, value in self.node_selector.items():
            if (
                not key
                or len(key) > 253
                or len(value) > 63
                or any(ord(c) < 32 for c in key + value)
            ):
                raise ValueError("Invalid node selector")
        if any(
            ord(c) < 32 for c in self.name + self.network_evidence + self.pid_evidence
        ):
            raise ValueError("Invalid target metadata")
        return self


class TargetDocument(Contract):
    schema_version: Literal[1]
    targets: list[KubernetesTarget] = Field(default_factory=list, max_length=1)

    @field_validator("schema_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Runtime schema version must be an integer")
        return value


class TargetSnapshot(Contract):
    id: Slug
    kind: Literal["docker", "kubernetes"]
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    configuration: dict

    @model_validator(mode="after")
    def authority(self):
        if len(json.dumps(self.configuration, allow_nan=False).encode()) > 65536:
            raise ValueError("Target snapshot exceeds limit")
        if (
            self.configuration.get("id") != self.id
            or self.configuration.get("kind") != self.kind
            or digest(self.configuration) != self.revision
        ):
            raise ValueError("Target snapshot authority mismatch")
        if self.kind == "kubernetes":
            KubernetesTarget.model_validate(self.configuration)
        elif self.id != "local-docker":
            raise ValueError("Unknown Docker target")
        return self


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise ValueError("Duplicate target setting")
        result[key] = value
    return result


class TargetRegistry:
    def __init__(self, settings):
        docker = {
            "id": "local-docker",
            "kind": "docker",
            "name": "Local Docker",
            "deployment_id": "docker-"
            + hashlib.sha256(
                f"{settings.compose_project_name}:{settings.host_root}".encode()
            ).hexdigest()[:24],
            "worker_endpoint": settings.worker_endpoint,
            "job_bridge": settings.job_bridge,
        }
        configs = [docker]
        path = getattr(settings, "runtime_targets_file", None)
        if path is not None:
            try:
                with Path(path).open("rb") as stream:
                    raw = stream.read(1048577)
                if len(raw) > 1048576:
                    raise ValueError("Target file exceeds limit")
                document = TargetDocument.model_validate(
                    json.loads(raw, object_pairs_hook=_pairs)
                )
                configs.extend(t.model_dump(mode="json") for t in document.targets)
            except (OSError, ValueError, RecursionError):
                raise ValueError("Invalid runtime target configuration") from None
        # Keep private immutable serialized copies; callers get independent pins.
        self._pins = {
            c["id"]: TargetSnapshot(
                id=c["id"], kind=c["kind"], revision=digest(c), configuration=c
            ).model_dump_json()
            for c in configs
        }

    def snapshot(self, target_id):
        if target_id not in self._pins:
            raise ValueError("Unknown deployment target")
        return TargetSnapshot.model_validate_json(self._pins[target_id])

    def require_snapshot(self, snapshot):
        try:
            actual = TargetSnapshot.model_validate(snapshot.model_dump())
            current = self.snapshot(actual.id)
            if (
                current.revision != actual.revision
                or current.configuration != actual.configuration
            ):
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError("Runtime target pin is no longer available") from None
        return current

    def resolve_worker(self, worker):
        """Decode private worker pins once, with uniform safe failure messages."""
        try:
            if worker.target_snapshot:
                snapshot = self.require_snapshot(
                    TargetSnapshot.model_validate(worker.target_snapshot)
                )
                if snapshot.id != worker.target_id:
                    raise ValueError()
            else:
                if worker.target_id != "local-docker":
                    raise ValueError()
                snapshot = self.snapshot("local-docker")
            if snapshot.kind == "kubernetes" and worker.protocol != "shared-v1":
                raise ValueError()
            return snapshot
        except (ValueError, AttributeError):
            raise ValueError("Worker runtime target pin is not available") from None

    def require_stage(self, snapshot, stage, *, protocol="shared-v1"):
        snapshot = self.require_snapshot(snapshot)
        if snapshot.kind == "docker":
            return
        if protocol != "shared-v1":
            raise ValueError("Kubernetes requires shared-v1 workers")
        if stage.executor == "builtin":
            return
        if not isinstance(stage, DockerStage):
            raise ValueError("Unsupported runtime executor")
        config = snapshot.configuration
        if (
            not config["network_qualified"]
            or not config["network_evidence"]
            or stage.network not in config["network_policy_names"]
        ):
            raise ValueError("Kubernetes action networking is not qualified")
        if (
            not config["pid_qualified"]
            or not config["pid_evidence"]
            or stage.limits.pids != config["pid_limit"]
        ):
            raise ValueError("Kubernetes action PID limit is not qualified")

    def public_targets(self):
        result = []
        for target_id in self._pins:
            snapshot = self.snapshot(target_id)
            config = snapshot.configuration
            kube = snapshot.kind == "kubernetes"
            result.append(
                {
                    "id": snapshot.id,
                    "name": config["name"],
                    "runtime": snapshot.kind,
                    "revision": snapshot.revision,
                    "capabilities": {
                        "create": True,
                        "start": True,
                        "stop": True,
                        "drain": False,
                        "kubernetes": kube,
                        "legacy": not kube,
                        "shared": True,
                        "log_stream": "combined" if kube else "split",
                        "scratch_executable": kube,
                        "container_actions": not kube
                        or bool(
                            config["network_qualified"]
                            and config["network_evidence"]
                            and "none" in config["network_policy_names"]
                            and config["pid_qualified"]
                            and config["pid_evidence"]
                        ),
                    },
                }
            )
        return result
