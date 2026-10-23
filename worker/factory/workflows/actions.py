"""Closed builtin implementations. Published data never selects Python attributes."""

from types import MappingProxyType
from urllib.parse import urlsplit
from factory.workflows.action_schemas import CATALOG
from factory.workflows.bindings import ResolvedInputs
from factory.workflows.contracts import ActionResult, ActionExecutionError
from factory.workflows.redaction import secret_scope, redact
from factory.workflows.references import json_bytes
from factory.domain.models import IssueInput, PullInput
from factory.bootstrap import action_service


def minimum_purposes(action, settings):
    if action not in CATALOG:
        raise ValueError("Unknown action version")
    if action in {"core.echo@1", "workflow.record@1"}:
        return ()
    purposes = {"github"}
    planner_needed = action in {"issue.plan@1", "issue.triage@1", "change.review@1"}
    if action == "pull.feedback@1":
        planner_needed = (
            settings.coding_factory
            or {
                "api": "factory.adapters.agents:api_coding",
                "codex": "factory.adapters.agents:codex_coding",
                "claude": "factory.adapters.agents:claude_coding",
            }[settings.developer_agent]
        ) == "factory.adapters.agents:api_coding"
    if planner_needed:
        if settings.planner_factory != "factory.adapters.agents:openai_model":
            raise ValueError(
                "Install a trusted credential policy for the custom planner"
            )
        purposes.add("openai")
    if action == "change.review@1":
        if settings.reviewer_factory != "factory.adapters.agents:openai_model":
            raise ValueError(
                "Install a trusted credential policy for the custom reviewer"
            )
        purposes.add("openai")
    if action in {"change.implement@1", "pull.feedback@1"}:
        coding = (
            settings.coding_factory
            or {
                "api": "factory.adapters.agents:api_coding",
                "codex": "factory.adapters.agents:codex_coding",
                "claude": "factory.adapters.agents:claude_coding",
            }[settings.developer_agent]
        )
        providers = {
            "factory.adapters.agents:api_coding": "openai",
            "factory.adapters.agents:codex_coding": "openai",
            "factory.adapters.agents:claude_coding": "anthropic",
        }
        if coding not in providers:
            raise ValueError("Install a trusted credential policy for the custom coder")
        purposes.add(providers[coding])
    return tuple(sorted(purposes))


def stage_purposes(stage, settings):
    return (
        minimum_purposes(stage.action, settings) if stage.executor == "builtin" else ()
    )


def _load(service, command, args):
    return service.load_issue(command)


def _plan(service, command, args):
    # Persist a dispatch intent before submitting children. A transport failure
    # after planning must not lose the remaining sub-issues on the retry path.
    key = f"followups:v1:{command.repository}:{command.issue_number}:plan"
    pending = service.state.get(key)
    if pending is None:
        progress = service.state.get(
            f"progress:{command.repository}:issue:{command.issue_number}:plan_issue"
        )
        if progress and progress.get("status") == "sub-issues-created":
            pending = {"numbers": progress["sub_issue_numbers"], "accepted": False}
            # Preserve recoverable operation progress if the intent store is unavailable.
            service.state.put(key, pending)
    result = service.plan_issue(command, args.state)
    if result.status == "sub-issues-created" and pending is None:
        pending = {"numbers": result.sub_issue_numbers, "accepted": False}
        service.state.put(key, pending)
    if (
        pending
        and not pending.get("accepted", False)
        and result.status in {"sub-issues-created", "already-planned"}
    ):
        for number in pending["numbers"]:
            service.dispatch.enqueue_issue(
                command.model_copy(update={"issue_number": number})
            )
        service.state.put(key, {**pending, "accepted": True})
    return result


def _triage(service, command, args):
    return service.triage_issue(command, args.state, args.plan)


def _implement(service, command, args):
    return service.generate_and_open_draft_pr(command, args.triage)


def _review(service, command, args):
    return service.review_opened_pull_request(command, args.publish)


