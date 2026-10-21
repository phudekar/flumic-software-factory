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

    def snapshot(self, document, profile, name, revision):
        from dataclasses import asdict

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(document))
            settings = load_settings(path, profile)
            values = asdict(settings)
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
