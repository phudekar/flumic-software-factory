import copy
from datetime import timedelta
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from factory.configuration import (
    TaskSettings,
    current_task,
    task_context,
    read_configuration,
)
from factory.settings import Settings
from factory.adapters.github_events import GitHubWebhook, normalize_webhook
from factory.adapters.token_budget import count_request_tokens
from model_client import ModelClient

ROOT = Path(__file__).resolve().parents[1]


class WorkerConfigTests(unittest.TestCase):
    def setUp(self):
        self.config = read_configuration(ROOT / "worker-config.yaml")

    def load(self, config=None, profile="developer"):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "worker-config.yaml"
            path.write_text(yaml.safe_dump(config or self.config))
            return Settings.load(
                {"WORKER_CONFIG_FILE": str(path), "WORKER_PROFILE": profile}
            )

    def test_includes_merge_relative_files_and_inline_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "parts").mkdir()
            (root / "parts/base.yaml").write_text(
                "defaults: {log_level: INFO, labels: {pr_open: custom-pr}}\nworkers: {simple: {}}\n"
            )
            (root / "override.yaml").write_text("defaults: {log_level: WARNING}\n")
            manifest = root / "worker-config.yaml"
            manifest.write_text(
                "version: 1\nincludes: [parts/base.yaml, override.yaml]\ndefaults: {log_level: DEBUG}\n"
            )
            config = read_configuration(manifest)
            self.assertEqual(config["defaults"]["log_level"], "DEBUG")
            self.assertEqual(config["defaults"]["labels"]["pr_open"], "custom-pr")
            self.assertEqual(
                Settings.load({"WORKER_CONFIG_FILE": str(manifest)}).labels.pr_open,
                "custom-pr",
            )

    def test_shared_profile_files_feed_standalone_settings(self):
        for filename, profile in (("default", "simple"), ("developer", "developer")):
            template = read_configuration(
                ROOT / f"config/worker-profiles/{filename}.yaml"
            )
            settings = self.load(profile=profile)
            self.assertEqual(
                settings.startup_script,
                template["document"]["settings"]["startup_script"],
            )
            for task, values in template["document"]["tasks"].items():
                self.assertEqual(
                    settings.task(task).additional_system_prompt,
                    values["additional_system_prompt"],
                )
            self.assertEqual(settings.worker_type, template["worker"]["worker_type"])

    def test_invalid_includes_fail(self):
        for contents in (
            "version: 1\nincludes: [worker-config.yaml]\n",
            "version: 1\nincludes: [../outside.yaml]\n",
            "version: 1\nincludes: [/tmp/outside.yaml]\n",
            "version: 1\nincludes: not-a-list\n",
        ):
            with (
                self.subTest(contents=contents),
                tempfile.TemporaryDirectory() as directory,
            ):
                manifest = Path(directory) / "worker-config.yaml"
                manifest.write_text(contents)
                with self.assertRaises(ValueError):
                    read_configuration(manifest)
        with tempfile.TemporaryDirectory() as directory:
            manifest = Path(directory) / "worker-config.yaml"
            manifest.write_text("version: 1\nincludes: [missing.yaml]\n")
            with self.assertRaises(FileNotFoundError):
                read_configuration(manifest)

    def test_task_defaults_templates_and_overrides_have_explicit_precedence(self):
        self.config["task_defaults"]["retries"] = 4
        self.config["task_templates"]["coding"]["retries"] = 3
        self.config["tasks"]["generate_and_open_draft_pr"]["retries"] = 1
        settings = self.load()
        self.assertEqual(settings.task("triage_issue").retries, 4)
        self.assertEqual(settings.task("process_pull_request_feedback").retries, 3)
        self.assertEqual(settings.task("generate_and_open_draft_pr").retries, 1)
        self.config["tasks"]["generate_and_open_draft_pr"]["template"] = "typo"
        with self.assertRaises(ValueError):
            self.load()
        self.config["tasks"]["generate_and_open_draft_pr"]["template"] = "coding"
        self.config["task_templates"]["unused"] = {"unknown_setting": 1}
        with self.assertRaises(ValueError):
            self.load()

    def test_split_configuration_preserves_existing_task_behavior(self):
        from factory.task_defaults import TASK_DEFAULTS

        settings = self.load()
        for name, defaults in TASK_DEFAULTS.items():
            self.assertEqual(
                settings.task(name).model_dump(exclude={"additional_system_prompt"}),
                TaskSettings.model_validate(defaults).model_dump(
                    exclude={"additional_system_prompt"}
                ),
                name,
            )
        self.config["workers"]["developer"]["codex_model"] = "inherited-model"
        self.assertEqual(
            self.load(profile="developer-observed").codex_model, "inherited-model"
        )

    def test_profiles_and_environment_do_not_override_yaml(self):
        self.config["defaults"]["openai_model"] = "configured-model"
        with patch.dict(
            os.environ, {"OPENAI_MODEL": "ignored-model", "LOG_LEVEL": "ERROR"}
        ):
            settings = self.load()
        self.assertEqual(settings.openai_model, "configured-model")
        self.assertEqual(settings.developer_agent, "codex")
        self.assertEqual(settings.log_level, "INFO")
        self.assertEqual(len(settings.tasks), 16)
        self.assertEqual(
            self.load(profile="simple-observed").observer_factory,
            "factory.adapters.otel:otel_observer",
        )

    def test_invalid_configuration_fails_before_creating_clients(self):
        mutations = [
            lambda c: c["defaults"].update(openai_api_key="placeholder"),
            lambda c: c["workers"]["developer"].update(log_level="TYPO"),
            lambda c: c["workers"]["developer"].update(max_patch_iterations=21),
            lambda c: c["defaults"].update(max_completion_tokens=40000),
            lambda c: c["defaults"].update(scan_cron="invalid"),
            lambda c: c["tasks"]["plan_issue"].update(retries=-1),
            lambda c: c["tasks"]["plan_issue"].update(timeout_seconds=0),
            lambda c: c["tasks"]["plan_issue"].update(model='model"\ninjected'),
            lambda c: c["tasks"].update(unknown_task={}),
            lambda c: c.update(unknown_section={}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                config = copy.deepcopy(self.config)
                mutate(config)
                with self.assertRaises(ValueError):
                    self.load(config)
        with self.assertRaises(ValueError):
            self.load(profile="missing")

    def test_duplicate_keys_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text("version: 1\nversion: 1\nworkers: {simple: {}}\n")
            with self.assertRaisesRegex(ValueError, "Duplicate"):
                Settings.load({"WORKER_CONFIG_FILE": str(path)})

    def test_model_task_prompt_and_limits_reach_api_request(self):
        settings = self.load()
        response = Mock(choices=[Mock(message=Mock(content="{}"))], usage=None)
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-placeholder"}),
            patch("model_client.OpenAI") as client,
        ):
            model = ModelClient(settings=settings)
            client.return_value.chat.completions.create.return_value = response
            messages = [
                {"role": "system", "content": "Built-in rules."},
                {"role": "user", "content": "Issue data."},
            ]
            with task_context(
                settings.task("triage_issue").model_copy(
                    update={"model": "gpt-4.1-mini"}
                )
            ):
                model._complete(messages)
            request = client.return_value.chat.completions.create.call_args.kwargs
            self.assertEqual(request["model"], "gpt-4.1-mini")
            self.assertEqual(
                request["max_completion_tokens"], settings.max_completion_tokens
            )
            self.assertIn("Built-in rules.", request["messages"][0]["content"])
            self.assertIn(
                settings.task("triage_issue").additional_system_prompt,
                request["messages"][0]["content"],
            )
            self.assertEqual(messages[0]["content"], "Built-in rules.")
            self.assertEqual(
                client.call_args.kwargs["max_retries"], settings.api_max_retries
            )
        self.assertIsNone(current_task().model)
        with self.assertRaises(ValueError):
            count_request_tokens(
                messages, max_context_tokens=10, max_completion_tokens=5
            )

    def test_cli_uses_profile_file_limit_above_default(self):
        import cli_agent
        from dataclasses import replace

        settings = replace(self.load(), max_file_bytes=70000)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "workspaces").mkdir()

            def run(agent, workspace, prompt, **options):
                self.assertIn("at most 70000 bytes", prompt)
                (workspace / "large.txt").write_text("x" * 66000)
                return '{"is_complete":true,"progress":"done","pull_title":"Done","pull_body":"Complete"}'

            with (
                patch.object(cli_agent, "AGENT_HOME", home),
                patch.object(cli_agent, "_extract_archive"),
                patch.object(cli_agent, "_set_agent_ownership"),
            ):
                plan = cli_agent.generate_patch(
                    title="Implement",
                    body="Description",
                    comments=[],
                    existing_paths=set(),
                    archive=b"",
                    agent="codex",
                    settings=settings,
                    runner=run,
                )
            self.assertEqual(len(plan.changes[0].content), 66000)

    def test_cli_receives_task_model_prompt_and_timeout(self):
        import cli_agent

        settings = self.load()
        settings = __import__("dataclasses").replace(settings, cli_timeout_seconds=77)
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / "workspaces").mkdir()
            captured = []

            def run(agent, workspace, prompt, **options):
                captured.append((agent, prompt, options))
                return '{"is_complete":true,"progress":"done","pull_title":"Done","pull_body":"Complete"}'

            with (
                patch.object(cli_agent, "AGENT_HOME", home),
                patch.object(cli_agent, "_extract_archive"),
                patch.object(cli_agent, "_set_agent_ownership"),
                patch.object(cli_agent, "_run_agent", side_effect=run),
                task_context(
                    settings.task("generate_and_open_draft_pr").model_copy(
                        update={"model": "task-cli-model"}
                    )
                ),
            ):
                cli_agent.generate_patch(
                    title="Implement",
                    body="Description",
                    comments=[],
                    existing_paths=set(),
                    archive=b"",
                    agent="codex",
                    settings=settings,
                )
            agent, prompt, options = captured[0]
            self.assertEqual(agent, "codex")
            self.assertIn(
                settings.task("generate_and_open_draft_pr").additional_system_prompt,
                prompt,
            )
            self.assertIn("Do not access credentials", prompt)
            self.assertEqual(
                options, {"model": "task-cli-model", "timeout_seconds": 77}
            )

    def test_task_context_resets_after_errors(self):
        with self.assertRaises(RuntimeError), task_context(TaskSettings(model="outer")):
            with task_context(TaskSettings(model="inner")):
                self.assertEqual(current_task().model, "inner")
            self.assertEqual(current_task().model, "outer")
            raise RuntimeError()
        self.assertIsNone(current_task().model)

    def test_custom_ready_label_is_recognized_by_webhook(self):
        payload = GitHubWebhook(
            action="labeled",
            repository={"full_name": "owner/repo"},
            issue={"number": 1, "title": "Issue", "state": "open"},
            label={"name": "ready-to-build"},
        )
        self.assertEqual(
            normalize_webhook(payload, "issues", ready_label="ready-to-build").kind,
            "work-item-ready",
        )
        self.assertEqual(normalize_webhook(payload, "issues").kind, "ignored")

    def test_hatchet_receives_task_limits_labels_and_cron(self):
        from factory.adapters.orchestration.hatchet import build_worker, HatchetDispatch

        self.config["defaults"].update(
            scan_cron="0 * * * *", worker_label_key="role", workflow_concurrency=7
        )
        self.config["tasks"]["generate_and_open_draft_pr"].update(
            retries=4, timeout_seconds=450
        )
        settings = self.load()
        options = {}
        engine = Mock()

        def decorator(**kwargs):
            options[kwargs["name"]] = kwargs
            return lambda fn: fn

        engine.task.side_effect = decorator
        engine.durable_task.side_effect = decorator
        engine.workflow.return_value.task.side_effect = decorator
        build_worker(engine, Mock(), settings, HatchetDispatch())
        self.assertEqual(engine.workflow.call_args.kwargs["concurrency"][1].max_runs, 7)
        task = options["generate-and-open-draft-pr"]
        self.assertEqual(task["retries"], 4)
        self.assertEqual(task["execution_timeout"], timedelta(seconds=450))
        self.assertEqual(task["desired_worker_labels"][0].key, "role")
        self.assertEqual(
            options["scan-pending-github-issues"]["on_crons"], ["0 * * * *"]
        )
        self.assertEqual(
            engine.worker.call_args.kwargs["labels"], {"role": "developer"}
        )

    def test_compose_passes_only_credentials_and_selectors(self):
        config = yaml.safe_load((ROOT / "compose.yaml").read_text())
        allowed = {
            "WORKER_PROFILE",
            "WORKER_CONFIG_FILE",
            "HATCHET_CLIENT_TOKEN",
            "GITHUB_TOKEN",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
        }
        for service in config["services"].values():
            self.assertFalse(set(service["environment"]) - allowed)
            self.assertIn(
                "./worker-config.yaml:/config/worker-config.yaml:ro", service["volumes"]
            )
