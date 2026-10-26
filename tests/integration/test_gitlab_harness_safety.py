"""Live GitLab qualification must fail closed before any external mutation."""

import unittest
from unittest.mock import Mock


class GitLabHarnessSafetyTests(unittest.TestCase):
    def values(self):
        return {
            "FLUMIC_LIVE_GITLAB": "1",
            "FLUMIC_GITLAB_ALLOW_WRITES": "1",
            "FLUMIC_GITLAB_ALLOW_DEFAULT_BRANCH_CHANGES": "1",
            "FLUMIC_GITLAB_PROJECT_PATH": "group/sub/repo",
            "FLUMIC_GITLAB_PROJECT_ID": "123",
            "GITLAB_TOKEN": "fixture-private-token",
            "FLUMIC_GITLAB_DEFAULT_BRANCH": "main",
            "FLUMIC_GITLAB_DEFAULT_SHA": "a" * 40,
        }

    def test_absent_opt_in_skips_but_partial_opt_in_fails_without_secret_echo(self):
        from integration.gitlab_live_fixture import LiveGitLabConfig

        with self.assertRaises(unittest.SkipTest):
            LiveGitLabConfig.from_env({})
        for key in self.values():
            if key == "FLUMIC_LIVE_GITLAB":
                continue
            values = self.values()
            values.pop(key)
            with self.assertRaises(ValueError) as error:
                LiveGitLabConfig.from_env(values)
            self.assertNotIn("fixture-private-token", str(error.exception))

    def test_fixed_host_and_strict_numeric_identity_before_client_construction(self):
        from integration.gitlab_live_fixture import LiveGitLabConfig

        for key, value in (
            ("FLUMIC_GITLAB_PROJECT_ID", "12.3"),
            ("FLUMIC_GITLAB_PROJECT_ID", "0"),
            ("FLUMIC_GITLAB_PROJECT_PATH", "https://elsewhere.test/group/repo"),
            ("FLUMIC_GITLAB_DEFAULT_SHA", "HEAD"),
        ):
            with self.assertRaises(ValueError):
                LiveGitLabConfig.from_env({**self.values(), key: value})
        config = LiveGitLabConfig.from_env(self.values())
        self.assertNotIn("fixture-private-token", repr(config))

    def test_wrong_remote_identity_or_moved_default_ref_blocks_every_write(self):
        from integration.gitlab_live_fixture import LiveGitLabConfig, GitLabLiveFixture

        config = LiveGitLabConfig.from_env(self.values())
        for project, head in (
            (
                {
                    "id": 999,
                    "path_with_namespace": "group/sub/repo",
                    "default_branch": "main",
                },
                "a" * 40,
            ),
            (
                {
                    "id": 123,
                    "path_with_namespace": "other/repo",
                    "default_branch": "main",
                },
                "a" * 40,
            ),
            (
                {
                    "id": 123,
                    "path_with_namespace": "group/sub/repo",
                    "default_branch": "main",
                },
                "b" * 40,
            ),
        ):
            client = Mock()
            client._request.return_value = project
            client.resolve_revision.return_value = head
            with self.assertRaises(ValueError):
                GitLabLiveFixture(config, client=client)
            self.assertTrue(
                all(call.args[0] == "GET" for call in client._request.call_args_list)
            )

    def test_cleanup_attempts_all_owned_operations_and_refuses_foreign_marker(self):
        from integration.gitlab_live_fixture import LiveGitLabConfig, GitLabLiveFixture

        config = LiveGitLabConfig.from_env(self.values())
        client = Mock()
        client._request.return_value = {
            "id": 123,
            "path_with_namespace": "group/sub/repo",
            "default_branch": "main",
        }
        client.resolve_revision.return_value = "a" * 40
        fixture = GitLabLiveFixture(config, client=client)
        client._request.reset_mock()
        fixture.issues = [7, 8]
        client.get_issue.side_effect = lambda iid: {
            "body": fixture.marker if iid == 8 else "human-owned issue"
        }
        failures = fixture.cleanup()
        self.assertTrue(failures)
        writes = [
            call for call in client._request.call_args_list if call.args[0] != "GET"
        ]
        self.assertEqual([call.args[:2] for call in writes], [("DELETE", "/issues/8")])

    def test_check_wait_accepts_void_success_and_retries_pending_evidence(self):
        from integration import test_gitlab_contract as contract
        from factory.domain.models import RepositoryError
        from unittest.mock import patch

        harness = contract.LiveGitLabContract()
        harness.client = Mock()
        harness.checked = []
        harness.client.verify_commit_checks.side_effect = [
            RepositoryError("pending"),
            None,
        ]
        with (
            patch.object(contract.time, "sleep"),
            patch.object(contract.time, "monotonic", side_effect=[0, 1, 601]),
        ):
            harness.wait_checks("a" * 40, "main")
        self.assertEqual(harness.client.verify_commit_checks.call_count, 2)
        self.assertEqual(harness.checked, [{"sha": "a" * 40, "ref": "main"}])

    def test_cleanup_preserves_advanced_branch_and_accepts_already_removed_branch(self):
        from integration.gitlab_live_fixture import LiveGitLabConfig, GitLabLiveFixture

        client = Mock()
        client._request.return_value = {
            "id": 123,
            "path_with_namespace": "group/sub/repo",
            "default_branch": "main",
        }
        client.resolve_revision.return_value = "a" * 40
        fixture = GitLabLiveFixture(
            LiveGitLabConfig.from_env(self.values()), client=client
        )
        fixture.branches = {fixture.prefix: "a" * 40}
        client._branch_head.return_value = "b" * 40
        self.assertTrue(fixture.cleanup())
        client._branch_head.return_value = None
        self.assertEqual(fixture.cleanup(), [])
        self.assertTrue(
            all(call.args[0] == "GET" for call in client._request.call_args_list)
        )

    def test_cleanup_can_never_delete_default_or_unowned_branches(self):
        from integration.gitlab_live_fixture import LiveGitLabConfig, GitLabLiveFixture

        client = Mock()
        client._request.return_value = {
            "id": 123,
            "path_with_namespace": "group/sub/repo",
            "default_branch": "main",
        }
        client.resolve_revision.return_value = "a" * 40
        client._branch_head.return_value = "a" * 40
        fixture = GitLabLiveFixture(
            LiveGitLabConfig.from_env(self.values()), client=client
        )
        fixture.branches = {"main": "a" * 40, "human-work": "a" * 40}
        self.assertEqual(len(fixture.cleanup()), 2)
        self.assertTrue(
            all(call.args[0] == "GET" for call in client._request.call_args_list)
        )

    def test_complete_live_harness_dry_run_never_reaches_network_and_restores_files(
        self,
    ):
        import base64
        import re
        from urllib.parse import unquote
        from unittest.mock import patch
        from integration import test_gitlab_contract as contract
        from integration.gitlab_live_fixture import LiveGitLabConfig, GitLabLiveFixture
        from gitlab_protocol_fixture import GitLabProtocolFixture
        from factory.adapters.gitlab import GitLabRequestError

        wire = GitLabProtocolFixture()
        original = wire.request
        counter = 1000
        pipeline = {}

        def request(method, path, *, body=None, query=None, raw=False):
            nonlocal counter, pipeline
            if path == "":
                return {**original(method, path), "shared_runners_enabled": True}
            if path == "/runners":
                return []
            if path.startswith("/repository/files/") and method == "GET":
                name = unquote(path.removeprefix("/repository/files/"))
                if name not in wire.files:
                    raise GitLabRequestError(404)
                return {
                    "encoding": "base64",
                    "content": base64.b64encode(wire.files[name].encode()).decode(),
                    "last_commit_id": wire.branches["main"],
                }
            if path == "/repository/commits" and method == "POST":
                counter += 1
                sha = f"{counter:040x}"
                for action in body["actions"]:
                    if action["action"] == "delete":
                        del wire.files[action["file_path"]]
                    else:
                        wire.files[action["file_path"]] = action["content"]
                wire.branches[body["branch"]] = sha
                wire.messages[sha] = body["commit_message"]
                for mr in wire.mrs.values():
                    if mr["source_branch"] == body["branch"]:
                        mr["sha"] = sha
                return {"id": sha}
            if path == "/pipelines":
                pipeline = {
                    "id": 10,
                    "project_id": 123,
                    "sha": query["sha"],
                    "ref": query["ref"],
                    "source": query["source"],
                    "status": "success",
                }
                return [pipeline]
            if path == "/pipelines/10":
                return pipeline
            merge = re.fullmatch(r"/merge_requests/(\d+)/merge", path)
            if merge:
                counter += 1
                sha = f"{counter:040x}"
                value = wire.mrs[int(merge[1])]
                value.update(state="merged", merge_commit_sha=sha)
                wire.branches["main"] = sha
                wire.messages[sha] = "fixture merge"
                return value.copy()
            if method == "DELETE":
                issue = re.fullmatch(r"/issues/(\d+)", path)
                mr = re.fullmatch(r"/merge_requests/(\d+)", path)
                if issue:
                    del wire.issues[int(issue[1])]
                elif mr:
                    del wire.mrs[int(mr[1])]
                elif path.startswith("/repository/branches/"):
                    del wire.branches[
                        unquote(path.removeprefix("/repository/branches/"))
                    ]
                elif path.startswith("/labels/"):
                    wire.labels.discard(unquote(path.removeprefix("/labels/")))
                else:
                    raise AssertionError("unexpected fixture cleanup endpoint")
                return None
            if (
                method == "PUT"
                and path.startswith("/merge_requests/")
                and "title" in (body or {})
            ):
                value = wire.mrs[int(path.rsplit("/", 1)[-1])]
                value.update(title=body["title"], draft=False)
                return value.copy()
            return original(method, path, body=body, query=query, raw=raw)

        wire.client._request = request
        values = {
            **self.values(),
            "FLUMIC_GITLAB_PROJECT_PATH": "owner/repo",
            "FLUMIC_LIVE_EVIDENCE_DIR": "",
        }
        config = LiveGitLabConfig.from_env(values)
        fixture = GitLabLiveFixture(config, client=wire.client)
        test = contract.LiveGitLabContract(
            "test_issue_plan_publication_feedback_ref_lease_merge_and_ci"
        )
        result = unittest.TestResult()
        with (
            patch.dict(contract.os.environ, values),
            patch.object(contract, "GitLabLiveFixture", return_value=fixture),
            patch(
                "factory.adapters.gitlab.build_opener",
                side_effect=AssertionError("dry run reached network"),
            ),
        ):
            test.run(result)
        self.assertEqual(result.errors, [])
        self.assertEqual(result.failures, [])
        self.assertEqual(test.fake_coding_calls, 2)
        self.assertEqual(test.physical_publications, 2)
        self.assertEqual(wire.files, {"src/app.py": "value = 0\n"})
        self.assertEqual(set(wire.branches), {"main"})
        self.assertEqual(set(wire.issues), {2, 4})
        self.assertEqual(wire.mrs, {})
        self.assertEqual(wire.labels, set())

    def test_public_inventory_never_exports_source_configuration_or_credentials(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from unittest.mock import patch
        from integration import test_gitlab_contract as contract
        from integration.gitlab_live_fixture import LiveGitLabConfig, GitLabLiveFixture

        config = LiveGitLabConfig.from_env(self.values())
        client = Mock()
        client._request.return_value = {
            "id": 123,
            "path_with_namespace": "group/sub/repo",
            "default_branch": "main",
        }
        client.resolve_revision.return_value = "a" * 40
        harness = contract.LiveGitLabContract()
        harness.config = config
        harness.fixture = GitLabLiveFixture(config, client=client)
        harness.writes = harness.fake_coding_calls = harness.physical_publications = 0
        harness.checked = []
        harness.cleanup_failures = []
        harness.original_ci = "PRIVATE_SOURCE_CONFIGURATION fixture-private-token"
        harness.dependencies = {"python": "fixture-version"}
        with (
            TemporaryDirectory() as directory,
            patch.dict(contract.os.environ, {"FLUMIC_LIVE_EVIDENCE_DIR": directory}),
        ):
            harness.record_evidence("prepared")
            files = list(Path(directory).glob("*.json"))
            self.assertEqual(len(files), 1)
            raw = files[0].read_text()
            self.assertNotIn("fixture-private-token", raw)
            self.assertNotIn("PRIVATE_SOURCE_CONFIGURATION", raw)
            value = json.loads(raw)
            self.assertEqual(value["stage"], "prepared")
            self.assertEqual(value["dependencies"], {"python": "fixture-version"})
            self.assertNotIn("config", value)
            self.assertNotIn("token", value)
