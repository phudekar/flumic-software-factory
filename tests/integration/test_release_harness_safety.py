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
            for key in (
                "CONTROL_TEST_DATABASE_URL",
                "CONTROL_MIGRATION_TEST_URL",
                "GITLAB_TOKEN",
            ):
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

    def test_echo_fixture_uses_distinct_decoy_and_action_worker_identities(self):
        import runpy
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import patch

        client = Mock()
        tasks = SimpleNamespace(coordinator="coordinator", advance="advance")
        names, pools = [], []
        script = Path(live.__file__).with_name("hatchet_dynamic_fixture.py")
        for role in ("action", "decoy"):
            with (
                patch("hatchet_sdk.Hatchet", return_value=client),
                patch(
                    "factory.adapters.orchestration.dynamic.register_coordinator",
                    return_value=tasks,
                ),
                patch(
                    "factory.adapters.orchestration.dynamic.register_action",
                    return_value="action",
                ),
                patch("factory.adapters.orchestration.dynamic.coordinate"),
                patch.object(
                    sys,
                    "argv",
                    [str(script), "/tmp/flumic-fixture", "unique-fixture", role],
                ),
            ):
                runpy.run_path(str(script), run_name="__main__")
            names.append(client.worker.call_args.args[0])
            pools.append(client.worker.call_args.kwargs["labels"]["factory_pool"])
        self.assertEqual(
            len(set(names)),
            2,
            "Distinct labels must not overwrite the same engine worker identity",
        )
        self.assertEqual(pools, ["unique-fixture", "unique-fixture-wrong"])

    def test_echo_cleanup_cancels_owned_unfinished_runs_and_still_stops_processes_on_error(
        self,
    ):
        import asyncio
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, patch
        from integration import test_hatchet_dynamic_contract as echo

        harness = echo.LiveHatchetContract()
        harness.owned_runs = ["owned-running", "owned-completed"]
        harness.client = Mock()
        harness.client.runs.aio_get_status = AsyncMock(
            side_effect=[
                SimpleNamespace(value="RUNNING"),
                SimpleNamespace(value="COMPLETED"),
            ]
        )
        harness.client.runs.aio_cancel = AsyncMock(
            side_effect=RuntimeError("fixture engine unavailable")
        )
        process = Mock()
        process.poll.return_value = None
        harness.processes = [process]
        harness.logs = [Mock()]
        harness.root = Path("/tmp/flumic-owned-fixture")
        harness.temp = Mock()
        with patch.dict(os.environ, {"FLUMIC_LIVE_EVIDENCE_DIR": ""}):
            with self.assertRaises(AssertionError):
                asyncio.run(harness.asyncTearDown())
        harness.client.runs.aio_cancel.assert_awaited_once_with("owned-running")
        process.terminate.assert_called_once()
        process.wait.assert_called_once()
        harness.logs[0].close.assert_called_once()
        harness.temp.cleanup.assert_called_once()

    def test_echo_run_idempotency_is_scoped_to_each_fixture_project(self):
        import asyncio
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from integration import test_hatchet_dynamic_contract as echo

        projects = []
        with TemporaryDirectory() as directory:
            for namespace in ("fixture-one", "fixture-two"):
                harness = echo.LiveHatchetContract()
                harness.namespace = namespace
                harness.root = Path(directory) / namespace
                harness.root.mkdir()
                harness.owned_runs = []
                task = AsyncMock(
                    return_value=SimpleNamespace(workflow_run_id=namespace)
                )
                harness.tasks = SimpleNamespace(
                    coordinator=SimpleNamespace(aio_run=task)
                )
                asyncio.run(harness.launch("same-run-name"))
                projects.append(task.call_args.args[0].project_id)
        self.assertEqual(projects, ["fixture-one", "fixture-two"])
