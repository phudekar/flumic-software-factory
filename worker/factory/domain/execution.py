"""Execution policy is an admission decision, independent of runtime vendors."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator

ExecutionMode = Literal['direct', 'protected']


class ExecutionPolicyError(ValueError):
    """A requested execution mode violates policy."""


class FrozenContract(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)


class ExecutionPolicy(FrozenContract):
    revision_id: str = Field(min_length=1, max_length=120)
    default_mode: ExecutionMode = 'direct'
    minimum_mode: ExecutionMode = 'direct'

    @model_validator(mode='after')
    def minimum_is_respected(self):
        if self.minimum_mode == 'protected' and self.default_mode != 'protected':
            raise ValueError('Default execution mode cannot weaken the project minimum')
        return self


class ExecutionPin(FrozenContract):
    mode: ExecutionMode
    policy_revision_id: str = Field(min_length=1, max_length=120)
    route_revision_id: str | None = None
    target_qualification_id: str | None = None
    target_qualification_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    tool_policy_revision_id: str | None = None


class TargetQualification(FrozenContract):
    id: str = Field(min_length=1, max_length=120)
    target_id: str = Field(min_length=1, max_length=120)
    runtime_digest: str = Field(min_length=1, max_length=240)
    capabilities: frozenset[str]
    expires_at: float = Field(ge=0, allow_inf_nan=False)
