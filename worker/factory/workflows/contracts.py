"""Safe transport contracts. Credentials are invocation-local private state."""

import json
from types import MappingProxyType
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class ActionExecutionError(RuntimeError):
    """Bounded sanitized failure from an action adapter."""


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ActionInvocation(Contract):
    project_id: str
    run_id: str
    execution_id: str


class StageReceipt(Contract):
    execution_id: str
    state: str


class RunAdvance(Contract):
    state: str
    ready: list[ActionInvocation] = Field(default_factory=list)


class UsageRecord(Contract):
    id: str = Field(min_length=1, max_length=120)
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    estimated_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    reported_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ActionResult(Contract):
    output: dict
    usage: list[UsageRecord] = Field(default_factory=list, max_length=1000)

    @model_validator(mode="after")
    def bounded(self):
        from factory.workflows.redaction import redact

        object.__setattr__(self, "output", redact(self.output))
        object.__setattr__(
            self,
            "usage",
            [
                UsageRecord.model_validate(redact(item.model_dump()))
                for item in self.usage
            ],
        )
        if len(self.model_dump_json().encode()) > 1048576:
            raise ValueError("Action result exceeds 1 MiB")
        return self


class ActionContext(Contract):
    project_id: str
    repository_id: str
    repository_revision_id: str
    config_revision_id: str
    workflow_revision_id: str = ""
    run_id: str
    stage_id: str
    execution_id: str
    attempt: int = Field(ge=1)
    assignment_id: str
    namespace: str
    settings_json: str
    secret_versions: tuple[str, ...] = ()
    _credentials: dict = PrivateAttr(default_factory=dict)

    def with_credentials(self, credentials):
        result = self.model_copy()
        result._credentials = dict(credentials)
        return result

    @property
    def credentials(self):
        return MappingProxyType(self._credentials)

    def credential(self, purpose):
        if purpose not in self._credentials:
            raise ValueError("Required credential grant is unavailable")
        return self._credentials[purpose]

    @property
    def settings(self):
        from factory.settings import Settings
        from factory.configuration import Labels, TaskSettings

        values = json.loads(self.settings_json)
        if "labels" in values:
            values["labels"] = Labels.model_validate(values["labels"])
        if "tasks" in values:
            values["tasks"] = {
                key: TaskSettings.model_validate(value)
                for key, value in values["tasks"].items()
            }
        return Settings(**values)
