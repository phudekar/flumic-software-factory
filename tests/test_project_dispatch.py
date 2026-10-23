import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
import test_builtin_actions as fixtures
from factory.domain.models import IssueInput, PullInput


class ProjectDispatchTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.BuiltinActionTests()
        fixture.setUp()
        self.context = fixture.context
        self.requests = []

        class Transport:
            def submit_followup(inner, context, request):
                self.requests.append((context, request))
                return True

        self.transport = Transport()

    def dispatch(self):
        from factory.workflows.project_dispatch import (
            ProjectDispatch,
            ProjectBindings,
            WorkflowTarget,
        )

        def target(key):
            return WorkflowTarget(
                snapshot_id=key + "-pin", workflow_revision_id=key + "-v1"
            )

        bindings = ProjectBindings(
            project_id="p",
            repository_id="repo",
            issue=target("issue"),
            feedback=target("feedback"),
            verify=target("verify"),
        )
        return ProjectDispatch(self.context, bindings, self.transport)

    def test_all_followups_preserve_project_repository_and_pinned_targets(self):
        dispatch = self.dispatch()
        issue = IssueInput(repository="owner/repo", issue_number=5)
        pull = PullInput(
            repository="owner/repo",
            issue_number=5,
            pull_number=8,
            pull_url="https://github.com/owner/repo/pull/8",
            branch="work",
        )
        self.assertTrue(dispatch.enqueue_issue(issue))
        dispatch.wait_for_discussion(issue)
        dispatch.wait_for_review(pull)
        dispatch.enqueue_feedback(pull)
        dispatch.verify_merge(pull)
        values = [request for _, request in self.requests]
        self.assertEqual(
            [item.kind for item in values],
            ["issue", "discussion", "review", "feedback", "verify"],
        )
        self.assertEqual(
            [item.target.snapshot_id for item in values],
            ["issue-pin", "issue-pin", "verify-pin", "feedback-pin", "verify-pin"],
        )
        self.assertEqual(
            {
                (item.project_id, item.repository_id, item.source_execution_id)
                for item in values
            },
            {("p", "repo", "e")},
        )
        self.assertEqual(
            {item.command["repository"] for item in values}, {"owner/repo"}
        )
        self.assertEqual({item.command["factory_run_id"] for item in values}, {"run"})

    def test_wrong_command_repository_rejected_before_transport(self):
        with self.assertRaises(ValueError):
            self.dispatch().enqueue_issue(
                IssueInput(repository="other/repo", issue_number=5)
            )
        self.assertEqual(self.requests, [])

    def test_replay_key_is_stable_despite_new_trace_span(self):
        dispatch = self.dispatch()
        dispatch.enqueue_issue(
            IssueInput(
                repository="owner/repo",
                issue_number=5,
                trace_context={"traceparent": "first"},
            )
        )
        dispatch.enqueue_issue(
            IssueInput(
                repository="owner/repo",
                issue_number=5,
                trace_context={"traceparent": "second"},
            )
        )
        self.assertEqual(
            self.requests[0][1].idempotency_key, self.requests[1][1].idempotency_key
        )

    def test_project_template_graphs_validate_and_preserve_non_ready_path(self):
        from factory.workflows.templates import project_templates

        graphs = project_templates("app", "simple-pool", "developer-pool")
        self.assertEqual(set(graphs), {"issue", "feedback", "verify"})
        issue = graphs["issue"]
        self.assertEqual(
            [stage.action for stage in issue.stages],
            [
                "issue.load@1",
                "issue.plan@1",
                "issue.triage@1",
                "change.implement@1",
                "change.review@1",
                "workflow.record@1",
            ],
        )
        self.assertTrue(all(stage.when is None for stage in issue.stages))
        self.assertEqual(issue.stages[2].after, ["load", "plan"])

    def test_followup_payload_is_bounded_before_transport(self):
        with self.assertRaises(ValueError):
            self.dispatch().enqueue_issue(
                IssueInput(
                    repository="owner/repo",
                    issue_number=2,
                    trace_context={"large": "x" * 65536},
                )
            )
        self.assertEqual(self.requests, [])


