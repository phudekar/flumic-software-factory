import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

import codex_healthcheck
import cli_agent


class CodexHealthcheckTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.agent_home = Path(self.temporary_directory.name)
        cli_agent._CODEX_AUTHENTICATED_KEY_HASH = None

    def test_missing_api_key_fails_without_running_codex(self):
        with (
            patch.dict(os.environ, {"OPENAI_API_KEY": ""}),
            patch("codex_healthcheck.subprocess.run") as run_mock,
        ):
            error = codex_healthcheck.check_codex_connection()

        self.assertEqual(error, "OPENAI_API_KEY is not set")
        run_mock.assert_not_called()

    def test_success_uses_direct_credentials_and_selected_model(self):
        def run(command, **kwargs):
            if command[1:3] == ["login", "--with-api-key"]:
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            output_path = Path(command[command.index("--output-last-message") + 1])
            output_path.write_text(
                codex_healthcheck.EXPECTED_RESPONSE, encoding="utf-8"
            )
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with (
            patch.object(codex_healthcheck, "AGENT_HOME", self.agent_home),
            patch.object(codex_healthcheck, "AGENT_UID", os.getuid()),
            patch.object(codex_healthcheck, "AGENT_GID", os.getgid()),
            patch.dict(
                os.environ,
                {
                    "GITHUB_TOKEN": "github-secret",
                    "HATCHET_CLIENT_TOKEN": "hatchet-secret",
                    "OPENAI_API_KEY": "real-provider-key",
                    "CODEX_MODEL": "gpt-6-luna-test",
                },
            ),
            patch("codex_healthcheck.subprocess.run", side_effect=run) as run_mock,
        ):
            error = codex_healthcheck.check_codex_connection()
            config = (self.agent_home / ".codex/config.toml").read_text(
                encoding="utf-8"
            )

        self.assertIsNone(error)
        self.assertNotIn("openai_base_url", config)
        self.assertIn('model = "gpt-6-luna-test"', config)
        command = run_mock.call_args.args[0]
        self.assertIn("--ephemeral", command)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        environment = run_mock.call_args.kwargs["env"]
        self.assertEqual(environment["OPENAI_API_KEY"], "real-provider-key")
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("HATCHET_CLIENT_TOKEN", environment)

    def test_failed_codex_login_marks_healthcheck_failed(self):
        with (
            patch.object(codex_healthcheck, "AGENT_HOME", self.agent_home),
            patch.object(codex_healthcheck, "AGENT_UID", os.getuid()),
            patch.object(codex_healthcheck, "AGENT_GID", os.getgid()),
            patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}),
            patch(
                "codex_healthcheck.subprocess.run",
                return_value=SimpleNamespace(returncode=1, stdout="", stderr=""),
            ),
        ):
            error = codex_healthcheck.check_codex_connection()

        self.assertEqual(error, "Codex API key login failed")


if __name__ == "__main__":
    unittest.main()
