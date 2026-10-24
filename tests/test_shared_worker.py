import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import unittest
from unittest.mock import Mock, patch


class SharedWorkerTests(unittest.TestCase):
    def settings(self):
        from factory.shared_worker import SharedSettings

        return SharedSettings(
            pool_id="pool", worker_id="worker", worker_name="shared", role="simple"
        )

    def test_shared_registration_has_bounded_slots_pool_labels_and_no_legacy_scans(
        self,
    ):
        from factory.shared_worker import build_shared_worker

        engine = Mock()
        registered = []

        def task(**options):
            def decorate(function):
                registered.append((options, function))
                return function

            return decorate

        engine.task.side_effect = task
        build_shared_worker(engine, self.settings(), Mock())
        arguments = engine.worker.call_args.kwargs
        self.assertEqual(arguments["slots"], 2)
        self.assertEqual(arguments["durable_slots"], 16)
        self.assertEqual(
            arguments["labels"],
            {
                "factory_protocol": "shared-v1",
                "factory_pool": "pool",
                "factory_role": "simple",
            },
        )
        self.assertEqual(len(registered), 1)
        self.assertEqual(
            registered[0][0]["name"], "flumic-shared-action-v1-pool-simple"
        )
        self.assertNotIn("on_crons", registered[0][0])
        self.assertNotIn("on_events", registered[0][0])
        self.assertEqual(set(arguments["workflows"]), {registered[0][1]})

    def test_shared_echo_uses_assignment_input_and_returns_only_receipt(self):
        from factory.shared_worker import SharedControl
        from factory.workflows.contracts import ActionInvocation

        client = SharedControl(
            self.settings(),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://control",
                "FACTORY_CONTROL_TOKEN": "infra-token",
            },
        )
        calls = []

        def request(path, body=None):
            calls.append((path, body))
            if path.endswith("/claim"):
                return {
                    "assignment_id": "assignment",
                    "lease_token": "private-lease",
                    "action": "core.echo@1",
                    "input": {"message": "alpha"},
                    "reused": False,
                }
            return {"execution_id": "execution", "state": "completed"}

        with patch.object(client, "request", side_effect=request):
            result = client.execute(
                ActionInvocation(
                    project_id="project", run_id="run", execution_id="execution"
                )
            )
        self.assertEqual(
            result.model_dump(), {"execution_id": "execution", "state": "completed"}
        )
        self.assertEqual(calls[-1][1]["result"]["output"], {"message": "alpha"})
        self.assertNotIn("alpha", result.model_dump_json())
        self.assertNotIn("private-lease", result.model_dump_json())

    def test_shared_main_never_loads_repository_settings_or_legacy_factory(self):
        import worker
        from factory import shared_worker

        with (
            patch.dict("os.environ", {"FACTORY_WORKER_PROTOCOL": "shared-v1"}),
            patch.object(shared_worker, "start_shared") as start,
            patch(
                "factory.configuration.load_managed_settings",
                side_effect=AssertionError("legacy profile loaded"),
            ),
        ):
            worker.main()
        start.assert_called_once()

    def test_active_assignment_renews_and_lost_lease_prevents_success(self):
        import threading
        from factory import shared_worker

        client = shared_worker.SharedControl(
            self.settings(),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://control",
                "FACTORY_CONTROL_TOKEN": "token",
            },
        )
        renewed = threading.Event()

        def request(path, body=None):
            renewed.set()
            raise RuntimeError("lease expired")

        with (
            patch.object(shared_worker, "RENEW_SECONDS", 0.01),
            patch.object(client, "request", side_effect=request),
        ):
            with self.assertRaises(RuntimeError):
                with client.lease("assignment", "proof"):
                    self.assertTrue(renewed.wait(2))

    def test_unknown_protocol_never_falls_back_to_legacy_workflows(self):
        import worker

        with patch.dict("os.environ", {"FACTORY_WORKER_PROTOCOL": "shared-v999"}):
            with self.assertRaises(ValueError):
                worker.main()

    def test_shared_hatchet_definition_serializes_with_required_pool_labels(self):
        import base64
        import json
        import warnings
        from hatchet_sdk import Hatchet
        from hatchet_sdk.config import ClientConfig
        from factory.shared_worker import build_shared_worker

        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "00000000-0000-0000-0000-000000000001",
                        "server_url": "http://localhost:8080",
                        "grpc_broadcast_address": "localhost:7070",
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        config = ClientConfig(token="eyJhbGciOiJub25lIn0." + claims + ".test")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            engine = Hatchet(config=config, client=Mock(config=config, debug=False))
        with patch.object(engine, "worker") as register:
            build_shared_worker(engine, self.settings(), Mock())
        definition = register.call_args.kwargs["workflows"][0].to_proto()
        text = str(definition)
        self.assertIn("factory_pool", text)
        self.assertIn("factory_role", text)
        self.assertIn("shared-v1", text)
        self.assertNotIn("on_crons", text)

    def test_catalog_runner_fetches_pinned_context_and_stored_origins(self):
        import json
        from factory.shared_worker import SharedControl
        from factory.workflows.contracts import (
            ActionContext,
            ActionInvocation,
            ActionResult,
        )
        from factory.workflows.bindings import BindingOrigin

        context = ActionContext(
            project_id="p",
            repository_id="repo",
            repository_revision_id="rv",
            config_revision_id="cfg",
            run_id="run",
            stage_id="load",
            execution_id="e",
            assignment_id="a",
            attempt=1,
            namespace="p/repo",
            settings_json=json.dumps({"repository": "owner/repo"}),
        )
        client = SharedControl(
            self.settings().model_copy(update={"actions": ["issue.load@1"]}),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://control",
                "FACTORY_CONTROL_TOKEN": "infra",
            },
        )

        def request(path, body=None):
            if path.endswith("/claim"):
                return {
                    "assignment_id": "a",
                    "lease_token": "lease",
                    "executor": "builtin@1",
                    "action": "issue.load@1",
                    "input": {"command": {"issue_number": 2}},
                    "reused": False,
                }
            if path.endswith("/context"):
                return {
                    "context": {
                        **context.model_dump(mode="json"),
                        "settings_json": context.settings_json,
                    },
                    "credentials": {"github": "scoped-token"},
                    "origins": [
                        {
                            "path": ["state"],
                            "stage_id": "parent",
                            "project_id": "p",
                            "repository_id": "repo",
                        }
                    ],
                }
            return {"execution_id": "e", "state": "completed"}

        with (
            patch.object(client, "request", side_effect=request),
            patch(
                "factory.bootstrap.run_builtin",
                return_value=ActionResult(output={"state": "open"}),
            ) as builtin,
        ):
            receipt = client.execute(
                ActionInvocation(project_id="p", run_id="run", execution_id="e")
            )
        self.assertEqual(receipt.state, "completed")
        self.assertEqual(builtin.call_args.args[0], "issue.load@1")
        self.assertEqual(
            builtin.call_args.args[1].credentials["github"], "scoped-token"
        )
        self.assertEqual(
            builtin.call_args.args[2].origins,
            (
                BindingOrigin(
                    path=("state",),
                    stage_id="parent",
                    project_id="p",
                    repository_id="repo",
                ),
            ),
        )

    def test_docker_runner_returns_waiting_after_durable_acceptance(self):
        from factory.shared_worker import SharedControl
        from factory.workflows.contracts import ActionInvocation

        client = SharedControl(
            self.settings().model_copy(
                update={"actions": ["docker.run@1"], "executors": ["docker@1"]}
            ),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://control",
                "FACTORY_CONTROL_TOKEN": "infra",
            },
        )
        calls = []

        def request(path, body=None):
            calls.append((path, body))
            if path.endswith("/claim"):
                return {
                    "assignment_id": "a",
                    "lease_token": "lease",
                    "executor": "docker@1",
                    "action": "docker.run@1",
                    "spec_digest": "a" * 64,
                    "input": {},
                    "reused": False,
                }
            return {"job_id": "job", "state": "pending"}

        with patch.object(client, "request", side_effect=request):
            receipt = client.execute(
                ActionInvocation(project_id="p", run_id="run", execution_id="e")
            )
        self.assertEqual(receipt.state, "waiting")
        self.assertTrue(calls[-1][0].endswith("/jobs"))
        self.assertEqual(set(calls[-1][1]), {"lease_token", "spec_digest"})
        self.assertFalse(any(path.endswith("/complete") for path, _ in calls))

    def test_catalog_runner_budget_covers_maximum_stage_timeout(self):
        from datetime import timedelta
        from factory.adapters.orchestration.dynamic import register_action

        engine = Mock()
        engine.task.return_value = lambda fn: fn
        register_action(engine, "pool", "simple", Mock())
        self.assertEqual(
            engine.task.call_args.kwargs["execution_timeout"], timedelta(seconds=3660)
        )
