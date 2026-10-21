from typing import Literal
from uuid import uuid4
from pydantic import BaseModel, Field


class FactoryEvent(BaseModel):
    schema_version: Literal[1] = 1
    event_id: str
    correlation_id: str = Field(default_factory=lambda: str(uuid4()))
    kind: Literal[
        "work-item-ready",
        "change-feedback-received",
        "change-state-observed",
        "ignored",
    ]
    repository: str
    issue_number: int | None = None
    pull_number: int | None = None
