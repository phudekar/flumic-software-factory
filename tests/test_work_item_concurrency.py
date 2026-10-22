import asyncio
import sys
from pathlib import Path
from datetime import timedelta
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch, AsyncMock
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from factory.settings import Settings
from factory.configuration import TaskSettings
from factory.domain.models import IssueInput
from factory.adapters.state import OperationBusy
from factory.adapters.orchestration.hatchet import HatchetDispatch, build_worker
from hatchet_sdk.exceptions import IdempotencyCollisionError


class WorkItemConcurrencyTests(unittest.TestCase):
    def test_hatchet_deduplicates_and_serializes_by_issue_without_losing_global_limit(
        self,
    ):
        engine = Mock()
        tasks = {}

        def decorate(**kwargs):
            tasks[kwargs["name"]] = kwargs
            return lambda fn: fn

        engine.task.side_effect = decorate
        engine.durable_task.side_effect = decorate
        engine.workflow.return_value.task.side_effect = decorate
        build_worker(
            engine, Mock(), Settings(workflow_concurrency=7), HatchetDispatch()
        )
        options = engine.workflow.call_args.kwargs
        key = "input.repository + ':issue:' + string(input.issue_number)"
        self.assertEqual(options["idempotency"].key_expression, key)
        limits = options["concurrency"]
        self.assertEqual(limits[0].expression, key)
        self.assertTrue(limits[0].is_tenant_scoped)
        self.assertEqual(limits[0].max_runs, 1)
        self.assertEqual(limits[1].max_runs, 7)
        for name in (
            "process-open-pull-request-feedback",
            "verify-merged-pr-and-close-issue",
        ):
            self.assertEqual(tasks[name]["concurrency"][0], limits[0])
            self.assertEqual(tasks[name]["schedule_timeout"], timedelta(days=1))

    def test_duplicate_issue_dispatch_is_already_active_not_failure(self):
        dispatch = HatchetDispatch()
        dispatch.tasks = SimpleNamespace(issue_workflow=Mock())
        dispatch.tasks.issue_workflow.run.side_effect = IdempotencyCollisionError(
            "existing-run"
        )
        with self.assertLogs("issue-worker.dispatch", level="INFO"):
            self.assertIs(
                dispatch.enqueue_issue(
                    IssueInput(repository="owner/repo", issue_number=13)
                ),
                False,
            )
        dispatch.tasks.issue_workflow.run.side_effect = RuntimeError("unavailable")
        with self.assertRaises(RuntimeError):
            dispatch.enqueue_issue(IssueInput(repository="owner/repo", issue_number=13))

    def test_claim_wait_retries_acquisition_but_never_the_body(self):
        from factory.application.operations import claim_with_wait

        calls = []

        @contextmanager
        def claim(resource, stage):
            calls.append("claim")
            if len(calls) == 1:
                raise OperationBusy("busy")
            yield

        state = SimpleNamespace(claim=claim)
        with patch("factory.application.operations.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "body failure"):
                with claim_with_wait(
                    state, "owner/repo:issue:13", "generate", TaskSettings()
                ):
                    calls.append("body")
                    raise RuntimeError("body failure")
        self.assertEqual(calls, ["claim", "claim", "body"])

    def test_zero_wait_preserves_busy_for_engine_retry(self):
        from factory.application.operations import claim_with_wait

        state = Mock()
        state.claim.side_effect = OperationBusy("busy")
        with self.assertRaises(OperationBusy):
            with claim_with_wait(
                state, "resource", "generate", TaskSettings(lock_wait_seconds=0)
            ):
                self.fail("must not execute without claim")

    def test_claim_wait_expires_and_respects_execution_timeout(self):
        from factory.application.operations import claim_with_wait

        state = Mock()
        state.claim.side_effect = OperationBusy("busy")
        with (
            patch(
                "factory.application.operations.time.monotonic",
                side_effect=[10, 10, 11],
            ),
            patch("factory.application.operations.time.sleep") as sleep,
        ):
            with self.assertRaises(OperationBusy):
                with claim_with_wait(
                    state,
                    "resource",
                    "stage",
                    TaskSettings(lock_wait_seconds=1, timeout_seconds=5),
                ):
                    self.fail("claim was not acquired")
        self.assertEqual(state.claim.call_count, 2)
        sleep.assert_called_once_with(1.0)
        state.claim.reset_mock()
        with patch("factory.application.operations.time.sleep") as sleep:
            with self.assertRaises(OperationBusy):
                with claim_with_wait(
                    state,
                    "resource",
                    "stage",
                    TaskSettings(lock_wait_seconds=30, timeout_seconds=1),
                ):
                    self.fail("claim was not acquired")
        sleep.assert_not_called()
        self.assertEqual(state.claim.call_count, 1)

    def test_busy_from_body_does_not_retry_body_or_acquisition(self):
        from factory.application.operations import claim_with_wait

        calls = []

        @contextmanager
        def claim(resource, stage):
            calls.append("claim")
            yield

        with self.assertRaises(OperationBusy):
            with claim_with_wait(
                SimpleNamespace(claim=claim), "resource", "stage", TaskSettings()
            ):
                calls.append("body")
                raise OperationBusy("body")
        self.assertEqual(calls, ["claim", "body"])

    def test_discussion_resumption_retries_until_predecessor_is_finished(self):
        engine = Mock()
        callbacks = {}

        def decorate(**kwargs):
            def register(fn):
                callbacks[kwargs["name"]] = fn
                return fn

            return register

        engine.task.side_effect = decorate
        engine.durable_task.side_effect = decorate
        engine.workflow.return_value.task.side_effect = decorate
        dispatch = HatchetDispatch()
        service = Mock()
        service.discussion_status.return_value = "ready"
        build_worker(engine, service, Settings(), dispatch)
        dispatch.enqueue_issue = Mock(side_effect=[False, True])
        context = SimpleNamespace(aio_sleep_for=AsyncMock())
        result = asyncio.run(
            callbacks["wait-for-ready-for-agent"](
                IssueInput(repository="owner/repo", issue_number=13), context
            )
        )
        self.assertEqual(result, {"status": "resumed"})
        self.assertEqual(dispatch.enqueue_issue.call_count, 2)
        context.aio_sleep_for.assert_awaited_once()
