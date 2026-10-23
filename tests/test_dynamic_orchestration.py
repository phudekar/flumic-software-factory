import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_frontier_is_sorted_bounded_and_replay_keys_stable(self):
        from factory.workflows.coordinator import coordinate
        from factory.workflows.contracts import (
            RunInvocation,
            RunAdvance,
            ActionInvocation,
            ActionRoute,
        )

        command = RunInvocation(project_id="p", run_id="r")
        frontier = RunAdvance(
            state="running",
            ready=[
                ActionInvocation(project_id="p", run_id="r", execution_id=x)
                for x in ["c", "a", "b"]
            ],
            routes={
                x: ActionRoute(stage_id=x, attempt=1, pool_id="pool", role="simple")
                for x in ["a", "b", "c"]
            },
        )
        histories = []
        for _ in range(2):
            history = []
            active = 0
            maximum = 0

            async def control(value, key):
                history.append(key)
                return frontier if len(history) == 1 else RunAdvance(state="completed")

            async def action(value, route, key):
                nonlocal active, maximum
                active += 1
                maximum = max(maximum, active)
                history.append(key)
                await asyncio.sleep(0.001)
                active -= 1
                return {"execution_id": value.execution_id, "state": "completed"}

            async def sleep():
                pass

            result = await coordinate(command, control, action, sleep, parallelism=2)
            self.assertEqual(result.state, "completed")
            self.assertEqual(maximum, 2)
            self.assertEqual(
                history,
                [
                    "advance/r/0",
                    "action/r/a/1/a",
                    "action/r/b/1/b",
                    "action/r/c/1/c",
                    "advance/r/1",
                ],
            )
            histories.append(history)
        self.assertEqual(histories[0], histories[1])

    async def test_child_transport_failure_does_not_invent_terminal_proof(self):
        from factory.workflows.coordinator import coordinate
        from factory.workflows.contracts import (
            RunInvocation,
            RunAdvance,
            ActionInvocation,
            ActionRoute,
        )

        rounds = iter(
            [
                RunAdvance(
                    state="running",
                    ready=[
                        ActionInvocation(project_id="p", run_id="r", execution_id="e")
                    ],
                    routes={
                        "e": ActionRoute(
                            stage_id="s", attempt=1, pool_id="pool", role="developer"
                        )
                    },
                ),
                RunAdvance(state="failed"),
            ]
        )

        async def control(value, key):
            return next(rounds)

        async def action(*args):
            raise RuntimeError("transport lost")

        async def sleep():
            pass

        result = await coordinate(
            RunInvocation(project_id="p", run_id="r"), control, action, sleep
        )
        self.assertEqual(result.state, "failed")

    async def test_cross_project_frontier_rejected_before_dispatch(self):
        from factory.workflows.coordinator import coordinate
        from factory.workflows.contracts import (
            RunInvocation,
            RunAdvance,
            ActionInvocation,
            ActionRoute,
        )

        async def control(*args):
            return RunAdvance(
                state="running",
                ready=[
                    ActionInvocation(project_id="other", run_id="r", execution_id="e")
                ],
                routes={
                    "e": ActionRoute(
                        stage_id="s", attempt=1, pool_id="pool", role="simple"
                    )
                },
            )

        async def action(*args):
            self.fail("cross-project dispatch")

        async def sleep():
            pass

        with self.assertRaises(ValueError):
            await coordinate(
                RunInvocation(project_id="p", run_id="r"), control, action, sleep
            )

    async def test_cancelled_terminal_never_dispatches(self):
        from factory.workflows.coordinator import coordinate
        from factory.workflows.contracts import RunInvocation, RunAdvance

        async def control(*args):
            return RunAdvance(state="cancelled")

        async def action(*args):
            self.fail("terminal dispatch")

        async def sleep():
            pass

        self.assertEqual(
            (
                await coordinate(
                    RunInvocation(project_id="p", run_id="r"), control, action, sleep
                )
            ).state,
            "cancelled",
        )

    async def test_public_sdk_registration_serializes_bounded_receipts(self):
        import base64
        import json
        from hatchet_sdk import Hatchet
        from hatchet_sdk.config import ClientConfig
        from factory.adapters.orchestration.dynamic import register_coordinator
        from factory.workflows.contracts import RunInvocation, RunAdvance

        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "test",
                        "server_url": "http://localhost:8080",
                        "grpc_broadcast_address": "localhost:7070",
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        engine = Hatchet(
            config=ClientConfig(token="eyJhbGciOiJub25lIn0." + claims + ".test")
        )
        tasks = register_coordinator(
            engine, lambda value: RunAdvance(state="completed")
        )
        result = tasks.advance.mock_run(RunInvocation(project_id="p", run_id="r"))
        self.assertEqual(RunAdvance.model_validate(result).state, "completed")

    async def test_malformed_receipt_fails_closed_after_other_children_settle(self):
        from factory.workflows.coordinator import coordinate
        from factory.workflows.contracts import (
            RunInvocation,
            RunAdvance,
            ActionInvocation,
            ActionRoute,
        )

        settled = []

        async def control(*args):
            return RunAdvance(
                state="running",
                ready=[ActionInvocation(project_id="p", run_id="r", execution_id="e")],
                routes={
                    "e": ActionRoute(
                        stage_id="s", attempt=1, pool_id="pool", role="simple"
                    )
                },
            )

        async def action(*args):
            settled.append("finished")
            return {"execution_id": "wrong", "state": "completed"}

        async def sleep():
            raise AssertionError("Protocol error was silently ignored")

        with self.assertRaisesRegex(ValueError, "Receipt crosses"):
            await coordinate(
                RunInvocation(project_id="p", run_id="r"), control, action, sleep
            )
        self.assertEqual(settled, ["finished"])

    async def test_waiting_receipt_is_not_dispatched_again_during_polling(self):
        from factory.workflows.coordinator import coordinate
        from factory.workflows.contracts import (
            RunInvocation,
            RunAdvance,
            ActionInvocation,
            ActionRoute,
        )

        rounds = 0
        calls = []

        async def control(*args):
            nonlocal rounds
            rounds += 1
            return (
                RunAdvance(state="completed")
                if rounds == 3
                else RunAdvance(
                    state="running",
                    ready=[
                        ActionInvocation(project_id="p", run_id="r", execution_id="e")
                    ],
                    routes={
                        "e": ActionRoute(
                            stage_id="s", attempt=1, pool_id="pool", role="simple"
                        )
                    },
                )
            )

        async def action(value, route, key):
            calls.append(value.execution_id)
            return {"execution_id": "e", "state": "waiting"}

        async def sleep():
            pass

        result = await coordinate(
            RunInvocation(project_id="p", run_id="r"), control, action, sleep
        )
        self.assertEqual(result.state, "completed")
        self.assertEqual(calls, ["e"])

    async def test_engine_receipts_reject_unbounded_or_unknown_state(self):
        from factory.workflows.contracts import StageReceipt
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            StageReceipt(execution_id="e", state="private output" * 1000)
        with self.assertRaises(ValidationError):
            StageReceipt(execution_id="e" * 121, state="completed")

    async def test_control_protocol_error_is_non_retryable_and_sanitized(self):
        import base64
        import json
        from hatchet_sdk import Hatchet, NonRetryableException
        from hatchet_sdk.config import ClientConfig
        from factory.adapters.orchestration.dynamic import register_coordinator
        from factory.workflows.contracts import RunInvocation

        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "test",
                        "server_url": "http://localhost:8080",
                        "grpc_broadcast_address": "localhost:7070",
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        engine = Hatchet(
            config=ClientConfig(token="eyJhbGciOiJub25lIn0." + claims + ".test")
        )
        tasks = register_coordinator(
            engine, lambda command: {"state": "invalid-private-data"}
        )
        with self.assertRaises(NonRetryableException) as error:
            tasks.advance.mock_run(RunInvocation(project_id="p", run_id="r"))
        self.assertNotIn("invalid-private-data", str(error.exception))
