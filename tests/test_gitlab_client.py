"""GitLab wire semantics must map into the shared repository boundary."""

import base64
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))


class GitLabClientTests(unittest.TestCase):
    def setUp(self):
        from factory.adapters.gitlab import GitLabClient

        self.client = GitLabClient(
            "group/sub/repo", project_id=123, token="fixture-token"
        )
        self.client._request = Mock()

    def test_issue_uses_iid_and_normalized_contract(self):
        self.client._request.return_value = {
            "id": 999,
            "iid": 7,
            "title": "Issue",
            "description": "Body",
            "state": "opened",
            "web_url": "https://gitlab.com/group/sub/repo/-/issues/7",
            "labels": ["ready-for-agent"],
        }
        issue = self.client.get_issue(7)
        self.assertEqual(
            (issue["id"], issue["number"], issue["body"], issue["state"]),
            (999, 7, "Body", "open"),
        )
        self.assertEqual(issue["labels"], [{"name": "ready-for-agent"}])
        self.client._request.assert_called_once_with("GET", "/issues/7")

    def test_mr_iid_and_state_are_not_issue_id(self):
        self.client._request.return_value = {
            "id": 888,
            "iid": 7,
            "title": "Draft: Change",
            "description": "Body",
            "state": "merged",
            "web_url": "https://gitlab.com/group/sub/repo/-/merge_requests/7",
            "draft": True,
            "source_branch": "factory",
            "target_branch": "main",
            "sha": "a" * 40,
            "merge_commit_sha": "b" * 40,
        }
        change = self.client.get_pull_request(7)
        self.assertEqual(
            (change["number"], change["state"], change["merged"], change["draft"]),
            (7, "closed", True, True),
        )
        self.assertEqual(change["head"]["sha"], "a" * 40)
        self.assertEqual(change["merge_commit_sha"], "b" * 40)
        self.client._request.assert_called_once_with("GET", "/merge_requests/7")

    def test_comments_skip_system_and_paginate(self):
        notes = [{"id": n, "body": str(n), "system": False} for n in range(100)]
        self.client._request.side_effect = [
            notes,
            [
                {"id": 101, "body": "human", "system": False},
                {"id": 102, "body": "system", "system": True},
            ],
        ]
        self.assertEqual(len(self.client.get_issue_comments(7)), 101)
        self.assertEqual(self.client._request.call_args.kwargs["query"]["page"], 2)

    def test_files_quote_nested_path_and_enforce_byte_bound(self):
        self.client._request.return_value = {
            "encoding": "base64",
            "content": base64.b64encode(b"print(1)").decode(),
        }
        self.assertEqual(
            self.client.get_source_files(["src/a.py"], branch="main"),
            {"src/a.py": "print(1)"},
        )
        self.client._request.assert_called_once_with(
            "GET", "/repository/files/src%2Fa.py", query={"ref": "main"}
        )
        self.client.max_file_bytes = 2
        with self.assertRaises(Exception):
            self.client.get_source_files(["src/a.py"], branch="main")

    def test_related_plan_tasks_use_links_with_explicit_marker(self):
        self.client._request.return_value = [
            {"id": 501, "iid": 12, "description": "<!-- factory-parent:123:7 -->"},
            {"id": 502, "iid": 13, "description": "unrelated"},
        ]
        self.assertEqual(self.client.list_sub_issue_ids(7), {501})
        self.client._request.assert_called_once_with(
            "GET", "/issues/7/links", query={"page": 1, "per_page": 100}
        )

    def test_mr_diff_rejects_incomplete_server_diff(self):
        self.client._request.return_value = [
            {"old_path": "a.py", "new_path": "a.py", "diff": "", "too_large": True}
        ]
        with self.assertRaises(Exception):
            self.client.get_pull_request_files(7)

    def test_explicit_token_and_validated_project_identity(self):
        from factory.adapters.gitlab import GitLabClient

        for token in ("", None):
            with self.assertRaises(ValueError):
                GitLabClient("g/r", project_id=123, token=token)
        for identity in (True, 0, "123"):
            with self.assertRaises(ValueError):
                GitLabClient("g/r", project_id=identity, token="token")

    def test_transport_fixed_host_header_and_bounded_safe_errors(self):
        from factory.adapters.gitlab import GitLabClient
        from unittest.mock import MagicMock, patch
        import io
        import urllib.error

        client = GitLabClient("g/r", project_id=123, token="fixture-secret")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"iid":7}'
        opener = Mock()
        opener.open.return_value = response
        with patch("factory.adapters.gitlab.build_opener", return_value=opener):
            self.assertEqual(client._request("GET", ""), {"iid": 7})
            self.assertEqual(
                opener.open.call_args.args[0].full_url,
                "https://gitlab.com/api/v4/projects/123",
            )
            self.assertEqual(client._request("GET", "/issues/7"), {"iid": 7})
            request = opener.open.call_args.args[0]
            self.assertEqual(
                request.full_url, "https://gitlab.com/api/v4/projects/123/issues/7"
            )
            self.assertEqual(request.get_header("Private-token"), "fixture-secret")
            private_response = io.BytesIO(b"fixture-secret")
            opener.open.side_effect = urllib.error.HTTPError(
                request.full_url,
                403,
                "fixture-secret",
                {},
                private_response,
            )
            with self.assertRaises(Exception) as raised:
                client._request("GET", "/issues/7")
            self.assertNotIn("fixture-secret", str(raised.exception))
            self.assertTrue(private_response.closed)

    def test_transport_never_follows_redirects(self):
        from factory.adapters.gitlab import NoGitLabRedirects

        self.assertIsNone(
            NoGitLabRedirects().redirect_request(
                None, None, 302, "redirect", {}, "https://evil.com/token"
            )
        )

    def test_create_plan_issue_adds_provider_parent_marker(self):
        self.client._request.return_value = {
            "id": 501,
            "iid": 12,
            "title": "Task",
            "state": "opened",
            "description": "child",
            "labels": [],
        }
        result = self.client.create_issue(
            title="Task",
            body="child\n<!-- hatchet-parent-issue-7-step-1 -->",
            labels=["agent-task"],
        )
        self.assertEqual(result["number"], 12)
        body = self.client._request.call_args.kwargs["body"]
        self.assertIn("<!-- factory-parent:123:7 -->", body["description"])
        self.assertEqual(body["labels"], "agent-task")

    def test_add_related_plan_task_uses_target_iid_not_global_id(self):
        self.client._request.side_effect = [
            [
                {
                    "id": 501,
                    "iid": 12,
                    "project_id": 123,
                    "description": "<!-- factory-parent:123:7 -->",
                }
            ],
            [],
            {"id": 99},
        ]
        self.client.add_sub_issue(7, 501)
        self.client._request.assert_called_with(
            "POST",
            "/issues/7/links",
            body={
                "target_project_id": 123,
                "target_issue_iid": 12,
                "link_type": "relates_to",
            },
        )

    def test_issue_comment_and_labels_use_incremental_mutation(self):
        self.client._request.side_effect = [[], {"id": 20}, None, None, None]
        self.client.comment_once(7, marker="<!-- factory -->", comment="hello")
        self.client.add_label(7, "ready")
        self.client.remove_label(7, "ready")
        self.client.close_issue(7)
        calls = self.client._request.call_args_list
        self.assertEqual(calls[1].kwargs["body"], {"body": "hello\n\n<!-- factory -->"})
        self.assertEqual(calls[2].kwargs["body"], {"add_labels": "ready"})
        self.assertEqual(calls[3].kwargs["body"], {"remove_labels": "ready"})
        self.assertEqual(calls[4].kwargs["body"], {"state_event": "close"})

    def test_marker_search_uses_all_states(self):
        self.client._request.return_value = [
            {"id": 999, "iid": 7, "description": "<!-- task:1 -->", "state": "closed"}
        ]
        self.assertEqual(
            self.client.find_issues_by_markers({"<!-- task:1 -->"}),
            {"<!-- task:1 -->": {"number": 7, "id": 999}},
        )
        self.assertEqual(self.client._request.call_args.kwargs["query"]["state"], "all")

    def test_existing_mr_match_is_exact_issue_marker(self):
        self.client._request.return_value = [
            {"iid": 1, "description": "Closes #70", "state": "opened"},
            {"iid": 2, "description": "Closes #7", "state": "opened"},
        ]
        self.assertEqual(self.client.find_issue_pull_request(7)["number"], 2)

    def test_archive_is_bounded_and_pinned(self):
        self.client._request.return_value = b"fixture-archive"
        self.assertEqual(
            self.client.get_repository_archive("a" * 40), b"fixture-archive"
        )
        self.client._request.assert_called_once_with(
            "GET", "/repository/archive.tar.gz", query={"sha": "a" * 40}, raw=True
        )

    def test_locked_mr_is_not_terminal_and_labels_are_normalized(self):
        self.client._request.return_value = {"iid": 7, "state": "locked"}
        with self.assertRaises(Exception):
            self.client.get_pull_request(7)
        self.client._request.return_value = {
            "iid": 7,
            "state": "opened",
            "labels": ["reviewed"],
        }
        self.assertEqual(
            self.client.get_pull_request(7)["labels"], [{"name": "reviewed"}]
        )

    def test_project_root_and_default_revision_are_explicit(self):
        self.client._request.side_effect = [
            {"id": 123, "default_branch": "main"},
            {"id": "a" * 40},
        ]
        self.assertEqual(self.client.resolve_revision(), "a" * 40)
        self.assertEqual(self.client._request.call_args_list[0].args, ("GET", ""))

    def test_unknown_remote_states_do_not_become_closed(self):
        self.client._request.return_value = {"id": 1, "iid": 7, "state": "unknown"}
        with self.assertRaises(Exception):
            self.client.get_issue(7)
        with self.assertRaises(Exception):
            self.client.get_pull_request(7)

    def test_create_draft_mr_uses_guarded_publisher_and_reuses_marker(self):
        self.client.publisher = Mock()
        self.client.publisher.publish.return_value = "c" * 40
        self.client._branch_head = Mock(return_value=None)
        self.client.resolve_revision = Mock(return_value="a" * 40)
        self.client.find_issue_pull_request = Mock(return_value=None)
        self.client._request.side_effect = [
            {"id": 123, "default_branch": "main"},
            {
                "id": 88,
                "iid": 9,
                "state": "opened",
                "description": "Closes #7",
                "draft": True,
                "source_branch": "factory",
                "target_branch": "main",
                "sha": "c" * 40,
            },
        ]
        from factory.domain.models import FileChange

        mr = self.client.create_draft_pull_request(
            issue_number=7,
            title="Change",
            body="Body",
            branch="factory",
            changes=[FileChange(path="a.py", content="print(1)")],
            expected_base_sha="a" * 40,
        )
        self.assertEqual(mr["number"], 9)
        publication = self.client.publisher.publish.call_args.kwargs
        self.assertEqual(
            (publication["expected_head"], publication["base_commit"]), ("", "a" * 40)
        )
        self.assertEqual(publication["credentials"], {"gitlab": "fixture-token"})
        body = self.client._request.call_args.kwargs["body"]
        self.assertEqual(body["title"], "Draft: Change")
        self.assertIn("Closes #7", body["description"])
        self.assertEqual(body["source_branch"], "factory")

    def test_feedback_update_uses_original_expected_head(self):
        self.client.publisher = Mock()
        self.client.publisher.publish.return_value = "c" * 40
        self.client._branch_head = Mock(return_value="b" * 40)
        from factory.domain.models import FileChange

        result = self.client.update_pull_request_branch(
            branch="factory",
            issue_number=7,
            feedback_cursor={"conversation": 2, "inline review": 3, "review": 0},
            changes=[FileChange(path="a.py", content="print(2)")],
            expected_base_sha="a" * 40,
        )
        self.assertEqual(result, "c" * 40)
        self.assertEqual(
            self.client.publisher.publish.call_args.kwargs["expected_head"], "a" * 40
        )
        self.assertIn(
            "hatchet-pr-feedback-cursor:",
            self.client.publisher.publish.call_args.kwargs["marker"],
        )

    def test_feedback_discussions_map_stable_note_ids_and_skip_echoes(self):
        self.client._request.return_value = [
            {
                "id": "thread",
                "notes": [
                    {
                        "id": 9,
                        "body": "Fix this",
                        "type": "DiffNote",
                        "position": {"new_path": "a.py", "new_line": 3},
                        "author": {"username": "human"},
                    },
                    {
                        "id": 10,
                        "body": "General thought",
                        "author": {"username": "human"},
                    },
                    {"id": 11, "body": "system", "system": True},
                    {
                        "id": 12,
                        "body": "factory\n<!-- hatchet-pr-feedback-cursor:conversation=10 -->",
                    },
                ],
            }
        ]
        values = self.client.get_pull_feedback(7)
        self.assertEqual(
            [(v["id"], v["source"]) for v in values],
            [(9, "inline review"), (10, "conversation")],
        )
        self.assertEqual(
            (values[0]["path"], values[0]["line"], values[0]["author"]),
            ("a.py", 3, "human"),
        )

    def test_feedback_cursor_recovers_from_commit_and_note(self):
        self.client._branch_head = Mock(return_value="a" * 40)
        self.client._request.side_effect = [
            [
                {
                    "id": 20,
                    "body": "<!-- hatchet-pr-feedback-cursor:conversation=2;inline-review=0;review=0 -->",
                }
            ],
            {
                "message": "factory <!-- hatchet-pr-feedback-cursor:conversation=1;inline-review=9;review=0 -->"
            },
        ]
        self.assertEqual(
            self.client.pull_feedback_cursor(7, "factory"),
            {"conversation": 2, "inline review": 9, "review": 0},
        )

    def test_pull_comments_labels_and_feedback_cursor_never_use_issue_namespace(self):
        self.client._request.side_effect = [[], {"id": 10}, None, [], {"id": 11}]
        self.client.comment_pull_once(7, marker="<!-- review -->", comment="Reviewed")
        self.client.add_pull_label(7, "agent-reviewed")
        self.client.record_pull_feedback(
            7, {"conversation": 3, "inline review": 0, "review": 0}, "Handled"
        )
        self.assertTrue(
            all(
                "/merge_requests/7" in value.args[1]
                for value in self.client._request.call_args_list
            )
        )
        self.assertIn(
            "conversation=3", self.client._request.call_args.kwargs["body"]["body"]
        )
