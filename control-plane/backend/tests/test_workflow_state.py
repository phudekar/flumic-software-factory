"""Durable state, replay identities and admission fences (no live engine/providers)."""

import copy
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from fastapi import HTTPException
from sqlalchemy import select
import test_api as fixtures
import test_workflow_definitions as definition_fixtures
import test_worker_pools as pool_fixtures
from app import database as models
from app.projects import ProjectScope


class WorkflowStateTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = definition_fixtures.WorkflowDefinitionTests.setup_graph
    publish = definition_fixtures.WorkflowDefinitionTests.publish
    worker = pool_fixtures.WorkerPoolTests.worker

    def arrange(self, slug="alpha", count=1, retries=0):
        from app.workflow_state import WorkflowState

        project, base, repos, pool, document = self.setup_graph(slug)
        document["stages"] = [
            {
                **copy.deepcopy(document["stages"][0]),
                "id": "echo-" + str(i),
                "retries": retries,
                "retry_delay_seconds": 5,
                "inputs": {"message": {"$ref": "input.message"}},
            }
            for i in range(count)
        ]
        published = self.publish(base, document)
        self.worker(pool, slug + "-worker")
        self.now = getattr(self, "now", 1000.0)
        self.store = WorkflowState(
            self.sessions,
            self.settings,
            fixtures.Configurations(self.settings.root),
            clock=lambda: self.now,
        )
        return ProjectScope(project), repos["backend"]["id"], published

    def start(self, scope, definition, key="one", message="private message"):
        return self.store.start_run(
            scope, definition["revision_id"], {"message": message}, key=key
        )

    def finish(self, scope, execution, output=None, failure=False):
        return self.store.finish_execution(
            scope,
            execution,
            output=output or {"message": "done"},
            failed=failure,
            confirmed_terminal=True,
        )

    def test_run_replay_pins_inputs_settings_and_execution_identity(self):
        from app.project_context import decrypt

        scope, repo, definition = self.arrange()
        run = self.start(scope, definition)
        self.assertEqual(run.id, self.start(scope, definition).id)
        with self.assertRaises(HTTPException) as error:
            self.start(scope, definition, message="changed")
        self.assertEqual(error.exception.status_code, 409)
        first = self.store.advance_run(scope, run.id)
        self.assertEqual(len(first.ready), 1)
        again = self.store.advance_run(scope, run.id)
        self.assertEqual(first.ready, again.ready)
        execution = first.ready[0].execution_id
        with self.sessions() as db:
            attempt = db.get(models.StageAttempt, execution)
            assignment = db.get(models.Assignment, attempt.assignment_id)
            self.assertNotIn("private message", assignment.input_ciphertext)
            self.assertEqual(
                decrypt(assignment.input_ciphertext, self.settings),
                {"message": "private message"},
            )
            context = db.get(models.ActionContextSnapshot, assignment.context_id)
            self.assertEqual(
                decrypt(context.ciphertext, self.settings)["context"]["repository_id"],
                repo,
            )
        receipt = self.finish(scope, execution)
        self.assertEqual(receipt.state, "completed")
        self.assertEqual(self.finish(scope, execution), receipt)
        with self.assertRaises(HTTPException):
            self.finish(scope, execution, {"different": True})
        self.assertEqual(self.store.advance_run(scope, run.id).state, "completed")

    def test_only_known_terminal_failure_can_allocate_retry_generation(self):
        scope, _, definition = self.arrange(retries=1)
        run = self.start(scope, definition)
        first = self.store.advance_run(scope, run.id).ready[0].execution_id
        self.now += 1000
        # A lost/expired transport is not proof that external effects stopped.
        self.assertEqual(
            self.store.advance_run(scope, run.id).ready[0].execution_id, first
        )
        with self.assertRaises(HTTPException):
            self.store.finish_execution(
                scope, first, failed=True, confirmed_terminal=False
            )
        self.finish(scope, first, failure=True)
        self.assertEqual(self.store.advance_run(scope, run.id).ready, [])
        self.now += 5
        second = self.store.advance_run(scope, run.id).ready[0].execution_id
        self.assertNotEqual(first, second)
        self.finish(scope, second, failure=True)
        self.assertEqual(self.store.advance_run(scope, run.id).state, "failed")
        with self.sessions() as db:
            attempts = list(
                db.scalars(
                    select(models.StageAttempt).where(
                        models.StageAttempt.run_id == run.id
                    )
                )
            )
            self.assertEqual(sorted(a.generation for a in attempts), [1, 2])

    def test_default_run_quota_and_round_robin_action_admission(self):
        from app.workflow_state import AdmissionLimits

        a, _, da = self.arrange("alpha", count=6)
        b, _, db = self.arrange("beta", count=6)
        # Use enough registered slots to exercise deployment/project quotas.
        with self.sessions.begin() as tx:
            for pool in tx.scalars(select(models.WorkerPool)):
                for index in range(3):
                    source = tx.scalar(
                        select(models.Worker).where(models.Worker.pool_id == pool.id)
                    )
                    tx.add(
                        models.Worker(
                            name=pool.name + str(index),
                            profile="simple",
                            revision_id=source.revision_id,
                            protocol="shared-v1",
                            pool_id=pool.id,
                            capabilities=source.capabilities,
                            registered_at=1000,
                            desired_state="running",
                            credential_hash="unused",
                        )
                    )
        runs = [self.start(a, da, key=str(i)) for i in range(3)]
        other = self.start(b, db)
        self.store.advance_run(a, runs[0].id)
        with self.sessions() as tx:
            self.assertEqual(tx.get(models.WorkflowRun, runs[2].id).state, "queued")
            active = list(
                tx.scalars(
                    select(models.StageAttempt).where(
                        models.StageAttempt.state == "ready"
                    )
                )
            )
            self.assertEqual(len(active), 8)
            self.assertEqual(sum(item.project_id == a.project_id for item in active), 4)
            self.assertEqual(sum(item.project_id == b.project_id for item in active), 4)
            self.assertEqual(tx.get(models.WorkflowRun, other.id).state, "running")
        self.assertEqual(AdmissionLimits().runs_per_project, 2)

    def test_state_is_project_repository_scoped_and_resource_claims_are_canonical(self):
        from app.workflow_state import canonical_resource

        a, ar, da = self.arrange("alpha")
        b, br, db = self.arrange("beta")
        self.store.put_state(a, ar, "plan:1", {"secret": "alpha-only"})
        self.assertEqual(
            self.store.get_state(a, ar, "plan:1"), {"secret": "alpha-only"}
        )
        self.assertIsNone(self.store.get_state(b, br, "plan:1"))
        with self.assertRaises(HTTPException):
            self.store.get_state(b, ar, "plan:1")
        self.store.delete_state(a, ar, "plan:1")
        self.assertIsNone(self.store.get_state(a, ar, "plan:1"))
        for scope, repository_id in ((a, ar), (b, br)):
            path = f"/api/v1/projects/{scope.project_id}/repositories/{repository_id}"
            current = self.client.get(path).json()
            response = self.client.put(
                path,
                json={
                    "alias": "backend",
                    "owner": "Owner",
                    "name": "Repo",
                    "parent_id": current["revision_id"],
                },
            )
            self.assertEqual(response.status_code, 200, response.text)
        resource = canonical_resource("GitHub.COM", "Owner", "Repo", "issue", 12)
        self.assertEqual(
            resource, canonical_resource("github.com", "owner", "repo", "issue", 12)
        )
        ra = self.start(a, da)
        rb = self.start(b, db)
        ea = self.store.advance_run(a, ra.id).ready[0].execution_id
        eb = self.store.advance_run(b, rb.id).ready[0].execution_id
        self.store.claim_resource(a, ea, resource)
        with self.assertRaises(HTTPException):
            self.store.claim_resource(b, eb, resource)
        self.now += 1000
        with self.assertRaises(HTTPException):
            self.store.claim_resource(b, eb, resource)
        self.finish(a, ea)
        self.store.claim_resource(b, eb, resource)

    @unittest.skipUnless(
        os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
        "PostgreSQL admission locking",
    )
    def test_postgres_quota_race_and_identical_run_creation(self):
        scope, _, definition = self.arrange(count=4)
        with ThreadPoolExecutor(max_workers=3) as threads:
            runs = list(threads.map(lambda _: self.start(scope, definition), range(3)))
        self.assertEqual(len({run.id for run in runs}), 1)
        with ThreadPoolExecutor(max_workers=3) as threads:
            results = list(
                threads.map(
                    lambda _: self.store.advance_run(scope, runs[0].id), range(3)
                )
            )
        self.assertEqual(results[0].ready, results[1].ready)
        with self.sessions() as db:
            # One worker, two slots: concurrent admissions cannot exceed capacity.
            self.assertEqual(len(list(db.scalars(select(models.Assignment)))), 2)

    def test_shared_worker_completion_advances_dag_and_preserves_pinned_context(self):
        from factory.shared_worker import SharedControl, SharedSettings
        from unittest.mock import patch
        from app.project_context import decrypt

        scope, _, definition = self.arrange()
        with self.sessions() as db:
            first = db.get(models.WorkflowRevision, definition["revision_id"])
            from app.project_context import decrypt as decode

            document = decode(first.graph_ciphertext, self.settings)
        document["stages"].append(
            {
                **copy.deepcopy(document["stages"][0]),
                "id": "report",
                "after": ["echo-0"],
                "inputs": {"message": {"$ref": "stages.echo-0.output.message"}},
            }
        )
        base = f"/api/v1/projects/{scope.project_id}"
        updated = self.client.put(
            base + "/workflow-definitions/" + definition["id"],
            json={"parent_id": definition["revision_id"], "document": document},
        ).json()
        run = self.start(scope, updated)
        path = base + "/repositories"
        connection = self.client.get(path).json()[0]
        self.client.put(
            path + "/" + connection["id"],
            json={
                "parent_id": connection["revision_id"],
                "alias": connection["alias"],
                "owner": "changed",
                "name": "changed",
            },
        )
        with self.sessions() as db:
            worker = db.scalar(
                select(models.Worker).where(models.Worker.protocol == "shared-v1")
            )
            pool = db.get(models.WorkerPool, worker.pool_id)
        _, headers = self.worker(pool={"id": pool.id}, name="api-worker")
        client = SharedControl(
            SharedSettings(
                pool_id=pool.id,
                worker_id=headers["X-Worker-Id"],
                worker_name="api-worker",
                role="simple",
            ),
            {
                "FACTORY_CONTROL_ENDPOINT": "http://testserver",
                "FACTORY_CONTROL_TOKEN": "unused",
            },
        )

        def request(route, body=None):
            response = self.client.post(route, json=body, headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()

        with patch.object(client, "request", side_effect=request):
            for stage_id in ("echo-0", "report"):
                advance = self.store.advance_run(scope, run.id)
                self.assertEqual(len(advance.ready), 1)
                invocation = advance.ready[0]
                receipt = client.execute(invocation)
                self.assertEqual(receipt.state, "completed")
                with self.sessions() as db:
                    attempt = db.get(models.StageAttempt, invocation.execution_id)
                    self.assertEqual(attempt.stage_id, stage_id)
                    self.assertEqual(attempt.state, "completed")
                    assignment = db.get(models.Assignment, attempt.assignment_id)
                    context = decrypt(
                        db.get(
                            models.ActionContextSnapshot, assignment.context_id
                        ).ciphertext,
                        self.settings,
                    )
                    self.assertEqual(
                        __import__("json").loads(context["context"]["settings_json"])[
                            "repository"
                        ],
                        "alpha/backend",
                    )
        self.assertEqual(self.store.advance_run(scope, run.id).state, "completed")

    def test_execution_digest_conflict_and_scoped_snapshot_reads(self):
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        execution = self.store.advance_run(scope, run.id).ready[0].execution_id
        with self.sessions() as db:
            item = db.get(models.StageAttempt, execution)
            spec_digest, input_digest = item.spec_digest, item.input_digest
        self.assertEqual(
            self.store.assert_execution(
                scope, execution, spec_digest=spec_digest, input_digest=input_digest
            ).id,
            execution,
        )
        with self.assertRaises(HTTPException):
            self.store.assert_execution(
                scope, execution, spec_digest="different", input_digest=input_digest
            )
        with self.assertRaises(HTTPException):
            self.store.advance_run(ProjectScope(models.DEFAULT_PROJECT_ID), run.id)
        with self.assertRaises(HTTPException):
            self.store.finish_execution(
                ProjectScope(models.DEFAULT_PROJECT_ID),
                execution,
                confirmed_terminal=True,
            )

    def test_dynamic_repository_mode_blocks_legacy_create_restart_and_pending_operations(
        self,
    ):
        from app.workflow_state import WorkflowState

        legacy = fixtures.ControlPlaneTests.create_worker(self)
        self.assertTrue(
            fixtures.reconcile_once(self.sessions, self.runtime, self.settings)
        )
        with self.sessions() as db:
            revision = db.get(models.Revision, legacy.revision_id)
            config = fixtures.Configurations(self.settings.root)
            repository = config.resolve(revision.document, legacy.profile).repository
        owner, name = repository.split("/")
        scope, _, definition = self.arrange()
        base = f"/api/v1/projects/{scope.project_id}"
        repo = self.client.post(
            base + "/repositories",
            json={"alias": "legacy", "owner": owner, "name": name},
        ).json()["id"]
        with self.assertRaises(HTTPException):
            self.store.enable_dynamic_repository(scope, repo)
        response = self.client.post(
            f"/api/v1/workers/{legacy.id}/actions",
            json={"action": "stop"},
            headers={"Idempotency-Key": "stop-for-dynamic"},
        )
        self.assertEqual(response.status_code, 202, response.text)
        for _ in range(10):
            if not fixtures.reconcile_once(self.sessions, self.runtime, self.settings):
                break
        self.store.enable_dynamic_repository(scope, repo)
        response = self.client.post(
            f"/api/v1/workers/{legacy.id}/actions",
            json={"action": "start"},
            headers={"Idempotency-Key": "restart-after-dynamic"},
        )
        self.assertEqual(response.status_code, 409, response.text)
        response = self.client.post(
            "/api/v1/workers",
            json={
                "name": "blocked-legacy",
                "profile": legacy.profile,
                "revision_id": legacy.revision_id,
            },
            headers={"Idempotency-Key": "blocked-new-worker"},
        )
        self.assertEqual(response.status_code, 409, response.text)
        # Echo is deterministic; enabling modes does not rewrite legacy SQLite.
        self.assertIsNotNone(WorkflowState)

    def test_completed_run_releases_run_admission_in_same_advance(self):
        scope, _, definition = self.arrange()
        runs = [self.start(scope, definition, key=str(i)) for i in range(3)]
        self.store.advance_run(scope, runs[0].id)
        first = self.store.advance_run(scope, runs[0].id).ready[0].execution_id
        self.finish(scope, first)
        self.assertEqual(len(self.store.advance_run(scope, runs[2].id).ready), 1)

    def test_one_deployment_slot_rotates_projects_instead_of_draining_fanout(self):
        from app.workflow_state import AdmissionLimits

        a, _, da = self.arrange("alpha", count=3)
        b, _, db = self.arrange("beta", count=3)
        ra, rb = self.start(a, da), self.start(b, db)
        self.store.limits = AdmissionLimits(actions_per_deployment=1)
        self.store.advance_run(a, ra.id)
        with self.sessions() as tx:
            first = tx.scalar(
                select(models.StageAttempt).where(models.StageAttempt.state == "ready")
            )
        first_scope = a if first.project_id == a.project_id else b
        second_scope, second_run = (b, rb) if first_scope == a else (a, ra)
        self.finish(first_scope, first.id)
        next_ready = self.store.advance_run(second_scope, second_run.id).ready
        self.assertEqual(len(next_ready), 1)
        self.assertEqual(next_ready[0].project_id, second_scope.project_id)

    def test_json_key_order_does_not_change_request_or_receipt_identity(self):
        scope, _, definition = self.arrange()
        first = self.store.start_run(
            scope,
            definition["revision_id"],
            {"message": "hello", "extra": True},
            key="ordered",
        )
        second = self.store.start_run(
            scope,
            definition["revision_id"],
            {"extra": True, "message": "hello"},
            key="ordered",
        )
        self.assertEqual(first.id, second.id)
        execution = self.store.advance_run(scope, first.id).ready[0].execution_id
        self.finish(scope, execution, {"a": 1, "b": 2})
        self.finish(scope, execution, {"b": 2, "a": 1})

    def test_utf8_input_uses_logical_json_budget_not_ascii_escape_expansion(self):
        from app.project_context import decrypt

        scope, _, definition = self.arrange()
        with self.sessions() as db:
            document = decrypt(
                db.get(
                    models.WorkflowRevision, definition["revision_id"]
                ).graph_ciphertext,
                self.settings,
            )
        document["stages"][0]["inputs"] = {
            "item-" + str(i): {"$ref": "input.message"} for i in range(12)
        }
        definition = self.client.put(
            f"/api/v1/projects/{scope.project_id}/workflow-definitions/{definition['id']}",
            json={"parent_id": definition["revision_id"], "document": document},
        ).json()
        run = self.start(scope, definition, message="é" * 20000)
        self.assertEqual(len(self.store.advance_run(scope, run.id).ready), 1)

    @unittest.skipUnless(
        os.environ.get("CONTROL_TEST_DATABASE_URL", "").startswith("postgresql"),
        "PostgreSQL resource locking",
    )
    def test_postgres_cross_project_canonical_claim_race(self):
        from app.workflow_state import canonical_resource

        a, ar, da = self.arrange("alpha")
        b, br, db = self.arrange("beta")
        for scope, repository_id in ((a, ar), (b, br)):
            path = f"/api/v1/projects/{scope.project_id}/repositories/{repository_id}"
            current = self.client.get(path).json()
            response = self.client.put(
                path,
                json={
                    "alias": "backend",
                    "owner": "Owner",
                    "name": "Repo",
                    "parent_id": current["revision_id"],
                },
            )
            self.assertEqual(response.status_code, 200)
        ra, rb = self.start(a, da), self.start(b, db)
        ea = self.store.advance_run(a, ra.id).ready[0].execution_id
        eb = self.store.advance_run(b, rb.id).ready[0].execution_id
        resource = canonical_resource("github.com", "owner", "repo", "pull", 17)

        def claim(pair):
            try:
                self.store.claim_resource(*pair, resource)
                return 200
            except HTTPException as error:
                return error.status_code

        with ThreadPoolExecutor(max_workers=2) as threads:
            self.assertEqual(sorted(threads.map(claim, [(a, ea), (b, eb)])), [200, 409])

    def test_populated_migration_roundtrip_preserves_legacy_and_pins(self):
        import tempfile
        import uuid
        from unittest.mock import patch
        from sqlalchemy import create_engine, text, inspect
        from sqlalchemy.engine import make_url
        from sqlalchemy.exc import IntegrityError
        from alembic.config import Config
        from alembic import command

        with tempfile.TemporaryDirectory() as tmp:
            url = os.environ.get(
                "CONTROL_MIGRATION_TEST_URL", "sqlite:///" + tmp + "/state.db"
            )
            admin = None
            if url.startswith("postgresql"):
                admin = create_engine(url)
                schema = "state_" + uuid.uuid4().hex
                with admin.begin() as connection:
                    connection.execute(text('CREATE SCHEMA "' + schema + '"'))
                url = (
                    make_url(url)
                    .update_query_dict({"options": "-csearch_path=" + schema})
                    .render_as_string(hide_password=False)
                )
            engine, sessions = models.database(url)
            try:
                cfg = Config(str(fixtures.ROOT / "control-plane/backend/alembic.ini"))
                with patch.dict(os.environ, {"CONTROL_DATABASE_URL": url}):
                    command.upgrade(cfg, "e201f1000005")
                    with sessions.begin() as db:
                        db.add(models.Project(id="p", slug="p", name="p"))
                        db.add(models.Project(id="q", slug="q", name="q"))
                        db.flush()
                        db.add(
                            models.WorkflowDefinitionRecord(
                                id="d", project_id="p", name="d"
                            )
                        )
                        db.flush()
                        db.add(
                            models.WorkflowRevision(
                                id="r",
                                project_id="p",
                                definition_id="d",
                                digest="d" * 64,
                                graph_ciphertext="encrypted",
                            )
                        )
                        db.flush()
                        db.add(
                            models.WorkflowLaunchSnapshot(
                                id="pin",
                                project_id="p",
                                workflow_revision_id="r",
                                ciphertext="encrypted",
                            )
                        )
                    command.upgrade(cfg, "head")
                    with sessions.begin() as db:
                        db.add(
                            models.WorkflowRun(
                                id="run",
                                project_id="p",
                                snapshot_id="pin",
                                sequence=1,
                                request_key="key",
                                request_digest="d" * 64,
                                input_ciphertext="private",
                            )
                        )
                        db.flush()
                        db.add(
                            models.StageAttempt(
                                id="attempt",
                                project_id="p",
                                run_id="run",
                                stage_id="a",
                                generation=1,
                            )
                        )
                    with self.assertRaises(IntegrityError), sessions.begin() as db:
                        db.add(
                            models.WorkflowRun(
                                id="foreign",
                                project_id="q",
                                snapshot_id="pin",
                                sequence=2,
                                request_key="key",
                                request_digest="d" * 64,
                                input_ciphertext="private",
                            )
                        )
                        db.flush()
                    with self.assertRaises(IntegrityError), sessions.begin() as db:
                        db.add(
                            models.StageAttempt(
                                id="foreign",
                                project_id="q",
                                run_id="run",
                                stage_id="a",
                                generation=1,
                            )
                        )
                        db.flush()
                    with sessions.begin() as db:
                        db.add(
                            models.RepositoryConnection(
                                id="qr", project_id="q", alias="q", owner="q", name="q"
                            )
                        )
                        db.add(models.WorkerPool(id="pool", name="pool", profile={}))
                        db.flush()
                        db.add(
                            models.Assignment(
                                id="qa",
                                project_id="q",
                                repository_id="qr",
                                pool_id="pool",
                                run_id="other",
                                stage_id="a",
                                execution_id="qe",
                                role="simple",
                                executor="builtin@1",
                                action="core.echo@1",
                                input_ciphertext="encrypted",
                            )
                        )
                    with self.assertRaises(IntegrityError), sessions.begin() as db:
                        db.add(
                            models.StageAttempt(
                                id="wrong-assignment",
                                project_id="p",
                                run_id="run",
                                stage_id="b",
                                generation=1,
                                assignment_id="qa",
                            )
                        )
                        db.flush()
                    with self.assertRaises(IntegrityError), sessions.begin() as db:
                        db.add(
                            models.ExternalResourceClaim(
                                resource="github.com/p/r",
                                project_id="q",
                                execution_id="attempt",
                                expires_at=1000,
                            )
                        )
                        db.flush()
                    command.downgrade(cfg, "e201f1000005")
                    self.assertNotIn("workflow_runs", inspect(engine).get_table_names())
                    with sessions() as db:
                        self.assertEqual(
                            db.get(models.WorkflowLaunchSnapshot, "pin").ciphertext,
                            "encrypted",
                        )
                        self.assertEqual(db.get(models.Project, "p").name, "p")
                    command.upgrade(cfg, "head")
                    self.assertIn("workflow_runs", inspect(engine).get_table_names())
            finally:
                engine.dispose()
                if admin:
                    with admin.begin() as connection:
                        connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
                    admin.dispose()

    def test_generic_relay_keeps_origins_but_dependency_only_does_not_bind_data(self):
        from app.project_context import decrypt

        for bound in (False, True):
            scope, repository_id, definition = self.arrange(
                "bound" if bound else "literal"
            )
            with self.sessions() as db:
                document = decrypt(
                    db.get(
                        models.WorkflowRevision, definition["revision_id"]
                    ).graph_ciphertext,
                    self.settings,
                )
            template = document["stages"][0]
            document["stages"] = [
                {
                    **template,
                    "id": "origin",
                    "repository": "frontend",
                    "inputs": {"command": {"issue_number": 1}},
                },
                {
                    **template,
                    "id": "relay",
                    "after": ["origin"],
                    "inputs": {"$ref": "stages.origin.output"},
                },
                {
                    **template,
                    "id": "load",
                    "action": "issue.load@1",
                    "after": ["relay"],
                    "inputs": {"$ref": "stages.relay.output"}
                    if bound
                    else {"command": {"issue_number": 1}},
                },
            ]
            definition = self.client.put(
                f"/api/v1/projects/{scope.project_id}/workflow-definitions/{definition['id']}",
                json={"parent_id": definition["revision_id"], "document": document},
            ).json()
            self.store.enable_dynamic_repository(scope, repository_id)
            run = self.start(scope, definition)
            for stage_id in ("origin", "relay"):
                execution = self.store.advance_run(scope, run.id).ready[0].execution_id
                self.finish(scope, execution, {"command": {"issue_number": 1}})
            self.store.advance_run(scope, run.id)
            with self.sessions() as db:
                final = db.scalar(
                    select(models.StageAttempt).where(
                        models.StageAttempt.run_id == run.id,
                        models.StageAttempt.stage_id == "load",
                    )
                )
                # No issue.load-capable worker exists: literal input stays pending;
                # binding relayed foreign data must fail before any assignment.
                self.assertEqual(final.state, "failed" if bound else "pending")

    def test_revoked_grant_or_pool_does_not_block_other_projects(self):
        a, _, da = self.arrange("alpha")
        b, _, db = self.arrange("beta")
        ra, rb = self.start(a, da), self.start(b, db)
        with self.sessions.begin() as tx:
            membership = tx.scalar(
                select(models.ProjectPool).where(
                    models.ProjectPool.project_id == a.project_id
                )
            )
            tx.delete(membership)
        advance = self.store.advance_run(b, rb.id)
        self.assertEqual(len(advance.ready), 1)
        self.assertEqual(self.store.advance_run(a, ra.id).ready, [])

    def test_missing_runtime_grant_fails_stage_without_partial_assignment(self):
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        self.store.purposes = lambda stage, settings: ("openai",)
        advance = self.store.advance_run(scope, run.id)
        self.assertEqual(advance.state, "failed")
        self.assertEqual(advance.ready, [])
        with self.sessions() as db:
            self.assertEqual(list(db.scalars(select(models.Assignment))), [])

    def test_injected_lease_clock_renews_claims_and_expiry_never_creates_attempt(self):
        from app.worker_pools import claim_execution, renew_execution, lease_token
        import uuid

        scope, repository_id, definition = self.arrange()
        run = self.start(scope, definition)
        invocation = self.store.advance_run(scope, run.id).ready[0]
        with self.sessions.begin() as db:
            worker = db.scalar(
                select(models.Worker).where(models.Worker.protocol == "shared-v1")
            )
            row, _ = claim_execution(
                db,
                invocation,
                worker.id,
                self.settings,
                claim_key=str(uuid.uuid4()),
                now=self.now,
            )
            assignment_id, proof, worker_id = (
                row.id,
                lease_token(self.settings, row),
                worker.id,
            )
        self.store.claim_resource(
            scope, invocation.execution_id, "github.com/alpha/backend/issue/1"
        )
        self.now += 20
        with self.sessions.begin() as db:
            worker = db.get(models.Worker, worker_id)
            row = renew_execution(
                db, worker, assignment_id, proof, self.settings, now=self.now
            )
            self.assertEqual(row.expires_at, self.now + 120)
            self.assertEqual(
                db.scalar(select(models.ExternalResourceClaim)).expires_at,
                self.now + 120,
            )
        self.now += 121
        with self.assertRaises(HTTPException), self.sessions.begin() as db:
            renew_execution(
                db,
                db.get(models.Worker, worker_id),
                assignment_id,
                proof,
                self.settings,
                now=self.now,
            )
        self.assertEqual(self.store.advance_run(scope, run.id).ready, [invocation])
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(models.StageAttempt)))), 1)

    def test_false_condition_skips_dependents_and_completes_without_assignments(self):
        from app.project_context import decrypt

        scope, _, definition = self.arrange()
        with self.sessions() as db:
            document = decrypt(
                db.get(
                    models.WorkflowRevision, definition["revision_id"]
                ).graph_ciphertext,
                self.settings,
            )
        document["stages"][0]["when"] = {"ref": "input.enabled", "equals": True}
        document["stages"].append(
            {
                **copy.deepcopy(document["stages"][0]),
                "id": "dependent",
                "after": ["echo-0"],
                "when": None,
            }
        )
        definition = self.client.put(
            f"/api/v1/projects/{scope.project_id}/workflow-definitions/{definition['id']}",
            json={"parent_id": definition["revision_id"], "document": document},
        ).json()
        run = self.store.start_run(
            scope, definition["revision_id"], {"enabled": False}, key="skip"
        )
        self.assertEqual(self.store.advance_run(scope, run.id).state, "completed")
        with self.sessions() as db:
            self.assertEqual(
                {item.state for item in db.scalars(select(models.StageAttempt))},
                {"skipped"},
            )
            self.assertEqual(list(db.scalars(select(models.Assignment))), [])


