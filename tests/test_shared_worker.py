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
