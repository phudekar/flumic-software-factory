"""Installed action behavior uses real application operations with fake integrations."""

import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.workflows.contracts import ActionContext
from factory.workflows.bindings import ResolvedInputs, BindingOrigin
import test_factory as fixtures


class BuiltinActionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.FactoryTests()
        self.fixture.setUp()
        self.context = ActionContext(
            project_id="p",
            repository_id="repo",
            repository_revision_id="rv",
            config_revision_id="cfg",
            workflow_revision_id="wf",
            run_id="run",
            stage_id="stage",
            execution_id="e",
            assignment_id="a",
            attempt=1,
            namespace="p/run/e",
            settings_json=json.dumps({"repository": "owner/repo"}),
        ).with_credentials({"github": "test-gh", "openai": "test-model"})
        self.opened = []

        @contextmanager
        def service_factory(context, **kwargs):
            self.opened.append(context.execution_id)
            # Existing service has real decorators/policy/state; only integrations are fake.
            yield self.fixture.service

        self.service_factory = service_factory

    def execute(self, action, inputs, origins=()):
        from factory.workflows.actions import ActionCatalog

        return ActionCatalog(service_factory=self.service_factory).execute(
            action,
            self.context,
            ResolvedInputs(inputs, origins),
            dispatch=self.fixture.dispatch,
            state=self.fixture.state,
        )

    def test_closed_catalog_invokes_decorated_load_operation(self):
        result = self.execute("issue.load@1", {"command": {"issue_number": 2}})
        self.assertEqual(
            result.output,
            {"issue_number": 2, "state": "open", "labels": ["agent-subtask"]},
        )
        self.fixture.repository.get_issue.assert_called_once_with(2)
        with self.assertRaises(ValueError):
            self.execute("__getattribute__", {})
        self.assertEqual(self.opened, ["e"])

    def test_non_ready_implementation_does_not_call_coder(self):
        self.context = self.context.model_copy(
            update={
                "settings_json": json.dumps(
                    {"repository": "owner/repo", "worker_type": "developer"}
                )
            }
        )
        result = self.execute(
            "change.implement@1",
            {
                "command": {"issue_number": 2},
                "triage": {"status": "waiting-for-discussion", "issue_number": 2},
            },
        )
        self.assertEqual(result.output["status"], "waiting-for-discussion")
        self.fixture.agent.generate.assert_not_called()
        self.assertIn(
            "progress:owner/repo:issue:2:generate_and_open_draft_pr",
            self.fixture.state.values,
        )

    def test_wrong_whole_output_origin_rejected_before_service(self):
        origins = (BindingOrigin(("publish",), "parent", "p", "another-repo"),)
        with self.assertRaises(ValueError):
            self.execute(
                "change.review@1",
                {
                    "command": {"issue_number": 2},
                    "publish": {
                        "status": "draft-pr-created",
                        "issue_number": 2,
                        "pull_number": 4,
                        "pull_url": "https://github.com/owner/repo/pull/4",
                        "branch": "work",
                    },
                },
                origins,
            )
        self.assertEqual(self.opened, [])
        self.fixture.repository.get_pull_request.assert_not_called()

    def test_mismatched_upstream_issue_rejected_before_service(self):
        with self.assertRaises(ValueError):
            self.execute(
                "change.implement@1",
                {
                    "command": {"issue_number": 2},
                    "triage": {"status": "ready", "issue_number": 8},
                },
            )
        self.assertEqual(self.opened, [])

    def test_affine_command_cannot_override_repository(self):
        with self.assertRaises(ValueError):
            self.execute(
                "issue.load@1",
                {"command": {"issue_number": 2, "repository": "elsewhere/repo"}},
            )
        self.assertEqual(self.opened, [])

    def test_plan_dispatches_subissues_with_original_correlation_and_retry_key(self):
        from factory.domain.models import IssueWorkPlan, PlannedSubtask

        self.fixture.repository.get_issue.return_value["labels"] = []
        self.fixture.repository.list_sub_issue_ids.return_value = set()
        self.fixture.repository.find_issues_by_markers.return_value = {}
        self.fixture.planner.plan_issue.return_value = IssueWorkPlan(
            tasks=[
                PlannedSubtask(
                    title="First", objective="Task", acceptance_criteria=["Works"]
                ),
                PlannedSubtask(
                    title="Second", objective="Task", acceptance_criteria=["Works"]
                ),
            ]
        )
        self.fixture.repository.create_issue.side_effect = [
            {"number": 7, "id": 70},
            {"number": 8, "id": 80},
        ]
        result = self.execute(
            "issue.plan@1",
            {
                "command": {"issue_number": 2},
                "state": {"issue_number": 2, "state": "open", "labels": []},
            },
        )
        self.assertEqual(result.output["status"], "sub-issues-created")
        self.assertEqual(
            [
                (kind, command.repository, command.issue_number, command.factory_run_id)
                for kind, command in self.fixture.dispatch.requests
            ],
            [("issue", "owner/repo", 7, "run"), ("issue", "owner/repo", 8, "run")],
        )

    def test_seeded_issue_pipeline_runs_existing_policy_and_review(self):
        from factory.workflows.templates import project_templates
        from factory.workflows.bindings import resolve_bindings, ParentOutput

        self.fixture.repository.create_draft_pull_request.return_value["html_url"] = (
            "https://github.com/owner/repo/pull/4"
        )
        self.fixture.repository.get_pull_request.return_value = {
            "number": 4,
            "head": {"ref": "work"},
            "labels": [],
        }
        self.fixture.repository.get_pull_request_files.return_value = []
        graph = project_templates("app", "simple", "developer")["issue"]
        outputs = {}
        for stage in graph.stages:
            self.context = self.context.model_copy(
                update={
                    "stage_id": stage.id,
                    "execution_id": stage.id,
                    "settings_json": json.dumps(
                        {"repository": "owner/repo", "worker_type": stage.role}
                    ),
                }
            )
            from factory.workflows.actions import ActionCatalog

            resolved = resolve_bindings(
                stage, {"issue_number": 2}, outputs, self.context
            )
            result = ActionCatalog(service_factory=self.service_factory).execute(
                stage.action,
                self.context,
                resolved,
                dispatch=self.fixture.dispatch,
                state=self.fixture.state,
            )
            outputs[stage.id] = ParentOutput("p", "repo", result.output)
        self.assertEqual(outputs["record"].output["status"], "review-posted")
        self.fixture.agent.generate.assert_called_once()
        self.fixture.reviewer.review_pull_request.assert_called_once()
        self.assertEqual(
            [kind for kind, _ in self.fixture.dispatch.requests], ["review"]
        )
        self.assertEqual(len(set(self.opened)), 6)

    def test_pull_feedback_and_verify_wrap_real_operations(self):
        command = {
            "issue_number": 2,
            "pull_number": 4,
            "pull_url": "https://github.com/owner/repo/pull/4",
            "branch": "work",
        }
        self.context = self.context.model_copy(
            update={
                "settings_json": json.dumps(
                    {"repository": "owner/repo", "worker_type": "developer"}
                )
            }
        )
        self.fixture.repository.get_pull_request.return_value = {"state": "closed"}
        self.assertEqual(
            self.execute("pull.feedback@1", {"command": command}).output["status"],
            "pull-request-not-open",
        )
        self.fixture.agent.generate.assert_not_called()
        self.context = self.context.model_copy(
            update={
                "settings_json": json.dumps(
                    {"repository": "owner/repo", "worker_type": "simple"}
                )
            }
        )
        self.fixture.repository.get_pull_request.return_value = {
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "a" * 40,
        }
        self.assertEqual(
            self.execute("pull.verify@1", {"command": command}).output["status"],
            "verified-merged",
        )
        self.fixture.repository.verify_commit_checks.assert_called_once_with("a" * 40)
        self.fixture.repository.close_issue.assert_called_once_with(2)

    def test_minimum_purposes_follow_actual_installed_coder(self):
        from factory.settings import Settings
        from factory.workflows.actions import minimum_purposes

        self.assertEqual(
            minimum_purposes("change.implement@1", Settings(developer_agent="claude")),
            ("anthropic", "github"),
        )
        self.assertEqual(
            minimum_purposes("issue.plan@1", Settings()), ("github", "openai")
        )
        self.assertEqual(minimum_purposes("workflow.record@1", Settings()), ())
        self.assertEqual(
            minimum_purposes(
                "change.implement@1",
                Settings(coding_factory="factory.adapters.agents:claude_coding"),
            ),
            ("anthropic", "github"),
        )
        with self.assertRaises(ValueError):
            minimum_purposes(
                "issue.plan@1", Settings(planner_factory="installed:custom")
            )

    def test_result_redacts_actual_grant_before_leaving_action_boundary(self):
        result = self.execute(
            "workflow.record@1",
            {"command": {"issue_number": 2}, "result": {"status": "test-gh"}},
        )
        self.assertNotIn("test-gh", result.model_dump_json())

    def test_echo_rejects_nonfinite_json_before_output_serialization(self):
        with self.assertRaises(ValueError):
            self.execute("core.echo@1", {"value": float("inf")})

    def test_seeded_non_ready_graph_records_business_status_without_models(self):
        from factory.workflows.templates import project_templates
        from factory.workflows.bindings import resolve_bindings, ParentOutput
        from factory.workflows.actions import ActionCatalog

        for state, labels, want, dispatch in [
            ("closed", [], "review-skipped-not-pending", []),
            (
                "open",
                [self.fixture.service.labels.needs_discussion],
                "review-skipped-waiting-for-discussion",
                ["discussion"],
            ),
            (
                "open",
                [self.fixture.service.labels.plan_created],
                "review-skipped-already-planned",
                [],
            ),
        ]:
            with self.subTest(state=state, labels=labels):
                self.setUp()
                self.fixture.repository.get_issue.return_value.update(
                    state=state, labels=[{"name": name} for name in labels]
                )
                outputs = {}
                for stage in project_templates("app", "simple", "developer")[
                    "issue"
                ].stages:
                    self.context = self.context.model_copy(
                        update={
                            "stage_id": stage.id,
                            "execution_id": stage.id,
                            "settings_json": json.dumps(
                                {"repository": "owner/repo", "worker_type": stage.role}
                            ),
                        }
                    )
                    result = ActionCatalog(
                        service_factory=self.service_factory
                    ).execute(
                        stage.action,
                        self.context,
                        resolve_bindings(
                            stage, {"issue_number": 2}, outputs, self.context
                        ),
                        dispatch=self.fixture.dispatch,
                        state=self.fixture.state,
                    )
                    outputs[stage.id] = ParentOutput("p", "repo", result.output)
                self.assertEqual(outputs["record"].output["status"], want)
                self.fixture.agent.generate.assert_not_called()
                self.fixture.planner.triage.assert_not_called()
                self.fixture.reviewer.review_pull_request.assert_not_called()
                self.assertEqual(
                    [kind for kind, _ in self.fixture.dispatch.requests], dispatch
                )

    def test_feedback_retains_cursor_without_duplicate_review_wait(self):
        command = self.fixture.feedback_setup().model_dump(
            exclude={"trace_context", "factory_run_id", "repository"}
        )
        command["pull_url"] = "https://github.com/owner/repo/pull/4"
        self.fixture.repository.get_pull_request.return_value["html_url"] = command[
            "pull_url"
        ]
        self.context = self.context.model_copy(
            update={
                "settings_json": json.dumps(
                    {"repository": "owner/repo", "worker_type": "developer"}
                )
            }
        )
        result = self.execute("pull.feedback@1", {"command": command})
        self.assertEqual(result.output["status"], "feedback-addressed")
        again = self.execute("pull.feedback@1", {"command": command})
        self.assertEqual(again.output["status"], "no-new-feedback")
        self.fixture.agent.generate.assert_called_once()
        self.fixture.repository.update_pull_request_branch.assert_called_once()
        self.assertEqual(self.fixture.dispatch.requests, [])

    def test_composition_creates_distinct_clients_and_never_mutates_environment(self):
        import os
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import Mock, patch
        from factory.bootstrap import run_builtin
        from factory.workflows.project_dispatch import ProjectBindings, WorkflowTarget

        captures = []

        def client(repository, **kwargs):
            captures.append((repository, kwargs["token"]))
            result = Mock()
            result.get_issue.return_value = {"number": 2, "state": "open", "labels": []}
            return result

        def invoke(name):
            target = WorkflowTarget(
                snapshot_id=name + "-pin", workflow_revision_id="v1"
            )
            bindings = ProjectBindings(
                project_id=name,
                repository_id=name,
                issue=target,
                feedback=target,
                verify=target,
            )
            context = self.context.model_copy(
                update={
                    "project_id": name,
                    "repository_id": name,
                    "execution_id": name,
                    "followup_bindings_json": bindings.model_dump_json(),
                    "settings_json": json.dumps({"repository": name + "/repo"}),
                }
            ).with_credentials({"github": "credential-" + name})
            return run_builtin(
                "issue.load@1",
                context,
                ResolvedInputs({"command": {"issue_number": 2}}, ()),
                Mock(),
            ).output

        before = dict(os.environ)
        with (
            patch("github_client.GitHubClient", side_effect=client),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(invoke, ["alpha", "beta"]))
        self.assertEqual(
            results, [{"issue_number": 2, "state": "open", "labels": []}] * 2
        )
        self.assertEqual(
            sorted(captures),
            [("alpha/repo", "credential-alpha"), ("beta/repo", "credential-beta")],
        )
        self.assertEqual(dict(os.environ), before)

    def test_subissue_dispatch_intent_survives_transport_failure_after_planning(self):
        from factory.domain.models import IssueWorkPlan, PlannedSubtask
        from factory.workflows.project_dispatch import (
            ProjectDispatch,
            ProjectBindings,
            WorkflowTarget,
        )

        self.fixture.repository.get_issue.return_value["labels"] = []
        self.fixture.repository.list_sub_issue_ids.return_value = set()
        self.fixture.repository.find_issues_by_markers.return_value = {}
        self.fixture.planner.plan_issue.return_value = IssueWorkPlan(
            tasks=[
                PlannedSubtask(
                    title=name, objective="Task", acceptance_criteria=["Works"]
                )
                for name in ("First", "Second")
            ]
        )
        self.fixture.repository.create_issue.side_effect = [
            {"number": 7, "id": 70},
            {"number": 8, "id": 80},
        ]
        target = WorkflowTarget(snapshot_id="pin", workflow_revision_id="v1")
        bindings = ProjectBindings(
            project_id="p",
            repository_id="repo",
            issue=target,
            feedback=target,
            verify=target,
        )
        accepted = set()
        failed = False

        class Transport:
            def submit_followup(inner, context, request):
                nonlocal failed
                if request.command["issue_number"] == 8 and not failed:
                    failed = True
                    raise RuntimeError("Dispatch unavailable")
                if request.idempotency_key in accepted:
                    return False
                accepted.add(request.idempotency_key)
                return True

        transport = Transport()

        def bind():
            dispatch = ProjectDispatch(self.context, bindings, transport)
            self.fixture.dispatch = self.fixture.service.dispatch = dispatch

        bind()
        args = {
            "command": {"issue_number": 2},
            "state": {"issue_number": 2, "state": "open", "labels": []},
        }
        with self.assertRaises(RuntimeError):
            self.execute("issue.plan@1", args)
        self.fixture.repository.get_issue.return_value["labels"] = [
            {"name": self.fixture.service.labels.plan_created}
        ]
        self.context = self.context.model_copy(
            update={"execution_id": "retry-execution", "attempt": 2}
        )
        bind()
        self.assertEqual(
            self.execute("issue.plan@1", args).output["status"], "already-planned"
        )
        self.assertEqual(len(accepted), 2)
        self.assertEqual(self.fixture.repository.create_issue.call_count, 2)
        self.assertTrue(
            self.fixture.state.values["followups:v1:owner/repo:2:plan"]["accepted"]
        )

    def test_validation_errors_cannot_echo_an_injected_grant(self):
        with self.assertRaises(Exception) as error:
            self.execute("issue.load@1", {"command": {"issue_number": "test-gh"}})
        self.assertNotIn("test-gh", str(error.exception))
        self.assertEqual(self.opened, [])

    def test_custom_planner_policy_covers_review_and_api_feedback(self):
        from factory.settings import Settings
        from factory.workflows.actions import minimum_purposes

        for action in ("change.review@1", "pull.feedback@1"):
            with (
                self.subTest(action=action),
                self.assertRaisesRegex(ValueError, "planner"),
            ):
                minimum_purposes(action, Settings(planner_factory="installed:custom"))
        self.assertEqual(
            minimum_purposes(
                "pull.feedback@1",
                Settings(developer_agent="claude", planner_factory="installed:custom"),
            ),
            ("anthropic", "github"),
        )

    def test_initial_intent_write_failure_recovers_progress_without_redispatch(self):
        from unittest.mock import patch

        key = "followups:v1:owner/repo:2:plan"
        progress = "progress:owner/repo:issue:2:plan_issue"
        self.fixture.state.put(
            progress, {"status": "sub-issues-created", "sub_issue_numbers": [7, 8]}
        )
        self.fixture.repository.get_issue.return_value["labels"] = [
            {"name": self.fixture.service.labels.plan_created}
        ]
        args = {
            "command": {"issue_number": 2},
            "state": {"issue_number": 2, "state": "open", "labels": []},
        }
        original_put = self.fixture.state.put

        def fail_intent(name, value):
            if name == key:
                raise RuntimeError("State unavailable")
            original_put(name, value)

        with patch.object(self.fixture.state, "put", side_effect=fail_intent):
            with self.assertRaisesRegex(RuntimeError, "State unavailable"):
                self.execute("issue.plan@1", args)
        self.assertEqual(self.fixture.state.get(progress)["sub_issue_numbers"], [7, 8])
        self.execute("issue.plan@1", args)
        self.assertEqual(
            [command.issue_number for _, command in self.fixture.dispatch.requests],
            [7, 8],
        )
        self.execute("issue.plan@1", args)
        self.assertEqual(len(self.fixture.dispatch.requests), 2)
