import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

import worker
from model_client import (
    FileChange,
    IssueWorkPlan,
    ModelError,
    PatchPlan,
    PlannedSubtask,
)
from worker import (
    _format_issue_plan_comment,
    _format_review_feedback,
    _format_sub_issue_body,
    _generate_code_patch,
    _pending_pull_feedback,
    _stored_issue_plan,
    _sub_issue_marker,
    _validate_patch,
)


class PatchValidationTests(unittest.TestCase):
    def test_cli_patch_can_change_any_safe_repository_path(self):
        plan = PatchPlan(
            pull_title="Update project configuration",
            pull_body="Updates project files.",
            is_complete=True,
            progress="done",
            changes=[
                FileChange(path=".github/workflows/test.yml", content="name: test\n"),
                FileChange(path="Cargo.lock", content="version = 3\n"),
                FileChange(path="src/config.toml", content="[app]\n"),
            ],
        )

        _validate_patch(plan, allow_all_repository_paths=True)

    def test_patch_validation_rejects_paths_outside_repository(self):
        plan = PatchPlan(
            pull_title="Unsafe path",
            pull_body="Should be rejected.",
            is_complete=True,
            progress="done",
            changes=[FileChange(path="../outside.py", content="print('unsafe')\n")],
        )

        with self.assertRaisesRegex(ModelError, "disallowed"):
            _validate_patch(plan, allow_all_repository_paths=True)

    def test_api_patch_validation_keeps_existing_path_allowlist(self):
        plan = PatchPlan(
            pull_title="Update workflow",
            pull_body="Should be rejected by API worker policy.",
            is_complete=True,
            progress="done",
            changes=[
                FileChange(path=".github/workflows/test.yml", content="name: test\n")
            ],
        )

        with self.assertRaisesRegex(ModelError, "disallowed"):
            _validate_patch(plan)


class PullRequestFeedbackTests(unittest.TestCase):
    def test_includes_automated_review_comment_but_ignores_agent_bookkeeping(self):
        feedback = [
            {
                "id": 10,
                "source": "conversation",
                "body": (
                    "Automated review found an issue.\n"
                    "<!-- hatchet-issue-agent-review-4 -->"
                ),
            },
            {
                "id": 11,
                "source": "conversation",
                "body": "Please handle the empty-list case.",
            },
            {
                "id": 12,
                "source": "conversation",
                "body": "Created PR: ... <!-- hatchet-issue-agent-pr-4 -->",
            },
            {
                "id": 13,
                "source": "conversation",
                "body": (
                    "Feedback processed. "
                    "<!-- hatchet-pr-feedback-cursor:conversation=11;inline-review=0;review=0 -->"
                ),
            },
        ]

        pending = _pending_pull_feedback(
            feedback,
            {"conversation": 0, "inline review": 0, "review": 0},
            4,
        )

        self.assertEqual([item["id"] for item in pending], [10, 11])
        formatted = _format_review_feedback(pending[0])
        self.assertIn("Automated review found an issue.", formatted)
        self.assertNotIn("hatchet-issue-agent-review", formatted)


class WorkerBackendRoutingTests(unittest.TestCase):
    def test_api_backend_generates_patch_without_repository_archive(self):
        github = Mock()
        expected = object()
        with (
            patch("worker.DEVELOPER_AGENT", "api"),
            patch("worker.ModelClient") as model_client,
        ):
            model_client.return_value.generate_patch.return_value = expected
            result = _generate_code_patch(
                github=github,
                title="Add feature",
                body="Implement it.",
                comments=[],
                files={"src/main.py": "print('hello')"},
                existing_paths={"src/main.py"},
            )

        self.assertIs(result, expected)
        github.get_repository_archive.assert_not_called()
        model_client.return_value.generate_patch.assert_called_once()

    def test_cli_backend_fetches_repository_archive(self):
        github = Mock()
        github.get_repository_archive.return_value = b"archive"
        expected = object()
        with (
            patch("worker.DEVELOPER_AGENT", "codex"),
            patch("worker.generate_cli_patch", return_value=expected) as generate,
        ):
            result = _generate_code_patch(
                github=github,
                title="Add feature",
                body="Implement it.",
                comments=["Please add a test."],
                files={},
                existing_paths={"src/main.py"},
                branch="feature-branch",
            )

        self.assertIs(result, expected)
        github.get_repository_archive.assert_called_once_with("feature-branch")
        self.assertEqual(generate.call_args.kwargs["archive"], b"archive")

    def test_worker_label_values_match_task_routing_roles(self):
        self.assertEqual(worker.SIMPLE_WORKER_LABEL.key, "worker_type")
        self.assertEqual(worker.SIMPLE_WORKER_LABEL.value, "simple")
        self.assertTrue(worker.SIMPLE_WORKER_LABEL.required)
        self.assertEqual(worker.DEVELOPER_WORKER_LABEL.key, "worker_type")
        self.assertEqual(worker.DEVELOPER_WORKER_LABEL.value, "developer")
        self.assertTrue(worker.DEVELOPER_WORKER_LABEL.required)


class IssuePlanningTests(unittest.TestCase):
    def setUp(self):
        self.plan = IssueWorkPlan(
            tasks=[
                PlannedSubtask(
                    title="Add the task form",
                    objective="Let users enter tasks.",
                    acceptance_criteria=["Submitting creates a task."],
                )
            ]
        )

    def test_issue_plan_comment_can_be_recovered_on_retry(self):
        comment = _format_issue_plan_comment(self.plan)

        restored = _stored_issue_plan([comment])

        self.assertEqual(restored, self.plan)

    def test_sub_issue_body_is_self_contained_and_has_idempotency_marker(self):
        marker = _sub_issue_marker(3, 1)
        body = _format_sub_issue_body(
            parent_number=3,
            parent_url="https://github.com/owner/repo/issues/3",
            objective=self.plan.tasks[0].objective,
            acceptance_criteria=self.plan.tasks[0].acceptance_criteria,
            marker=marker,
        )

        self.assertIn("Part of parent issue [#3]", body)
        self.assertIn("## Acceptance criteria", body)
        self.assertIn(marker, body)


if __name__ == "__main__":
    unittest.main()
