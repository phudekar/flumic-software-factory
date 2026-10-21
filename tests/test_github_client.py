import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from github_client import GitHubClient


class GitHubClientTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient.__new__(GitHubClient)
        self.client.repository = "owner/repo"
        self.client._request = Mock()

    def test_feedback_collection_includes_conversation_inline_and_review_comments(self):
        self.client._request.side_effect = [
            [
                {
                    "id": 5,
                    "body": "Please clarify the expected behavior.",
                    "user": {"login": "reviewer"},
                }
            ],
            [
                {
                    "id": 7,
                    "body": "Handle this edge case.",
                    "path": "src/worker.py",
                    "line": 12,
                    "user": {"login": "reviewer"},
                }
            ],
            [
                {
                    "id": 6,
                    "body": "Please update the docs.",
                    "user": {"login": "reviewer"},
                }
            ],
        ]

        feedback = self.client.get_pull_feedback(4)

        self.assertEqual([item["id"] for item in feedback], [5, 6, 7])
        self.assertEqual(feedback[-1]["path"], "src/worker.py")
        self.assertEqual(self.client._request.call_count, 3)

    def test_pull_request_files_returns_only_text_patches(self):
        self.client._request.return_value = [
            {
                "filename": "src/worker.py",
                "patch": "@@ -1 +1 @@\n-return None\n+return value",
                "status": "modified",
            }
        ]

        files = self.client.get_pull_request_files(4)

        self.assertEqual(
            files,
            [
                {
                    "path": "src/worker.py",
                    "patch": "@@ -1 +1 @@\n-return None\n+return value",
                    "status": "modified",
                }
            ],
        )
        self.client._request.assert_called_once_with(
            "GET",
            "/pulls/4/files",
            query={"per_page": "100", "page": "1"},
        )

    def test_find_issues_by_markers_skips_pull_requests_and_finds_children(self):
        marker = "<!-- hatchet-parent-issue-3-step-1 -->"
        self.client._request.return_value = [
            {"number": 9, "id": 900, "body": f"Child body {marker}"},
            {"number": 8, "pull_request": {}, "body": f"PR body {marker}"},
        ]

        found = self.client.find_issues_by_markers({marker})

        self.assertEqual(found, {marker: {"number": 9, "id": 900}})

    def test_create_issue_submits_label_with_the_new_issue(self):
        self.client._request.return_value = {"number": 12, "id": 1200}

        created = self.client.create_issue(
            title="Implement a task form",
            body="Add the form.",
            labels=["agent-subtask"],
        )

        self.assertEqual(created["number"], 12)
        self.assertEqual(created["id"], 1200)
        self.client._request.assert_called_once_with(
            "POST",
            "/issues",
            body={
                "title": "Implement a task form",
                "body": "Add the form.",
                "labels": ["agent-subtask"],
            },
        )

    def test_repository_archive_uses_authenticated_api_request(self):
        self.client._token = "test-github-token"
        self.client._base = "https://api.github.com/repos/owner/repo"
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b"archive-data"
        repository_response = {"default_branch": "main"}
        self.client._request.return_value = repository_response

        with patch(
            "github_client.urllib.request.urlopen", return_value=response
        ) as urlopen:
            archive = self.client.get_repository_archive()

        self.assertEqual(archive, b"archive-data")
        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.get_header("Authorization"), "Bearer test-github-token"
        )

    def test_add_sub_issue_uses_github_native_parent_relationship(self):
        self.client._request.return_value = {}

        self.client.add_sub_issue(3, 1200)

        self.client._request.assert_called_once_with(
            "POST",
            "/issues/3/sub_issues",
            body={"sub_issue_id": 1200},
        )

    def test_list_sub_issue_ids_returns_issue_database_ids(self):
        self.client._request.return_value = [{"id": 1200}, {"id": 1300}]

        issue_ids = self.client.list_sub_issue_ids(3)

        self.assertEqual(issue_ids, {1200, 1300})

    def test_feedback_cursor_uses_comment_marker_and_branch_commit(self):
        self.client._list_issue_comments = Mock(
            return_value=[
                {
                    "body": (
                        "Handled feedback. <!-- hatchet-pr-feedback-cursor:"
                        "conversation=24;inline-review=0;review=0 -->"
                    )
                }
            ]
        )
        self.client._get_branch_sha = Mock(return_value="head-sha")
        self.client._request.return_value = {
            "message": (
                "Commit\n\n<!-- hatchet-pr-feedback-cursor:"
                "conversation=0;inline-review=0;review=31 -->"
            )
        }

        cursor = self.client.pull_feedback_cursor(4, "hatchet/issue-2")

        self.assertEqual(
            cursor,
            {"conversation": 24, "inline review": 0, "review": 31},
        )
        self.client._request.assert_called_once_with("GET", "/git/commits/head-sha")

    def test_feedback_tracking_comment_is_updated_in_place(self):
        self.client._list_issue_comments = Mock(
            return_value=[
                {
                    "id": 19,
                    "body": (
                        "Previous result. <!-- hatchet-pr-feedback-cursor:"
                        "conversation=4;inline-review=0;review=0 -->"
                    ),
                }
            ]
        )

        self.client.record_pull_feedback(
            4,
            {"conversation": 8, "inline review": 12, "review": 0},
            "Updated the branch.",
        )

        self.client._request.assert_called_once()
        request = self.client._request.call_args
        self.assertEqual(request.args[:2], ("PATCH", "/issues/comments/19"))
        self.assertIn(
            "conversation=8;inline-review=12;review=0",
            request.kwargs["body"]["body"],
        )

    def test_feedback_branch_update_updates_existing_branch_without_creating_pr(self):
        self.client._request.side_effect = [
            {"object": {"sha": "old-sha"}},
            {"tree": {"sha": "old-tree"}},
            {"sha": "blob-sha"},
            {"sha": "tree-sha"},
            {"sha": "new-sha"},
            {"object": {"sha": "old-sha"}},
            {},
        ]

        result = self.client.update_pull_request_branch(
            branch="hatchet/issue-2",
            issue_number=2,
            feedback_cursor={
                "conversation": 0,
                "inline review": 0,
                "review": 31,
            },
            changes=[SimpleNamespace(path="src/worker.py", content="updated\n")],
        )

        self.assertEqual(result, "new-sha")
        commit_request = next(
            request
            for request in self.client._request.call_args_list
            if request.args[:2] == ("POST", "/git/commits")
        )
        self.assertIn(
            "conversation=0;inline-review=0;review=31",
            commit_request.kwargs["body"]["message"],
        )
        self.assertIn(
            call(
                "PATCH",
                "/git/refs/heads/hatchet%2Fissue-2",
                body={"sha": "new-sha", "force": False},
            ),
            self.client._request.call_args_list,
        )
        self.assertFalse(
            any(
                request.args[:2] == ("POST", "/pulls")
                for request in self.client._request.call_args_list
            )
        )


