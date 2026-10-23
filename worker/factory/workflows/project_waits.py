"""Durable project waits exchange request IDs and short bounded status receipts."""

from typing import Literal
from pydantic import Field
from factory.workflows.contracts import Contract


class WaitInvocation(Contract):
    wait_id: str = Field(min_length=1, max_length=120)


class WaitCheck(Contract):
    kind: Literal["discussion", "review"]
    state: Literal["waiting", "ready", "issue-not-open", "merged", "closed-unmerged"]
    poll_seconds: int = Field(default=30, ge=1, le=3600)


async def coordinate_wait(wait_id, check, resume, sleep):
    WaitInvocation(wait_id=wait_id)
    round_number = 0
    while True:
        status = WaitCheck.model_validate(
            await check(wait_id, f"check/{wait_id}/{round_number}")
        )
        allowed = {
            "discussion": {"waiting", "ready", "issue-not-open"},
            "review": {"waiting", "merged", "closed-unmerged"},
        }
        if status.state not in allowed[status.kind]:
            raise ValueError("Wait status does not match request kind")
        if status.state in {"issue-not-open", "closed-unmerged"}:
            return {"state": status.state}
        if status.state in {"ready", "merged"}:
            accepted = await resume(wait_id, f"resume/{wait_id}/{round_number}")
            if type(accepted) is not bool:
                raise ValueError("Wait resumption must confirm durable acceptance")
            if accepted:
                return {
                    "state": "resumed"
                    if status.kind == "discussion"
                    else "verification-queued"
                }
        await sleep(status.poll_seconds)
        round_number += 1


def check_project_wait(
    context, kind, command, *, dispatch, state, service_factory=None, observer=None
):
    """Trusted short check: caller loads the stored request/context, not live defaults."""
    from factory.bootstrap import action_service
    from factory.domain.models import IssueInput, PullInput
    from factory.workflows.actions import _pull_scope

    if (
        kind not in {"discussion", "review"}
        or command.repository != context.settings.repository
    ):
        raise ValueError("Wait command crosses pinned repository scope")
    if not isinstance(command, IssueInput if kind == "discussion" else PullInput):
        raise ValueError("Wait command type does not match request kind")
    if kind == "review":
        _pull_scope(command, context.settings.repository)
    scoped = context.with_credentials({"github": context.credential("github")})
    command = command.model_copy(
        update={
            "factory_run_id": context.factory_run_id or context.run_id,
            "trace_context": dict(command.trace_context) or dict(context.trace_context),
        }
    )
    with (service_factory or action_service)(
        scoped, dispatch=dispatch, state=state, observer=observer
    ) as service:
        status = (
            service.discussion_status(command)
            if kind == "discussion"
            else service.pull_wait_status(command)
        )
        return WaitCheck(
            kind=kind,
            state=status,
            poll_seconds=context.settings.discussion_poll_seconds,
        )
