"""Owned trigger configuration and encrypted, versioned ingress credentials."""

import unittest
from unittest.mock import patch
import test_api as fixtures
import test_workflow_definitions as definitions
from app import database as models
from app.project_context import decrypt


class RepositoryTriggersTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = definitions.WorkflowDefinitionTests.setup_graph
    publish = definitions.WorkflowDefinitionTests.publish

    def setup_binding(self, event_kind="issue-ready"):
        project, base, repos, _, document = self.setup_graph()
        # Identity verification is separately qualified; install a verified
        # source revision without making a remote request in these API tests.
        with self.sessions.begin() as db:
            repo = db.get(models.RepositoryConnection, repos["backend"]["id"])
            repo.provider = "gitlab"
            repo.host = "gitlab.com"
            revision = db.get(models.RepositoryRevision, repo.revision_id)
            revision.document = {
                **revision.document,
                "provider": "gitlab",
                "host": "gitlab.com",
                "identity": {
                    "provider": "gitlab",
                    "host": "gitlab.com",
                    "owner": "alpha",
                    "name": "backend",
                    "project_id": 123,
                },
            }
        document["stages"] = document["stages"][:1]
        definition = self.publish(base, document)
        body = {
            "repository_id": repos["backend"]["id"],
            "definition_id": definition["id"],
            "event_kind": event_kind,
        }
        response = self.client.post(base + "/repository-triggers", json=body)
        self.assertEqual(response.status_code, 201, response.text)
        return project, base, body, response.json()

    def test_create_returns_token_once_and_stores_ciphertext(self):
        _, base, _, created = self.setup_binding()
        token = created["webhook_token"]
        self.assertGreaterEqual(len(token), 40)
        public = self.client.get(base + "/repository-triggers/" + created["id"])
        self.assertEqual(public.status_code, 200, public.text)
        self.assertNotIn(token, public.text)
        self.assertNotIn("ciphertext", public.text)
        self.assertNotIn("webhook_token", public.text)
        with self.sessions() as db:
            secret = db.get(models.TriggerToken, created["token_version_id"])
            self.assertNotIn(token, secret.ciphertext)
            self.assertEqual(decrypt(secret.ciphertext, self.settings), token)
            self.assertEqual(secret.binding_id, created["id"])

    def test_rotate_and_revoke_require_current_version(self):
        _, base, _, created = self.setup_binding()
        url = base + "/repository-triggers/" + created["id"]
        rotated = self.client.post(
            url + "/rotate-token", json={"parent_id": created["token_version_id"]}
        )
        self.assertEqual(rotated.status_code, 200, rotated.text)
        self.assertNotEqual(rotated.json()["webhook_token"], created["webhook_token"])
        self.assertEqual(
            self.client.post(
                url + "/rotate-token", json={"parent_id": created["token_version_id"]}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.client.post(
                url + "/revoke-token",
                json={"parent_id": rotated.json()["token_version_id"]},
            ).status_code,
            200,
        )
        self.assertEqual(self.client.get(url).json()["token_version_id"], None)
        with self.sessions() as db:
            self.assertIsNotNone(
                db.get(models.TriggerToken, created["token_version_id"]).revoked_at
            )

    def test_foreign_bindings_and_repository_references_are_hidden(self):
        _, base, body, created = self.setup_binding()
        other = self.client.post(
            "/api/v1/projects", json={"slug": "other", "name": "Other"}
        ).json()["id"]
        foreign = "/api/v1/projects/" + other + "/repository-triggers"
        self.assertEqual(
            self.client.get(foreign + "/" + created["id"]).status_code, 404
        )
        self.assertEqual(self.client.post(foreign, json=body).status_code, 404)
        self.assertEqual(self.client.get(foreign).json(), [])

    def test_edit_has_cas_and_cannot_change_repository_or_event_kind(self):
        _, base, body, created = self.setup_binding()
        url = base + "/repository-triggers/" + created["id"]
        edit = {
            "parent_id": created["revision_id"],
            "definition_id": body["definition_id"],
            "enabled": False,
        }
        response = self.client.put(url, json=edit)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()["enabled"])
        self.assertNotEqual(response.json()["revision_id"], created["revision_id"])
        self.assertEqual(self.client.put(url, json=edit).status_code, 409)
        self.assertEqual(
            self.client.put(
                url, json={**edit, "repository_id": body["repository_id"]}
            ).status_code,
            422,
        )

    def test_trigger_root_repository_must_match_definition(self):
        project, base, repos, _, document = self.setup_graph()
        definition = self.publish(base, document)
        response = self.client.post(
            base + "/repository-triggers",
            json={
                "repository_id": repos["frontend"]["id"],
                "definition_id": definition["id"],
                "event_kind": "issue-ready",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_authentication_is_constant_time_and_rotated_tokens_stop_working(self):
        from fastapi import HTTPException
        from app.repository_triggers import authenticate

        _, base, _, created = self.setup_binding()
        with (
            self.sessions() as db,
            patch(
                "app.repository_triggers.secrets.compare_digest",
                wraps=__import__("secrets").compare_digest,
            ) as compare,
        ):
            proof = authenticate(
                db, created["id"], created["webhook_token"], self.settings
            )
            self.assertEqual(proof.token_version_id, created["token_version_id"])
            compare.assert_called_once()
        url = base + "/repository-triggers/" + created["id"]
        self.client.post(
            url + "/rotate-token", json={"parent_id": created["token_version_id"]}
        )
        for binding, token in (
            (created["id"], created["webhook_token"]),
            ("missing", "x"),
            (created["id"], ""),
            (created["id"], "é"),
            (created["id"], "x" * 300),
        ):
            with self.sessions() as db, self.assertRaises(HTTPException) as error:
                authenticate(db, binding, token, self.settings)
            self.assertEqual(error.exception.status_code, 401)

    def test_disabled_binding_does_not_authenticate_new_events(self):
        from fastapi import HTTPException
        from app.repository_triggers import authenticate

        _, base, body, created = self.setup_binding()
        self.client.put(
            base + "/repository-triggers/" + created["id"],
            json={
                "parent_id": created["revision_id"],
                "definition_id": body["definition_id"],
                "enabled": False,
            },
        )
        with self.sessions() as db, self.assertRaises(HTTPException) as error:
            authenticate(db, created["id"], created["webhook_token"], self.settings)
        self.assertEqual(error.exception.status_code, 401)

    def test_acceptance_pins_one_run_and_duplicate_keeps_old_revision(self):
        from app.trigger_runtime import TriggerRuntime
        from app.repository_triggers import authenticate
        from app.services import Configurations
        from factory.adapters.gitlab_events import GitLabEvent
        from unittest.mock import Mock

        _, base, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        event = GitLabEvent(
            event_id="delivery-1",
            payload_digest="a" * 64,
            project_id=123,
            repository="alpha/backend",
            object_kind="issue",
            action="open",
            issue_number=7,
        )
        remote = Mock()
        remote.get_issue.return_value = {
            "number": 7,
            "state": "open",
            "labels": [{"name": "agent-ready"}],
        }
        with self.sessions() as db:
            proof = authenticate(
                db, created["id"], created["webhook_token"], self.settings
            )
        with patch.object(
            runtime,
            "source",
            return_value=(
                remote,
                {
                    "ready_for_agent": "agent-ready",
                    "pr_open": "agent-pr-open",
                    "plan_created": "agent-plan-created",
                },
            ),
        ):
            first = runtime.accept_event(created["id"], event, proof=proof)
        self.assertEqual(first["status"], "accepted")
        self.assertTrue(first["run_id"])
        document = self.client.get(
            base + "/workflow-definition-revisions/" + first["workflow_revision_id"]
        ).json()["document"]
        document["stages"][0]["inputs"] = {"message": "new revision"}
        updated = self.client.put(
            base + "/workflow-definitions/" + created["definition_id"],
            json={"parent_id": first["workflow_revision_id"], "document": document},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertNotEqual(
            updated.json()["revision_id"], first["workflow_revision_id"]
        )
        # Acceptance already committed its pin before any dispatch. A fresh
        # controller recovers that gap using only owned IDs, without repinning.
        self.workflow.submit_dynamic.return_value = {"run_id": "recovered-engine-id"}
        self.app.state.dynamic_runtime.reconcile_outbox(self.workflow)
        self.assertEqual(
            self.workflow.submit_dynamic.call_args.args[1],
            {"project_id": created["project_id"], "run_id": first["run_id"]},
        )
        with patch.object(
            runtime,
            "source",
            side_effect=AssertionError("duplicate must not reread source"),
        ):
            again = runtime.accept_event(created["id"], event, proof=proof)
        self.assertEqual(again, first)
        with self.sessions() as db:
            from sqlalchemy import select, func

            self.assertEqual(
                db.scalar(select(func.count()).select_from(models.WorkflowRun)), 1
            )
            self.assertEqual(
                db.scalar(select(func.count()).select_from(models.TriggerReceipt)), 1
            )
        changed = event.model_copy(update={"payload_digest": "b" * 64})
        from fastapi import HTTPException

        with self.assertRaises(HTTPException) as error:
            runtime.accept_event(created["id"], changed, proof=proof)
        self.assertEqual(error.exception.status_code, 409)

    def test_remote_read_rotation_blocks_acceptance_and_project_mismatch_reads_nothing(
        self,
    ):
        from app.trigger_runtime import TriggerRuntime
        from app.repository_triggers import authenticate
        from app.services import Configurations
        from factory.adapters.gitlab_events import GitLabEvent
        from fastapi import HTTPException
        from unittest.mock import Mock

        _, base, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        event = GitLabEvent(
            event_id="delivery-2",
            payload_digest="a" * 64,
            project_id=123,
            repository="alpha/backend",
            object_kind="issue",
            action="open",
            issue_number=7,
        )
        with self.sessions() as db:
            proof = authenticate(
                db, created["id"], created["webhook_token"], self.settings
            )
        with patch.object(
            runtime, "source", side_effect=AssertionError("must reject before source")
        ):
            with self.assertRaises(HTTPException) as error:
                runtime.accept_event(
                    created["id"],
                    event.model_copy(update={"project_id": 999}),
                    proof=proof,
                )
        self.assertEqual(error.exception.status_code, 422)
        remote = Mock()

        def rotate(_):
            result = self.client.post(
                base + "/repository-triggers/" + created["id"] + "/rotate-token",
                json={"parent_id": created["token_version_id"]},
            )
            self.assertEqual(result.status_code, 200)
            return {"number": 7, "state": "open", "labels": [{"name": "agent-ready"}]}

        remote.get_issue.side_effect = rotate
        with patch.object(
            runtime,
            "source",
            return_value=(
                remote,
                {
                    "ready_for_agent": "agent-ready",
                    "pr_open": "agent-pr-open",
                    "plan_created": "agent-plan-created",
                },
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                runtime.accept_event(created["id"], event, proof=proof)
        self.assertEqual(error.exception.status_code, 401)

    def test_webhook_uses_own_token_without_operator_session_or_csrf(self):
        _, _, _, created = self.setup_binding()
        payload = {
            "object_kind": "issue",
            "project": {"id": 123, "path_with_namespace": "alpha/backend"},
            "object_attributes": {"iid": 7, "action": "open"},
        }
        self.client.cookies.clear()
        self.client.headers.pop("X-CSRF-Token", None)
        url = "/api/v1/hooks/gitlab/" + created["id"]
        with patch.object(
            self.app.state.repository_triggers,
            "accept_event",
            return_value={"status": "accepted"},
        ) as accept:
            response = self.client.post(
                url,
                headers={
                    "X-Gitlab-Token": created["webhook_token"],
                    "X-Gitlab-Event-UUID": "delivery-id",
                },
                json=payload,
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(accept.call_args.args[1].event_id, "delivery-id")
        self.assertEqual(
            accept.call_args.kwargs["proof"].token_version_id,
            created["token_version_id"],
        )
        self.assertEqual(self.client.post(url, json=payload).status_code, 401)
        self.assertEqual(self.client.get("/api/v1/projects").status_code, 401)

    def test_webhook_bounds_body_and_does_not_echo_invalid_input(self):
        _, _, _, created = self.setup_binding()
        url = "/api/v1/hooks/gitlab/" + created["id"]
        headers = {"X-Gitlab-Token": created["webhook_token"]}
        with patch.object(
            self.app.state.repository_triggers,
            "accept_event",
            side_effect=AssertionError("invalid body cannot launch"),
        ):
            large = self.client.post(url, headers=headers, content=b"x" * 1048577)
            self.assertEqual(large.status_code, 413)
            invalid = self.client.post(
                url, headers=headers, json={"object_kind": "glpat-" + "a" * 30}
            )
            self.assertEqual(invalid.status_code, 422)
            self.assertNotIn("glpat-", invalid.text)

    def test_distinct_delivery_retries_coalesce_and_unready_transition_rearms(self):
        from app.trigger_runtime import TriggerRuntime
        from app.repository_triggers import authenticate
        from app.services import Configurations
        from factory.adapters.gitlab_events import GitLabEvent
        from unittest.mock import Mock

        _, _, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        event = GitLabEvent(
            event_id="first",
            payload_digest="a" * 64,
            project_id=123,
            repository="alpha/backend",
            object_kind="issue",
            action="open",
            issue_number=7,
        )
        remote = Mock()
        issue = {"number": 7, "state": "open", "labels": [{"name": "ready"}]}
        remote.get_issue.return_value = issue
        labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }
        with self.sessions() as db:
            proof = authenticate(
                db, created["id"], created["webhook_token"], self.settings
            )
        with patch.object(runtime, "source", return_value=(remote, labels)):
            first = runtime.accept_event(created["id"], event, proof=proof)
            retry = runtime.accept_event(
                created["id"],
                event.model_copy(update={"event_id": "retry"}),
                proof=proof,
            )
            self.assertNotEqual(retry["status"], "accepted")
            with self.sessions.begin() as db:
                db.get(models.WorkflowRun, first["run_id"]).state = "completed"
            same = runtime.accept_event(
                created["id"],
                event.model_copy(update={"event_id": "routine-comment"}),
                proof=proof,
            )
            self.assertEqual(same["status"], "ignored")
            remote.get_issue.return_value = {**issue, "labels": []}
            runtime.accept_event(
                created["id"],
                event.model_copy(update={"event_id": "unready"}),
                proof=proof,
            )
            remote.get_issue.return_value = issue
            fresh = runtime.accept_event(
                created["id"],
                event.model_copy(update={"event_id": "ready-again"}),
                proof=proof,
            )
            self.assertEqual(fresh["status"], "accepted")
            self.assertNotEqual(fresh["run_id"], first["run_id"])

    def test_polling_recovers_missing_delivery_without_repeating_run(self):
        from app.trigger_runtime import TriggerRuntime
        from app.services import Configurations
        from unittest.mock import Mock
        from sqlalchemy import select, func

        _, _, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        remote = Mock()
        remote._request.return_value = [{"iid": 7, "updated_at": "one"}]
        remote.get_issue.return_value = {
            "number": 7,
            "state": "open",
            "labels": [{"name": "ready"}],
        }
        labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }
        with patch.object(runtime, "source", return_value=(remote, labels)):
            self.assertEqual(runtime.poll_once(force=True), 1)
            self.assertEqual(runtime.poll_once(force=True), 1)
        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(models.WorkflowRun)), 1
            )
            binding = db.get(models.RepositoryTrigger, created["id"])
            self.assertIsNone(binding.lease_owner)
            self.assertIsNone(binding.last_error)
            self.assertGreater(binding.next_poll_at, 0)

    def test_poll_tracked_issue_refreshes_even_without_scan_timestamp_change(self):
        from app.trigger_runtime import TriggerRuntime
        from app.services import Configurations
        from unittest.mock import Mock
        from sqlalchemy import select, func

        _, _, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        remote = Mock()
        remote._request.return_value = [{"iid": 7, "updated_at": "unchanged"}]
        ready = {"number": 7, "state": "open", "labels": [{"name": "ready"}]}
        remote.get_issue.return_value = ready
        labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }
        with patch.object(runtime, "source", return_value=(remote, labels)):
            runtime.poll_once(force=True)
            with self.sessions.begin() as db:
                run = db.scalar(select(models.WorkflowRun))
                run.state = "completed"
            remote._request.return_value = []
            remote.get_issue.return_value = {**ready, "labels": []}
            runtime.poll_once(force=True)
            # A second unready observation must not become a cached duplicate
            # that prevents the next tracked fresh read from seeing readiness.
            runtime.poll_once(force=True)
            remote.get_issue.return_value = ready
            runtime.poll_once(force=True)
            runtime.poll_once(force=True)
        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(models.WorkflowRun)), 2
            )

    def test_older_inflight_read_cannot_overwrite_newer_observation(self):
        from app.trigger_runtime import TriggerRuntime
        from app.services import Configurations
        from factory.adapters.gitlab_events import GitLabEvent
        from fastapi import HTTPException
        from unittest.mock import Mock
        from sqlalchemy import select

        _, _, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        event = GitLabEvent(
            event_id="older",
            payload_digest="a" * 64,
            project_id=123,
            repository="alpha/backend",
            object_kind="issue",
            action="open",
            issue_number=7,
        )
        remote = Mock()
        ready = {"number": 7, "state": "open", "labels": [{"name": "ready"}]}

        def concurrent_read(_):
            remote.get_issue.side_effect = None
            remote.get_issue.return_value = ready
            runtime.accept_event(
                created["id"], event.model_copy(update={"event_id": "newer"})
            )
            return {**ready, "labels": []}

        remote.get_issue.side_effect = concurrent_read
        labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }
        with patch.object(runtime, "source", return_value=(remote, labels)):
            with self.assertRaises(HTTPException) as error:
                runtime.accept_event(created["id"], event)
        self.assertEqual(error.exception.status_code, 409)
        with self.sessions() as db:
            self.assertTrue(db.scalar(select(models.TriggerObservation)).eligible)

    @unittest.skipUnless(
        __import__("os")
        .environ.get("CONTROL_TEST_DATABASE_URL", "")
        .startswith("postgresql"),
        "requires isolated PostgreSQL schemas",
    )
    def test_webhook_and_poll_concurrency_accepts_one_pinned_run(self):
        from app.trigger_runtime import TriggerRuntime
        from app.repository_triggers import authenticate
        from app.services import Configurations
        from factory.adapters.gitlab_events import GitLabEvent
        from fastapi import HTTPException
        from unittest.mock import Mock
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from sqlalchemy import select, func

        _, _, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        event = GitLabEvent(
            event_id="webhook-race",
            payload_digest="a" * 64,
            project_id=123,
            repository="alpha/backend",
            object_kind="issue",
            action="open",
            issue_number=7,
        )
        with self.sessions() as db:
            proof = authenticate(
                db, created["id"], created["webhook_token"], self.settings
            )
        remote = Mock()
        ready = {"number": 7, "state": "open", "labels": [{"name": "ready"}]}
        barrier = Barrier(2)

        def read(_):
            barrier.wait(timeout=10)
            return ready

        remote.get_issue.side_effect = read
        labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }

        def accept(identifier, authenticated):
            try:
                return runtime.accept_event(
                    created["id"],
                    event.model_copy(update={"event_id": identifier}),
                    proof=proof if authenticated else None,
                )["status"]
            except HTTPException as error:
                return error.status_code

        with patch.object(runtime, "source", return_value=(remote, labels)):
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(accept, "webhook-race", True),
                    executor.submit(accept, "poll-race", False),
                ]
                results = [future.result(timeout=20) for future in futures]
            self.assertEqual(results.count("accepted"), 1)
            self.assertIn(409, results)
            remote.get_issue.side_effect = None
            remote.get_issue.return_value = ready
            accept("reconcile-race", False)
        with self.sessions() as db:
            self.assertEqual(
                db.scalar(select(func.count()).select_from(models.WorkflowRun)), 1
            )

    def test_trigger_source_never_falls_back_to_global_gitlab_token(self):
        from app.trigger_runtime import TriggerRuntime
        from app.services import Configurations
        from fastapi import HTTPException

        _, _, _, created = self.setup_binding()
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        with self.sessions() as db:
            repository = db.get(models.RepositoryConnection, created["repository_id"])
            revision_id = repository.revision_id
        with (
            patch.dict(
                __import__("os").environ, {"GITLAB_TOKEN": "ambient-must-not-be-used"}
            ),
            patch(
                "factory.adapters.gitlab.build_opener",
                side_effect=AssertionError("no remote call without scoped credential"),
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                runtime.source(
                    created["project_id"], created["repository_id"], revision_id
                )
        self.assertEqual(error.exception.status_code, 404)

    def test_followup_binding_configuration_rejects_incomplete_roles(self):
        _, base, body, created = self.setup_binding()
        response = self.client.post(
            base + "/repository-triggers",
            json={
                **body,
                "configuration": {
                    "followups": {
                        created["repository_id"]: {"issue": body["definition_id"]}
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_feedback_during_active_run_remains_pending_after_completion(self):
        from app.trigger_runtime import TriggerRuntime
        from app.services import Configurations
        from factory.adapters.gitlab_events import GitLabEvent
        from unittest.mock import Mock

        _, _, _, created = self.setup_binding("feedback")
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        event = GitLabEvent(
            event_id="note-41",
            payload_digest="a" * 64,
            project_id=123,
            repository="alpha/backend",
            object_kind="note",
            action="create",
            pull_number=4,
            note_id=41,
        )
        remote = Mock(repository="alpha/backend", project_id=123)
        remote.get_pull_request.return_value = {
            "number": 4,
            "html_url": "https://gitlab.com/alpha/backend/-/merge_requests/4",
            "body": "Closes #7",
            "state": "open",
            "merged": False,
            "source_project_id": 123,
            "target_project_id": 123,
            "head": {"ref": "factory-7"},
        }
        remote.get_issue.return_value = {
            "number": 7,
            "state": "open",
            "labels": [{"name": "pr-open"}],
        }
        remote.get_pull_feedback.return_value = [{"id": 41}]
        labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }
        with patch.object(runtime, "source", return_value=(remote, labels)):
            first = runtime.accept_event(created["id"], event)
            self.assertEqual(first["status"], "accepted")
            remote.get_pull_feedback.return_value = [{"id": 41}, {"id": 42}]
            pending = runtime.accept_event(
                created["id"],
                event.model_copy(update={"event_id": "note-42", "note_id": 42}),
            )
            self.assertEqual(pending["status"], "coalesced")
            self.assertEqual(pending["run_id"], first["run_id"])
            with self.sessions.begin() as db:
                db.get(models.WorkflowRun, first["run_id"]).state = "completed"
            later = runtime.accept_event(
                created["id"],
                event.model_copy(update={"event_id": "poll-new-feedback"}),
            )
            self.assertEqual(later["status"], "accepted")
            self.assertNotEqual(later["run_id"], first["run_id"])

    def test_closed_unmerged_change_never_queues_verification(self):
        from app.trigger_runtime import TriggerRuntime
        from app.services import Configurations
        from factory.adapters.gitlab_events import GitLabEvent
        from unittest.mock import Mock

        _, _, _, created = self.setup_binding("verify")
        runtime = TriggerRuntime(
            self.sessions, self.settings, Configurations(self.settings.root)
        )
        event = GitLabEvent(
            event_id="closed",
            payload_digest="a" * 64,
            project_id=123,
            repository="alpha/backend",
            object_kind="merge_request",
            action="close",
            pull_number=4,
        )
        remote = Mock(repository="alpha/backend", project_id=123)
        pull = {
            "number": 4,
            "html_url": "https://gitlab.com/alpha/backend/-/merge_requests/4",
            "body": "Closes #7",
            "state": "closed",
            "merged": False,
            "source_project_id": 123,
            "target_project_id": 123,
            "head": {"ref": "factory-7"},
            "merge_commit_sha": "a" * 40,
        }
        remote.get_pull_request.return_value = pull
        remote.get_issue.return_value = {
            "number": 7,
            "state": "open",
            "labels": [{"name": "pr-open"}],
        }
        labels = {
            "ready_for_agent": "ready",
            "pr_open": "pr-open",
            "plan_created": "plan-created",
        }
        with patch.object(runtime, "source", return_value=(remote, labels)):
            closed = runtime.accept_event(created["id"], event)
            self.assertEqual(closed["status"], "ignored")
            self.assertIsNone(closed["run_id"])
            remote.get_pull_request.return_value = {**pull, "merged": True}
            merged = runtime.accept_event(
                created["id"], event.model_copy(update={"event_id": "merged"})
            )
            self.assertEqual(merged["status"], "accepted")
