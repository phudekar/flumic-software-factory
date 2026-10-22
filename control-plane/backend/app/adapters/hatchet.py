"""Bounded Hatchet reads. Payloads are allowlisted, never raw SDK responses."""

from app.security import redact
from factory.settings import Settings
from factory.adapters.orchestration.hatchet import create_client


def plain(value):
    return (
        value.model_dump(mode="json", by_alias=True)
        if hasattr(value, "model_dump")
        else value
    )


def items(value):
    data = plain(value)
    if isinstance(data, list):
        return data
    return data.get("rows") or data.get("data") or []


class HatchetEngine:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = create_client(self.settings)
        return self._client

    def workers(self):
        result = []
        for worker in items(self.client.workers.list()):
            worker = plain(worker)
            metadata = worker.get("metadata") or {}
            result.append(
                {
                    "id": metadata.get("id") or worker.get("id"),
                    "name": worker.get("name"),
                    "last_heartbeat": worker.get("lastHeartbeatAt"),
                    "paused": worker.get("isPaused"),
                    "status": worker.get("status"),
                    "slots": worker.get("maxRuns"),
                }
            )
        return result

    @staticmethod
    def normalize(row):
        row = plain(row)
        metadata = row.get("metadata") or {}
        return {
            "id": row.get("workflowRunExternalId")
            or row.get("taskExternalId")
            or metadata.get("id"),
            "name": row.get("displayName") or row.get("workflowName"),
            "status": row.get("status"),
            "workflow": row.get("workflowName"),
            "created_at": row.get("createdAt") or metadata.get("createdAt"),
            "started_at": row.get("startedAt"),
            "finished_at": row.get("finishedAt"),
        }

    def runs(self, offset=0, limit=50):
        result = []
        for value in items(
            self.client.runs.list(offset=offset, limit=limit, include_payloads=False)
        ):
            row = plain(value)
            if self.allowed(row.get("workflowName", "")):
                result.append(self.normalize(row))
        return result

    @staticmethod
    def allowed(name):
        return name in {
            "software-factory-issue-agent",
            "verify-merged-pr-and-close-issue",
            "process-open-pull-request-feedback",
            "scan-pending-github-issues",
            "scan-open-pr-feedback",
            "handle-github-issue-event",
            "handle-github-pull-request-feedback-event",
            "inspect-github-pull-request-event",
            "trigger-issue-from-sample-event",
            "wait-for-ready-for-agent",
            "wait-for-pr-review-and-merge",
        }

    def workflow_name(self, value):
        name = value.get("workflowName")
        if not name and value.get("workflowId"):
            definition = plain(self.client.workflows.get(value["workflowId"]))
            name = definition.get("name")
        return name or ""

    def run(self, external_id):
        value = plain(self.client.runs.get(external_id))
        run = value.get("run", {})
        run["workflowName"] = self.workflow_name(run)
        if not self.allowed(run.get("workflowName", "")):
            raise ValueError("Run is outside this factory")
        tasks = [plain(task) for task in value.get("tasks", [])]
        return {
            "run": self.normalize(run),
            "tasks": [
                {
                    "id": task.get("taskExternalId"),
                    "name": task.get("displayName"),
                    "status": task.get("status"),
                    "attempt": task.get("attempt"),
                    "error_message": redact(task.get("errorMessage")),
                    "started_at": task.get("startedAt"),
                    "finished_at": task.get("finishedAt"),
                }
                for task in tasks
            ],
        }

    def logs(self, external_id, limit=200):
        task = plain(self.client.runs.get_task_run(external_id))
        if not self.allowed(self.workflow_name(task)):
            raise ValueError("Task is outside this factory")
        result = []
        for row in items(
            self.client.logs.list(task_run_id=external_id, limit=min(limit, 500))
        ):
            row = plain(row)
            result.append(
                {
                    key: row.get(key)
                    for key in ("createdAt", "message", "level", "taskExternalId")
                }
            )
        return result

    def definition_summary(self, value):
        row = plain(value)
        name = row.get("name", "")
        if not self.allowed(name):
            raise ValueError("Workflow is outside this factory")
        paused = row.get("isPaused")
        return {
            "id": (row.get("metadata") or {}).get("id"),
            "name": name,
            "paused": paused if isinstance(paused, bool) else None,
            "pause_supported": isinstance(paused, bool),
            "cron_behavior": row.get("pausedWorkflowCronRunQueueBehavior"),
            "scheduled_behavior": row.get("pausedWorkflowScheduledRunQueueBehavior"),
            "input_example": self.input_example(name),
        }

    def definitions(self):
        result = []
        # Filter every page so unrelated tenant definitions cannot hide later
        # factory definitions or appear in the operator controls.
        for offset in range(0, 10000, 100):
            page = items(self.client.workflows.list(limit=100, offset=offset))
            for value in page:
                row = plain(value)
                if self.allowed(row.get("name", "")):
                    result.append(self.definition_summary(row))
            if len(page) < 100:
                return sorted(result, key=lambda row: row["name"])
        raise ValueError("Workflow list exceeds the supported pagination limit")

    def definition(self, identifier):
        result = self.definition_summary(self.client.workflows.get(identifier))
        if result["id"] != identifier:
            raise ValueError("Workflow identity does not match the request")
        return result

    @staticmethod
    def input_model(name):
        from hatchet_sdk import EmptyModel
        from factory.domain.models import IssueInput, PullInput
        from factory.adapters.github_events import GitHubWebhook

        if name in {"scan-pending-github-issues", "scan-open-pr-feedback"}:
            return EmptyModel
        if name in {
            "software-factory-issue-agent",
            "trigger-issue-from-sample-event",
            "wait-for-ready-for-agent",
        }:
            return IssueInput
        if name in {
            "verify-merged-pr-and-close-issue",
            "process-open-pull-request-feedback",
            "wait-for-pr-review-and-merge",
        }:
            return PullInput
        return GitHubWebhook

    def input_example(self, name):
        model = self.input_model(name)
        fields = model.model_fields
        if not fields:
            return {}
        repository = self.settings.repository
        if "action" in fields:
            return {
                "action": "opened",
                "repository": {"full_name": repository},
                "issue": {
                    "number": None,
                    "title": "",
                    "body": "",
                    "state": "open",
                    "labels": [],
                },
            }
        result = {"repository": repository, "issue_number": None}
        if "pull_number" in fields:
            result.update(pull_number=None, pull_url="", branch="")
        return result

    def validate_input(self, name, payload):
        from pydantic import ValidationError
        from factory.adapters.github_events import GitHubWebhook

        model = self.input_model(name)
        if model is not GitHubWebhook and set(payload) - set(model.model_fields):
            raise ValueError("Unknown workflow input fields")
        try:
            parsed = model.model_validate(payload)
        except ValidationError as exc:
            fields = sorted(
                {".".join(str(part) for part in item["loc"]) for item in exc.errors()}
            )
            raise ValueError(
                "Invalid workflow input fields: " + ", ".join(fields)
            ) from None
        repository = getattr(parsed, "repository", None)
        if repository is not None:
            repository = getattr(repository, "full_name", repository)
            expected = self.settings.repository
            if repository != expected:
                raise ValueError("Workflow input repository is outside this factory")
        return parsed.model_dump(mode="json")

    def control(
        self,
        identifier,
        action,
        payload,
        queue_ttl_seconds=86400,
        cron_behavior="DROP",
        scheduled_behavior="DROP",
        operation_id=None,
    ):
        from datetime import timedelta

        definition = self.definition(identifier)
        if action in {"pause", "resume"} and not definition["pause_supported"]:
            raise ValueError(
                "This Hatchet server does not report native workflow pause support"
            )
        if action == "pause":
            result = self.client.workflows.pause(
                identifier,
                queue_ttl=timedelta(seconds=queue_ttl_seconds),
                paused_workflow_cron_run_queue_behavior=cron_behavior,
                paused_workflow_scheduled_run_queue_behavior=scheduled_behavior,
            )
            return self.definition_summary(result)
        if action == "resume":
            return self.definition_summary(self.client.workflows.unpause(identifier))
        if action == "trigger":
            from hatchet_sdk.exceptions import IdempotencyCollisionError

            name = definition["name"]
            prepared = self.input_model(name).model_validate(
                self.validate_input(name, payload)
            )
            # Construct a local run handle, not a registration. Workflow.run uses
            # the SDK's typed collision handling and performs one trigger call.
            workflow = self.client.workflow(
                name=name, input_validator=self.input_model(name)
            )
            try:
                ref = workflow.run(
                    prepared,
                    wait_for_result=False,
                    additional_metadata={"factory_control_operation_id": operation_id}
                    if operation_id
                    else {},
                )
            except IdempotencyCollisionError as collision:
                return {
                    "run_id": collision.existing_run_external_id,
                    "status": "ALREADY_ACTIVE",
                    "deduplicated": True,
                }
            identifier = ref.workflow_run_id
            if not isinstance(identifier, str) or not identifier:
                raise RuntimeError("Engine response did not contain a run identity")
            return {"run_id": identifier, "status": "SUBMITTED"}
        raise ValueError("Unsupported workflow action")
