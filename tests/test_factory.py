import ast
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing, contextmanager
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from factory.application.service import FactoryService
from factory.application.ports import AgentCapabilities, CodingRequest
from factory.domain.models import (
    FileChange,
    IssueInput,
    IssueWorkPlan,
    ModelError,
    PatchPlan,
    PlannedSubtask,
    PullInput,
    PullRequestReview,
    StaleRevisionError,
    TriagePlan,
    TriageState,
)
from factory.domain.policy import ExecutionPolicy
from factory.settings import Settings
from factory.adapters.agents import APICodingAgent, CLICodingAgent
from factory.adapters.state import MemoryStateStore, OperationBusy, SQLiteStateStore
from factory.adapters.telemetry import JsonFormatter, LoggingObserver
from factory.adapters.orchestration.local import LocalDispatch, LocalRunner
from factory.adapters.github_events import GitHubWebhook, normalize_webhook


class QuietObserver:
    @contextmanager
    def observe(self, stage, attributes):
        yield


def completed_patch():
    return PatchPlan(
        pull_title="Implement validation",
        pull_body="Adds input validation.",
        is_complete=True,
        progress="done",
        changes=[FileChange(path="src/app.py", content="value = 1\n")],
    )


class FactoryTests(unittest.TestCase):
    def setUp(self):
        self.repository = Mock()
        self.repository.repository = "owner/repo"
        self.repository.get_issue.return_value = {
            "number": 2,
            "state": "open",
            "title": "Validate input",
            "body": "Reject invalid input.",
            "labels": [{"name": "agent-subtask"}],
        }
        self.repository.get_issue_comments.return_value = []
        self.repository.list_source_paths.return_value = ["src/app.py"]
        self.repository.list_repository_paths.return_value = {"src/app.py"}
        self.repository.get_source_files.return_value = {"src/app.py": "value = 0\n"}
        self.repository.find_issue_pull_request.return_value = None
        self.repository.resolve_revision.return_value = "a" * 40
        self.repository.create_draft_pull_request.return_value = {
            "number": 4,
            "html_url": "https://example.test/pull/4",
            "head": {"ref": "hatchet/issue-2"},
        }
        self.planner = Mock()
        self.planner.triage.return_value = TriagePlan(
            needs_discussion=False, source_paths=["src/app.py"]
        )
        self.agent = Mock()
        self.agent.capabilities = AgentCapabilities("selected-files")
        self.agent.generate.return_value = completed_patch()
        self.reviewer = Mock()
        self.reviewer.review_pull_request.return_value = PullRequestReview(findings=[])
        self.state = MemoryStateStore()
        self.dispatch = LocalDispatch()
        self.service = FactoryService(
            repository="owner/repo",
            repositories=lambda _: self.repository,
            planner=lambda: self.planner,
            reviewer=lambda: self.reviewer,
            coding_agent=lambda: self.agent,
            dispatch=self.dispatch,
            policy=ExecutionPolicy(),
            state=self.state,
            observer=QuietObserver(),
        )
        self.command = IssueInput(repository="owner/repo", issue_number=2)
        self.triage = TriageState(
            status="ready", issue_number=2, source_paths=["src/app.py"]
        )

    def test_polling_repository_failures_are_observed_without_holding_wait_interval(
        self,
    ):
        from factory.adapters.control import ControlObserver
        from factory.adapters.telemetry import LoggingObserver
        from factory.domain.models import RepositoryError
        from factory.settings import Settings

        publisher = Mock()
        observer = ControlObserver(LoggingObserver(), Settings(), publisher=publisher)
        self.service.observer = observer
        self.repository.get_issue.side_effect = RepositoryError(
            "GitHub API request failed with HTTP 503"
        )
        try:
            with self.assertRaises(RepositoryError):
                self.service.discussion_status(self.command)
            failed = [
                call.args
                for call in publisher.emit.call_args_list
                if call.args[0] == "attempt.completed"
            ]
            self.assertEqual(failed[0][2]["stage"], "wait_for_ready_for_agent")
            self.assertEqual(
                failed[0][1]["error_message"], "GitHub API request failed with HTTP 503"
            )
        finally:
            observer.shutdown()

    def test_custom_labels_and_task_context_reach_application_adapters(self):
        from factory.configuration import Labels, TaskSettings, current_task

        self.service.labels = Labels(
            pr_open="factory-pr", ready_for_agent="factory-ready"
        )
        self.service.task_settings = lambda stage: TaskSettings(
            model="task-model", additional_system_prompt=stage
        )
        seen = []

        def generate(request):
            seen.append(current_task())
            return completed_patch()

        self.agent.generate.side_effect = generate
        self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.assertEqual(seen[0].model, "task-model")
        self.assertEqual(seen[0].additional_system_prompt, "generate_and_open_draft_pr")
        self.repository.add_label.assert_called_with(2, "factory-pr")
        self.repository.get_issue.return_value["labels"] = [{"name": "factory-ready"}]
        self.assertEqual(self.service.discussion_status(self.command), "ready")
        self.assertIsNone(current_task().model)

    def test_implementation_uses_immutable_context_and_explicit_policy(self):
        result = self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.assertEqual(result.status, "draft-pr-created")
        self.repository.get_source_files.assert_called_once_with(
            ["src/app.py"], branch="a" * 40
        )
        request = self.agent.generate.call_args.args[0]
        self.assertEqual(request.base_commit, "a" * 40)
        self.assertIsNone(request.archive)
        self.repository.get_repository_archive.assert_not_called()
        self.assertEqual(
            self.repository.create_draft_pull_request.call_args.kwargs[
                "expected_base_sha"
            ],
            "a" * 40,
        )
        self.assertEqual(
            self.dispatch.requests[0][1].factory_run_id, self.command.factory_run_id
        )
        self.assertIsNotNone(self.state.get("pull:v1:owner/repo:2"))

    def test_archive_agent_does_not_require_provider_specific_application_logic(self):
        self.agent.capabilities = AgentCapabilities("repository-archive")
        self.repository.get_repository_archive.return_value = b"archive"
        self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.repository.get_source_files.assert_not_called()
        self.repository.get_repository_archive.assert_called_once_with("a" * 40)
        self.assertEqual(self.agent.generate.call_args.args[0].archive, b"archive")

    def test_archive_capability_does_not_grant_configuration_file_permissions(self):
        self.agent.capabilities = AgentCapabilities("repository-archive")
        self.agent.generate.return_value.changes[0].path = ".github/workflows/test.yml"
        with self.assertRaisesRegex(ModelError, "disallowed"):
            self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.repository.create_draft_pull_request.assert_not_called()

    def test_incomplete_patch_is_never_published(self):
        self.agent.generate.return_value.is_complete = False
        with self.assertRaisesRegex(ModelError, "incomplete"):
            self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.repository.create_draft_pull_request.assert_not_called()

    def test_retry_reuses_prepared_patch_after_publication_failure(self):
        self.repository.create_draft_pull_request.side_effect = [
            RuntimeError("temporary failure"),
            self.repository.create_draft_pull_request.return_value,
        ]
        with self.assertRaises(RuntimeError):
            self.service.generate_and_open_draft_pr(self.command, self.triage)
        result = self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.assertEqual(result.status, "draft-pr-created")
        self.agent.generate.assert_called_once()
        self.assertEqual(self.repository.create_draft_pull_request.call_count, 2)

    def test_stale_patch_is_invalidated_before_retry(self):
        self.repository.create_draft_pull_request.side_effect = StaleRevisionError(
            "stale"
        )
        with self.assertRaises(StaleRevisionError):
            self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.assertIsNone(self.state.get("patch:v1:owner/repo:2"))

    def test_overlapping_delivery_cannot_generate_a_second_patch(self):
        from factory.configuration import TaskSettings

        self.service.task_settings = lambda stage: TaskSettings(lock_wait_seconds=0)
        with self.state.claim("owner/repo:issue:2", "other-run"):
            with self.assertRaises(OperationBusy):
                self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.agent.generate.assert_not_called()

    def test_existing_pr_recovers_registration_and_still_receives_review(self):
        existing = self.repository.create_draft_pull_request.return_value
        self.repository.find_issue_pull_request.return_value = existing
        published = self.service.generate_and_open_draft_pr(self.command, self.triage)
        self.assertEqual(published.status, "draft-pr-exists")
        self.agent.generate.assert_not_called()
        self.repository.get_pull_request.return_value = {**existing, "labels": []}
        self.repository.get_pull_request_files.return_value = [
            {"path": "src/app.py", "patch": "+value = 1", "status": "modified"},
        ]
        reviewed = self.service.review_opened_pull_request(self.command, published)
        self.assertEqual(reviewed.status, "review-posted")
        self.reviewer.review_pull_request.assert_called_once()
        self.planner.review_pull_request.assert_not_called()

    def test_local_runner_executes_the_same_operations_without_hatchet(self):
        self.repository.get_pull_request.return_value = {
            "number": 4,
            "head": {"ref": "hatchet/issue-2"},
            "labels": [],
        }
        self.repository.get_pull_request_files.return_value = []
        result = LocalRunner(self.service).run_issue(self.command)
        self.assertEqual(result.status, "review-posted")
        self.assertEqual([kind for kind, _ in self.dispatch.requests], ["review"])

    def test_plan_survives_comment_publication_failure(self):
        self.repository.get_issue.return_value["labels"] = []
        self.repository.list_sub_issue_ids.return_value = set()
        self.repository.find_issues_by_markers.return_value = {}
        self.repository.create_issue.return_value = {"number": 3, "id": 300}
        self.planner.plan_issue.return_value = IssueWorkPlan(
            tasks=[
                PlannedSubtask(
                    title="Validate form",
                    objective="Check form input.",
                    acceptance_criteria=["Reject invalid input."],
                )
            ]
        )
        self.repository.comment_once.side_effect = RuntimeError("request failed")
        loaded = self.service.load_issue(self.command)
        with self.assertRaises(RuntimeError):
            self.service.plan_issue(self.command, loaded)
        self.repository.comment_once.side_effect = None
        self.service.plan_issue(self.command, loaded)
        self.planner.plan_issue.assert_called_once()
        self.assertEqual(self.repository.create_issue.call_count, 1)

    def feedback_setup(self):
        self.repository.get_issue.return_value["labels"] = [{"name": "agent-pr-open"}]
        self.repository.get_pull_request.return_value = {
            "number": 4,
            "state": "open",
            "html_url": "https://example.test/pull/4",
            "head": {"ref": "hatchet/issue-2"},
            "body": "Closes #2",
        }
        self.repository.pull_feedback_cursor.return_value = {
            "conversation": 0,
            "inline review": 0,
            "review": 0,
        }
        self.repository.get_pull_feedback.return_value = [
            {
                "id": 10,
                "source": "conversation",
                "body": "Validate empty input.",
                "author": "reviewer",
            }
        ]
        return PullInput(
            repository="owner/repo",
            issue_number=2,
            pull_number=4,
            pull_url="https://example.test/pull/4",
            branch="hatchet/issue-2",
        )

    def test_duplicate_feedback_uses_factory_cursor(self):
        command = self.feedback_setup()
        self.service.process_pull_request_feedback(command)
        result = self.service.process_pull_request_feedback(command)
        self.assertEqual(result.status, "no-new-feedback")
        self.agent.generate.assert_called_once()
        self.repository.update_pull_request_branch.assert_called_once()

    def test_feedback_commit_marker_recovers_recording_failure(self):
        command = self.feedback_setup()
        self.repository.record_pull_feedback.side_effect = RuntimeError(
            "comment failed"
        )
        with self.assertRaises(RuntimeError):
            self.service.process_pull_request_feedback(command)
        # State write also may be lost in a process crash: GitHub marker recovers it.
        self.state.delete("feedback:v1:owner/repo:4")
        self.repository.pull_feedback_cursor.return_value["conversation"] = 10
        result = self.service.process_pull_request_feedback(command)
        self.assertEqual(result.status, "no-new-feedback")
        self.repository.update_pull_request_branch.assert_called_once()

    def test_wait_reconciles_merge_without_approval_event(self):
        command = self.feedback_setup()
        self.repository.get_pull_request.return_value.update(
            state="closed", merged=True
        )
        self.assertEqual(self.service.pull_wait_status(command), "merged")

    def test_closed_unmerged_pr_records_terminal_state(self):
        command = self.feedback_setup()
        self.repository.get_pull_request.return_value.update(
            state="closed", merged=False
        )
        self.assertEqual(self.service.pull_wait_status(command), "closed-unmerged")
        self.assertEqual(
            self.state.get("pull-lifecycle:v1:owner/repo:4")["status"],
            "closed-unmerged",
        )

    def test_foreign_repository_is_rejected_before_any_integration_call(self):
        with self.assertRaises(ValueError):
            self.service.load_issue(IssueInput(repository="other/repo", issue_number=2))
        self.repository.get_issue.assert_not_called()