class SQLiteAdmissionTests(unittest.TestCase):
    setup_graph = WorkflowStateTests.setup_graph
    publish = WorkflowStateTests.publish
    worker = WorkflowStateTests.worker
    arrange = WorkflowStateTests.arrange
    start = WorkflowStateTests.start

    def setUp(self):
        import tempfile
        from unittest.mock import patch

        self.sqlite_dir = tempfile.TemporaryDirectory()
        with patch.dict(
            os.environ,
            {
                "CONTROL_TEST_DATABASE_URL": "sqlite:///"
                + self.sqlite_dir.name
                + "/admission.db"
            },
        ):
            fixtures.ControlPlaneTests.setUp(self)

    def tearDown(self):
        fixtures.ControlPlaneTests.tearDown(self)
        self.sqlite_dir.cleanup()

    def test_file_backed_concurrent_run_creation_and_admission_have_one_identity_and_slot(
        self,
    ):
        from app.workflow_state import AdmissionLimits

        scope, _, definition = self.arrange(count=3)
        self.store.limits = AdmissionLimits(actions_per_deployment=1)
        with ThreadPoolExecutor(max_workers=3) as threads:
            runs = list(threads.map(lambda _: self.start(scope, definition), range(3)))
        self.assertEqual(len({run.id for run in runs}), 1)
        with ThreadPoolExecutor(max_workers=3) as threads:
            results = list(
                threads.map(
                    lambda _: self.store.advance_run(scope, runs[0].id), range(3)
                )
            )
        self.assertEqual(results[0].ready, results[1].ready)
        with self.sessions() as db:
            self.assertEqual(len(list(db.scalars(select(models.Assignment)))), 1)


class ProvenanceBoundTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = WorkflowStateTests.setup_graph
    publish = WorkflowStateTests.publish
    worker = WorkflowStateTests.worker
    arrange = WorkflowStateTests.arrange
    start = WorkflowStateTests.start

    def test_repeated_two_parent_relays_deduplicate_lineage_instead_of_exponential_growth(
        self,
    ):
        from app.project_context import decrypt

        scope, _, definition = self.arrange()
        with self.sessions() as db:
            document = decrypt(
                db.get(
                    models.WorkflowRevision, definition["revision_id"]
                ).graph_ciphertext,
                self.settings,
            )
        template = document["stages"][0]
        count = 18
        document["stages"] = []
        for index in range(count):
            parents = [
                "stage-" + str(i).zfill(2) for i in range(max(0, index - 2), index)
            ]
            inputs = (
                {"x": 1, "other": 1}
                if not parents
                else {
                    "x": {"$ref": "stages." + parents[-1] + ".output.x"},
                    "other": {"$ref": "stages." + parents[0] + ".output.x"},
                }
            )
            document["stages"].append(
                {
                    **template,
                    "id": "stage-" + str(index).zfill(2),
                    "after": parents,
                    "inputs": inputs,
                }
            )
        definition = self.client.put(
            f"/api/v1/projects/{scope.project_id}/workflow-definitions/{definition['id']}",
            json={"parent_id": definition["revision_id"], "document": document},
        ).json()
        run = self.start(scope, definition)
        for index in range(count):
            execution = self.store.advance_run(scope, run.id).ready[0].execution_id
            self.store.finish_execution(
                scope, execution, output={"x": 1, "other": 1}, confirmed_terminal=True
            )
        with self.sessions() as db:
            final = db.get(models.StageAttempt, execution)
            origins = decrypt(final.origins_ciphertext, self.settings)
            self.assertLessEqual(len(origins), 4 * count)

    def test_unique_provenance_metadata_has_an_explicit_budget(self):
        from app.workflow_state import bounded_origins

        values = (
            {
                "path": ["x" * 100] * 60,
                "stage_id": "source-" + str(index),
                "project_id": "p",
                "repository_id": "r",
            }
            for index in range(100)
        )
        with self.assertRaisesRegex(ValueError, "provenance"):
            bounded_origins(values)


