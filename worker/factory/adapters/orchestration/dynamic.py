"""Fixed Hatchet registrations. Only public SDK child APIs are used.

The control handler is trusted infrastructure code (WorkflowState.advance_run),
never a handler supplied in a published workflow. Register its worker separately
from project action workers. Public authenticated launch/state APIs arrive in #11.
"""

from datetime import timedelta
from types import SimpleNamespace
from hatchet_sdk.runnables.eviction import EvictionPolicy
from hatchet_sdk import (
    Context,
    DurableContext,
    DesiredWorkerLabel,
    NonRetryableException,
)
from factory.workflows.contracts import (
    ActionInvocation,
    RunInvocation,
    RunAdvance,
    StageReceipt,
)
from factory.workflows.coordinator import coordinate
from hatchet_sdk.types.idempotency import StatusBasedIdempotencyConfig


def labels(**values):
    return [
        DesiredWorkerLabel(key=k, value=v, required=True) for k, v in values.items()
    ]


def action_name(pool_id, role):
    return f"flumic-shared-action-v1-{pool_id}-{role}"


def register_action(hatchet, pool_id, role, execute):
    @hatchet.task(
        name=action_name(pool_id, role),
        input_validator=ActionInvocation,
        desired_worker_labels=labels(
            factory_protocol="shared-v1", factory_pool=pool_id, factory_role=role
        ),
        execution_timeout=timedelta(seconds=3660),
        retries=0,
    )
    def action(input: ActionInvocation, ctx: Context) -> StageReceipt:
        return StageReceipt.model_validate(execute(input))

    return action


def register_coordinator(
    hatchet,
    advance_run,
    *,
    namespace="flumic-dynamic-v1",
    parallelism=8,
    poll_seconds=2,
    eviction_policy=EvictionPolicy(
        ttl=timedelta(minutes=15), allow_capacity_eviction=True, priority=0
    ),
):
    if not 1 <= parallelism <= 8 or not 1 <= poll_seconds <= 60:
        raise ValueError("Invalid coordinator bounds")
    affinity = labels(factory_protocol="shared-v1", factory_role="control")

    @hatchet.task(
        name=namespace + "-advance",
        input_validator=RunInvocation,
        desired_worker_labels=affinity,
        execution_timeout=timedelta(seconds=30),
        retries=3,
        backoff_factor=2.0,
        backoff_max_seconds=10,
    )
    def advance(input: RunInvocation, ctx: Context) -> RunAdvance:
        try:
            return RunAdvance.model_validate(advance_run(input))
        except ValueError:
            # Malformed protocol/configuration is terminal, not a temporary DB
            # outage. Never put the invalid payload in the engine exception.
            raise NonRetryableException("Invalid control protocol receipt") from None

    @hatchet.durable_task(
        name=namespace + "-coordinate",
        idempotency=StatusBasedIdempotencyConfig(
            key_expression="input.project_id + ':' + input.run_id",
            fallback_ttl=timedelta(days=7),
        ),
        input_validator=RunInvocation,
        desired_worker_labels=affinity,
        execution_timeout=timedelta(days=7),
        retries=0,
        eviction_policy=eviction_policy,
    )
    async def coordinator(input: RunInvocation, ctx: DurableContext) -> RunAdvance:
        async def control(command, key):
            return await advance.aio_run(command, child_key=key)

        async def action(command, route, key):
            task = hatchet.stubs.task(
                name=action_name(route.pool_id, route.role),
                input_validator=ActionInvocation,
                output_validator=StageReceipt,
            )
            return await task.aio_run(
                command,
                child_key=key,
                desired_worker_labels=labels(
                    factory_protocol="shared-v1",
                    factory_pool=route.pool_id,
                    factory_role=route.role,
                ),
            )

        async def sleep():
            await ctx.aio_sleep_for(timedelta(seconds=poll_seconds))

        return await coordinate(input, control, action, sleep, parallelism=parallelism)

    return SimpleNamespace(coordinator=coordinator, advance=advance)


def build_control_worker(hatchet, name, advance_run, *, slots=2, durable_slots=16):
    if not 1 <= slots <= 8 or not 1 <= durable_slots <= 16:
        raise ValueError("Invalid control worker capacity")
    tasks = register_coordinator(hatchet, advance_run)
    return hatchet.worker(
        name,
        slots=slots,
        durable_slots=durable_slots,
        labels={"factory_protocol": "shared-v1", "factory_role": "control"},
        workflows=[tasks.coordinator, tasks.advance],
    )


def register_project_waits(
    hatchet, check_wait, resume_wait, *, namespace="flumic-project-waits-v1"
):
    """Trusted infrastructure handlers load authenticated, stored request bindings.

    Neither callback may resolve latest definitions or accept tenant-supplied
    context as authority. Public transport/engine outbox wiring is in #11.
    """
    from factory.workflows.project_waits import (
        WaitInvocation,
        WaitCheck,
        coordinate_wait,
    )

    affinity = labels(factory_protocol="shared-v1", factory_role="control")

    @hatchet.task(
        name=namespace + "-check",
        input_validator=WaitInvocation,
        desired_worker_labels=affinity,
        retries=3,
        backoff_factor=2.0,
        backoff_max_seconds=10,
        execution_timeout=timedelta(seconds=30),
    )
    def check(input: WaitInvocation, ctx: Context) -> WaitCheck:
        try:
            return WaitCheck.model_validate(check_wait(input.wait_id))
        except ValueError:
            raise NonRetryableException("Invalid project wait receipt") from None

    @hatchet.task(
        name=namespace + "-resume",
        input_validator=WaitInvocation,
        desired_worker_labels=affinity,
        retries=3,
        backoff_factor=2.0,
        backoff_max_seconds=10,
        execution_timeout=timedelta(seconds=30),
    )
    def resume(input: WaitInvocation, ctx: Context) -> bool:
        try:
            result = resume_wait(input.wait_id)
            if type(result) is not bool:
                raise ValueError("Expected durable acceptance")
            return result
        except ValueError:
            raise NonRetryableException(
                "Invalid project wait resumption receipt"
            ) from None

    @hatchet.durable_task(
        name=namespace + "-wait",
        idempotency=StatusBasedIdempotencyConfig(
            key_expression="input.wait_id", fallback_ttl=timedelta(days=7)
        ),
        input_validator=WaitInvocation,
        desired_worker_labels=affinity,
        retries=0,
        execution_timeout=timedelta(days=7),
    )
    async def wait(input: WaitInvocation, ctx: DurableContext) -> dict[str, str]:
        async def check_child(wait_id, key):
            return await check.aio_run(WaitInvocation(wait_id=wait_id), child_key=key)

        async def resume_child(wait_id, key):
            return await resume.aio_run(WaitInvocation(wait_id=wait_id), child_key=key)

        async def sleep(seconds):
            await ctx.aio_sleep_for(timedelta(seconds=seconds))

        return await coordinate_wait(input.wait_id, check_child, resume_child, sleep)

    return SimpleNamespace(wait=wait, check=check, resume=resume)
