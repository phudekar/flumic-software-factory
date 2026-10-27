"""Bounded, vendor-neutral one-time job contracts."""

from typing import Literal
from pydantic import Field, field_validator, model_validator, model_serializer
from factory.workflows.contracts import Contract, UsageRecord
from factory.workflows.definitions import DockerLimits, DockerStage
from factory.workflows.references import json_bytes


class RuntimePin(Contract):
    target_id: str = Field(default="local-docker", pattern=r"^[a-z][a-z0-9-]{0,79}$")
    target_revision: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")
    target_snapshot: dict = Field(default_factory=dict, repr=False)
    deadline: float | None = Field(default=None, gt=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def pin(self):
        if (
            self.target_id != "local-docker"
            or self.target_revision
            or self.target_snapshot
        ):
            from app.runtime_targets import TargetSnapshot

            snapshot = TargetSnapshot.model_validate(self.target_snapshot)
            if (
                snapshot.id != self.target_id
                or snapshot.revision != self.target_revision
            ):
                raise ValueError("Job target pin mismatch")
            if snapshot.kind == "kubernetes" and self.deadline is None:
                raise ValueError("Kubernetes jobs require an absolute deadline")
        return self

    @model_serializer(mode="wrap")
    def compatible_wire(self, handler):
        result = handler(self)
        for key, default in (
            ("target_id", "local-docker"),
            ("target_revision", ""),
            ("target_snapshot", {}),
            ("deadline", None),
        ):
            if result.get(key) == default:
                result.pop(key, None)
        return result


class JobLaunch(RuntimePin):
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str = Field(min_length=1, max_length=120)
    execution_id: str = Field(min_length=1, max_length=120)
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    image: str = Field(
        max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$"
    )
    command: list[str] = Field(min_length=1, max_length=128)
    inputs: dict
    context: dict = Field(default_factory=dict)
    credentials: dict[str, str] = Field(default_factory=dict, repr=False, exclude=True)
    network: Literal["none", "bridge"] = "none"
    limits: DockerLimits = Field(default_factory=DockerLimits)

    @field_validator("command")
    @classmethod
    def argv(cls, value):
        if any(len(v) > 4096 for v in value):
            raise ValueError("Job argument exceeds limit")
        return DockerStage.valid_argv(value)

    @field_validator("inputs")
    @classmethod
    def input_size(cls, value):
        if len(json_bytes(value)) > 1048576:
            raise ValueError("Job input exceeds 1 MiB")
        return value

    @field_validator("context")
    @classmethod
    def context_size(cls, value):
        if len(json_bytes(value)) > 16384:
            raise ValueError("Job context exceeds 16 KiB")
        return value

    @field_validator("credentials")
    @classmethod
    def grants(cls, value):
        if set(value) - {"github", "gitlab", "openai", "anthropic"} or any(
            not v or len(v) > 16384 for v in value.values()
        ):
            raise ValueError("Invalid job credential purposes")
        return value


class RuntimeJobRef(RuntimePin):
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str
    execution_id: str
    spec_digest: str
    image: str


class RuntimeJobState(Contract):
    state: Literal[
        "missing",
        "created",
        "running",
        "exited",
        "dead",
        "paused",
        "restarting",
        "removing",
    ]
    exit_code: int | None = None
    oom: bool = False


class JobCapture(Contract):
    log_stream: Literal["split", "combined"] = "split"

    @model_serializer(mode="wrap")
    def compatible_wire(self, handler):
        result = handler(self)
        if self.log_stream == "split":
            result.pop("log_stream", None)
        return result

    error: str | None = Field(default=None, max_length=200)
    exit_code: int | None = None
    oom: bool = False
    output: dict
    usage: list[dict] = Field(default_factory=list, max_length=256)
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False


class JobReceipt(Contract):
    job_id: str
    state: str
    execution_id: str
    deadline: float
    result: dict | None = None


def sanitize_capture(capture, secrets):
    """Keep protocol authority separate from bounded, filtered program content.

    One replacement pass cannot repeatedly expand its own redaction marker.
    Logs and the result envelope are charged again after replacing grants.
    """
    import re

    values = sorted(set(filter(None, secrets)), key=len, reverse=True)
    pattern = (
        re.compile("|".join(re.escape(value) for value in values)) if values else None
    )

    def text(value):
        return pattern.sub(lambda match: "[REDACTED]", value) if pattern else value

    def content(value):
        if isinstance(value, str):
            return text(value)
        if isinstance(value, dict):
            return {text(key): content(item) for key, item in value.items()}
        if isinstance(value, list):
            return [content(item) for item in value]
        return value

    # Determine failure before processing program-controlled strings or keys.
    failed = (
        capture.error is not None or capture.oom or capture.exit_code not in (None, 0)
    )
    result = capture.model_dump(mode="json")
    result["error"] = (
        text(capture.error).encode()[:200].decode("utf-8", errors="ignore")
        if capture.error is not None
        else None
    )
    if failed and result["error"] is None:
        result["error"] = "Job exited nonzero or was killed for memory usage"
    try:
        raw = {"output": capture.output, "usage": capture.usage}
        if len(json_bytes(raw)) > 1048576:
            raise ValueError("Job result exceeds 1 MiB")
        envelope = {
            "output": content(capture.output),
            "usage": [
                {
                    key if key in UsageRecord.model_fields else text(key): content(
                        value
                    )
                    for key, value in record.items()
                }
                for record in capture.usage
            ],
        }
        if len(json_bytes(envelope)) > 1048576:
            raise ValueError("Redacted job result exceeds 1 MiB")
        result.update(envelope)
    except (ValueError, RecursionError):
        result.update(
            output={}, usage=[], error="Job result exceeds bounded JSON contract"
        )
        failed = True
    for stream in ("stdout", "stderr"):
        encoded = text(getattr(capture, stream)).encode()
        result[stream] = encoded[:262144].decode("utf-8", errors="ignore")
        result[stream + "_truncated"] = (
            getattr(capture, stream + "_truncated") or len(encoded) > 262144
        )
    return result, failed
