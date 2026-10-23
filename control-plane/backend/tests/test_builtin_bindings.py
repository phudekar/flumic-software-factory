"""Project template seeding and frozen follow-up snapshot bundles."""

import unittest
from fastapi import HTTPException
import test_api as fixtures
import test_workflow_definitions as definitions
from app import database as models
from app.projects import ProjectScope


class BuiltinBindingTests(unittest.TestCase):
    setUp = fixtures.ControlPlaneTests.setUp
    tearDown = fixtures.ControlPlaneTests.tearDown
    setup_graph = definitions.WorkflowDefinitionTests.setup_graph
    publish = definitions.WorkflowDefinitionTests.publish

    def setup_bundle(self, slug="alpha", *, credentials=False):
        project, base, repos, _, doc = self.setup_graph(slug)
        pools = {}
        from factory.workflows.action_schemas import CATALOG

        for role in ("simple", "developer"):
            response = self.client.post(
                "/api/v1/worker-pools",
                json={
                    "name": slug + "-" + role,
                    "role": role,
                    "actions": [
                        key
                        for key, value in CATALOG.items()
                        if value.role in {None, role}
                    ],
                },
            )
            self.assertEqual(response.status_code, 201, response.text)
            pools[role] = response.json()["id"]
            self.client.put(
                base + "/worker-pools/" + pools[role], json={"enabled": True}
            )
        secrets = {}
        if credentials:
            github = self.client.post(
                base + "/secrets",
                json={
                    "name": "github",
                    "purpose": "github",
                    "repository_id": repos["backend"]["id"],
                    "value": "private-github",
                },
            ).json()["id"]
            model = self.client.post(
                base + "/secrets",
                json={"name": "model", "purpose": "openai", "value": "private-model"},
            ).json()["id"]
            update = self.client.put(
                base + "/repositories/" + repos["backend"]["id"],
                json={
                    "alias": "backend",
                    "owner": slug,
                    "name": "backend",
                    "parent_id": repos["backend"]["revision_id"],
                    "credential_id": github,
                },
            )
            self.assertEqual(update.status_code, 200, update.text)
            repos["backend"] = update.json()
            secrets = {"openai": model}
        from app.builtin_workflows import seed_project_templates

        scope = ProjectScope(project)
        values = seed_project_templates(
            self.sessions,
            scope,
            fixtures.Configurations(self.settings.root),
            self.settings,
            repository="backend",
            simple_pool=pools["simple"],
            developer_pool=pools["developer"],
            secrets=secrets,
        )
        return scope, base, repos, pools, values

    def test_seed_is_idempotent_and_pins_all_three_followup_workflows(self):
        scope, base, repos, pools, values = self.setup_bundle()
        from app.builtin_workflows import seed_project_templates, pin_project_bundle

        again = seed_project_templates(
            self.sessions,
            scope,
            fixtures.Configurations(self.settings.root),
            self.settings,
            repository="backend",
            simple_pool=pools["simple"],
            developer_pool=pools["developer"],
        )
        self.assertEqual(values, again)
        with self.sessions.begin() as db:
            pin = pin_project_bundle(
                db,
                scope,
                values["issue"],
                {repos["backend"]["id"]: values},
                fixtures.Configurations(self.settings.root),
                self.settings,
            )
            from app.workflow_definitions import read_definition_pin

            document = read_definition_pin(db, scope, pin.id, self.settings)
            from factory.workflows.project_dispatch import ProjectBindings

            bindings = ProjectBindings.model_validate_json(
                document["stages"]["plan"]["followup_bindings_json"]
            )
            self.assertEqual(bindings.repository_id, repos["backend"]["id"])
            self.assertEqual(bindings.issue.snapshot_id, pin.id)
            target = read_definition_pin(
                db, scope, bindings.verify.snapshot_id, self.settings
            )
            self.assertEqual(target["workflow_revision_id"], values["verify"])
            self.assertEqual(
                target["repositories"]["backend"]["revision_id"],
                document["repositories"]["backend"]["revision_id"],
            )
            self.assertEqual(
                target["factory_revision_id"], document["factory_revision_id"]
            )

    def test_foreign_or_wrong_repository_followup_rejected(self):
        scope, _, repos, _, values = self.setup_bundle()
        other, _, _, _, foreign = self.setup_bundle("beta")
        from app.builtin_workflows import pin_project_bundle

        with self.assertRaises(HTTPException):
            with self.sessions.begin() as db:
                pin_project_bundle(
                    db,
                    scope,
                    values["issue"],
                    {repos["backend"]["id"]: foreign},
                    fixtures.Configurations(self.settings.root),
                    self.settings,
                )
        with self.assertRaises(HTTPException):
            with self.sessions.begin() as db:
                pin_project_bundle(
                    db,
                    scope,
                    values["issue"],
                    {repos["frontend"]["id"]: values},
                    fixtures.Configurations(self.settings.root),
                    self.settings,
                )

    def test_followup_launch_uses_existing_snapshot_after_live_repository_edit(self):
        scope, base, repos, _, values = self.setup_bundle()
        from app.builtin_workflows import pin_project_bundle
        from app.workflow_state import WorkflowState
        from app.workflow_definitions import read_definition_pin

        config = fixtures.Configurations(self.settings.root)
        with self.sessions.begin() as db:
            pin = pin_project_bundle(
                db,
                scope,
                values["issue"],
                {repos["backend"]["id"]: values},
                config,
                self.settings,
            )
            original = read_definition_pin(db, scope, pin.id, self.settings)
        response = self.client.put(
            base + "/repositories/" + repos["backend"]["id"],
            json={
                "alias": "backend",
                "parent_id": repos["backend"]["revision_id"],
                "owner": "changed",
                "name": "new-repo",
                "overrides": {},
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        store = WorkflowState(self.sessions, self.settings, config)
        # Activate both canonical repositories explicitly; never infer takeover.
        from app.workflow_state import repository_key

        with self.sessions.begin() as db:
            key = repository_key(original["repositories"]["backend"]["document"])
            db.add(models.RepositoryExecutionMode(repository=key, mode="dynamic"))
        run = store.start_pinned_run(
            scope, pin.id, {"issue_number": 2}, key="pinned-child"
        )
        self.assertEqual(run.snapshot_id, pin.id)
        self.assertEqual(
            store.start_pinned_run(
                scope, pin.id, {"issue_number": 2}, key="pinned-child"
            ).id,
            run.id,
        )
        with self.sessions() as db:
            kept = read_definition_pin(db, scope, run.snapshot_id, self.settings)
            self.assertEqual(
                kept["repositories"]["backend"]["document"]["owner"], "alpha"
            )

    def test_new_run_rejects_invalid_correlation_metadata_before_admission(self):
        project, base, repos, pool, document = self.setup_graph()
        document["stages"] = document["stages"][:1]
        revision = self.publish(base, document)
        from app.workflow_state import WorkflowState

        store = WorkflowState(
            self.sessions, self.settings, fixtures.Configurations(self.settings.root)
        )
        for metadata in (
            {"trace_context": "bad"},
            {"factory_run_id": {"not": "an-id"}},
        ):
            with (
                self.subTest(metadata=metadata),
                self.assertRaises(HTTPException) as error,
            ):
                store.start_run(
                    ProjectScope(project),
                    revision["revision_id"],
                    metadata,
                    key="bad-" + str(len(str(metadata))),
                )
            self.assertEqual(error.exception.status_code, 422)

    def test_admission_delivers_minimum_grants_and_frozen_dispatch_context(self):
        scope, base, repos, pools, values = self.setup_bundle(credentials=True)
        import test_worker_pools as worker_fixtures

        worker, headers = worker_fixtures.WorkerPoolTests.worker(
            self, {"id": pools["simple"]}
        )
        from factory.workflows.action_schemas import CATALOG

        with self.sessions.begin() as db:
            item = db.get(models.Worker, worker)
            item.capabilities = {
                **item.capabilities,
                "actions": [
                    key
                    for key, descriptor in CATALOG.items()
                    if descriptor.role in {None, "simple"}
                ],
            }
        from app.workflow_state import WorkflowState

        store = WorkflowState(
            self.sessions, self.settings, fixtures.Configurations(self.settings.root)
        )
        store.enable_dynamic_repository(scope, repos["backend"]["id"])
        run = store.start_run(
            scope,
            values["issue"],
            {
                "issue_number": 2,
                "factory_run_id": "root-correlation",
                "trace_context": {
                    "traceparent": "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
                },
            },
            key="root",
            bindings={repos["backend"]["id"]: values},
        )
        first = store.advance_run(scope, run.id).ready[0]
        claim = self.client.post(
            "/api/v1/shared-worker/claim",
            headers=headers,
            json={**first.model_dump(), "claim_key": "a-stable-claim-key"},
        )
        self.assertEqual(claim.status_code, 200, claim.text)
        body = claim.json()
        delivered = self.client.post(
            "/api/v1/shared-worker/assignments/" + body["assignment_id"] + "/context",
            headers=headers,
            json={"lease_token": body["lease_token"]},
        )
        self.assertEqual(delivered.status_code, 200, delivered.text)
        self.assertEqual(delivered.json()["credentials"], {"github": "private-github"})
        context = delivered.json()["context"]
        self.assertEqual(context["factory_run_id"], "root-correlation")
        self.assertEqual(
            context["trace_context"]["traceparent"],
            "00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        )
        from factory.workflows.project_dispatch import ProjectBindings

        bindings = ProjectBindings.model_validate_json(
            context["followup_bindings_json"]
        )
        self.assertEqual(
            (bindings.project_id, bindings.repository_id),
            (scope.project_id, repos["backend"]["id"]),
        )
        self.assertEqual(bindings.issue.workflow_revision_id, values["issue"])
        store.finish_execution(
            scope,
            first.execution_id,
            output={"issue_number": 2, "state": "open", "labels": []},
            confirmed_terminal=True,
        )
        second = store.advance_run(scope, run.id).ready[0]
        from app.project_context import decrypt

        with self.sessions() as db:
            attempt = db.get(models.StageAttempt, second.execution_id)
            assignment = db.get(models.Assignment, attempt.assignment_id)
            pinned = decrypt(
                db.get(models.ActionContextSnapshot, assignment.context_id).ciphertext,
                self.settings,
            )
            self.assertEqual(set(pinned["grants"]), {"github", "openai"})
            self.assertTrue(decrypt(attempt.origins_ciphertext, self.settings))

    def test_bundle_rejects_missing_secondary_repository_dispatch_before_launch(self):
        scope, base, repos, pools, values = self.setup_bundle()
        document = {
            "schema_version": 1,
            "name": "two-repository-root",
            "repositories": ["backend", "frontend"],
            "default_repository": "backend",
            "stages": [
                {
                    "id": "load",
                    "executor": "builtin",
                    "action": "issue.load@1",
                    "role": "simple",
                    "pool_id": pools["simple"],
                    "repository": "frontend",
                    "inputs": {"command": {"issue_number": 2}},
                }
            ],
        }
        root = self.publish(base, document)
        from app.builtin_workflows import pin_project_bundle

        with self.assertRaises(HTTPException) as error:
            with self.sessions.begin() as db:
                pin_project_bundle(
                    db,
                    scope,
                    root["revision_id"],
                    {repos["backend"]["id"]: values},
                    fixtures.Configurations(self.settings.root),
                    self.settings,
                )
        self.assertEqual(error.exception.status_code, 422)

    def test_legacy_echo_metadata_does_not_poison_global_admission(self):
        project, base, _, _, document = self.setup_graph()
        document["stages"] = document["stages"][:1]
        revision = self.publish(base, document)
        from app.workflow_state import WorkflowState
        from app.project_context import encrypt

        store = WorkflowState(
            self.sessions, self.settings, fixtures.Configurations(self.settings.root)
        )
        run = store.start_run(
            ProjectScope(project), revision["revision_id"], {}, key="old-run"
        )
        with self.sessions.begin() as db:
            # Simulate a pre-catalog run where these were ordinary business keys.
            db.get(models.WorkflowRun, run.id).input_ciphertext = encrypt(
                {"trace_context": "old-business-value"}, self.settings
            )
        self.assertEqual(
            store.advance_run(ProjectScope(project), run.id).state, "running"
        )

    def test_multirepository_bundle_routes_each_stage_to_its_own_pinned_workflows(self):
        scope, base, repos, pools, backend = self.setup_bundle()
        from app.builtin_workflows import seed_project_templates, pin_project_bundle
        from app.workflow_definitions import read_definition_pin
        from factory.workflows.project_dispatch import ProjectBindings

        config = fixtures.Configurations(self.settings.root)
        frontend = seed_project_templates(
            self.sessions,
            scope,
            config,
            self.settings,
            repository="frontend",
            simple_pool=pools["simple"],
            developer_pool=pools["developer"],
        )
        document = {
            "schema_version": 1,
            "name": "multi-repository",
            "repositories": ["backend", "frontend"],
            "default_repository": "backend",
            "stages": [
                {
                    "id": alias,
                    "executor": "builtin",
                    "action": "issue.load@1",
                    "role": "simple",
                    "pool_id": pools["simple"],
                    "repository": alias,
                    "inputs": {"command": {"issue_number": 2}},
                }
                for alias in ("backend", "frontend")
            ],
        }
        root = self.publish(base, document)
        with self.sessions.begin() as db:
            pin = pin_project_bundle(
                db,
                scope,
                root["revision_id"],
                {repos["backend"]["id"]: backend, repos["frontend"]["id"]: frontend},
                config,
                self.settings,
            )
            value = read_definition_pin(db, scope, pin.id, self.settings)
            targets = []
            for alias in ("backend", "frontend"):
                bindings = ProjectBindings.model_validate_json(
                    value["stages"][alias]["followup_bindings_json"]
                )
                self.assertEqual(bindings.repository_id, repos[alias]["id"])
                target = read_definition_pin(
                    db, scope, bindings.issue.snapshot_id, self.settings
                )
                self.assertEqual(target["definition"]["default_repository"], alias)
                self.assertEqual(
                    target["stages"]["load"]["repository_id"], repos[alias]["id"]
                )
                targets.append(bindings.issue.snapshot_id)
            self.assertNotEqual(*targets)
