"""Whole-path two-project qualification; fakes are only external integrations."""

import unittest
from project_release_fixture import ProjectReleaseFixture


class ProjectReleaseTests(unittest.TestCase):
    def setUp(self):
        self.fixture = ProjectReleaseFixture()
        self.addCleanup(self.fixture.close)

    def test_two_projects_two_repositories_shared_roles_mixed_parallel_graph_and_pins(
        self,
    ):
        f = self.fixture
        runs = [f.launch(slug) for slug in f.projects]
        old_revisions = [f.detail(run)["revision_id"] for run in runs]
        f.rotate("release-alpha")
        states, frontiers = f.finish(runs)
        self.assertEqual(states, ["completed", "completed"])
        self.assertTrue(any(set(ready) == {"left", "right"} for _, ready in frontiers))
        self.assertEqual({role for role, _, _ in f.claims}, {"simple", "developer"})
        self.assertEqual(len(f.jobs.launches), 2)
        for index, (slug, project) in enumerate(f.projects.items()):
            run = runs[index]
            detail = f.detail(run)
            self.assertEqual(detail["revision_id"], old_revisions[index])
            self.assertEqual(
                next(stage for stage in detail["stages"] if stage["id"] == "collect")[
                    "repository_id"
                ],
                project["repositories"]["target"]["id"],
            )
            self.assertTrue(
                all(
                    stage["settings"]["openai_model"] == "fixture-" + slug + "-v1"
                    for stage in detail["stages"]
                )
            )
            self.assertEqual(
                next(a for a in detail["attempts"] if a["stage_id"] == "optional")[
                    "state"
                ],
                "skipped",
            )
            self.assertEqual(f.results(run)["done"]["output"]["result"]["owner"], slug)
            base = project["base"]
            usage = f.call(base + "/usage?run_id=" + run.run_id)
            self.assertEqual(
                (usage["calls"], usage["input_tokens"], usage["output_tokens"]),
                (2, 4, 6),
            )
            self.assertAlmostEqual(usage["reported_usd"], 0.03)
            self.assertIsNone(usage["estimated_usd"])
            events = f.call(
                base + "/events?run_id=" + run.run_id + "&kind=model.completed"
            )
            other = "release-beta" if slug == "release-alpha" else "release-alpha"
            self.assertNotIn(other, str(events))
            self.assertNotIn(slug + "-github-v1", str(events))
            model = next(
                e for e in events if e["payload"].get("conversation_available")
            )
            exchange = f.call(base + "/model-calls/" + model["id"] + "/conversation")
            self.assertEqual(exchange["request"], "[REDACTED]")
            foreign = f.projects[other]["base"]
            self.assertEqual(
                f.client.get(
                    foreign + "/model-calls/" + model["id"] + "/conversation"
                ).status_code,
                404,
            )
            from app import database as models
            from app.project_context import decrypt
            import test_api as fixtures

            with f.sessions() as db:
                row = db.scalar(
                    fixtures.select(models.ProjectBusinessState).where(
                        models.ProjectBusinessState.project_id == project["id"],
                        models.ProjectBusinessState.repository_id
                        == project["repositories"]["source"]["id"],
                        models.ProjectBusinessState.key == "release-fixture",
                    )
                )
                self.assertEqual(
                    decrypt(row.ciphertext, f.settings),
                    {"owner": slug, "model": "fixture-" + slug + "-v1"},
                )
        self.assertEqual(f.call("/api/v1/events"), [])

    def test_lost_worker_completion_and_new_controller_do_not_repeat_completed_action(
        self,
    ):
        from factory.shared_worker import SharedRequestError

        f = self.fixture
        run = f.launch("release-alpha")
        frontier = f.runtime.advance(run)
        invocation = frontier.ready[0]
        original = f.controls["simple"].request

        def lose_reply(path, body=None):
            result = original(path, body)
            if path.endswith("/complete"):
                raise SharedRequestError()
            return result

        f.controls["simple"].request = lose_reply
        with self.assertRaises(SharedRequestError):
            f.execute(invocation, "simple")
        f.controls["simple"].request = original
        f.controls["simple"].shutdown()
        f.controls["simple"] = f.control("simple")
        f.restart_controller_and_broker()
        states, _ = f.finish([run])
        self.assertEqual(states, ["completed"])
        load_claims = [claim for claim in f.claims if claim[2] == "issue.load@1"]
        self.assertEqual(len(load_claims), 1)
        usage = f.call(
            f.projects["release-alpha"]["base"] + "/usage?run_id=" + run.run_id
        )
        self.assertEqual(usage["calls"], 2)

    def test_broker_crash_after_create_and_capture_retry_reattach_one_job(self):
        f = self.fixture
        run = f.launch("release-alpha")
        job = f.until_job(run)
        f.jobs.crash_after_create = True
        f.runtime.broker.reconcile_once(f.jobs)
        self.assertEqual(len(f.jobs.launches), 1)
        f.restart_controller_and_broker()
        f.offset += 5
        f.jobs.crash_before_capture = True
        f.runtime.broker.reconcile_once(f.jobs)
        f.offset += 5
        states, _ = f.finish([run])
        self.assertEqual(states, ["completed"])
        self.assertEqual(list(f.jobs.launches), [job])
        self.assertEqual(f.jobs.creations.count(job), 1)
        before = f.call(
            f.projects["release-alpha"]["base"] + "/usage?run_id=" + run.run_id
        )
        for _ in range(3):
            f.offset += 5
            f.runtime.broker.reconcile_once(f.jobs)
            f.runtime.reconcile_background()
        after = f.call(
            f.projects["release-alpha"]["base"] + "/usage?run_id=" + run.run_id
        )
        self.assertEqual(before, after)
        self.assertEqual(after["calls"], 2)

    def test_cancellation_and_deadline_settle_after_submitting_worker_disappears(self):
        from app import database as models

        for mode in ("cancel", "deadline"):
            with self.subTest(mode=mode):
                f = self.fixture
                run = f.launch("release-alpha", key="worker-gone-" + mode)
                job = f.until_job(run)
                f.jobs.wait = True
                f.runtime.broker.reconcile_once(f.jobs)
                self.assertEqual(
                    f.call(f.projects["release-alpha"]["base"] + "/jobs/" + job)[
                        "state"
                    ],
                    "running",
                )
                with f.sessions.begin() as db:
                    db.get(
                        models.Worker, f.workers["developer"]["id"]
                    ).desired_state = "stopped"
                if mode == "cancel":
                    receipt = f.call(
                        f.projects["release-alpha"]["base"]
                        + "/workflow-runs/"
                        + run.run_id
                        + "/cancel",
                        {},
                        202,
                    )
                    self.assertEqual(receipt["state"], "cancelling")
                    f.offset += 5
                else:
                    f.offset += 305
                states, _ = f.finish([run])
                self.assertEqual(
                    states, ["cancelled" if mode == "cancel" else "failed"]
                )
                self.assertEqual(
                    f.call(f.projects["release-alpha"]["base"] + "/jobs/" + job)[
                        "state"
                    ],
                    "cancelled" if mode == "cancel" else "timed-out",
                )
                self.assertIn(job, f.jobs.terminated)
                with f.sessions.begin() as db:
                    db.get(
                        models.Worker, f.workers["developer"]["id"]
                    ).desired_state = "created"

    def test_one_deployment_slot_and_revoked_grant_do_not_starve_other_project(self):
        import time
        from app import database as models
        from app.workflow_state import AdmissionLimits

        f = self.fixture
        f.store.limits = AdmissionLimits(
            actions_per_deployment=1, actions_per_project=1
        )
        runs = [f.launch(slug) for slug in f.projects]
        alpha = f.projects["release-alpha"]
        with f.sessions.begin() as db:
            db.get(
                models.SecretVersion, alpha["secrets"]["github"]["id"]
            ).revoked_at = time.time()
        states, _ = f.finish(runs)
        self.assertEqual(states, ["failed", "completed"])
        self.assertEqual(
            f.call(
                f.projects["release-beta"]["base"] + "/usage?run_id=" + runs[1].run_id
            )["calls"],
            2,
        )
        self.assertNotIn("release-alpha-github-v1", str(f.detail(runs[0])))
