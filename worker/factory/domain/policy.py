from dataclasses import dataclass

from factory.domain.models import ModelError, PatchPlan
from factory.domain.safety import (
    MAX_SOURCE_FILE_BYTES,
    assert_safe_text,
    is_allowed_source_path,
    is_safe_repository_path,
)


@dataclass(frozen=True)
class ExecutionPolicy:
    path_scope: str = "source"
    max_files: int = 50
    max_file_bytes: int = MAX_SOURCE_FILE_BYTES

    def __post_init__(self):
        if self.path_scope not in {"source", "repository"}:
            raise ValueError("Unknown patch path policy")

    def validate_patch(self, plan: PatchPlan) -> None:
        if not plan.is_complete:
            raise ModelError("Cannot publish an incomplete patch")
        assert_safe_text(plan.pull_title, plan.pull_body, plan.progress)
        if len(plan.changes) > self.max_files:
            raise ModelError("Patch contains too many files")
        seen: set[str] = set()
        allowed = (
            is_safe_repository_path
            if self.path_scope == "repository"
            else is_allowed_source_path
        )
        for change in plan.changes:
            if not allowed(change.path) or change.path in seen:
                raise ModelError("Patch contains a disallowed or duplicate path")
            seen.add(change.path)
            if len(change.content.encode("utf-8")) > self.max_file_bytes:
                raise ModelError(
                    f"Patch file exceeds the size limit: {change.path} "
                    f"({len(change.content.encode('utf-8'))} bytes; maximum {self.max_file_bytes} bytes)"
                )
            assert_safe_text(change.path, change.content)
