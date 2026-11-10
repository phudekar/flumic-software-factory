"""Identity and allowance contracts for a brokered stage attempt."""
from typing import Annotated, Literal

from pydantic import Field, model_validator

from factory.domain.execution import FrozenContract

BrokerAudience = Literal["model", "repository"] | Annotated[
    str, Field(pattern=r"^mcp:[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")
]
BrokerOperation = Annotated[
    str, Field(max_length=120, pattern=r"^(model|repository)\.[a-z][a-z0-9_.-]*$")
]
MAX_CAPABILITY_TTL_SECONDS = 300
MAX_ALLOWANCE = 2**31 - 1

RecordId = Annotated[str, Field(min_length=1, max_length=36)]
Allowance = Annotated[int, Field(ge=0, le=MAX_ALLOWANCE)]
PositiveAllowance = Annotated[int, Field(gt=0, le=MAX_ALLOWANCE)]


class CapabilityScope(FrozenContract):
    project_id: RecordId
    run_id: RecordId
    stage_id: str = Field(min_length=1, max_length=100)
    attempt_id: RecordId
    assignment_id: RecordId
    audience: BrokerAudience
    operations: tuple[BrokerOperation, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def validate_operations(self):
        if len(set(self.operations)) != len(self.operations):
            raise ValueError("Broker operations must be unique")
        if self.audience in {"model", "repository"} and any(
            not operation.startswith(self.audience + ".") for operation in self.operations
        ):
            raise ValueError("Broker operations must match their audience")
        return self


class SessionLimits(FrozenContract):
    request_count: PositiveAllowance
    concurrent_requests: PositiveAllowance
    input_tokens: Allowance
    output_tokens: Allowance
    ttl_seconds: int = Field(gt=0, le=MAX_CAPABILITY_TTL_SECONDS)

    @model_validator(mode="after")
    def validate_concurrency(self):
        if self.concurrent_requests > self.request_count:
            raise ValueError("Concurrent requests cannot exceed the request allowance")
        return self
