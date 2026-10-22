"""Shared actions must never resolve credentials from another invocation."""

import os
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest
from unittest.mock import patch
from factory.settings import Settings
import github_client
import model_client


class ActionCredentialTests(unittest.TestCase):
    def test_concurrent_explicit_client_keys_do_not_mutate_environment(self):
        before = dict(os.environ)

        def invoke(name):
            github = github_client.GitHubClient(name + "/repo", token="gh-" + name)
            model = model_client.ModelClient(
                api_key="sk-" + name, settings=Settings(repository=name + "/repo")
            )
            return github.repository, github._token, model.client.api_key

        with ThreadPoolExecutor(max_workers=2) as pool:
            try:
                result = list(pool.map(invoke, ["alpha", "beta"]))
            except TypeError as exc:
                self.fail("explicit credentials are unsupported: " + str(exc))
        self.assertEqual(
            result,
            [
                ("alpha/repo", "gh-alpha", "sk-alpha"),
                ("beta/repo", "gh-beta", "sk-beta"),
            ],
        )
        self.assertEqual(dict(os.environ), before)

    def test_explicit_empty_key_cannot_fall_back_to_environment(self):
        with patch.dict(
            os.environ,
            {"GITHUB_TOKEN": "global-token", "OPENAI_API_KEY": "global-model"},
        ):
            with self.assertRaises(github_client.GitHubError):
                github_client.GitHubClient("a/b", token="")
            with self.assertRaises(model_client.ModelError):
                model_client.ModelClient(api_key="")

    def test_private_context_secrets_and_effective_settings(self):
        from factory.workflows.contracts import ActionContext

        context = ActionContext(
            project_id="p",
            repository_id="repo",
            repository_revision_id="r1",
            config_revision_id="cfg",
            run_id="r",
            stage_id="s",
            execution_id="e",
            attempt=1,
            assignment_id="assignment",
            namespace="p/r/e",
            settings_json='{"repository":"alpha/repo"}',
        )
        context = context.with_credentials({"github": "super-private-value"})
        self.assertNotIn("super-private-value", context.model_dump_json())
        self.assertNotIn("super-private-value", repr(context))
        self.assertEqual(context.credential("github"), "super-private-value")
        self.assertEqual(context.settings.repository, "alpha/repo")
        with self.assertRaises(TypeError):
            context.credentials["github"] = "changed"

    def test_cli_uses_separate_homes_and_child_environment(self):
        import cli_agent
        from types import SimpleNamespace

        captures = []

        def run(command, **kwargs):
            captures.append(kwargs["env"])
            return SimpleNamespace(
                returncode=0,
                stdout='{"type":"result","subtype":"success","result":"done"}',
                stderr="",
            )

        before = dict(os.environ)
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli_agent.subprocess, "run", side_effect=run),
            patch.object(cli_agent, "read_claude_result", return_value="done"),
        ):
            for name in ("a", "b"):
                home = Path(tmp) / name
                home.mkdir()
                cli_agent._run_agent(
                    "claude",
                    home,
                    "prompt",
                    credentials={"anthropic": "key-" + name},
                    agent_home=home,
                )
        self.assertNotEqual(captures[0]["HOME"], captures[1]["HOME"])
        self.assertEqual([e["ANTHROPIC_API_KEY"] for e in captures], ["key-a", "key-b"])
        self.assertEqual(dict(os.environ), before)

    def test_action_service_isolates_factories_and_redacts_outputs_and_errors(self):
        from factory.workflows.contracts import ActionContext
        from factory.bootstrap import action_service
        from factory.workflows.redaction import redact
        from unittest.mock import Mock

        def invoke(name):
            context = ActionContext(
                project_id=name,
                repository_id=name,
                repository_revision_id="repo-v1",
                config_revision_id="cfg-v1",
                workflow_revision_id="wf-v1",
                run_id="run",
                stage_id="stage",
                execution_id=name,
                attempt=1,
                assignment_id="assignment",
                namespace=name,
                settings_json=__import__("json").dumps({"repository": name + "/repo"}),
            )
            context = context.with_credentials(
                {"github": "private-gh-" + name, "openai": "private-model-" + name}
            )
            with action_service(context, dispatch=Mock(), state=Mock()) as service:
                gh = service.repositories(context.settings.repository)
                model = service.planner()
                home = service.coding_agent()
                return (
                    gh.repository,
                    gh._token,
                    model.client.api_key,
                    redact({"private-gh-" + name: "private-model-" + name}),
                    str(home),
                )

        before = dict(os.environ)
        with ThreadPoolExecutor(max_workers=2) as pool:
            a, b = list(pool.map(invoke, ["alpha", "beta"]))
        self.assertEqual(
            a[:3], ("alpha/repo", "private-gh-alpha", "private-model-alpha")
        )
        self.assertEqual(b[:3], ("beta/repo", "private-gh-beta", "private-model-beta"))
        self.assertEqual(a[3], {"[REDACTED]": "[REDACTED]"})
        self.assertEqual(b[3], {"[REDACTED]": "[REDACTED]"})
        self.assertEqual(dict(os.environ), before)

    def test_action_redaction_covers_logging_cli_errors_and_result(self):
        from factory.workflows.redaction import secret_scope, redact
        from factory.adapters.telemetry import (
            JsonFormatter,
            CorrelationFilter,
            LoggingObserver,
        )
        from factory.adapters.otel import safe_attributes
        from factory.workflows.contracts import ActionResult, UsageRecord
        import logging
        import cli_agent
        from types import SimpleNamespace

        secret = "a-short-custom-key"
        with secret_scope([secret]):
            record = logging.LogRecord(
                "test", logging.ERROR, "", 0, "failed %s", (secret,), None
            )
            self.assertNotIn(secret, JsonFormatter().format(record))
            with LoggingObserver().observe("test", {"repository": secret}):
                CorrelationFilter().filter(record)
                self.assertNotIn(secret, str(record.factory_context))
            self.assertNotIn(secret, str(safe_attributes({"repository": secret})))
            with (
                tempfile.TemporaryDirectory() as tmp,
                patch.object(
                    cli_agent.subprocess,
                    "run",
                    return_value=SimpleNamespace(
                        returncode=1, stdout="", stderr="error " + secret
                    ),
                ),
            ):
                with self.assertRaises(model_client.ModelError) as failure:
                    cli_agent._run_agent(
                        "claude",
                        Path(tmp),
                        "prompt",
                        credentials={"anthropic": secret},
                        agent_home=Path(tmp),
                    )
                self.assertNotIn(secret, str(failure.exception))
            self.assertNotIn(
                secret,
                ActionResult(
                    output={"result": secret},
                    usage=[UsageRecord(id="call", provider="test", model=secret)],
                ).model_dump_json(),
            )
        self.assertEqual(redact(secret), secret)

    def test_codex_same_key_authenticates_each_isolated_home(self):
        import cli_agent
        from types import SimpleNamespace

        homes = []

        def run(command, **kwargs):
            if command[:2] == ["codex", "login"]:
                homes.append(kwargs["env"]["HOME"])
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.object(cli_agent.subprocess, "run", side_effect=run):
            for home in ["/tmp/isolated-a", "/tmp/isolated-b"]:
                self.assertIsNone(
                    cli_agent._ensure_codex_authenticated(
                        {"HOME": home, "OPENAI_API_KEY": "same-key"}
                    )
                )
        self.assertEqual(homes, ["/tmp/isolated-a", "/tmp/isolated-b"])

    def test_action_scope_cleans_home_and_sanitizes_escaping_errors(self):
        from factory.bootstrap import action_service
        from factory.workflows.contracts import ActionContext
        from unittest.mock import Mock

        context = ActionContext(
            project_id="p",
            repository_id="r",
            repository_revision_id="rv",
            config_revision_id="cv",
            run_id="run",
            stage_id="s",
            execution_id="e",
            attempt=1,
            assignment_id="a",
            namespace="p/r",
            settings_json='{"repository":"a/b","developer_agent":"claude"}',
        ).with_credentials({"anthropic": "private-custom-secret"})
        with self.assertRaises(Exception) as error:
            with action_service(context, dispatch=Mock(), state=Mock()) as service:
                home = service.coding_agent().agent_home
                self.assertTrue(home.exists())
                raise RuntimeError("failed with private-custom-secret")
        self.assertFalse(home.exists())
        self.assertNotIn("private-custom-secret", str(error.exception))

    def test_codex_child_uses_private_home_and_output_files(self):
        import cli_agent
        from types import SimpleNamespace

        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            if command[:2] == ["codex", "exec"]:
                output = Path(command[command.index("--output-last-message") + 1])
                self.assertEqual(output.parent, Path(kwargs["env"]["HOME"]))
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
                output.write_text("completed")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(cli_agent.subprocess, "run", side_effect=run),
        ):
            home = Path(tmp)
            self.assertEqual(
                cli_agent._run_agent(
                    "codex",
                    home,
                    "prompt",
                    model="test-model",
                    credentials={"openai": "private-key"},
                    agent_home=home,
                ),
                "completed",
            )
            self.assertEqual(calls[-1][1]["env"]["CODEX_HOME"], str(home / ".codex"))
            self.assertEqual(list(home.glob("codex-last-message-*")), [])

    def test_action_repository_factory_rejects_repository_outside_context(self):
        from factory.bootstrap import action_service
        from factory.workflows.contracts import ActionContext
        from unittest.mock import Mock

        context = ActionContext(
            project_id="p",
            repository_id="r",
            repository_revision_id="rv",
            config_revision_id="cv",
            run_id="run",
            stage_id="s",
            execution_id="e",
            attempt=1,
            assignment_id="a",
            namespace="p/r",
            settings_json='{"repository":"a/b"}',
        ).with_credentials({"github": "private-key"})
        with action_service(context, dispatch=Mock(), state=Mock()) as service:
            with self.assertRaises(ValueError):
                service.repositories("other/repository")
