"""Strict version-one DAG data. Publishing cannot execute arbitrary imports/code."""

from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from factory.workflows.action_schemas import CATALOG, validate_arguments
from factory.workflows.references import json_bytes, references, parse_reference
from factory.workflows.policy import validate_behavior_overrides

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,99}$")]
Identity = Annotated[str, Field(min_length=1, max_length=120)]


class Schema(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False
    )


class Condition(Schema):
    ref: str
    equals: str | int | float | bool | None

    @field_validator("ref")
    @classmethod
    def valid_ref(cls, value):
        parse_reference(value)
        return value


class DockerLimits(Schema):
    cpu: float = Field(default=1.0, ge=0.1, le=8, allow_inf_nan=False)
    memory_mib: int = Field(default=512, ge=64, le=4096)
    pids: int = Field(default=128, ge=16, le=512)
    tmpfs_mib: int = Field(default=64, ge=16, le=256)


class Stage(Schema):
    id: Slug
    role: Literal["simple", "developer"]
    pool_id: Identity
    profile: Slug | None = None
    repository: Slug | None = None
    after: list[Slug] = Field(default_factory=list, max_length=63)
    inputs: dict = Field(default_factory=dict)
    when: Condition | None = None
    timeout_seconds: int = Field(default=300, ge=1, le=3600)
    retries: int = Field(default=0, ge=0, le=3)
    retry_delay_seconds: int = Field(default=0, ge=0, le=3600)
    overrides: dict = Field(default_factory=dict)
    secrets: dict[Literal["github", "openai", "anthropic"], Identity] = Field(
        default_factory=dict
    )

    @field_validator("overrides")
    @classmethod
    def behavior(cls, value):
        return validate_behavior_overrides(value)


class BuiltinStage(Stage):
    executor: Literal["builtin"]
    action: Identity

    @model_validator(mode="after")
    def builtin(self):
        descriptor = CATALOG.get(self.action)
        if descriptor is None:
            raise ValueError("Unknown action version")
        if descriptor.role and self.role != descriptor.role:
            raise ValueError("Action requires its catalog role")
        validate_arguments(self.action, self.inputs, dynamic=True)
        return self


class DockerStage(Stage):
    executor: Literal["docker"]
    image: str = Field(
        max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[a-f0-9]{64}$"
    )
    command: list[Annotated[str, Field(max_length=4096)]] = Field(
        min_length=1, max_length=128
    )
    network: Literal["none", "bridge"] = "none"
    limits: DockerLimits = Field(default_factory=DockerLimits)

    @field_validator("command")
    @classmethod
    def valid_argv(cls, value):
        if not value[0].strip() or any("\x00" in item for item in value):
            raise ValueError("Docker argv requires an executable and no NUL bytes")
        return value


class WorkflowDefinition(Schema):
    schema_version: Literal[1]
    name: Slug
    repositories: list[Slug] = Field(min_length=1, max_length=64)
    default_repository: Slug
    defaults: dict = Field(default_factory=dict)
    secrets: dict[Literal["github", "openai", "anthropic"], Identity] = Field(
        default_factory=dict
    )
    stages: list[
        Annotated[BuiltinStage | DockerStage, Field(discriminator="executor")]
    ] = Field(min_length=1, max_length=64)

    @model_validator(mode="before")
    @classmethod
    def size(cls, value):
        if len(json_bytes(value)) > 262144:
            raise ValueError("Definition exceeds 256 KiB")
        if isinstance(value, dict) and type(value.get("schema_version")) is not int:
            raise ValueError("schema_version must be an integer")
        return value

    @field_validator("defaults")
    @classmethod
    def behavior(cls, value):
        return validate_behavior_overrides(value)

    @model_validator(mode="after")
    def graph(self):
        aliases = set(self.repositories)
        if (
            len(aliases) != len(self.repositories)
            or self.default_repository not in aliases
        ):
            raise ValueError("Invalid default or duplicate repository alias")
        stages = {stage.id: stage for stage in self.stages}
        if len(stages) != len(self.stages):
            raise ValueError("Duplicate stage ID")
        for stage in self.stages:
            selected = stage.repository or self.default_repository
            if selected not in aliases:
                raise ValueError("Stage repository is not declared")
            if (
                len(set(stage.after)) != len(stage.after)
                or stage.id in stage.after
                or set(stage.after) - set(stages)
            ):
                raise ValueError("Invalid stage dependencies")
            refs = [(parts, True) for _, parts in references(stage.inputs)]
            if stage.when:
                refs.append((parse_reference(stage.when.ref), False))
            for parts, argument in refs:
                if parts[0] != "stages":
                    continue
                if parts[1] not in stage.after:
                    raise ValueError(
                        "Referenced producer must be an explicit direct parent"
                    )
                producer = stages[parts[1]]
                if (
                    argument
                    and isinstance(stage, BuiltinStage)
                    and stage.action != "core.echo@1"
                    and (producer.repository or self.default_repository) != selected
                ):
                    raise ValueError(
                        "Repository-affine builtin input has another repository origin"
                    )
        visited = set()
        active = set()

        def visit(identity):
            if identity in active:
                raise ValueError("Workflow graph contains a cycle")
            if identity in visited:
                return
            active.add(identity)
            for parent in stages[identity].after:
                visit(parent)
            active.remove(identity)
            visited.add(identity)

        for identity in stages:
            visit(identity)
        if len(json_bytes(self.model_dump(mode="json"))) > 262144:
            raise ValueError("Normalized definition exceeds 256 KiB")
        return self


def stage_capability(stage):
    return (
        stage.executor + "@1",
        stage.action if isinstance(stage, BuiltinStage) else "docker.run@1",
    )
