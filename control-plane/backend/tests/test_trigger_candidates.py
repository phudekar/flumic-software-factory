"""Only current source facts, never webhook fields, authorize workflow inputs."""

import unittest
from unittest.mock import Mock
from fastapi import HTTPException
from app.trigger_runtime import TriggerRuntime
from factory.adapters.gitlab_events import GitLabEvent


class TriggerCandidateTests(unittest.TestCase):
    def setUp(self):
        self.runtime = TriggerRuntime(None, None, None)
        self.client = Mock(repository="group/repo", project_id=123)
        self.labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }
        self.event = GitLabEvent(
            event_id="delivery",
            payload_digest="a" * 64,
            project_id=123,
            repository="group/repo",
            object_kind="merge_request",
            action="update",
            pull_number=4,
        )
        self.pull = {
            "number": 4,
            "html_url": "https://gitlab.com/group/repo/-/merge_requests/4",
            "body": "Closes #7",
            "state": "open",
            "merged": False,
            "source_project_id": 123,
            "target_project_id": 123,
            "head": {"ref": "factory-7"},
            "merge_commit_sha": None,
        }
        self.client.get_pull_feedback.return_value = []
        self.client.get_pull_request.return_value = self.pull
        self.client.get_issue.return_value = {
            "number": 7,
            "state": "open",
            "labels": [{"name": "pr-open"}],
        }

    def candidate(self, kind, event=None):
        return self.runtime.candidate(
            kind, event or self.event, self.client, self.labels, {}
        )

    def test_feedback_uses_latest_human_note_id_and_fresh_linked_issue(self):
        self.client.get_pull_feedback.return_value = [{"id": 41}, {"id": 42}]
        key, _, eligible, stamp, inputs = self.candidate("feedback")
        self.assertEqual((key, eligible, stamp), ("merge-request/4", True, "42"))
        self.assertEqual(inputs["issue_number"], 7)
        self.assertEqual(inputs["branch"], "factory-7")
        self.client.get_issue.return_value = {
            "state": "closed",
            "labels": [{"name": "pr-open"}],
        }
        self.assertFalse(self.candidate("feedback")[2])

    def test_only_actual_merged_commit_allows_verification(self):
        self.assertFalse(self.candidate("verify")[2])
        self.client.get_pull_request.return_value = {
            **self.pull,
            "state": "closed",
            "merged": False,
        }
        self.assertFalse(self.candidate("verify")[2])
        self.client.get_pull_request.return_value = {
            **self.pull,
            "state": "closed",
            "merged": True,
        }
        self.assertFalse(self.candidate("verify")[2])
        self.client.get_pull_request.return_value = {
            **self.pull,
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "a" * 40,
        }
        self.assertTrue(self.candidate("verify")[2])
        self.assertEqual(self.candidate("verify")[3], "a" * 40)

    def test_fork_and_mismatched_iid_are_rejected(self):
        for override in (
            {"source_project_id": 999},
            {"target_project_id": 999},
            {"number": 99},
        ):
            self.client.get_pull_request.return_value = {**self.pull, **override}
            with self.assertRaises(HTTPException):
                self.candidate("feedback")

    def test_system_note_is_ignored_before_remote_read(self):
        self.assertIsNone(
            self.candidate(
                "feedback",
                self.event.model_copy(update={"object_kind": "note", "system": True}),
            )
        )
        self.client.get_pull_request.assert_not_called()

    def test_source_issue_iid_cannot_differ_from_requested_iid(self):
        self.client.get_issue.return_value = {
            "number": 99,
            "state": "open",
            "labels": [{"name": "ready"}],
        }
        event = self.event.model_copy(
            update={"object_kind": "issue", "issue_number": 7, "pull_number": None}
        )
        with self.assertRaises(HTTPException):
            self.candidate("issue-ready", event)
