"""The opt-in harness must preserve its own cleanup and credential boundaries."""

import os
import sys
import unittest
from unittest.mock import Mock, patch
from integration import test_project_release_contract as live


class ReleaseHarnessSafetyTests(unittest.TestCase):
    def test_container_cleanup_failure_still_stops_api(self):
        harness = live.LiveProjectReleaseContract()
        harness.engine_roots_settled = True
        harness.jobs = Mock()
        harness.server = Mock()
        harness.stop_processes = Mock()
        harness.cleanup_jobs = Mock(side_effect=RuntimeError("daemon unavailable"))
        harness.stop_server = Mock()
        with self.assertRaisesRegex(RuntimeError, "daemon unavailable"):
            harness.stop_live_resources()
        harness.stop_server.assert_called_once()

    def test_shared_process_has_no_test_database_or_migration_credentials(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from types import SimpleNamespace

        harness = live.LiveProjectReleaseContract()
        with TemporaryDirectory() as directory:
            harness.fixture = SimpleNamespace(
                root=Path(directory),
                settings=Mock(),
                workers={"simple": {"id": "worker"}},
            )
            harness.processes = []
            harness.logs = []
            harness.configs = {"simple": Path(directory) / "config.json"}
            with (
                patch.dict(
                    os.environ,
                    {
                        "CONTROL_TEST_DATABASE_URL": "private-db",
                        "GITLAB_TOKEN": "private-source-token",
                        "CONTROL_MIGRATION_TEST_URL": "private-migration",
                    },
                ),
                patch.dict(
                    sys.modules,
                    {
                        "app": Mock(),
                        "app.reconciler": SimpleNamespace(
                            worker_token=lambda *_: "scoped-worker-token"
                        ),
                    },
                ),
                patch("subprocess.Popen") as spawn,
            ):
                harness.process("simple")
            env = spawn.call_args.kwargs["env"]
            for key in ("CONTROL_TEST_DATABASE_URL", "CONTROL_MIGRATION_TEST_URL", "GITLAB_TOKEN"):
                self.assertFalse(
                    key in env, key + " leaked into shared worker environment"
                )
            for log in harness.logs:
                log.close()

    def test_process_failure_does_not_skip_other_processes_or_logs(self):
        harness = live.LiveProjectReleaseContract()
        first, second = Mock(), Mock()
        first.poll.return_value = second.poll.return_value = None
        first.terminate.side_effect = RuntimeError("process gone")
        harness.processes = [first, second]
        harness.logs = [Mock()]
        harness.engine_roots_settled = True
        with self.assertRaisesRegex(RuntimeError, "process gone"):
            harness.stop_processes()
        second.terminate.assert_called_once()
        second.wait.assert_called_once()
        harness.logs[0].close.assert_called_once()

    def test_job_failure_does_not_skip_other_owned_jobs(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        harness = live.LiveProjectReleaseContract()
        db = MagicMock()
        db.scalars.return_value = [
            SimpleNamespace(launch_ciphertext="one"),
            SimpleNamespace(launch_ciphertext="two"),
        ]
        sessions = MagicMock()
        sessions.return_value.__enter__.return_value = db
        harness.fixture = SimpleNamespace(sessions=sessions, settings=Mock())
        ref = Mock(side_effect=lambda **values: values)
        ref.model_fields = {"id": None}
        harness.jobs = Mock()
        harness.jobs.terminate.side_effect = [RuntimeError("daemon failure"), None]
        with patch.dict(
            sys.modules,
            {
                "app": Mock(),
                "app.database": SimpleNamespace(RuntimeJob=object),
                "app.job_contracts": SimpleNamespace(RuntimeJobRef=ref),
                "app.project_context": SimpleNamespace(
                    decrypt=lambda value, _: {"launch": {"id": value}}
                ),
                "sqlalchemy": SimpleNamespace(select=lambda value: value),
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "daemon failure"):
                harness.cleanup_jobs()
        self.assertEqual(harness.jobs.terminate.call_count, 2)
        self.assertEqual(harness.jobs.cleanup.call_count, 2)
