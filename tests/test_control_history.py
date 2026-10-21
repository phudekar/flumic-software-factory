import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.settings import Settings
from factory.adapters.control import ControlObserver, redact
from factory.adapters.telemetry import LoggingObserver
from factory.application.telemetry import (
    ModelUsage,
    model_call,
    record_conversation,
    current_recorder,
)


class ControlHistoryTests(unittest.TestCase):
    def capture(self, mode="redacted"):
        publisher = Mock()
        observer = ControlObserver(
            LoggingObserver(), Settings(conversation_capture=mode), publisher=publisher
        )
        return observer, publisher

    def test_exchange_is_redacted_and_linked_to_attempt(self):
        observer, publisher = self.capture()
        with patch.dict(
            os.environ, {"OPENAI_API_KEY": "sk-syntheticcredential1234567890"}
        ):
            with observer.observe(
                "triage_issue", {"factory_run_id": "factory-1", "issue_number": 4}
            ):
                with model_call("openai", "gpt-5-nano") as call:
                    record_conversation(
                        request=[
                            {
                                "role": "system",
                                "content": "sk-syntheticcredential1234567890",
                            }
                        ]
                    )
                    call.record(
                        ModelUsage(
                            input_tokens=100, output_tokens=20, cached_input_tokens=0
                        )
                    )
                    record_conversation(response="Result text")
        calls = [
            args
            for args in publisher.emit.call_args_list
            if args.args[0] == "model.completed"
        ]
        payload, metadata = calls[0].args[1:]
        self.assertEqual(payload["conversation"]["request"][0]["content"], "[REDACTED]")
        self.assertEqual(payload["conversation"]["response"], "Result text")
        self.assertEqual(metadata["factory_run_id"], "factory-1")
        self.assertEqual(metadata["stage"], "triage_issue")
        self.assertTrue(metadata["attempt_id"])
        self.assertEqual(payload["input_tokens"], 100)
        self.assertIsNone(current_recorder())
        observer.shutdown()

    def test_metadata_mode_omits_content_and_failure_does_not_hide_attempt(self):
        observer, publisher = self.capture("metadata_only")
        with (
            self.assertRaises(RuntimeError),
            observer.observe("triage_issue", {"factory_run_id": "failed"}),
        ):
            with model_call("openai", "unknown-model"):
                record_conversation(request="Not retained")
                raise RuntimeError("private exception detail")
        records = publisher.emit.call_args_list
        payload = next(
            item.args[1] for item in records if item.args[0] == "model.completed"
        )
        self.assertNotIn("conversation", payload)
        self.assertEqual(payload["outcome"], "failed")
        self.assertNotIn("private exception detail", str(payload))
        self.assertEqual(records[-1].args[1]["outcome"], "failed")
        observer.shutdown()

    def test_only_actual_nested_accounting_excludes_parent(self):
        observer, publisher = self.capture()
        with observer.observe("generate_and_open_draft_pr", {}):
            with model_call("anthropic", None, agent="claude"):
                with model_call(
                    "anthropic", "reported-model", agent="claude.model"
                ) as call:
                    call.record(ModelUsage(input_tokens=12, output_tokens=3))
            with model_call("anthropic", None, agent="claude"):
                pass
        calls = [
            call.args[1]
            for call in publisher.emit.call_args_list
            if call.args[0] == "model.completed"
        ]
        self.assertTrue(calls[0]["accounting_leaf"])
        self.assertFalse(calls[1]["accounting_leaf"])
        self.assertEqual(calls[0]["parent_call_id"], calls[1]["call_id"])
        self.assertTrue(calls[2]["accounting_leaf"])
        observer.shutdown()

    def test_disabled_capture_does_not_suppress_model_errors(self):
        observer, publisher = self.capture("disabled")
        with self.assertRaises(RuntimeError), observer.observe("triage_issue", {}):
            with model_call("openai", "unknown"):
                raise RuntimeError("failure")
        self.assertFalse(
            any(
                call.args[0] == "model.completed"
                for call in publisher.emit.call_args_list
            )
        )
        observer.shutdown()

    def test_redacts_known_values_patterns_and_bounds_unicode(self):
        self.assertNotIn(
            "abcdefghijklmnopqrstuvwxyz123456",
            redact("sk-abcdefghijklmnopqrstuvwxyz123456"),
        )
        self.assertIn("[TRUNCATED]", redact("界" * 50000))
        self.assertNotIn(
            "PRIVATE KEY",
            redact(
                "-----BEGIN PRIVATE KEY-----\nsecret key material\n-----END PRIVATE KEY-----"
            ).replace("[REDACTED PRIVATE KEY]", ""),
        )


class StartupTests(unittest.TestCase):
    def test_script_runs_and_redacts_output(self):
        import tempfile
        from factory.startup import run_startup_script

        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "installed"
            with patch.dict(
                os.environ, {"TEST_API_KEY": "syntheticcredential987654321"}
            ):
                with self.assertLogs("issue-worker.startup", level="INFO") as logs:
                    run_startup_script(
                        Settings(
                            startup_script=f'touch "{target}"\nprintf "%s" "$TEST_API_KEY"'
                        )
                    )
            self.assertTrue(target.exists())
            self.assertNotIn("syntheticcredential987654321", "\n".join(logs.output))
            self.assertIn("[REDACTED]", "\n".join(logs.output))

    def test_failure_and_timeout_abort_startup(self):
        from factory.startup import run_startup_script

        with self.assertRaisesRegex(RuntimeError, "exit status 7"):
            run_startup_script(Settings(startup_script="exit 7"))
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            run_startup_script(
                Settings(startup_script="sleep 20", startup_timeout_seconds=1)
            )

    def test_startup_limits(self):
        for kwargs in [
            {"startup_script": "x" * 32769},
            {"startup_timeout_seconds": 0},
            {"startup_timeout_seconds": True},
        ]:
            with self.assertRaises(ValueError):
                Settings(**kwargs)


class ManagedProfileLoadingTests(unittest.TestCase):
    def test_load_latest_profile_and_fail_closed_on_outage(self):
        import json
        from factory.configuration import load_managed_settings

        env = {
            "FACTORY_CONTROL_ENDPOINT": "http://control.invalid",
            "WORKER_PROFILE": "simple",
            "FACTORY_CONTROL_WORKER_ID": "worker-id",
            "FACTORY_CONTROL_TOKEN": "test-token",
        }
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(
            {
                "version": 1,
                "defaults": {
                    "openai_model": "profile-latest",
                    "startup_script": "echo prerequisites",
                },
                "workers": {"simple": {}},
            }
        ).encode()
        with patch("urllib.request.urlopen", return_value=response) as send:
            settings = load_managed_settings(env)
        self.assertEqual(settings.openai_model, "profile-latest")
        self.assertEqual(settings.startup_script, "echo prerequisites")
        self.assertEqual(send.call_args.args[0].get_header("X-worker-id"), "worker-id")
        with patch(
            "urllib.request.urlopen", side_effect=OSError("private connection details")
        ):
            with self.assertRaisesRegex(
                RuntimeError, "Could not load the saved worker profile"
            ) as error:
                load_managed_settings(env)
        self.assertNotIn("private connection details", str(error.exception))
