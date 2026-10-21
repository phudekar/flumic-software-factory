from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "control-plane/backend"))
from app.adapters.hatchet import HatchetEngine
from app.adapters.docker import DockerRuntime
from app.settings import AppSettings


class AdapterTests(unittest.TestCase):
    def test_engine_never_returns_payloads_and_resolves_run_workflow_name(self):
        client = Mock()
        client.runs.list.return_value = {
            "rows": [
                {"workflowName": "unrelated", "input": {"secret": "never return"}},
                {
                    "workflowName": "software-factory-issue-agent",
                    "workflowRunExternalId": "run-1",
                    "displayName": "Factory run",
                    "status": "COMPLETED",
                    "input": {"secret": "never return"},
                },
            ]
        }
        client.runs.get.return_value = {
            "run": {
                "workflowId": "wf-1",
                "displayName": "Factory run",
                "metadata": {"id": "run-1"},
                "input": {"secret": "never return"},
            },
            "tasks": [
                {
                    "taskExternalId": "task-1",
                    "status": "FAILED",
                    "errorMessage": "RepositoryError: Selected source file exceeds the size limit",
                    "input": {"secret": "never return"},
                }
            ],
        }
        client.workflows.get.return_value = {"name": "software-factory-issue-agent"}
        engine = HatchetEngine(Path("."), client)
        self.assertEqual(len(engine.runs()), 1)
        self.assertNotIn("never return", str(engine.runs()))
        self.assertNotIn("never return", str(engine.run("run-1")))
        self.assertEqual(engine.run("run-1")["tasks"][0]["id"], "task-1")
        self.assertIn(
            "Selected source file exceeds",
            engine.run("run-1")["tasks"][0]["error_message"],
        )

    def test_task_logs_resolve_missing_workflow_name_from_definition(self):
        for task in (
            {"workflowName": None, "workflowId": "factory-definition"},
            {"workflowId": "factory-definition"},
        ):
            with self.subTest(task=task):
                client = Mock()
                client.runs.get_task_run.return_value = task
                client.workflows.get.return_value = {
                    "name": "software-factory-issue-agent"
                }
                client.logs.list.return_value = {
                    "rows": [
                        {
                            "createdAt": "2026-10-21T00:00:00Z",
                            "message": "Stage log",
                            "level": "INFO",
                            "input": "must not be returned",
                        }
                    ]
                }
                engine = HatchetEngine(Path("."), client)
                logs = engine.logs("task-id")
                self.assertEqual(logs[0]["message"], "Stage log")
                self.assertNotIn("must not be returned", str(logs))
                client.workflows.get.assert_called_once_with("factory-definition")
                client.logs.list.assert_called_once_with(
                    task_run_id="task-id", limit=200
                )

    def test_task_logs_do_not_bypass_factory_scope_when_name_is_missing(self):
        for task, definition in [
            (
                {"workflowName": None, "workflowId": "other-definition"},
                {"name": "another-application"},
            ),
            ({"workflowName": None}, {}),
            (
                {
                    "workflowName": "another-application",
                    "workflowId": "factory-definition",
                },
                {"name": "software-factory-issue-agent"},
            ),
        ]:
            with self.subTest(task=task):
                client = Mock()
                client.runs.get_task_run.return_value = task
                client.workflows.get.return_value = definition
                engine = HatchetEngine(Path("."), client)
                with self.assertRaisesRegex(ValueError, "outside this factory"):
                    engine.logs("task-id")
                client.logs.list.assert_not_called()

    def test_docker_scope_blocks_unrelated_containers(self):
        client = Mock()
        unrelated = Mock()
        unrelated.labels = {
            "com.docker.compose.project": "another-project",
            "com.docker.compose.service": "worker",
        }
        client.containers.get.return_value = unrelated
        client.containers.list.return_value = [unrelated]
        settings = Mock(spec=AppSettings)
        settings.project = "hatchet-test"
        runtime = DockerRuntime(settings, Mock(), client=client)
        self.assertEqual(runtime.list(), [])
        with self.assertRaises(ValueError):
            runtime.logs("unrelated", 100)
        unrelated.logs.assert_not_called()

    def test_configure_refuses_to_remove_a_running_container(self):
        runtime = DockerRuntime(Mock(), Mock(), client=Mock())
        current = Mock(status="running")
        runtime.container = Mock(return_value=current)
        with self.assertRaises(ValueError):
            runtime.configure(Mock(), {}, "unused")
        current.remove.assert_not_called()

    def test_failed_image_build_preserves_stopped_container(self):
        runtime = DockerRuntime(Mock(), Mock(), client=Mock())
        current = Mock(status="exited")
        runtime.container = Mock(return_value=current)
        runtime.prepare_image = Mock(side_effect=ValueError("build failed"))
        with self.assertRaisesRegex(ValueError, "build failed"):
            runtime.configure(Mock(), {}, "unused")
        current.remove.assert_not_called()
