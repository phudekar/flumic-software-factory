"""Real owned API/shared-worker/state path; GitLab wire and models are fixtures."""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from project_release_fixture import ProjectReleaseFixture
from factory.domain.repositories import RepositoryIdentity
from factory.workflows.contracts import RunInvocation
from factory.adapters.pricing import Pricing
from factory.shared_worker import SharedRequestError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "tests"))
from gitlab_protocol_fixture import GitLabProtocolFixture


class GitLabProjectReleaseTests(unittest.TestCase):
    def test_mixed_source_shared_pool_pins_grants_and_recovers_delivery_and_completion(
        self,
    ):
        f = ProjectReleaseFixture()
        self.addCleanup(f.close)
        project = f.projects["release-alpha"]
        base = project["base"]
        remote = GitLabProtocolFixture()
        remote.issues[2]["labels"] = ["ready-for-agent"]
        body = {
            "alias": "gitlab-source",
            "provider": "gitlab",
            "host": "gitlab.com",
            "owner": "owner",
            "name": "repo",
        }
        repo = f.call(base + "/repositories", body, 201)
        old_secret = f.call(
            base + "/secrets",
            {
                "name": "gitlab-source",
                "purpose": "gitlab",
                "repository_id": repo["id"],
                "value": "fixture-gitlab-v1",
            },
            201,
        )
        body["credential_id"] = old_secret["id"]
        repo = f.call(
            base + "/repositories/" + repo["id"],
            {**body, "parent_id": repo["revision_id"]},
            method="PUT",
        )
        identity = RepositoryIdentity(
            provider="gitlab",
            host="gitlab.com",
            owner="owner",
            name="repo",
            project_id=123,
        )
        with patch(
            "app.repository_validation.resolve_gitlab_project", return_value=identity
        ):
            repo = f.call(
                base + "/repositories/" + repo["id"] + "/validate",
                {"parent_id": repo["revision_id"]},
            )
        f.call(base + "/repositories/" + repo["id"] + "/dynamic-mode", {})
        graph = {
            "schema_version": 1,
            "name": "gitlab-release",
            "repositories": ["gitlab-source"],
            "default_repository": "gitlab-source",
            "stages": [
                {
                    "id": "load",
                    "executor": "builtin",
                    "action": "issue.load@1",
                    "role": "simple",
                    "pool_id": f.pools["simple"]["id"],
                    "inputs": {
                        "command": {"issue_number": {"$ref": "input.issue_number"}}
                    },
                }
            ],
        }
        definition = f.call(base + "/workflow-definitions", {"document": graph}, 201)
        binding = f.call(
            base + "/repository-triggers",
            {
                "repository_id": repo["id"],
                "definition_id": definition["id"],
                "event_kind": "issue-ready",
                "configuration": {
                    "followups": {
                        repo["id"]: {
                            kind: definition["id"]
                            for kind in ("issue", "feedback", "verify")
                        }
                    }
                },
            },
            201,
        )
        payload = {
            "object_kind": "issue",
            "project": {"id": 123, "path_with_namespace": "owner/repo"},
            "object_attributes": {"iid": 2, "action": "open"},
        }
        headers = {
            "X-Gitlab-Token": binding["webhook_token"],
            "webhook-id": "combined-release-delivery",
        }
        triggers = f.case.app.state.repository_triggers
        labels = {
            "ready_for_agent": "ready-for-agent",
            "pr_open": "agent-pr-open",
            "plan_created": "agent-plan-created",
        }
        with patch.object(triggers, "source", return_value=(remote.client, labels)):
            accepted = f.call(
                "/api/v1/hooks/gitlab/" + binding["id"], payload, 202, headers=headers
            )
            replay = f.call(
                "/api/v1/hooks/gitlab/" + binding["id"], payload, 202, headers=headers
            )
        self.assertEqual(accepted, replay)
        self.assertEqual(accepted["status"], "accepted")
        gl_run = RunInvocation(project_id=project["id"], run_id=accepted["run_id"])
        # Rotate the saved connection before a shared worker requests context.
        # The accepted launch must still resolve its original scoped version.
        new_secret = f.call(
            base + "/secrets",
            {
                "name": "gitlab-source",
                "purpose": "gitlab",
                "repository_id": repo["id"],
                "value": "fixture-gitlab-v2",
                "parent_id": old_secret["id"],
            },
            201,
        )
        f.call(
            base + "/repositories/" + repo["id"],
            {
                **body,
                "credential_id": new_secret["id"],
                "parent_id": repo["revision_id"],
            },
            method="PUT",
        )
        gh_run = f.launch("release-beta")
        adapter_calls = []

        def adapter(actual, *, token, **kwargs):
            self.assertEqual(actual, identity)
            self.assertEqual(token, "fixture-gitlab-v1")
            adapter_calls.append(actual.canonical_key)
            return remote.client

        gl_invocation = None
        with (
            patch("factory.bootstrap.repository_adapter", side_effect=adapter),
            patch("factory.adapters.control.Pricing", return_value=Pricing()),
        ):
            for _ in range(25):
                f.offset += 1
                for run in (gl_run, gh_run):
                    frontier = f.runtime.advance(run)
                    for invocation in frontier.ready:
                        role = frontier.routes[invocation.execution_id].role
                        if run == gl_run:
                            gl_invocation = invocation
                            original = f.controls[role].request

                            def lose_completion(path, body=None):
                                result = original(path, body)
                                if path.endswith("/complete"):
                                    raise SharedRequestError()
                                return result

                            f.controls[role].request = lose_completion
                            try:
                                with self.assertRaises(SharedRequestError):
                                    f.controls[role].execute(invocation)
                            finally:
                                f.controls[role].request = original
                        else:
                            f.execute(invocation, role)
                f.runtime.broker.reconcile_once(f.jobs)
                f.runtime.reconcile_background()
                f.drain_history()
                if all(
                    f.detail(run)["state"] == "completed" for run in (gl_run, gh_run)
                ):
                    break
            self.assertEqual(f.detail(gl_run)["state"], "completed")
            self.assertEqual(f.detail(gh_run)["state"], "completed")
            self.assertIsNotNone(gl_invocation)
            # Lost completion response replay must not execute the service again.
            f.restart_controller_and_broker()
            self.assertEqual(f.runtime.advance(gl_run).ready, [])
        self.assertEqual(adapter_calls, ["gitlab.com/project/123"])
        detail = f.detail(gl_run)
        self.assertEqual(detail["revision_id"], definition["revision_id"])
        self.assertEqual(detail["stages"][0]["repository_id"], repo["id"])
        self.assertNotIn("fixture-gitlab-v1", str(detail))
        self.assertNotIn("fixture-gitlab-v2", str(detail))
        self.assertEqual(
            f.call(
                f.projects["release-beta"]["base"] + "/workflow-runs/" + gl_run.run_id,
                status=404,
            ),
            {"detail": "Resource not found"},
        )
