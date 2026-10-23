"""Shared behavior override boundary; infrastructure and identity are reserved."""

import json
from factory.domain.safety import find_sensitive_categories

BEHAVIOR_FIELDS = frozenset(
    {
        "developer_agent",
        "max_patch_iterations",
        "path_policy",
        "openai_model",
        "codex_model",
        "claude_model",
        "max_files_per_iteration",
        "max_file_bytes",
        "max_context_tokens",
        "max_completion_tokens",
        "api_max_retries",
        "api_timeout_seconds",
        "cli_timeout_seconds",
        "discussion_poll_seconds",
        "labels",
        "tasks",
        "conversation_capture",
    }
)


def validate_behavior_overrides(value):
    if not isinstance(value, dict) or set(value) - BEHAVIOR_FIELDS:
        raise ValueError("Unsupported behavior override")
    tasks = value.get("tasks", {})
    if not isinstance(tasks, dict) or any(
        not isinstance(task, dict) or "worker_type" in task for task in tasks.values()
    ):
        raise ValueError("Task role is not a behavior override")
    if find_sensitive_categories(json.dumps(value, allow_nan=False)):
        raise ValueError("Use credential references instead of inline secrets")
    return value
