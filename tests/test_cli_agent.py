import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

import cli_agent
from model_client import ModelError


def make_archive(entries: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, contents in entries.items():
            member = tarfile.TarInfo(f"repository/{name}")
            member.size = len(contents)
            archive.addfile(member, io.BytesIO(contents))
    return output.getvalue()


class CLIAgentTests(unittest.TestCase):
    def setUp(self):
        cli_agent._CODEX_AUTHENTICATED_KEY_HASH = None

    def test_generation_accumulates_changes_across_iterations(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            agent_home = Path(temporary_directory)
            (agent_home / "workspaces").mkdir()
            archive = make_archive({"src/main.py": b"print('before')\n"})
            responses = [
                json.dumps(
                    {
                        "is_complete": False,
                        "progress": "Updated the implementation.",
                        "pull_title": "Implement feature",
                        "pull_body": "Adds the feature.",
                    }
                ),
                json.dumps(
                    {
                        "is_complete": True,
                        "progress": "Implementation is complete.",
                        "pull_title": "Implement feature",
                        "pull_body": "Adds the feature.",
                    }
                ),
            ]

            def run_agent(_agent, workspace, _prompt):
                if len(responses) == 2:
                    (workspace / "src/main.py").write_text(
                        "print('updated')\n", encoding="utf-8"
                    )
                    dependencies = workspace / "node_modules/.bin"
                    dependencies.mkdir(parents=True)
                    (dependencies / "tool").symlink_to("../tool/bin.js")
                    (dependencies.parent / "generated.txt").write_text("dependency")
                else:
                    self.assertTrue((workspace / "node_modules/.bin/tool").is_symlink())
                    (workspace / "src/extra.py").write_text(
                        "print('extra')\n", encoding="utf-8"
                    )
                return responses.pop(0)

            with (
                patch.object(cli_agent, "AGENT_HOME", agent_home),
                patch.object(cli_agent, "AGENT_UID", os.getuid()),
                patch.object(cli_agent, "AGENT_GID", os.getgid()),
                patch.dict(os.environ, {"DEVELOPER_AGENT": "codex"}),
                patch.object(cli_agent, "_run_agent", side_effect=run_agent) as run,
            ):
                plan = cli_agent.generate_patch(
                    title="Implement feature",
                    body="Add it.",
                    comments=[],
                    existing_paths={"src/main.py"},
                    archive=archive,
                )

        self.assertEqual(run.call_count, 2)
        self.assertTrue(plan.is_complete)
        self.assertEqual(
            {change.path: change.content for change in plan.changes},
            {
                "src/main.py": "print('updated')\n",
                "src/extra.py": "print('extra')\n",
            },
        )

    def test_archive_rejects_parent_directory_traversal(self):
        archive = make_archive({"../outside.py": b"bad"})
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ModelError, "unsafe path"):
                cli_agent._extract_archive(archive, Path(temporary_directory))

    def test_change_collection_allows_updating_dependency_manifest(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            manifest = workspace / "Cargo.toml"
            manifest.write_text('[package]\nname = "before"\n', encoding="utf-8")
            baseline = cli_agent._snapshot(workspace)
            manifest.write_text('[package]\nname = "after"\n', encoding="utf-8")

            changes = cli_agent._collect_changes(
                workspace=workspace,
                baseline=baseline,
                previous=baseline,
                existing_paths={"Cargo.toml"},
            )

        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].path, "Cargo.toml")
        self.assertIn('name = "after"', changes[0].content)

    def test_change_collection_allows_vite_config(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            vite_config = workspace / "frontend/vite.config.ts"
            vite_config.parent.mkdir()
            vite_config.write_text("export default {};\n", encoding="utf-8")
            baseline = cli_agent._snapshot(workspace)
            vite_config.write_text(
                "export default { server: { port: 5173 } };\n", encoding="utf-8"
            )

            changes = cli_agent._collect_changes(
                workspace=workspace,
                baseline=baseline,
                previous=baseline,
                existing_paths={"frontend/vite.config.ts"},
            )

        self.assertEqual(
            [change.path for change in changes], ["frontend/vite.config.ts"]
        )

    def test_change_collection_ignores_generated_python_bytecode(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "hello_world/hello.py"
            source.parent.mkdir()
            source.write_text("print('hello')\n", encoding="utf-8")
            baseline = cli_agent._snapshot(workspace)
            cache = source.parent / "__pycache__/hello.cpython-311.pyc"
            cache.parent.mkdir()
            cache.write_bytes(b"\xa7\r\r\ncompiled")
            standalone_bytecode = workspace / "module.pyo"
            standalone_bytecode.write_bytes(b"compiled")

            changes = cli_agent._collect_changes(
                workspace=workspace,
                baseline=baseline,
                previous=baseline,
                existing_paths={"hello_world/hello.py"},
            )

        self.assertEqual(changes, [])

    def test_change_collection_allows_any_safe_repository_text_path(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            changed_files = {
                ".github/workflows/test.yml": "name: test\n",
                "Cargo.lock": "version = 3\n",
                "src/config.toml": "[application]\n",
                "Dockerfile": "FROM python:3.12\n",
            }
            for relative_path, content in changed_files.items():
                path = workspace / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")

            baseline = cli_agent._snapshot(workspace)
            for relative_path, content in changed_files.items():
                path = workspace / relative_path
                path.write_text(content + "# updated\n", encoding="utf-8")

            changes = cli_agent._collect_changes(
                workspace=workspace,
                baseline=baseline,
                previous=baseline,
                existing_paths=set(changed_files),
            )

        self.assertEqual({change.path for change in changes}, set(changed_files))

    def test_change_collection_prunes_new_generated_directories(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "src/main.py"
            source.parent.mkdir()
            source.write_text("before\n")
            baseline = cli_agent._snapshot(workspace)
            source.write_text("after\n")
            for name in cli_agent.GENERATED_DIRECTORY_NAMES:
                generated = workspace / "frontend" / name
                generated.mkdir(parents=True)
                (generated / "generated.txt").write_text("not a patch\n")
                (generated / "external").symlink_to("/outside/workspace")
                (generated / "loop").symlink_to(generated, target_is_directory=True)

            changes = cli_agent._collect_changes(
                workspace=workspace,
                baseline=baseline,
                previous=baseline,
                existing_paths=set(baseline),
            )

        self.assertEqual(
            [(c.path, c.content) for c in changes], [("src/main.py", "after\n")]
        )

    def test_original_generated_directory_files_remain_visible(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            source = workspace / "venv/source.py"
            source.parent.mkdir()
            source.write_text("before\n")
            baseline = cli_agent._snapshot(workspace)
            source.write_text("after\n")
            changes = cli_agent._collect_changes(
                workspace=workspace,
                baseline=baseline,
                previous=baseline,
                existing_paths=set(baseline),
            )
            self.assertEqual([c.path for c in changes], ["venv/source.py"])
            source.unlink()
            with self.assertRaisesRegex(ModelError, "deleted files"):
                cli_agent._collect_changes(
                    workspace=workspace,
                    baseline=baseline,
                    previous=baseline,
                    existing_paths=set(baseline),
                )

    def test_snapshot_rejects_source_and_generated_root_symlinks(self):
        for name, target in [
            ("source.py", "missing.py"),
            ("source.py", "/etc/passwd"),
            ("node_modules", "/tmp"),
        ]:
            with (
                self.subTest(name=name, target=target),
                tempfile.TemporaryDirectory() as temporary_directory,
            ):
                workspace = Path(temporary_directory)
                (workspace / name).symlink_to(target)
                with self.assertRaisesRegex(ModelError, f"symbolic link: {name}"):
                    cli_agent._snapshot(workspace, baseline={})

    def test_snapshot_preserves_original_empty_generated_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            workspace = Path(temporary_directory)
            generated = workspace / "node_modules"
            generated.mkdir()
            baseline = cli_agent._snapshot(workspace)
            (generated / "link").symlink_to("missing")
            with self.assertRaisesRegex(ModelError, "symbolic link: node_modules/link"):
                cli_agent._snapshot(
                    workspace,
                    baseline=baseline,
                    original_directories={"node_modules"},
                )

    def test_cli_receives_provider_key_but_not_worker_credentials(self):
        completion = '{"is_complete":true,"progress":"done","pull_title":"Done","pull_body":"Completed."}'
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            workspace = home / "workspace"
            workspace.mkdir()

            def run(command, **kwargs):
                if command[1:3] == ["login", "--with-api-key"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(completion, encoding="utf-8")
                schema_path = Path(command[command.index("--output-schema") + 1])
                schema = json.loads(schema_path.read_text(encoding="utf-8"))
                self.assertIn("is_complete", schema["properties"])
                self.assertIn("pull_title", schema["required"])
                self.assertIs(schema["additionalProperties"], False)
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            with (
                patch.object(cli_agent, "AGENT_HOME", home),
                patch.object(cli_agent, "AGENT_UID", os.getuid()),
                patch.object(cli_agent, "AGENT_GID", os.getgid()),
                patch.dict(
                    os.environ,
                    {
                        "GITHUB_TOKEN": "github-secret",
                        "HATCHET_CLIENT_TOKEN": "hatchet-secret",
                        "OPENAI_API_KEY": "real-provider-key",
                        "CODEX_MODEL": "gpt-6-luna-test",
                    },
                ),
                patch("cli_agent.subprocess.run", side_effect=run) as run_mock,
            ):
                response = cli_agent._run_agent("codex", workspace, "prompt")
                codex_config = (home / ".codex/config.toml").read_text(encoding="utf-8")

        self.assertEqual(response, completion)
        command = run_mock.call_args.args[0]
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
        login_call = next(
            call
            for call in run_mock.call_args_list
            if call.args[0][1:3] == ["login", "--with-api-key"]
        )
        self.assertEqual(login_call.kwargs["input"], "real-provider-key")
        environment = run_mock.call_args.kwargs["env"]
        self.assertEqual(environment["OPENAI_API_KEY"], "real-provider-key")
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("HATCHET_CLIENT_TOKEN", environment)
        self.assertNotIn("ANTHROPIC_API_KEY", environment)
        self.assertNotIn("openai_base_url", codex_config)
        self.assertIn('model = "gpt-6-luna-test"', codex_config)
        self.assertIn("--output-schema", command)

    def test_codex_output_is_logged_without_prompt_or_credentials(self):
        completion = '{"is_complete":true,"progress":"done","pull_title":"Done","pull_body":"Completed."}'
        prompt = "implement requested change"
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            workspace = home / "workspace"
            workspace.mkdir()

            def run(command, **kwargs):
                if command[1:3] == ["login", "--with-api-key"]:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                output_path = Path(command[command.index("--output-last-message") + 1])
                output_path.write_text(completion, encoding="utf-8")
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"Codex started: {prompt}",
                    stderr="Authorization: Bearer provider-secret-token",
                )

            with (
                patch.object(cli_agent, "AGENT_HOME", home),
                patch.object(cli_agent, "AGENT_UID", os.getuid()),
                patch.object(cli_agent, "AGENT_GID", os.getgid()),
                patch.dict(
                    os.environ,
                    {"OPENAI_API_KEY": "provider-secret-token-123"},
                ),
                patch("cli_agent.subprocess.run", side_effect=run),
                self.assertLogs("issue-worker.cli-agent", level="INFO") as logs,
            ):
                cli_agent._run_agent("codex", workspace, prompt)

        output = "\n".join(logs.output)
        self.assertIn("CLI process finished", output)
        self.assertNotIn("Codex started:", output)
        self.assertNotIn(prompt, output)
        self.assertNotIn("provider-secret-token", output)

    def test_cli_login_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            workspace = home / "workspace"
            workspace.mkdir()
            with (
                patch.object(cli_agent, "AGENT_HOME", home),
                patch.object(cli_agent, "AGENT_UID", os.getuid()),
                patch.object(cli_agent, "AGENT_GID", os.getgid()),
                patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}),
                patch(
                    "cli_agent.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=1,
                        stdout="",
                        stderr=("HTTP 403 Forbidden: Bearer sk-sensitive-token"),
                    ),
                ),
            ):
                with self.assertRaises(ModelError) as raised:
                    cli_agent._run_agent("codex", workspace, "private prompt")

        self.assertEqual(str(raised.exception), "Codex API key login failed")

    def test_cli_failure_keeps_diagnostic_without_echoing_prompt(self):
        prompt = (
            "Work only in this repository. Implement the issue and run tests. "
            "Do not access credentials."
        )
        stderr = (
            "OpenAI Codex v0.163.0\n"
            "workdir: /workspace\n"
            f"user {prompt}\n"
            "ERROR codex_api::client: request failed with HTTP 403 Forbidden\n"
        )

        details = cli_agent._sanitize_cli_stderr(stderr, prompt=prompt)

        self.assertIn("HTTP 403 Forbidden", details)
        self.assertNotIn(prompt, details)
        self.assertNotIn("Do not access credentials", details)

    def test_cli_failure_extracts_structured_provider_error(self):
        stderr = (
            'ERROR: {"type":"error","error":{"type":"invalid_request_error",'
            '"code":"model_not_found","status":403,'
            '"message":"The requested model is not available."}}'
        )

        details = cli_agent._sanitize_cli_stderr(stderr)

        self.assertIn("type=invalid_request_error", details)
        self.assertIn("code=model_not_found", details)
        self.assertIn("status=403", details)
        self.assertIn("The requested model is not available.", details)

    def test_cli_failure_redacts_prompt_inside_structured_provider_error(self):
        prompt = "do not disclose this repository context"
        stderr = (
            'ERROR: {"error":{"type":"invalid_request_error",'
            f'"message":"Request failed for {prompt}"' + "}}"
        )

        details = cli_agent._sanitize_cli_stderr(stderr, prompt=prompt)

        self.assertNotIn(prompt, details)
        self.assertIn("[PROMPT REDACTED]", details)

    def test_claude_cli_receives_only_direct_anthropic_key(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            home = Path(temporary_directory)
            workspace = home / "workspace"
            workspace.mkdir()
            with (
                patch.object(cli_agent, "AGENT_HOME", home),
                patch.object(cli_agent, "AGENT_UID", os.getuid()),
                patch.object(cli_agent, "AGENT_GID", os.getgid()),
                patch.dict(
                    os.environ,
                    {
                        "ANTHROPIC_API_KEY": "test-anthropic-key",
                        "OPENAI_API_KEY": "test-openai-key",
                    },
                ),
                patch(
                    "cli_agent.subprocess.run",
                    return_value=SimpleNamespace(
                        returncode=0, stdout="completion", stderr=""
                    ),
                ) as run_mock,
            ):
                response = cli_agent._run_agent("claude", workspace, "prompt")

        environment = run_mock.call_args.kwargs["env"]
        self.assertEqual(environment["ANTHROPIC_API_KEY"], "test-anthropic-key")
        self.assertNotIn("OPENAI_API_KEY", environment)
        self.assertNotIn("GITHUB_TOKEN", environment)
        self.assertNotIn("HATCHET_CLIENT_TOKEN", environment)
        self.assertEqual(response, "completion")

    def test_codex_model_rejects_config_injection(self):
        with patch.dict(os.environ, {"CODEX_MODEL": 'valid"\nmodel_provider="other'}):
            with self.assertRaisesRegex(ModelError, "unsupported characters"):
                cli_agent.selected_codex_model()

    def test_parser_finds_completion_json_after_prose_and_other_json(self):
        response = (
            'Codex notes: {"step": "done"}\n'
            "Final response:\n```json\n"
            '{"is_complete":true,"progress":"done","pull_title":"Done",'
            '"pull_body":"Completed."}\n```'
        )

        completion = cli_agent._parse_completion(response)

        self.assertTrue(completion.is_complete)
        self.assertEqual(completion.pull_title, "Done")

    def test_parser_reports_invalid_fields_without_echoing_response(self):
        response = (
            'Here is the answer: {"is_complete":"not-a-bool",'
            '"private":"sensitive-value"}'
        )

        with self.assertRaises(ModelError) as raised:
            cli_agent._parse_completion(response)

        self.assertIn("is_complete (bool_parsing)", str(raised.exception))
        self.assertNotIn("sensitive-value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
