"""Provider composition uses explicit installed adapters and scoped context."""

import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.settings import Settings
from factory.workflows.contracts import ActionContext


class GitLabCompositionTests(unittest.TestCase):
    def test_legacy_settings_gitlab_explicit_and_github_default(self):
        settings = Settings.from_env(
            {
                "REPOSITORY_PROVIDER": "gitlab",
                "GITLAB_REPOSITORY": "g/sub/r",
                "GITLAB_PROJECT_ID": "123",
            }
        )
        self.assertEqual(
            (
                settings.repository_provider,
                settings.repository,
                settings.gitlab_project_id,
            ),
            ("gitlab", "g/sub/r", 123),
        )
        self.assertEqual(Settings().repository_provider, "github")
        with self.assertRaises(ValueError):
            Settings(repository="g/sub/r")
        with self.assertRaises(ValueError):
            Settings(repository_provider="gitlab", repository="g/r")

    def context(self):
        return ActionContext(
            project_id="p",
            repository_id="r",
            repository_revision_id="rv",
            config_revision_id="cfg",
            run_id="run",
            stage_id="s",
            execution_id="e",
            attempt=1,
            assignment_id="a",
            namespace="n",
            settings_json=json.dumps({"repository": "g/sub/r"}),
            repository_identity_json=json.dumps(
                {
                    "provider": "gitlab",
                    "host": "gitlab.com",
                    "owner": "g/sub",
                    "name": "r",
                    "project_id": 123,
                }
            ),
        )

    def test_pinned_identity_controls_settings_and_minimum_grants(self):
        from factory.workflows.actions import minimum_purposes

        context = self.context()
        self.assertEqual(context.settings.repository_provider, "gitlab")
        self.assertEqual(
            minimum_purposes("issue.load@1", context.settings), ("gitlab",)
        )
        self.assertEqual(
            minimum_purposes("issue.plan@1", context.settings), ("gitlab", "openai")
        )

    def test_scoped_repository_factory_never_uses_global_token(self):
        from factory.bootstrap import action_service

        context = self.context().with_credentials({"gitlab": "scoped-token"})
        with patch.dict(os.environ, {"GITLAB_TOKEN": "global-token"}):
            with action_service(
                context, dispatch=Mock(), state=Mock(), observer=Mock()
            ) as service:
                client = service.repositories("g/sub/r")
                self.assertEqual(
                    (client.project_id, client._token), (123, "scoped-token")
                )
                with self.assertRaises(ValueError):
                    service.repositories("other/repo")
        with self.assertRaises((ValueError, RuntimeError)):
            with action_service(
                self.context(), dispatch=Mock(), state=Mock(), observer=Mock()
            ) as service:
                service.repositories("g/sub/r")

    def test_gitlab_pull_url_scope_rejects_other_provider(self):
        from factory.workflows.actions import _pull_scope

        identity = self.context().repository_identity
        value = Mock(
            pull_number=7, pull_url="https://gitlab.com/g/sub/r/-/merge_requests/7"
        )
        _pull_scope(value, "g/sub/r", identity=identity)
        value.pull_url = "https://github.com/g/sub/r/pull/7"
        with self.assertRaises(ValueError):
            _pull_scope(value, "g/sub/r", identity=identity)

    def test_github_pull_namespace_wrappers_preserve_existing_requests(self):
        from github_client import GitHubClient

        client = GitHubClient("g/r", token="scoped")
        client.get_issue_comments = Mock(return_value=["review"])
        client.comment_once = Mock()
        client.add_label = Mock()
        self.assertEqual(client.get_pull_comments(7), ["review"])
        client.comment_pull_once(7, marker="marker", comment="comment")
        client.add_pull_label(7, "reviewed")
        client.comment_once.assert_called_once_with(
            7, marker="marker", comment="comment"
        )
        client.add_label.assert_called_once_with(7, "reviewed")

    def test_shared_review_uses_change_namespace_not_issue_iid(self):
        import test_factory as fixtures

        fixture = fixtures.FactoryTests()
        fixture.setUp()
        fixture.repository.get_pull_comments = Mock(return_value=[])
        fixture.repository.comment_pull_once = Mock()
        fixture.repository.add_pull_label = Mock()
        fixture.repository.get_pull_request.return_value = {
            "title": "Change",
            "body": "Body",
            "labels": [],
            "head": {"ref": "work", "sha": "a" * 40},
        }
        fixture.repository.get_pull_request_files.return_value = [
            {"path": "src/app.py", "status": "modified", "patch": "diff"}
        ]
        published = fixture.service.generate_and_open_draft_pr(
            fixture.command, fixture.triage
        )
        fixture.service.review_opened_pull_request(fixture.command, published)
        fixture.repository.get_pull_comments.assert_called_once_with(4)
        fixture.repository.comment_pull_once.assert_called_once()
        fixture.repository.add_pull_label.assert_called_once_with(
            4, fixture.service.labels.pr_reviewed
        )
        self.assertFalse(
            any(
                call.args and call.args[0] == 4
                for call in fixture.repository.comment_once.call_args_list
            )
        )

    def test_merge_verification_passes_actual_target_branch_to_checks(self):
        import test_factory as fixtures
        from factory.domain.models import PullInput

        fixture = fixtures.FactoryTests()
        fixture.setUp()
        fixture.repository.get_pull_request.return_value = {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "a" * 40,
            "base": {"ref": "release"},
        }
        fixture.service.verify_merged_pr(
            PullInput(
                repository="owner/repo",
                issue_number=2,
                pull_number=4,
                pull_url="https://gitlab.com/owner/repo/-/merge_requests/4",
                branch="factory",
            )
        )
        fixture.repository.verify_commit_checks.assert_called_once_with(
            "a" * 40, ref="release"
        )

    def test_fork_feedback_is_rejected_for_legacy_scan_and_direct_service(self):
        import test_factory as fixtures
        from gitlab_protocol_fixture import GitLabProtocolFixture
        from factory.domain.models import PullInput, RepositoryError

        fixture = fixtures.FactoryTests()
        fixture.setUp()
        remote = GitLabProtocolFixture()
        fixture.service.repositories = lambda _: remote.client
        remote.issues[2]["labels"] = [fixture.service.labels.pr_open]
        remote.mrs[4] = {
            "iid": 4,
            "state": "opened",
            "title": "Fork change",
            "description": "Closes #2",
            "source_project_id": 999,
            "target_project_id": 123,
            "source_branch": "main",
            "target_branch": "main",
            "sha": remote.base,
            "web_url": "https://gitlab.com/owner/repo/-/merge_requests/4",
        }
        remote.notes[("merge_requests", 4)] = [
            {
                "id": 100,
                "body": "Please fix this",
                "system": False,
                "author": {"username": "human"},
            }
        ]
        scan = fixture.service.scan_open_pr_feedback()
        self.assertEqual(scan.pull_requests_queued, 0)
        with self.assertRaises(RepositoryError):
            fixture.service.process_pull_request_feedback(
                PullInput(
                    repository="owner/repo",
                    issue_number=2,
                    pull_number=4,
                    pull_url=remote.mrs[4]["web_url"],
                    branch="main",
                )
            )
        self.assertEqual(remote.publications, [])
        self.assertEqual(remote.writes, [])
        fixture.agent.generate.assert_not_called()

    def test_complete_shared_action_flow_on_gitlab_wire_fixture(self):
        import test_factory as fixtures
        from gitlab_protocol_fixture import GitLabProtocolFixture
        from factory.domain.models import IssueWorkPlan, PlannedSubtask
        from factory.workflows.actions import ActionCatalog
        from factory.workflows.bindings import ResolvedInputs
        from contextlib import contextmanager

        fixture = fixtures.FactoryTests()
        fixture.setUp()
        remote = GitLabProtocolFixture()
        fixture.service.repositories = lambda _: remote.client
        fixture.planner.plan_issue.return_value = IssueWorkPlan(
            tasks=[
                PlannedSubtask(
                    title="Task",
                    objective="Implement input validation",
                    acceptance_criteria=["Reject invalid input"],
                )
            ]
        )

        @contextmanager
        def service_factory(context, **kwargs):
            yield fixture.service

        catalog = ActionCatalog(service_factory=service_factory)
        context = ActionContext(
            project_id="p",
            repository_id="r",
            repository_revision_id="rv",
            config_revision_id="cfg",
            run_id="run",
            stage_id="s",
            execution_id="e",
            attempt=1,
            assignment_id="a",
            namespace="n",
            settings_json=json.dumps({"repository": "owner/repo"}),
            repository_identity_json=json.dumps(
                {
                    "provider": "gitlab",
                    "host": "gitlab.com",
                    "owner": "owner",
                    "name": "repo",
                    "project_id": 123,
                }
            ),
        ).with_credentials({"gitlab": "fixture-gitlab", "openai": "fixture-model"})

        def execute(action, values, role="simple"):
            local = context.model_copy(
                update={
                    "settings_json": json.dumps(
                        {"repository": "owner/repo", "worker_type": role}
                    )
                }
            )
            return catalog.execute(
                action,
                local,
                ResolvedInputs(values, ()),
                dispatch=fixture.dispatch,
                state=fixture.state,
            ).output

        parent = {"issue_number": 2}
        state = execute("issue.load@1", {"command": parent})
        plan = execute("issue.plan@1", {"command": parent, "state": state})
        self.assertEqual(plan["sub_issue_numbers"], [20])
        self.assertEqual(remote.links[2], {20})
        command = {"issue_number": 20}
        state = execute("issue.load@1", {"command": command})
        plan = execute("issue.plan@1", {"command": command, "state": state})
        triage = execute(
            "issue.triage@1", {"command": command, "state": state, "plan": plan}
        )
        from factory.domain.models import RepositoryError

        remote.lose_mr_response = True
        with self.assertRaises(RepositoryError):
            execute(
                "change.implement@1",
                {"command": command, "triage": triage},
                "developer",
            )
        published = execute(
            "change.implement@1", {"command": command, "triage": triage}, "developer"
        )
        self.assertEqual(published["status"], "draft-pr-exists")
        self.assertEqual(len(remote.publications), 1)
        self.assertEqual(fixture.agent.generate.call_count, 1)
        self.assertEqual(published["pull_number"], 4)
        reviewed = execute(
            "change.review@1", {"command": command, "publish": published}
        )
        self.assertIn("review", reviewed["status"])
        self.assertIn(fixture.service.labels.pr_reviewed, remote.mrs[4]["labels"])
        self.assertNotIn(("issues", 4), remote.notes)
        remote.notes[("merge_requests", 4)].append(
            {
                "id": 100,
                "body": "Please handle another edge case",
                "author": {"username": "human"},
                "system": False,
            }
        )
        pull = {
            "issue_number": 20,
            "pull_number": 4,
            "pull_url": published["pull_url"],
            "branch": published["branch"],
        }
        feedback = execute("pull.feedback@1", {"command": pull}, "developer")
        self.assertIn("feedback", feedback["status"])
        self.assertEqual(len(remote.publications), 2)
        repeat = execute("pull.feedback@1", {"command": pull}, "developer")
        self.assertEqual(repeat["status"], "no-new-feedback")
        self.assertEqual(len(remote.publications), 2)
        remote.mrs[4].update(state="merged", merge_commit_sha=remote.merge, draft=False)
        verified = execute("pull.verify@1", {"command": pull})
        self.assertEqual(verified["status"], "verified-merged")
        self.assertEqual(remote.issues[20]["state"], "closed")
        self.assertEqual(remote.issues[4]["description"], "")

    def test_static_source_state_and_claims_do_not_collide_with_github(self):
        from factory.adapters.repository_state import source_scoped_state
        from factory.adapters.state import MemoryStateStore
        from factory.domain.repositories import RepositoryIdentity
        from factory.domain.models import OperationBusy

        store = MemoryStateStore()
        github = source_scoped_state(
            store,
            RepositoryIdentity(
                provider="github", host="github.com", owner="owner", name="repo"
            ),
        )
        gitlab = source_scoped_state(
            store,
            RepositoryIdentity(
                provider="gitlab",
                host="gitlab.com",
                owner="owner",
                name="repo",
                project_id=123,
            ),
        )
        self.assertIs(github, store)
        github.put("same", {"owner": "github"})
        gitlab.put("same", {"owner": "gitlab"})
        self.assertEqual(github.get("same"), {"owner": "github"})
        self.assertEqual(gitlab.get("same"), {"owner": "gitlab"})
        renamed = source_scoped_state(
            store,
            RepositoryIdentity(
                provider="gitlab",
                host="gitlab.com",
                owner="new",
                name="name",
                project_id=123,
            ),
        )
        with (
            github.claim("owner/repo:issue:7", "stage"),
            gitlab.claim("owner/repo:issue:7", "stage"),
        ):
            with self.assertRaises(OperationBusy):
                with renamed.claim("new/name:issue:7", "stage"):
                    pass

    def test_gitlab_wait_uses_only_pinned_source_grant_and_url_scope(self):
        from contextlib import contextmanager
        from factory.workflows.project_waits import check_project_wait
        from factory.domain.models import PullInput

        context = self.context().with_credentials(
            {"gitlab": "source-only", "openai": "never-deliver-model"}
        )
        captured = []
        service = Mock()
        service.pull_wait_status.return_value = "merged"

        @contextmanager
        def factory(scoped, **kwargs):
            captured.append(dict(scoped.credentials))
            yield service

        command = PullInput(
            repository="g/sub/r",
            issue_number=2,
            pull_number=7,
            pull_url="https://gitlab.com/g/sub/r/-/merge_requests/7",
            branch="factory",
        )
        status = check_project_wait(
            context,
            "review",
            command,
            dispatch=Mock(),
            state=Mock(),
            service_factory=factory,
        )
        self.assertEqual(status.state, "merged")
        self.assertEqual(captured, [{"gitlab": "source-only"}])

    def test_shared_registration_reports_only_locally_installed_provider_support(self):
        from factory.shared_worker import SharedSettings, SharedControl

        control = SharedControl(
            SharedSettings(
                pool_id="pool", worker_id="worker", worker_name="worker", role="simple"
            ),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://127.0.0.1:1",
                "FACTORY_CONTROL_TOKEN": "fixture",
            },
        )
        self.addCleanup(control.shutdown)
        control.request = Mock(return_value={})
        with patch("shutil.which", return_value=None):
            control.register()
        self.assertEqual(control.request.call_args.args[1]["providers"], ["github"])
        with patch("shutil.which", return_value="/usr/bin/git"):
            control.register()
        self.assertEqual(
            control.request.call_args.args[1]["providers"], ["github", "gitlab"]
        )
