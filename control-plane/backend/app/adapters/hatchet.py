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
    def __init__(self, root, client=None):
        self.root = root
        self._client = client

    @property
    def client(self):
        if self._client is None:
            self._client = create_client(
                Settings.load(
                    {"WORKER_CONFIG_FILE": str(self.root / "worker-config.yaml")}
                )
            )
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