class LifecycleFenceTests(unittest.TestCase):
    setUp = SQLiteAdmissionTests.setUp
    tearDown = SQLiteAdmissionTests.tearDown
    setup_graph = WorkflowStateTests.setup_graph
    publish = WorkflowStateTests.publish
    worker = WorkflowStateTests.worker
    arrange = WorkflowStateTests.arrange
    start = WorkflowStateTests.start

    def test_blocked_docker_observation_does_not_block_another_project_transition(self):
        from threading import Event

        legacy = fixtures.ControlPlaneTests.create_worker(self)
        self.assertTrue(
            fixtures.reconcile_once(self.sessions, self.runtime, self.settings)
        )
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        entered, release, advanced = Event(), Event(), Event()

        def observe():
            entered.set()
            release.wait(timeout=10)
            return []

        self.runtime.list.side_effect = observe

        def advance():
            result = self.store.advance_run(scope, run.id)
            advanced.set()
            return result

        with ThreadPoolExecutor(max_workers=2) as threads:
            lifecycle = threads.submit(
                self.client.post,
                f"/api/v1/workers/{legacy.id}/actions",
                json={"action": "start"},
                headers={"Idempotency-Key": "slow-observation"},
            )
            self.assertTrue(entered.wait(timeout=3))
            other = threads.submit(advance)
            try:
                independent = advanced.wait(timeout=2)
            finally:
                release.set()
            self.assertTrue(
                independent, "Docker observation holds the deployment transition lock"
            )
            self.assertEqual(lifecycle.result(timeout=5).status_code, 202)
            self.assertEqual(len(other.result(timeout=5).ready), 1)

    def test_lifecycle_revalidates_worker_state_after_docker_observation(self):
        legacy = fixtures.ControlPlaneTests.create_worker(self)
        self.assertTrue(
            fixtures.reconcile_once(self.sessions, self.runtime, self.settings)
        )

        def changed():
            with self.sessions.begin() as db:
                db.get(models.Worker, legacy.id).desired_state = "stopped"
            return []

        self.runtime.list.side_effect = changed
        response = self.client.post(
            f"/api/v1/workers/{legacy.id}/actions",
            json={"action": "start"},
            headers={"Idempotency-Key": "changed-observation"},
        )
        self.assertEqual(response.status_code, 409, response.text)

    def test_cancel_blocks_new_admission_and_waits_for_claimed_action(self):
        scope, _, definition = self.arrange(count=3, retries=1)
        run = self.start(scope, definition)
        first = self.store.advance_run(scope, run.id)
        execution = first.ready[0].execution_id
        with self.sessions.begin() as db:
            attempt = db.get(models.StageAttempt, execution)
            assignment = db.get(models.Assignment, attempt.assignment_id)
            assignment.status = "running"
            attempt.state = "running"
        receipt = self.store.cancel_run(scope, run.id)
        self.assertEqual(receipt.state, "cancelling")
        self.assertEqual(receipt.ready, [])
        self.assertEqual(self.store.advance_run(scope, run.id).ready, [])
        self.store.finish_execution(
            scope, execution, failed=True, confirmed_terminal=True
        )
        self.assertEqual(self.store.advance_run(scope, run.id).state, "cancelled")
        with self.sessions() as db:
            self.assertEqual(
                len(
                    list(
                        db.scalars(
                            select(models.StageAttempt).where(
                                models.StageAttempt.run_id == run.id
                            )
                        )
                    )
                ),
                3,
            )

    def test_advance_provides_exact_pinned_assignment_routes(self):
        scope, _, definition = self.arrange()
        run = self.start(scope, definition)
        advance = self.store.advance_run(scope, run.id)
        route = advance.routes[advance.ready[0].execution_id]
        self.assertEqual(route.stage_id, "echo-0")
        self.assertEqual(route.attempt, 1)
        self.assertEqual(route.role, "simple")
        with self.sessions() as db:
            item = db.get(models.StageAttempt, advance.ready[0].execution_id)
            self.assertEqual(
                route.pool_id, db.get(models.Assignment, item.assignment_id).pool_id
            )