def _record(service, command, args):
    return service.record_workflow_result(command, args.result)


def _feedback(service, command, args):
    return service.process_pull_request_feedback(command)


def _verify(service, command, args):
    return service.verify_merged_pr(command)


_HANDLERS = MappingProxyType(
    {
        "issue.load@1": _load,
        "issue.plan@1": _plan,
        "issue.triage@1": _triage,
        "change.implement@1": _implement,
        "change.review@1": _review,
        "workflow.record@1": _record,
        "pull.feedback@1": _feedback,
        "pull.verify@1": _verify,
    }
)


def _pull_scope(value, repository):
    if value.pull_url:
        url = urlsplit(value.pull_url)
        if (
            url.scheme != "https"
            or url.netloc.lower() != "github.com"
            or url.path.lower() != f"/{repository}/pull/{value.pull_number}".lower()
            or url.query
            or url.fragment
        ):
            raise ValueError(
                "Pull URL does not match action repository and pull number"
            )


class ActionCatalog:
    ids = tuple(CATALOG)

    def __init__(self, *, service_factory=action_service, purposes=minimum_purposes):
        self.service_factory, self.purposes = service_factory, purposes

    def execute(self, action, context, inputs, *, dispatch, state, observer=None):
        # Validation failures can echo bad values too. Cover the whole boundary,
        # including argument parsing before any service/client is constructed.
        with secret_scope(context.credentials.values()):
            try:
                return self._execute(
                    action,
                    context,
                    inputs,
                    dispatch=dispatch,
                    state=state,
                    observer=observer,
                )
            except Exception as error:
                safe = redact(str(error))
                if safe != str(error):
                    raise ActionExecutionError(safe[:4000]) from None
                raise

    def _execute(self, action, context, inputs, *, dispatch, state, observer=None):
        descriptor = CATALOG.get(action)
        if descriptor is None or not isinstance(inputs, ResolvedInputs):
            raise ValueError("Unknown action or unresolved input provenance")
        if len(json_bytes(inputs.data)) > 1048576:
            raise ValueError("Action input exceeds 1 MiB")
        for origin in inputs.origins:
            if origin.project_id != context.project_id or (
                action != "core.echo@1"
                and origin.repository_id != context.repository_id
            ):
                raise ValueError("Action input provenance crosses repository scope")
        if action == "core.echo@1":
            return ActionResult(output=inputs.data)
        import json

        if not json.loads(context.settings_json).get("repository"):
            raise ValueError("An explicit action repository is required")
        settings = context.settings
        if descriptor.role != settings.worker_type:
            raise ValueError("Action role does not match context")
        args = descriptor.arguments.model_validate(inputs.data, strict=True)
        for value in (
            vars(args).get("state"),
            vars(args).get("plan"),
            vars(args).get("triage"),
            vars(args).get("publish"),
            vars(args).get("result"),
        ):
            if (
                value is not None
                and value.issue_number is not None
                and value.issue_number != args.command.issue_number
            ):
                raise ValueError("Upstream output belongs to another issue")
        if action in {"pull.feedback@1", "pull.verify@1"}:
            _pull_scope(args.command, settings.repository)
        if action == "change.review@1" and args.publish.pull_number is not None:
            _pull_scope(args.publish, settings.repository)
        for purpose in self.purposes(action, settings):
            context.credential(purpose)
        common = {
            "repository": settings.repository,
            "factory_run_id": context.factory_run_id or context.run_id,
            "trace_context": dict(context.trace_context),
            **args.command.model_dump(),
        }
        command = (
            PullInput(**common)
            if action in {"pull.feedback@1", "pull.verify@1"}
            else IssueInput(**common)
        )
        with self.service_factory(
            context, dispatch=dispatch, state=state, observer=observer
        ) as service:
            result = _HANDLERS[action](service, command, args)
            validated = descriptor.output.model_validate(result)
            return ActionResult(output=validated.model_dump(mode="json"))
