"""Configuration use cases, independent of Docker and Hatchet."""

import json
from pathlib import Path
import tempfile
import yaml
from factory.configuration import load_settings, read_configuration
from factory.domain.safety import find_sensitive_categories
from app.security import digest


class Configurations:
    def __init__(self, root):
        self.root = Path(root)
        self.approved = read_configuration(self.root / "worker-config.yaml")

    def validate(self, document):
        if len(json.dumps(document).encode()) > 262144:
            raise ValueError("Configuration exceeds 256 KiB")
        if find_sensitive_categories(json.dumps(document)):
            raise ValueError("Configuration contains potential credentials")
        workers = document.get("workers", {})
        if not isinstance(workers, dict) or not workers or len(workers) > 32:
            raise ValueError("Define between 1 and 32 worker profiles")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.yaml"
            path.write_text(yaml.safe_dump(document))
            profiles = {}
            allowed_factories = {
                value
                for key, value in self.approved.get("defaults", {}).items()
                if key.endswith("factory")
            }
            for name in workers:
                if (
                    not isinstance(name, str)
                    or not name.replace("-", "").replace("_", "").isalnum()
                ):
                    raise ValueError("Invalid profile name")
                settings = load_settings(path, name)
                for key in (
                    "planner_factory",
                    "reviewer_factory",
                    "coding_factory",
                    "observer_factory",
                    "state_factory",
                ):
                    value = getattr(settings, key)
                    if value and value not in allowed_factories | {
                        "factory.adapters.otel:otel_observer"
                    }:
                        raise ValueError(
                            "Adapter factory is not in the approved registry"
                        )
                profiles[name] = {
                    "worker_type": settings.worker_type,
                    "model": settings.openai_model,
                    "coding_agent": settings.developer_agent,
                    "tasks": {
                        key: settings.task(key).model_dump() for key in settings.tasks
                    },
                }
        return profiles

    def resolve(self, document, profile):
        """Resolve a saved document without reading the bootstrap configuration."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(document))
            return load_settings(path, profile)

    def effective(
        self, document, profile, repository, workflow=None, stage=None, *, limits=None
    ):
        """Compile behavior precedence after resolving project profile defaults.

        Limits come from trusted deployment/pool policy, never the HTTP body.
        This also provides the resolver used by future pinned run contexts.
        """
        merged = self._behavior_snapshot(
            document,
            profile,
            (repository.get("overrides", {}), workflow or {}, stage or {}),
        )
        merged["defaults"]["repository"] = (
            repository["owner"] + "/" + repository["name"]
        )
        provider = repository.get("provider", "github")
        identity = repository.get("identity") or {}
        merged["defaults"].update(
            repository_provider=provider,
            repository_host=repository.get("host", "github.com"),
            gitlab_project_id=identity.get("project_id")
            if provider == "gitlab"
            else None,
            repository_check_policy_json=json.dumps(repository.get("check_policy", {}))
            if provider == "gitlab"
            else "{}",
        )
        self.validate(merged)
        resolved = self.resolve(merged, profile)
        for key, maximum in (limits or {}).items():
            value = getattr(resolved, key, None)
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or value > maximum
            ):
                raise ValueError(
                    "Effective configuration exceeds execution pool limits"
                )
        return resolved

    def _behavior_snapshot(self, document, profile, layers):
        from copy import deepcopy
        from app.projects import validate_overrides

        merged = self.snapshot(document, profile, "preview", "preview")
        for layer in layers:
            validate_overrides(layer)
            for key, value in layer.items():
                if key == "tasks":
                    for task, values in value.items():
                        merged["tasks"].setdefault(task, {}).update(deepcopy(values))
                else:
                    merged["defaults"][key] = deepcopy(value)
        return merged

    def validate_repository_behavior(self, document, profile, overrides):
        return self.validate(self._behavior_snapshot(document, profile, (overrides,)))

    def snapshot(self, document, profile, name, revision):
        from dataclasses import asdict

        settings = self.resolve(document, profile)
        from factory.settings import compatible_settings

        values = compatible_settings(asdict(settings))
        values["labels"] = settings.labels.model_dump()
        values["tasks"] = {
            key: value.model_dump() for key, value in settings.tasks.items()
        }
        tasks = values.pop("tasks")
        values["worker_name"] = name
        values["control_revision"] = revision
        return {
            "version": 1,
            "defaults": values,
            "workers": {profile: {}},
            "tasks": tasks,
        }

    @staticmethod
    def hash(document):
        return digest(json.dumps(document, sort_keys=True))

    # Worker-local knobs. Registration, routing, state and repository policy stay
    # in the factory configuration because workers share workflow definitions.
    BUILD_FIELDS = frozenset(
        {
            "base_image",
            "system_packages",
            "image_setup_script",
            "codex_version",
            "claude_version",
        }
    )
    LOCAL_FIELDS = frozenset(
        {
            "developer_agent",
            "max_patch_iterations",
            "log_level",
            "log_format",
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
            "conversation_capture",
            "planner_factory",
            "reviewer_factory",
            "coding_factory",
            "observer_factory",
            "base_image",
            "system_packages",
            "image_setup_script",
            "codex_version",
            "claude_version",
            "startup_script",
            "startup_timeout_seconds",
        }
    )
    LOCAL_TASK_FIELDS = frozenset({"model", "additional_system_prompt"})

    def split(self, document, profile):
        resolved = self.snapshot(document, profile, "preview", "preview")
        local = {
            "settings": {
                k: v for k, v in resolved["defaults"].items() if k in self.LOCAL_FIELDS
            },
            "tasks": {
                name: {k: v for k, v in task.items() if k in self.LOCAL_TASK_FIELDS}
                for name, task in resolved["tasks"].items()
            },
        }
        shared = {
            "settings": {
                k: v
                for k, v in resolved["defaults"].items()
                if k not in self.LOCAL_FIELDS | {"worker_name", "control_revision"}
            },
            "tasks": {
                name: {k: v for k, v in task.items() if k not in self.LOCAL_TASK_FIELDS}
                for name, task in resolved["tasks"].items()
            },
        }
        return shared, local

    def compose(self, factory_document, profile, local):
        if not isinstance(local, dict) or set(local) != {"settings", "tasks"}:
            raise ValueError(
                "Worker configuration requires settings and tasks mappings"
            )
        if (
            not isinstance(local["settings"], dict)
            or set(local["settings"]) - self.LOCAL_FIELDS
        ):
            raise ValueError("Worker settings contain factory-level or unknown fields")
        if not isinstance(local["tasks"], dict):
            raise ValueError("Worker tasks must be a mapping")
        shared, defaults = self.split(factory_document, profile)
        if set(local["tasks"]) - set(defaults["tasks"]):
            raise ValueError("Unknown workflow task")
        for task in local["tasks"].values():
            if not isinstance(task, dict) or set(task) - self.LOCAL_TASK_FIELDS:
                raise ValueError(
                    "Worker tasks may only override model and additional_system_prompt"
                )
        result = {
            "version": 1,
            "workers": {profile: {}},
            "defaults": {
                **shared["settings"],
                **defaults["settings"],
                **local["settings"],
            },
            "tasks": {
                name: {
                    **task,
                    **defaults["tasks"][name],
                    **local["tasks"].get(name, {}),
                }
                for name, task in shared["tasks"].items()
            },
        }
        self.validate(result)
        return result

    def factory_view(self, document):
        return {
            "profiles": {
                profile: self.split(document, profile)[0]
                for profile in document["workers"]
            }
        }

    def update_factory(self, base, shared):
        expected = self.factory_view(base)
        if (
            not isinstance(shared, dict)
            or set(shared) != {"profiles"}
            or set(shared["profiles"]) != set(expected["profiles"])
        ):
            raise ValueError("Factory profiles must match existing registered profiles")
        workers, tasks = {}, None
        # Resolve profile inheritance first, so no hidden override defeats an edit.
        for profile, value in shared["profiles"].items():
            old_shared, local = self.split(base, profile)
            if not isinstance(value, dict) or set(value) != {"settings", "tasks"}:
                raise ValueError("Invalid factory profile")
            if not isinstance(value["settings"], dict) or set(value["settings"]) != set(
                old_shared["settings"]
            ):
                raise ValueError("Factory settings cannot contain worker-local fields")
            if (
                not isinstance(value["tasks"], dict)
                or set(value["tasks"]) != set(old_shared["tasks"])
                or any(
                    not isinstance(task, dict)
                    or set(task) != set(old_shared["tasks"][name])
                    for name, task in value["tasks"].items()
                )
            ):
                raise ValueError(
                    "Factory tasks cannot contain worker prompts or models"
                )
            # Task registration is global to a workflow, not profile-specific.
            if tasks is not None and tasks != value["tasks"]:
                raise ValueError(
                    "Shared task registration must be identical across profiles"
                )
            tasks = value["tasks"]
            workers[profile] = {**value["settings"], **local["settings"]}
        first = next(iter(base["workers"]))
        local_tasks = self.split(base, first)[1]["tasks"]
        result = {
            "version": 1,
            "defaults": {},
            "workers": workers,
            "tasks": {
                name: {**task, **local_tasks[name]} for name, task in tasks.items()
            },
        }
        self.validate(result)
        return result
