"""Bounded, vendor-neutral one-time job contracts."""

from typing import Literal
from pydantic import Field, field_validator
from factory.workflows.contracts import Contract
from factory.workflows.definitions import DockerLimits, DockerStage
from factory.workflows.references import json_bytes


class JobLaunch(Contract):
    job_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    project_id: str = Field(min_length=1, max_length=120)
    execution_id: str = Field(min_length=1, max_length=120)
    spec_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    image: str = Field(
        max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$"
    )
    command: list[str] = Field(min_length=1, max_length=128)
    inputs: dict
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

    @field_validator("credentials")
    @classmethod
    def grants(cls, value):
        if set(value) - {"github", "openai", "anthropic"} or any(
            not v or len(v) > 16384 for v in value.values()
        ):
            raise ValueError("Invalid job credential purposes")
        return value


class RuntimeJobRef(Contract):
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
