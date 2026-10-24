"""Concurrent action attribution must survive asynchronous delivery and replay."""

import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.workflows.contracts import ActionContext
from factory.settings import Settings


def context(project):
    return ActionContext(
        project_id=project,
        repository_id=project + "-repo",
        repository_revision_id="repo-revision",
        config_revision_id="config",
        workflow_revision_id=project + "-workflow",
        run_id=project + "-run",
        stage_id="plan",
        execution_id=project + "-execution",
        attempt=2,
        assignment_id=project + "-assignment",
        namespace=project + "/repo",
        settings_json=json.dumps({"repository": project + "/repo"}),
    ).with_credentials({"openai": project + "-private-secret"})


class ProjectTelemetryTests(unittest.TestCase):
    def test_concurrent_actions_keep_model_logs_secrets_and_identity_local(self):
        from factory.application.telemetry import (
            action_scope,
            action_metadata,
            model_call,
            ModelUsage,
            record_conversation,
        )
        from factory.adapters.control import ControlObserver
        from factory.adapters.telemetry import LoggingObserver

        barrier = threading.Barrier(2)
        observers, publishers = {}, {}
        for project in ("alpha", "beta"):
            publishers[project] = Mock()
            observers[project] = ControlObserver(
                LoggingObserver(),
                Settings(conversation_capture="redacted"),
                publisher=publishers[project],
                assignment_id=project + "-assignment",
            )
        before = dict(os.environ)

        def run(project):
            with action_scope(context(project)):
                with observers[project].observe("plan_issue", {"project_id": "forged"}):
                    with model_call("openai", "fixture") as call:
                        barrier.wait(timeout=5)
                        call.record(ModelUsage(input_tokens=0, output_tokens=3))
                        record_conversation(
                            request=project + "-private-secret", response=project
                        )
                        logging.getLogger("issue-worker.fixture").warning(
                            project + "-private-secret"
                        )
            self.assertEqual(action_metadata(), {})

        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(run, ("alpha", "beta")))
            self.assertEqual(dict(os.environ), before)
            for project, publisher in publishers.items():
                events = publisher.emit.call_args_list
                self.assertTrue(events)
                for event in events:
                    self.assertEqual(event.args[2]["project_id"], project)
                    self.assertEqual(
                        event.args[2]["attempt_id"], project + "-execution"
                    )
                    self.assertEqual(event.args[2]["stage"], "plan")
                    self.assertNotIn(project + "-private-secret", str(event))
                started = next(
                    event.args[1]
                    for event in events
                    if event.args[0] == "attempt.started"
                )
                self.assertEqual(started["project_id"], project)
                model = next(
                    event.args[1]
                    for event in events
                    if event.args[0] == "model.completed"
                )
                self.assertEqual(model["input_tokens"], 0)
                self.assertEqual(model["conversation"]["request"], "[REDACTED]")
        finally:
            for observer in observers.values():
                observer.shutdown()

    def test_otel_spans_and_usage_metrics_share_authority_but_heartbeat_does_not(self):
        import test_telemetry as fixtures
        from factory.application.telemetry import action_scope, model_call, ModelUsage

        case = fixtures.TelemetryTests(
            "test_unknown_usage_has_no_zero_token_attributes"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        with action_scope(context("alpha")):
            with case.observer.observe("plan_issue", {"project_id": "forged"}):
                with model_call("openai", "fixture") as call:
                    call.record(ModelUsage(input_tokens=0, output_tokens=3))
        for span in case.exporter.get_finished_spans():
            self.assertEqual(span.attributes["factory.project_id"], "alpha")
            self.assertEqual(span.attributes["factory.execution_id"], "alpha-execution")
        data = case.reader.get_metrics_data()
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    for point in metric.data.data_points:
                        if metric.name.startswith("factory.worker."):
                            self.assertNotIn("factory.project_id", point.attributes)
                        else:
                            self.assertEqual(
                                point.attributes.get("factory.project_id"), "alpha"
                            )

    def test_action_scope_carries_delivered_worker_identity(self):
        from factory.application.telemetry import action_scope, action_metadata

        original = context("alpha")
        source = ActionContext.model_validate(
            {
                **original.model_dump(),
                "settings_json": original.settings_json,
                "worker_id": "shared-worker",
            }
        )
        with action_scope(source):
            self.assertEqual(action_metadata()["worker_id"], "shared-worker")

    def test_shared_worker_gauges_total_concurrent_actions_and_remain_alive_when_idle(
        self,
    ):
        import test_telemetry as fixtures
        from unittest.mock import patch
        from factory.shared_worker import SharedControl, SharedSettings
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import InMemoryMetricReader

        reader = InMemoryMetricReader()
        provider = MeterProvider(metric_readers=[reader])
        closed = {"value": False}
        original_shutdown = provider.shutdown

        def shutdown():
            if not closed["value"]:
                closed["value"] = True
                original_shutdown()

        provider.shutdown = shutdown
        self.addCleanup(shutdown)
        control = SharedControl(
            SharedSettings(
                pool_id="pool", worker_id="worker", worker_name="shared", role="simple"
            ),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://control",
                "FACTORY_CONTROL_TOKEN": "infra",
            },
        )
        self.addCleanup(control.shutdown)
        cases = []

        def create(settings):
            case = fixtures.TelemetryTests(
                "test_unknown_usage_has_no_zero_token_attributes"
            )
            case.setUp()
            self.addCleanup(case.doCleanups)
            cases.append(case)
            return case.observer

        def samples(name):
            result = []
            for current in [reader, *[case.reader for case in cases]]:
                data = current.get_metrics_data()
                if data is None:
                    continue
                for resource in data.resource_metrics:
                    for scope in resource.scope_metrics:
                        for metric in scope.metrics:
                            if metric.name == name:
                                result.extend(
                                    point
                                    for point in metric.data.data_points
                                    if name.endswith("heartbeat")
                                    or point.attributes.get("stage") == "all"
                                )
            return result

        with (
            patch("factory.bootstrap.load_factory", return_value=create),
            patch("factory.adapters.otel.MeterProvider", return_value=provider),
            patch("factory.adapters.otel.PeriodicExportingMetricReader"),
            patch("factory.adapters.otel.OTLPMetricExporter"),
        ):
            with control.observation(context("alpha"), "alpha-proof") as alpha:
                with alpha.observe("plan_issue", {}):
                    with control.observation(context("beta"), "beta-proof") as beta:
                        with beta.observe("plan_issue", {}):
                            self.assertEqual(
                                [
                                    point.value
                                    for point in samples("factory.worker.active_tasks")
                                ],
                                [2],
                            )
                    self.assertEqual(
                        [
                            point.value
                            for point in samples("factory.worker.active_tasks")
                        ],
                        [1],
                    )
            self.assertEqual(
                [point.value for point in samples("factory.worker.active_tasks")], [0]
            )
            self.assertTrue(samples("factory.worker.heartbeat"))
            self.assertFalse(closed["value"])
            for point in samples("factory.worker.heartbeat"):
                self.assertNotIn("factory.project_id", point.attributes)
            control.shutdown()
            self.assertTrue(closed["value"])

    def test_nested_otel_accounting_counts_only_leaf_usage(self):
        import test_telemetry as fixtures
        from factory.application.telemetry import action_scope, model_call, ModelUsage

        case = fixtures.TelemetryTests(
            "test_unknown_usage_has_no_zero_token_attributes"
        )
        case.setUp()
        self.addCleanup(case.doCleanups)
        with action_scope(context("alpha")), case.observer.observe("plan_issue", {}):
            with model_call("openai", "parent") as parent:
                parent.record(ModelUsage(input_tokens=10, output_tokens=2))
                with model_call("openai", "child") as child:
                    child.record(ModelUsage(input_tokens=10, output_tokens=2))
        total = 0
        for resource in case.reader.get_metrics_data().resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    if metric.name == "factory.model.tokens":
                        total += sum(
                            point.value
                            for point in metric.data.data_points
                            if point.attributes.get("token.type") == "input"
                        )
        self.assertEqual(total, 10)

    def test_typed_usage_protocol_keys_survive_action_secret_collisions(self):
        from factory.workflows.contracts import ActionResult, UsageRecord
        from factory.workflows.redaction import secret_scope

        with secret_scope(["input_tokens"]):
            result = ActionResult(
                output={},
                usage=[
                    UsageRecord(
                        id="call", provider="external", model="fixture", input_tokens=7
                    )
                ],
            )
        self.assertEqual(result.usage[0].input_tokens, 7)

    def test_external_usage_records_require_unique_identities(self):
        from factory.workflows.contracts import ActionResult, UsageRecord

        usage = UsageRecord(id="same-call", provider="external", model="fixture")
        with self.assertRaisesRegex(ValueError, "Duplicate usage identity"):
            ActionResult(output={}, usage=[usage, usage])

    def test_grant_collisions_do_not_rename_protocol_identity_or_usage_keys(self):
        import test_telemetry as fixtures
        from factory.application.telemetry import action_scope, model_call, ModelUsage
        from factory.adapters.telemetry import CorrelationFilter, JsonFormatter

        for secret in ("input_tokens", "project_id"):
            with self.subTest(secret=secret):
                case = fixtures.TelemetryTests(
                    "test_unknown_usage_has_no_zero_token_attributes"
                )
                case.setUp()
                self.addCleanup(case.doCleanups)
                with action_scope(
                    context("alpha").with_credentials({"openai": secret})
                ):
                    with case.observer.observe("plan_issue", {}):
                        with model_call("openai", "fixture") as call:
                            call.record(ModelUsage(input_tokens=7, output_tokens=2))
                        record = logging.LogRecord(
                            "issue-worker.fixture",
                            logging.INFO,
                            "",
                            0,
                            "safe message",
                            (),
                            None,
                        )
                        CorrelationFilter().filter(record)
                        document = json.loads(JsonFormatter().format(record))
                        self.assertEqual(document["project_id"], "alpha")
                model = case.exporter.get_finished_spans()[0]
                self.assertEqual(model.attributes["gen_ai.usage.input_tokens"], 7)
                self.assertEqual(model.attributes["factory.project_id"], "alpha")

    def test_spool_budget_drops_oversized_redacted_capture_but_keeps_usage(self):
        from factory.adapters.shared_history import SharedPublisher
        from factory.application.telemetry import action_scope

        control = Mock()
        control.settings.worker_id = "shared-worker"
        source = context("alpha").with_credentials({"openai": "z"})
        with tempfile.TemporaryDirectory() as directory:
            publisher = SharedPublisher(control, Path(directory), start=False)
            with action_scope(source):
                publisher.bound(source, "scoped-proof").emit(
                    "model.completed",
                    {
                        "call_id": "fixture-call",
                        "provider": "openai",
                        "model": "fixture",
                        "input_tokens": 3,
                        "conversation": {"request": ["z" * 5000] * 20},
                    },
                    {},
                )
            document = json.loads(next(Path(directory).rglob("*.json")).read_text())
            payload = document["events"][0]["payload"]
            self.assertEqual(payload["input_tokens"], 3)
            self.assertEqual(payload["capture_status"], "size_limit")
            self.assertNotIn("conversation", payload)

    @unittest.skipUnless(
        os.environ.get("RUN_SHARED_SPOOL_OWNER_CONTRACT") == "1" and os.geteuid() == 0,
        "requires isolated root-owned spool contract",
    )
    def test_root_worker_reclaims_host_spool_ownership_from_api_user(self):
        from factory.adapters.shared_history import SharedPublisher

        control = Mock()
        control.settings.worker_id = "shared-worker"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "shared-worker"
            root.mkdir(mode=0o755)
            os.chown(root, 10001, 10001)
            publisher = SharedPublisher(control, Path(directory), start=False)
            self.assertEqual(root.stat().st_uid, os.getuid())
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            publisher.bound(context("alpha"), "proof").emit(
                "log", {"message": "safe"}, {}
            )
            self.assertEqual(len(list(root.glob("*.json"))), 1)

    def test_existing_spool_directory_is_made_private_for_the_worker_user(self):
        from factory.adapters.shared_history import SharedPublisher

        control = Mock()
        control.settings.worker_id = "shared-worker"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "shared-worker"
            root.mkdir(mode=0o755)
            SharedPublisher(control, Path(directory), start=False)
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual(root.stat().st_uid, os.getuid())

    def test_poisoned_project_spool_cannot_starve_another_projects_delivery(self):
        from factory.adapters.shared_history import SharedPublisher

        control = Mock()
        control.settings.worker_id = "shared-worker"
        with tempfile.TemporaryDirectory() as directory:
            publisher = SharedPublisher(control, Path(directory), start=False)
            for _ in range(30):
                publisher.bound(context("alpha"), "alpha-proof").emit(
                    "log", {"message": "alpha"}, {}
                )
            publisher.bound(context("beta"), "beta-proof").emit(
                "log", {"message": "beta"}, {}
            )

            def send(path, body):
                if "alpha-assignment" in path:
                    raise RuntimeError("old record requires investigation")
                return {"accepted": 1}

            control.request.side_effect = send
            publisher.flush_once()
            publisher.flush_once()
            self.assertTrue(
                any(
                    "beta-assignment" in call.args[0]
                    for call in control.request.call_args_list
                )
            )
            self.assertFalse(
                any(
                    "beta-assignment" in path.name
                    for path in Path(directory).rglob("*.json")
                )
            )

    def test_spool_hashes_untrusted_call_ids_and_redacts_nonprotocol_keys(self):
        from factory.adapters.shared_history import SharedPublisher
        from factory.application.telemetry import action_scope

        control = Mock()
        control.settings.worker_id = "shared-worker"
        control.headers = {"Authorization": "Bearer infra-secret"}
        source = context("alpha")
        credential = "alpha-private-secret"
        with tempfile.TemporaryDirectory() as directory:
            publisher = SharedPublisher(control, Path(directory), start=False)
            with action_scope(context("beta")):
                bound = publisher.bound(source, "scoped-proof")
                for _ in range(2):
                    bound.emit(
                        "model.completed",
                        {
                            "provider": "openai",
                            "model": "fixture",
                            "call_id": credential,
                            "parent_call_id": credential,
                            credential: "source-key",
                            "transport_copy": "infra-secret",
                            "proof_copy": "scoped-proof",
                        },
                        {},
                    )
            documents = [
                json.loads(path.read_text()) for path in Path(directory).rglob("*.json")
            ]
            self.assertEqual(len(documents), 2)
            self.assertNotIn(credential, str(documents))
            self.assertNotIn("infra-secret", str(documents))
            values = [doc["events"][0]["payload"] for doc in documents]
            self.assertNotIn("scoped-proof", str(values))
            self.assertEqual(values[0]["call_id"], values[1]["call_id"])
            self.assertEqual(values[0]["call_id"], values[0]["parent_call_id"])

    def test_disk_spool_replays_original_assignment_after_context_is_gone(self):
        from factory.adapters.shared_history import SharedPublisher
        from factory.application.telemetry import action_scope

        control = Mock()
        control.settings.worker_id = "shared-worker"
        with tempfile.TemporaryDirectory() as directory:
            publisher = SharedPublisher(control, Path(directory), start=False)
            with action_scope(context("alpha")):
                publisher.bound(context("alpha"), "scoped-proof").emit(
                    "model.completed",
                    {
                        "provider": "openai",
                        "model": "fixture",
                        "call_id": "call-1",
                        "input_tokens": 0,
                        "conversation": {"request": "alpha-private-secret"},
                    },
                    {},
                )
            files = list(Path(directory).rglob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertNotIn("alpha-private-secret", files[0].read_text())
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
            control.request.side_effect = RuntimeError("offline")
            publisher.flush_once()
            self.assertTrue(files[0].exists())
            control.request.side_effect = None
            control.request.return_value = {"accepted": 1}
            restarted = SharedPublisher(control, Path(directory), start=False)
            with action_scope(context("beta")):
                restarted.flush_once()
            path, body = control.request.call_args.args
            self.assertIn("alpha-assignment", path)
            self.assertEqual(body["lease_token"], "scoped-proof")
            self.assertEqual(body["events"][0]["attribution"]["project_id"], "alpha")
            self.assertEqual(list(Path(directory).rglob("*.json")), [])