class RevisionPublicationTests(unittest.TestCase):
    def setUp(self):
        self.client = GitHubClient.__new__(GitHubClient)
        self.client.repository = "owner/repo"
        self.client._request = Mock()

    def test_immutable_tree_lookup_does_not_use_a_branch_endpoint(self):
        revision = "a" * 40
        self.client._request.return_value = {
            "tree": [{"path": "src/app.py", "type": "blob", "size": 10}]
        }
        self.assertEqual(self.client.list_repository_paths(revision), {"src/app.py"})
        self.client._request.assert_called_once_with(
            "GET", f"/git/trees/{revision}", query={"recursive": "1"}
        )

    def test_stale_feedback_branch_is_rejected_before_writes(self):
        from factory.domain.models import StaleRevisionError

        self.client._get_branch_sha = Mock(return_value="b" * 40)
        with self.assertRaises(StaleRevisionError):
            self.client.update_pull_request_branch(
                branch="branch",
                issue_number=2,
                feedback_cursor={},
                changes=[],
                expected_base_sha="a" * 40,
            )
        self.client._request.assert_not_called()

    def test_changed_default_branch_is_rejected_before_publication(self):
        from factory.domain.models import StaleRevisionError

        self.client._get_branch_sha = Mock(return_value=None)
        self.client._request.side_effect = [
            {"default_branch": "main"},
            {"commit": {"sha": "b" * 40}},
        ]
        with self.assertRaises(StaleRevisionError):
            self.client.create_draft_pull_request(
                issue_number=2,
                title="Change",
                body="Implement.",
                branch="branch",
                changes=[],
                expected_base_sha="a" * 40,
            )
        self.assertTrue(
            all(call.args[0] == "GET" for call in self.client._request.call_args_list)
        )

    def test_branch_commit_is_recovered_when_pr_creation_failed(self):
        revision, published = "a" * 40, "b" * 40
        changes = [SimpleNamespace(path="src/app.py", content="value = 1\n")]
        self.client._get_branch_sha = Mock(return_value=None)
        self.client._commit_branch_changes = Mock(return_value=published)
        self.client._request.side_effect = [
            {"default_branch": "main"},
            {"commit": {"sha": revision}},
            RuntimeError("PR request failed"),
        ]
        with self.assertRaises(RuntimeError):
            self.client.create_draft_pull_request(
                issue_number=2,
                title="Change",
                body="Implement.",
                branch="branch",
                changes=changes,
                expected_base_sha=revision,
            )
        message = self.client._commit_branch_changes.call_args.kwargs["message"]
        self.client._get_branch_sha.return_value = published
        self.client._request.side_effect = [
            {"default_branch": "main"},
            {"message": message},
            {"number": 4},
        ]
        result = self.client.create_draft_pull_request(
            issue_number=2,
            title="Change",
            body="Implement.",
            branch="branch",
            changes=changes,
            expected_base_sha=revision,
        )
        self.assertEqual(result, {"number": 4})
        self.client._commit_branch_changes.assert_called_once()

    def test_branch_race_before_ref_update_does_not_overwrite(self):
        from factory.domain.models import StaleRevisionError

        self.client._get_branch_sha = Mock(return_value="new-sha")
        self.client._request.side_effect = [
            {"tree": {"sha": "tree"}},
            {"sha": "blob"},
            {"sha": "tree"},
            {"sha": "commit"},
        ]
        with self.assertRaises(StaleRevisionError):
            self.client._commit_branch_changes(
                branch="branch",
                base_sha="old-sha",
                issue_number=2,
                changes=[SimpleNamespace(path="src/app.py", content="value = 1\n")],
            )
        self.assertFalse(
            any(call.args[0] == "PATCH" for call in self.client._request.call_args_list)
        )


if __name__ == "__main__":
    unittest.main()
