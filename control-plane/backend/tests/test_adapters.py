from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "worker"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "control-plane/backend"))
from app.adapters.hatchet import HatchetEngine
from factory.settings import Settings
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
        engine = HatchetEngine(
            Settings(repository="phudekar/software-factory-test"), client
        )
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
                engine = HatchetEngine(
                    Settings(repository="phudekar/software-factory-test"), client
                )
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
                engine = HatchetEngine(
                    Settings(repository="phudekar/software-factory-test"), client
                )
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

    def test_remove_is_idempotent_when_runtime_is_already_absent(self):
        client = Mock()
        client.containers.list.return_value = []
        runtime = DockerRuntime(Mock(), Mock(), client=client)
        runtime.action("worker-removed", "remove")
        client.containers.get.assert_not_called()

    def test_workflow_controls_are_scoped_and_use_native_pause_options(self):
        from datetime import timedelta

        client = Mock()
        client.workflows.get.return_value = {
            "metadata": {"id": "factory-id"},
            "name": "scan-pending-github-issues",
            "isPaused": False,
        }
        client.workflows.pause.return_value = {
            "metadata": {"id": "factory-id"},
            "name": "scan-pending-github-issues",
            "isPaused": True,
        }
        engine = HatchetEngine(
            Settings(repository="phudekar/software-factory-test"), client
        )
        result = engine.control("factory-id", "pause", {}, 3600, "DROP", "QUEUE")
        self.assertTrue(result["paused"])
        client.workflows.pause.assert_called_once_with(
            "factory-id",
            queue_ttl=timedelta(seconds=3600),
            paused_workflow_cron_run_queue_behavior="DROP",
            paused_workflow_scheduled_run_queue_behavior="QUEUE",
        )
        client.workflows.get.return_value = {
            "name": "unrelated",
            "metadata": {"id": "other"},
        }
        with self.assertRaises(ValueError):
            engine.control("other", "resume", {})
        client.workflows.unpause.assert_not_called()

    def test_workflow_trigger_validates_input_and_returns_only_run_identity(self):
        client = Mock()
        client.workflows.get.return_value = {
            "metadata": {"id": "wf"},
            "name": "software-factory-issue-agent",
            "isPaused": False,
        }
        client.workflow.return_value.run.return_value.workflow_run_id = "run-id"
        engine = HatchetEngine(
            Settings(repository="phudekar/software-factory-test"), client
        )
        with self.assertRaises(ValueError):
            engine.control("wf", "trigger", {"issue_number": -1})
        client.workflow.return_value.run.assert_not_called()
        result = engine.control(
            "wf",
            "trigger",
            {"repository": "phudekar/software-factory-test", "issue_number": 13},
        )
        self.assertEqual(result["run_id"], "run-id")
        self.assertNotIn("secret", str(result))

    def test_manual_duplicate_trigger_returns_existing_run_identity(self):
        from hatchet_sdk.exceptions import IdempotencyCollisionError

        client = Mock()
        client.workflows.get.return_value = {
            "metadata": {"id": "wf"},
            "name": "software-factory-issue-agent",
            "isPaused": False,
        }
        client.workflow.return_value.run.side_effect = IdempotencyCollisionError(
            "existing-run"
        )
        engine = HatchetEngine(
            Settings(repository="phudekar/software-factory-test"), client
        )
        result = engine.control(
            "wf",
            "trigger",
            {"repository": "phudekar/software-factory-test", "issue_number": 13},
        )
        self.assertEqual(
            result,
            {
                "run_id": "existing-run",
                "status": "ALREADY_ACTIVE",
                "deduplicated": True,
            },
        )
        client.workflow.return_value.run.assert_called_once()

    def test_unknown_pause_capability_is_not_assumed_supported(self):
        client = Mock()
        client.workflows.get.return_value = {
            "metadata": {"id": "wf"},
            "name": "scan-pending-github-issues",
        }
        engine = HatchetEngine(
            Settings(repository="phudekar/software-factory-test"), client
        )
        with self.assertRaisesRegex(ValueError, "pause"):
            engine.control("wf", "pause", {})
        client.workflows.pause.assert_not_called()

    def test_shared_worker_container_has_no_project_credentials_or_global_data(self):
        import os
        from types import SimpleNamespace
        from unittest.mock import patch
        worker=SimpleNamespace(id='shared',name='shared',profile='simple',revision_id='pool',protocol='shared-v1',pool_id='pool')
        settings=SimpleNamespace(worker_endpoint='http://control')
        client=Mock();client.containers.list.return_value=[]
        client.images.get.return_value.attrs={'Config':{'Labels':{'software-factory.shared-protocol':'1'}}}
        runtime=DockerRuntime(settings,Mock(),client)
        with patch.dict(os.environ,{'HATCHET_CLIENT_TOKEN':'infra-token','GITHUB_TOKEN':'project-github','OPENAI_API_KEY':'project-model','ANTHROPIC_API_KEY':'project-anthropic'}):
            runtime.create(worker,{'protocol':'shared-v1','role':'simple'},'control-token')
        args=client.containers.create.call_args.kwargs
        self.assertEqual(set(args['environment']),{'HATCHET_CLIENT_TOKEN','FACTORY_CONTROL_ENDPOINT','FACTORY_CONTROL_TOKEN','FACTORY_CONTROL_WORKER_ID','FACTORY_WORKER_PROTOCOL','FACTORY_WORKER_POOL_ID'})
        self.assertEqual(args['volumes'],{})
        self.assertNotIn('project-github',str(args))
        self.assertNotIn('project-model',str(args))
        self.assertNotIn('docker.sock',str(args))