class ProjectWaitTests(unittest.IsolatedAsyncioTestCase):
    async def test_discussion_resume_retries_active_run_then_uses_pinned_request(self):
        from factory.workflows.project_waits import coordinate_wait, WaitCheck

        checks = []
        resumes = []
        sleeps = []

        async def check(wait, key):
            checks.append((wait, key))
            return WaitCheck(kind="discussion", state="ready", poll_seconds=7)

        async def resume(wait, key):
            resumes.append((wait, key))
            return len(resumes) == 2

        async def sleep(seconds):
            sleeps.append(seconds)

        result = await coordinate_wait("wait-1", check, resume, sleep)
        self.assertEqual(result, {"state": "resumed"})
        self.assertEqual(sleeps, [7])
        self.assertEqual(
            checks, [("wait-1", "check/wait-1/0"), ("wait-1", "check/wait-1/1")]
        )
        self.assertEqual(
            resumes, [("wait-1", "resume/wait-1/0"), ("wait-1", "resume/wait-1/1")]
        )

    async def test_review_wait_queues_verification_and_closed_does_not_resume(self):
        from factory.workflows.project_waits import coordinate_wait, WaitCheck

        calls = []

        async def resume(wait, key):
            calls.append(wait)
            return True

        async def sleep(seconds):
            self.fail("Terminal review status must not sleep")

        async def merged(*args):
            return WaitCheck(kind="review", state="merged")

        async def closed(*args):
            return WaitCheck(kind="review", state="closed-unmerged")

        self.assertEqual(
            await coordinate_wait("pinned", merged, resume, sleep),
            {"state": "verification-queued"},
        )
        self.assertEqual(
            await coordinate_wait("closed", closed, resume, sleep),
            {"state": "closed-unmerged"},
        )
        self.assertEqual(calls, ["pinned"])

    async def test_short_status_check_delivers_only_github_and_preserves_scope(self):
        from contextlib import contextmanager
        from factory.workflows.project_waits import check_project_wait

        fixture = fixtures.BuiltinActionTests()
        fixture.setUp()
        fixture.fixture.repository.get_issue.return_value["labels"] = [
            {"name": fixture.fixture.service.labels.ready_for_agent}
        ]
        captured = []

        @contextmanager
        def service(context, **kwargs):
            captured.append(dict(context.credentials))
            yield fixture.fixture.service

        value = check_project_wait(
            fixture.context,
            "discussion",
            IssueInput(repository="owner/repo", issue_number=2),
            dispatch=fixture.fixture.dispatch,
            state=fixture.fixture.state,
            service_factory=service,
        )
        self.assertEqual(value.state, "ready")
        self.assertEqual(captured, [{"github": "test-gh"}])
        fixture.fixture.planner.triage.assert_not_called()
        with self.assertRaises(ValueError):
            check_project_wait(
                fixture.context,
                "discussion",
                IssueInput(repository="other/repo", issue_number=2),
                dispatch=None,
                state=None,
                service_factory=service,
            )
        self.assertEqual(len(captured), 1)

    async def test_wait_registration_executes_short_handlers_with_real_sdk(self):
        import base64
        import json
        from hatchet_sdk import Hatchet
        from hatchet_sdk.config import ClientConfig
        from factory.adapters.orchestration.dynamic import register_project_waits
        from factory.workflows.project_waits import WaitInvocation, WaitCheck

        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "test",
                        "server_url": "http://localhost:8080",
                        "grpc_broadcast_address": "localhost:7070",
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        engine = Hatchet(
            config=ClientConfig(token="eyJhbGciOiJub25lIn0." + claims + ".test")
        )
        tasks = register_project_waits(
            engine,
            lambda wait: WaitCheck(kind="discussion", state="ready"),
            lambda wait: False,
        )
        self.assertEqual(
            tasks.check.mock_run(WaitInvocation(wait_id="pinned-id")).state, "ready"
        )
        self.assertFalse(tasks.resume.mock_run(WaitInvocation(wait_id="pinned-id")))
        # Serializing the actual durable definition catches unsupported SDK options.
        self.assertTrue(tasks.wait.to_proto().name.endswith("-wait"))

    async def test_wait_resume_protocol_failures_are_nonretryable_and_sanitized(self):
        import base64
        import json
        from hatchet_sdk import Hatchet, NonRetryableException
        from hatchet_sdk.config import ClientConfig
        from factory.adapters.orchestration.dynamic import register_project_waits
        from factory.workflows.project_waits import WaitInvocation, WaitCheck

        claims = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {
                        "sub": "test",
                        "server_url": "http://localhost:8080",
                        "grpc_broadcast_address": "localhost:7070",
                    }
                ).encode()
            )
            .decode()
            .rstrip("=")
        )
        engine = Hatchet(
            config=ClientConfig(token="eyJhbGciOiJub25lIn0." + claims + ".test")
        )

        def invalid(wait_id):
            raise ValueError("private-invalid-binding")

        tasks = register_project_waits(
            engine, lambda wait: WaitCheck(kind="discussion", state="ready"), invalid
        )
        with self.assertRaises(NonRetryableException) as error:
            tasks.resume.mock_run(WaitInvocation(wait_id="stored-id"))
        self.assertNotIn("private-invalid-binding", str(error.exception))
