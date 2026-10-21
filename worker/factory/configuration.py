"""Strict, non-secret YAML configuration and task-local model instructions."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import fields
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class Labels(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_min_length=1)
    needs_discussion: str = "needs-discussion"
    ready_for_agent: str = "ready-for-agent"
    pr_open: str = "agent-pr-open"
    pr_reviewed: str = "agent-pr-reviewed"
    plan_created: str = "agent-plan-created"
    plan_subtask: str = "agent-subtask"


class TaskSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    worker_type: Literal["simple", "developer"] = "simple"
    timeout_seconds: int = Field(default=120, ge=1, le=2_592_000)
    retries: int = Field(default=2, ge=0, le=20)
    backoff_factor: float = Field(default=2.0, ge=1, le=100)
    backoff_max_seconds: int = Field(default=30, ge=1, le=86400)
    concurrency: int | None = Field(default=None, ge=1, le=1000)
    model: str | None = Field(
        default=None, min_length=1, pattern=r"^[A-Za-z0-9._:/-]+$"
    )
    additional_system_prompt: str = Field(default="", max_length=16000)


_current_task = ContextVar("factory_task_configuration", default=None)


@contextmanager
def task_context(task):
    token = _current_task.set(task)
    try:
        yield
    finally:
        _current_task.reset(token)


def current_task():
    return _current_task.get() or TaskSettings()


def read_configuration(path):
    """Load relative YAML fragments with explicit, recursive mapping overrides."""

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if key in result:
                raise ValueError("Duplicate configuration key")
            result[key] = loader.construct_object(value_node)
        return result

    UniqueLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping
    )
    root = Path(path).resolve()

    def merge(left, right):
        result = dict(left)
        for key, value in right.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = value
        return result

    def read(file, stack=()):
        if file in stack:
            raise ValueError("Cyclic configuration includes")
        try:
            config = yaml.load(file.read_text(), Loader=UniqueLoader)
        except yaml.YAMLError:
            raise ValueError("Invalid worker configuration YAML") from None
        if not isinstance(config, dict):
            raise ValueError("Configuration fragments must be mappings")
        includes = config.pop("includes", [])
        if not isinstance(includes, list) or any(
            not isinstance(item, str) for item in includes
        ):
            raise ValueError("includes must be a list of relative file paths")
        combined = {}
        for item in includes:
            child = (file.parent / item).resolve()
            if Path(item).is_absolute() or not child.is_relative_to(root.parent):
                raise ValueError(
                    "Configuration includes must stay within the manifest directory"
                )
            combined = merge(combined, read(child, (*stack, file)))
        return merge(combined, config)

    return read(root)


def load_settings(path, profile):
    from factory.settings import Settings
    from factory.task_defaults import TASK_DEFAULTS

    config = read_configuration(path)
    if not isinstance(config, dict) or config.get("version") != 1:
        raise ValueError("Worker configuration must be a version: 1 mapping")
    if set(config) - {
        "version",
        "defaults",
        "workers",
        "tasks",
        "task_defaults",
        "task_templates",
    }:
        raise ValueError("Unknown worker configuration section")
    defaults, workers, tasks = (
        config.get(key, {}) for key in ("defaults", "workers", "tasks")
    )
    if not all(isinstance(value, dict) for value in (defaults, workers, tasks)):
        raise ValueError("defaults, workers and tasks must be mappings")
    if profile not in workers or not isinstance(workers[profile], dict):
        raise ValueError("Unknown or invalid WORKER_PROFILE")
    valid_fields = {field.name for field in fields(Settings)} - {"tasks"}
    for values in [defaults, *workers.values()]:
        if not isinstance(values, dict) or set(values) - (valid_fields | {"extends"}):
            raise ValueError(
                "Unknown worker setting; credentials belong in the environment"
            )
    if set(tasks) - set(TASK_DEFAULTS):
        raise ValueError("Unknown workflow task in configuration")
    task_defaults = config.get("task_defaults", {})
    templates = config.get("task_templates", {})
    if not isinstance(task_defaults, dict) or not isinstance(templates, dict):
        raise ValueError("task_defaults and task_templates must be mappings")
    # Validate even unused templates, so typos fail at startup.
    TaskSettings.model_validate(task_defaults)
    for template in templates.values():
        if not isinstance(template, dict):
            raise ValueError("Task templates must be mappings")
        TaskSettings.model_validate(template)
    task_values = {}
    for name, base in TASK_DEFAULTS.items():
        override = tasks.get(name, {})
        if not isinstance(override, dict):
            raise ValueError("Task settings must be mappings")
        override = dict(override)
        template = override.pop("template", None)
        if template is not None and (
            not isinstance(template, str) or template not in templates
        ):
            raise ValueError("Unknown task template")
        task_values[name] = TaskSettings.model_validate(
            {
                **base,
                **task_defaults,
                **templates.get(template, {}),
                **override,
            }
        )

    def resolve(name, seen=()):
        if name in seen or name not in workers:
            raise ValueError("Invalid or cyclic worker profile inheritance")
        worker = dict(workers[name])
        parent = worker.pop("extends", None)
        return {**(resolve(parent, (*seen, name)) if parent else {}), **worker}

    if "extends" in defaults:
        raise ValueError("Only worker profiles may extend another profile")
    values = {**defaults, **resolve(profile), "tasks": task_values}
    return TypeAdapter(Settings).validate_python(values)


def load_managed_settings(env):
    """Resolve a linked profile on process start, including Docker restarts.

    A control-plane outage must not silently run an outdated profile. Managed
    workers fail startup and the runtime retry policy can retry later.
    Standalone workers never enter this optional integration path.
    """
    import json
    import tempfile
    import urllib.request

    request = urllib.request.Request(
        env["FACTORY_CONTROL_ENDPOINT"].rstrip("/") + "/api/v1/worker-bootstrap",
        headers={
            "X-Worker-Id": env.get("FACTORY_CONTROL_WORKER_ID", ""),
            "Authorization": "Bearer " + env.get("FACTORY_CONTROL_TOKEN", ""),
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read(262145)
        if len(raw) > 262144:
            raise ValueError("Profile exceeds size limit")
        document = json.loads(raw)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.yaml"
            path.write_text(yaml.safe_dump(document))
            return load_settings(path, env.get("WORKER_PROFILE", "simple"))
    except Exception:
        raise RuntimeError(
            "Could not load the saved worker profile from the control plane; retry startup when it is available"
        ) from None
