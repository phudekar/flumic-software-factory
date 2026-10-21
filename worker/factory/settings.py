"""Runtime configuration. Reading the environment is explicit, never an import effect."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Mapping

from factory.configuration import Labels, TaskSettings


def parse_max_patch_iterations(value: str) -> int:
    try:
        iterations = int(value)
    except ValueError:
        raise ValueError(
            "MAX_PATCH_ITERATIONS must be an integer between 1 and 20"
        ) from None
    if not 1 <= iterations <= 20:
        raise ValueError("MAX_PATCH_ITERATIONS must be between 1 and 20")
    return iterations


@dataclass(frozen=True)
class Settings:
    repository: str = "phudekar/software-factory"
    worker_type: str = "simple"
    developer_agent: str = "api"
    orchestrator: str = "hatchet"
    worker_name: str = ""
    hatchet_tls_strategy: str = "tls"
    scan_cron: str = "*/5 * * * *"
    max_patch_iterations: int = 10
    path_policy: str = "source"
    state_path: str = "/data/factory.sqlite3"
    log_level: str = "INFO"
    log_format: str = "json"
    planner_factory: str = "factory.adapters.agents:openai_model"
    reviewer_factory: str = "factory.adapters.agents:openai_model"
    coding_factory: str = ""
    observer_factory: str = "factory.adapters.telemetry:logging_observer"
    state_factory: str = "factory.adapters.state:sqlite_state"

    openai_model: str = "gpt-4.1-mini"
    codex_model: str = "gpt-6-luna"
    claude_model: str = ""
    max_files_per_iteration: int = 5
    max_file_bytes: int = 32768
    max_context_tokens: int = 32000
    max_completion_tokens: int = 16000
    api_max_retries: int = 2
    api_timeout_seconds: float = 90.0
    cli_timeout_seconds: int = 240
    workflow_concurrency: int = 3
    discussion_poll_seconds: int = 30
    worker_label_key: str = "worker_type"
    labels: Labels = field(default_factory=Labels)
    tasks: dict[str, TaskSettings] = field(default_factory=dict)
    telemetry_endpoint: str = "http://factory-signoz-ingester:4318"
    deployment_environment: str = "development"
    pricing_file: str = ""
    startup_script: str = ""
    startup_timeout_seconds: int = 600
    control_revision: str = ""
    conversation_capture: str = "redacted"

    def task(self, name):
        from factory.task_defaults import TASK_DEFAULTS

        return self.tasks.get(name) or TaskSettings.model_validate(TASK_DEFAULTS[name])

    @classmethod
    def load(cls, env=None):
        from factory.configuration import load_settings

        env = os.environ if env is None else env
        return load_settings(
            env.get("WORKER_CONFIG_FILE", "worker-config.yaml"),
            env.get("WORKER_PROFILE", "simple"),
        )

    def __post_init__(self):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository):
            raise ValueError("GITHUB_REPOSITORY must be in owner/name form")
        if self.worker_type not in {"simple", "developer"}:
            raise ValueError("WORKER_TYPE must be 'simple' or 'developer'")
        if self.hatchet_tls_strategy not in {"tls", "none", "mtls"}:
            raise ValueError("Invalid hatchet_tls_strategy")
        if self.orchestrator not in {"hatchet", "local"}:
            raise ValueError("ORCHESTRATOR must be 'hatchet' or 'local'")
        if not self.coding_factory and self.developer_agent not in {
            "api",
            "codex",
            "claude",
        }:
            raise ValueError("Set CODING_AGENT_FACTORY for a custom coding backend")
        if self.path_policy not in {"source", "repository"}:
            raise ValueError("PATCH_PATH_POLICY must be 'source' or 'repository'")
        if not 1 <= self.max_patch_iterations <= 20:
            raise ValueError("MAX_PATCH_ITERATIONS must be between 1 and 20")
        if self.log_format not in {"json", "text"}:
            raise ValueError("LOG_FORMAT must be 'json' or 'text'")

        if (
            not isinstance(self.startup_script, str)
            or len(self.startup_script.encode()) > 32768
        ):
            raise ValueError("startup_script must be at most 32 KiB")
        if (
            isinstance(self.startup_timeout_seconds, bool)
            or not isinstance(self.startup_timeout_seconds, int)
            or not 1 <= self.startup_timeout_seconds <= 3600
        ):
            raise ValueError("startup_timeout_seconds must be between 1 and 3600")
        if self.conversation_capture not in {"redacted", "metadata_only", "disabled"}:
            raise ValueError("Invalid conversation_capture")
        if self.log_level.upper() not in {
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR",
            "CRITICAL",
        }:
            raise ValueError("Invalid logging level")
        for name in (
            "max_file_bytes",
            "max_context_tokens",
            "max_completion_tokens",
            "api_timeout_seconds",
            "cli_timeout_seconds",
            "workflow_concurrency",
            "discussion_poll_seconds",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or value <= 0:
                raise ValueError(f"{name} must be positive")
        if not 0 <= self.api_max_retries <= 20:
            raise ValueError("api_max_retries must be between 0 and 20")
        if not 1 <= self.max_files_per_iteration <= 5:
            raise ValueError("max_files_per_iteration must be between 1 and 5")
        if self.max_file_bytes > 32768:
            raise ValueError("max_file_bytes exceeds the 32768-byte safety ceiling")
        if self.max_completion_tokens >= self.max_context_tokens:
            raise ValueError("max_completion_tokens must be below max_context_tokens")
        if len(self.scan_cron.split()) != 5:
            raise ValueError("scan_cron must contain five cron fields")
        if not self.worker_label_key.strip():
            raise ValueError("worker_label_key must not be empty")
        if len(set(self.labels.model_dump().values())) != 6:
            raise ValueError("Workflow labels must be unique")
        for model in (self.openai_model, self.codex_model, self.claude_model):
            if model and not re.fullmatch(r"[A-Za-z0-9._:/-]+", model):
                raise ValueError("Model identifier contains unsupported characters")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        agent = env.get("DEVELOPER_AGENT", "api")
        return cls(
            repository=env.get("GITHUB_REPOSITORY", cls.repository),
            worker_type=env.get("WORKER_TYPE", cls.worker_type),
            developer_agent=agent,
            orchestrator=env.get("ORCHESTRATOR", cls.orchestrator),
            worker_name=env.get("HATCHET_WORKER_NAME", ""),
            scan_cron=env.get("SCAN_CRON", cls.scan_cron),
            max_patch_iterations=parse_max_patch_iterations(
                env.get("MAX_PATCH_ITERATIONS", "10")
            ),
            # Migration defaults preserve existing deployments. Applications use only policy.
            path_policy=env.get("PATCH_PATH_POLICY")
            or ("source" if agent == "api" else "repository"),
            state_path=env.get("FACTORY_STATE_PATH", cls.state_path),
            log_level=env.get("LOG_LEVEL", cls.log_level),
            log_format=env.get("LOG_FORMAT", cls.log_format),
            planner_factory=env.get("PLANNER_FACTORY", cls.planner_factory),
            reviewer_factory=env.get("REVIEWER_FACTORY", cls.reviewer_factory),
            coding_factory=env.get("CODING_AGENT_FACTORY", ""),
            observer_factory=env.get("OBSERVER_FACTORY", cls.observer_factory),
            state_factory=env.get("STATE_FACTORY", cls.state_factory),
        )
