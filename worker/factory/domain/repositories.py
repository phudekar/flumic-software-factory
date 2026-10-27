"""Source-host identity and check policy, independent of SDKs and transports."""

import re
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

_SEGMENT = re.compile(r"[A-Za-z0-9_.-]{1,100}")
_SUPPORTED_SOURCES = frozenset(
    {
        "push",
        "web",
        "api",
        "schedule",
        "merge_request_event",
        "trigger",
        "pipeline",
        "parent_pipeline",
        "external",
        "chat",
        "webide",
    }
)


class RepositoryLocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["github", "gitlab"]
    host: StrictStr
    owner: Annotated[StrictStr, Field(min_length=1, max_length=1024)]
    name: StrictStr

    @field_validator("host")
    @classmethod
    def normalize_host(cls, value):
        return value.lower()

    @model_validator(mode="after")
    def validate_identity(self):
        if self.host != {"github": "github.com", "gitlab": "gitlab.com"}[self.provider]:
            raise ValueError("Unsupported repository provider/host pair")
        parts = [*self.owner.split("/"), self.name]
        if len(parts) > 21 or any(
            not _SEGMENT.fullmatch(part) or part in {".", ".."} for part in parts
        ):
            raise ValueError("Invalid repository namespace or project name")
        if self.provider == "github" and len(parts) != 2:
            raise ValueError("GitHub identity requires owner/name")
        return self

    @property
    def path(self):
        return self.owner + "/" + self.name

    @property
    def credential_purpose(self):
        return self.provider


class RepositoryIdentity(RepositoryLocation):
    project_id: Annotated[StrictInt, Field(gt=0, le=2**63 - 1)] | None = None

    @model_validator(mode="after")
    def validate_resolved_identity(self):
        if self.provider == "github" and self.project_id is not None:
            raise ValueError("GitHub identity cannot carry a GitLab project ID")
        if self.provider == "gitlab" and self.project_id is None:
            raise ValueError("Executable GitLab identity requires numeric project ID")
        return self

    @property
    def canonical_key(self):
        if self.provider == "github":
            return "github.com/" + self.path.lower()
        return "gitlab.com/project/" + str(self.project_id)

    @property
    def credential_purpose(self):
        return self.provider


class RepositoryCheckPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required_pipeline_sources: tuple[StrictStr, ...] = ("push",)
    required_status_names: tuple[StrictStr, ...] = ()

    @model_validator(mode="after")
    def validate_policy(self):
        sources, statuses = self.required_pipeline_sources, self.required_status_names
        if not sources or len(sources) > 16 or len(set(sources)) != len(sources):
            raise ValueError("Require one to sixteen distinct pipeline sources")
        if set(sources) - _SUPPORTED_SOURCES:
            raise ValueError("Unsupported required pipeline source")
        if len(statuses) > 32 or len(set(statuses)) != len(statuses):
            raise ValueError("Require at most thirty-two distinct external statuses")
        if any(
            not name
            or len(name) > 256
            or name != name.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            for name in statuses
        ):
            raise ValueError("Invalid required external status name")
        return self
