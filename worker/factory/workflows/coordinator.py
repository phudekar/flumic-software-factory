"""Replay body: child scheduling and durable waits only; no business I/O."""

import asyncio
from factory.workflows.contracts import RunAdvance, StageReceipt

TERMINAL = frozenset({"completed", "failed", "cancelled"})


async def coordinate(command, control, action, sleep, *, parallelism=8):
    if not 1 <= parallelism <= 8:
        raise ValueError("Coordinator parallelism must be 1..8")
    round_number = 0
    seen = set()
    while True:
        frontier = RunAdvance.model_validate(
            await control(command, f"advance/{command.run_id}/{round_number}")
        )
        if frontier.state in TERMINAL:
            return RunAdvance(state=frontier.state)
        if frontier.state not in {"queued", "running", "cancelling"}:
            raise ValueError("Unknown run state")
        ready = frontier.ready
        if len({item.execution_id for item in ready}) != len(ready):
            raise ValueError("Duplicate execution in frontier")
        for item in ready:
            if item.project_id != command.project_id or item.run_id != command.run_id:
                raise ValueError("Frontier crosses run scope")
            if item.execution_id not in frontier.routes:
                raise ValueError("Missing pinned assignment route")
        ready = sorted(
            ready,
            key=lambda item: (
                frontier.routes[item.execution_id].stage_id,
                frontier.routes[item.execution_id].attempt,
                item.execution_id,
            ),
        )

        # A transport failure is not proof of terminal action/job state. The
        # control service alone can reconcile it. Never synthesize a retry.
        async def execute(item):
            route = frontier.routes[item.execution_id]
            key = f"action/{command.run_id}/{route.stage_id}/{route.attempt}/{item.execution_id}"
            receipt = StageReceipt.model_validate(await action(item, route, key))
            if receipt.execution_id != item.execution_id:
                raise ValueError("Receipt crosses execution scope")
            return receipt

        eligible = (
            [item for item in ready if item.execution_id not in seen]
            if frontier.state != "cancelling"
            else []
        )
        for offset in range(0, len(eligible), parallelism):
            batch = eligible[offset : offset + parallelism]
            results = await asyncio.gather(
                *(execute(item) for item in batch), return_exceptions=True
            )
            for result in results:
                if isinstance(result, (ValueError, asyncio.CancelledError)):
                    raise result
            seen.update(item.execution_id for item in batch)
        round_number += 1
        # Always yield through the durable engine; no local clock/timer/I/O.
        await sleep()