class AdapterTests(unittest.TestCase):
    def test_api_coding_adapter_translates_neutral_request(self):
        model = Mock()
        agent = APICodingAgent(lambda: model)
        request = CodingRequest(
            title="Task", body="Implement", comments=[], files={}, existing_paths=set()
        )
        agent.generate(request)
        model.generate_patch.assert_called_once_with(
            title="Task",
            body="Implement",
            comments=[],
            files={},
            existing_paths=set(),
            review_feedback=[],
        )

    def test_cli_coding_adapter_passes_explicit_backend_and_execution_runner(self):
        runner = Mock()
        agent = CLICodingAgent("claude", max_iterations=3, runner=runner)
        with patch("cli_agent.generate_patch") as generate:
            agent.generate(
                CodingRequest(
                    title="Task",
                    body="Implement",
                    comments=[],
                    files={},
                    existing_paths=set(),
                    archive=b"archive",
                )
            )
        self.assertEqual(generate.call_args.kwargs["agent"], "claude")
        self.assertIs(generate.call_args.kwargs["runner"], runner)
        self.assertEqual(generate.call_args.kwargs["max_iterations"], 3)

    def test_sqlite_claim_is_shared_across_instances_and_released_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            first, second = SQLiteStateStore(path), SQLiteStateStore(path)
            with self.assertRaisesRegex(RuntimeError, "stop"):
                with first.claim("resource", "implementation"):
                    with self.assertRaises(OperationBusy):
                        with second.claim("resource", "feedback"):
                            self.fail("duplicate claim")
                    first.put("plan", {"version": 1})
                    raise RuntimeError("stop")
            with second.claim("resource", "retry"):
                self.assertEqual(second.get("plan"), {"version": 1})
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute(
                    "SELECT status, error_type FROM operations ORDER BY rowid"
                ).fetchall()
            self.assertEqual(rows, [("failed", "RuntimeError"), ("completed", None)])

    def test_sqlite_claim_is_released_when_process_exits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            code = "from factory.adapters.state import SQLiteStateStore; import os; s=SQLiteStateStore(__import__('sys').argv[1]); c=s.claim('resource','crash'); c.__enter__(); os._exit(0)"
            subprocess.run(
                [sys.executable, "-c", code, path],
                env={
                    **os.environ,
                    "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "worker"),
                },
                check=True,
            )
            store = SQLiteStateStore(path)
            with store.claim("resource", "recovery"):
                pass
            with closing(sqlite3.connect(path)) as db:
                rows = db.execute(
                    "SELECT status FROM operations ORDER BY rowid"
                ).fetchall()
            self.assertEqual(rows, [("interrupted",), ("completed",)])

    def test_settings_are_explicit_and_preserve_migration_defaults(self):
        self.assertEqual(
            Settings.from_env({"DEVELOPER_AGENT": "claude"}).path_policy, "repository"
        )
        self.assertEqual(
            Settings.from_env(
                {"DEVELOPER_AGENT": "claude", "PATCH_PATH_POLICY": "source"}
            ).path_policy,
            "source",
        )
        with self.assertRaises(ValueError):
            Settings.from_env({"MAX_PATCH_ITERATIONS": "21"})

    def test_custom_role_factories_are_independent_and_lazy(self):
        from factory.bootstrap import build_service

        planner, reviewer, coder = Mock(), Mock(), Mock()
        factories = {
            "test:planner": Mock(return_value=planner),
            "test:reviewer": Mock(return_value=reviewer),
            "test:coder": Mock(return_value=coder),
            "test:state": Mock(return_value=MemoryStateStore()),
            "test:observer": Mock(return_value=QuietObserver()),
        }
        with patch(
            "factory.bootstrap.load_factory", side_effect=lambda path: factories[path]
        ):
            service = build_service(
                Settings(
                    repository="owner/repo",
                    planner_factory="test:planner",
                    reviewer_factory="test:reviewer",
                    coding_factory="test:coder",
                    state_factory="test:state",
                    observer_factory="test:observer",
                ),
                LocalDispatch(),
            )
        factories["test:planner"].assert_not_called()
        self.assertIs(service.planner(), planner)
        self.assertIs(service.reviewer(), reviewer)
        self.assertIs(service.coding_agent(), coder)

    def test_clean_imports_do_not_initialize_integrations_or_read_credentials(self):
        source = """
import sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'hatchet_sdk', 'openai', 'tiktoken'}:
            raise AssertionError('Provider imported: ' + fullname)
sys.meta_path.insert(0, Block())
import worker
import cli_agent
from factory.application.service import FactoryService
"""
        subprocess.run(
            [sys.executable, "-c", source],
            env={
                "PATH": os.environ["PATH"],
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "worker"),
            },
            check=True,
        )

    def test_core_does_not_import_adapter_or_provider_modules(self):
        root = Path(__file__).resolve().parents[1] / "worker/factory"
        for directory in ("domain", "application"):
            for file in (root / directory).glob("*.py"):
                for node in ast.walk(ast.parse(file.read_text())):
                    names = (
                        [node.module or ""]
                        if isinstance(node, ast.ImportFrom)
                        else [a.name for a in node.names]
                        if isinstance(node, ast.Import)
                        else []
                    )
                    for name in names:
                        self.assertFalse(
                            name.startswith(
                                (
                                    "factory.adapters",
                                    "hatchet_sdk",
                                    "openai",
                                    "model_client",
                                    "github_client",
                                    "cli_agent",
                                )
                            ),
                            (file, name),
                        )

    def test_github_event_normalization_is_stable_and_repository_scoped(self):
        webhook = GitHubWebhook(
            action="opened",
            repository={"full_name": "owner/repo"},
            issue={"number": 2, "title": "Task", "state": "open"},
        )
        first, second = (
            normalize_webhook(webhook, "issues"),
            normalize_webhook(webhook, "issues"),
        )
        self.assertEqual(first.kind, "work-item-ready")
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(first.schema_version, 1)
        self.assertEqual(first.repository, "owner/repo")

    def test_observer_records_duration_and_error_type_without_exception_content(self):
        with self.assertLogs("issue-worker.operations", level="INFO") as logs:
            with self.assertRaises(ValueError):
                with LoggingObserver().observe("implement", {"issue_number": 2}):
                    raise ValueError("private source contents")
        record = logs.records[-1]
        self.assertEqual(record.error_type, "ValueError")
        self.assertEqual(record.outcome, "failed")
        self.assertGreaterEqual(record.duration_seconds, 0)
        self.assertNotIn("private source contents", JsonFormatter().format(record))

    def test_hatchet_registration_serializes_without_a_live_server(self):
        import base64
        import json
        import warnings
        from hatchet_sdk import Hatchet
        from hatchet_sdk.config import ClientConfig
        from factory.adapters.orchestration.hatchet import HatchetDispatch, build_worker

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
        dispatch = HatchetDispatch()
        with patch.object(engine, "worker") as register:
            build_worker(engine, Mock(), Settings(), dispatch)
        workflows = register.call_args.kwargs["workflows"]
        self.assertEqual(len(workflows), 11)
        definitions = [workflow.to_proto() for workflow in workflows]
        self.assertEqual(definitions[0].name, "software-factory-issue-agent")
        self.assertEqual(
            definitions[0].idempotency.expression,
            "input.repository + ':issue:' + string(input.issue_number)",
        )
        self.assertEqual(len(definitions[0].concurrency_arr), 2)
        self.assertFalse(definitions[0].concurrency_arr[0].is_tenant_scoped)
        self.assertEqual(definitions[0].concurrency_arr[0].max_runs, 1)
        self.assertEqual(len(workflows[0].tasks), 6)
        self.assertIsNotNone(dispatch.tasks)

    def test_json_logging_redacts_recognizable_credentials(self):
        record = logging.LogRecord(
            "test", logging.INFO, "", 0, "sk-abcdefghijklmnopqrstuvwxyz123456", (), None
        )
        rendered = JsonFormatter().format(record)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", rendered)
        self.assertIn("redacted", rendered)


if __name__ == "__main__":
    unittest.main()
