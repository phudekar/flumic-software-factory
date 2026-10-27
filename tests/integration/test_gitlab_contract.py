"""Opt-in real GitLab.com HTTP/Git/CI contract; never calls a model provider.

See docs/gitlab.md for the exact write scope, pinned identity and cleanup.
"""

# ruff: noqa: E402 -- standalone opt-in script bootstraps repository import roots.
import base64
import json
import os
from pathlib import Path
import sys
import time
import unittest
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "worker"), str(ROOT / "tests")]
from integration.gitlab_live_fixture import LiveGitLabConfig, GitLabLiveFixture
from factory.adapters.gitlab import GitLabRequestError
from factory.domain.models import (
    FileChange,
    IssueInput,
    IssueWorkPlan,
    PatchPlan,
    PlannedSubtask,
    PullInput,
    RepositoryError,
    StaleRevisionError,
    TriageState,
    TriagePlan,
)
from factory.configuration import Labels
from factory.workflows.redaction import secret_scope


class LiveGitLabContract(unittest.TestCase):
    def setUp(self):
        self.config = LiveGitLabConfig.from_env(os.environ)
        self.scope = secret_scope([self.config.token])
        self.scope.__enter__()
        self.addCleanup(self.scope.__exit__, None, None, None)
        resume_path = os.environ.get("FLUMIC_GITLAB_RESUME_INVENTORY")
        self.resume_info = None
        if resume_path:
            raw = Path(resume_path).read_bytes()
            if len(raw) > 65536:
                raise ValueError("Resume inventory exceeds its size limit")
            self.resume_info = json.loads(raw)
            self.fixture = GitLabLiveFixture.resume(self.config, self.resume_info)
        else:
            self.fixture = GitLabLiveFixture(self.config)
        self.client = self.fixture.client
        self.writes = 0
        self.cleanup_failures = []
        self.original_ci = None
        self.ci_prepared = False
        self.paths = {}
        self.physical_publications = 0
        self.fake_coding_calls = 0
        self.checked = []
        import platform
        import subprocess
        from importlib.metadata import version

        self.dependencies = {
            "python": platform.python_version(),
            "hatchet-sdk": version("hatchet-sdk"),
            "pydantic": version("pydantic"),
            "git": subprocess.check_output(["git", "--version"], text=True).strip(),
        }
        if self.resume_info:
            previous = self.file(".gitlab-ci.yml", ref=self.config.default_sha)
            self.original_ci = (
                base64.b64decode(previous["content"]).decode() if previous else None
            )
            self.ci_prepared = True
            self.writes = self.resume_info["writes"]
            self.physical_publications = self.resume_info["physical_publications"]
            self.fake_coding_calls = self.resume_info["fake_coding_calls"]
            self.checked = list(self.resume_info.get("verified_refs", []))
            self.paths = {
                "src/flumic_contract_" + self.fixture.nonce + ".py": True,
                "src/flumic_race_" + self.fixture.nonce + ".py": True,
            }
        original = self.client._request

        def guarded(method, path, **kwargs):
            if method != "GET":
                project = original("GET", "")
                if (
                    project.get("id") != self.config.project_id
                    or project.get("path_with_namespace") != self.config.repository
                    or project.get("default_branch") != self.config.default_branch
                ):
                    raise ValueError("Live source identity changed; mutation blocked")
                self.writes += 1
                if self.writes > 128:
                    raise ValueError("Live contract mutation budget exhausted")
            return original(method, path, **kwargs)

        self.client._request = guarded
        self.addCleanup(self.cleanup_live_resources)
        project = self.fixture.assert_project()
        if project.get("ci_config_path") not in (None, "", ".gitlab-ci.yml"):
            raise ValueError(
                "Live qualification requires the default CI configuration path"
            )
        runners = self.client._request("GET", "/runners", query={"per_page": 1})
        if not project.get("shared_runners_enabled") and not runners:
            raise ValueError(
                "Enable an available test-project CI runner before live qualification"
            )

    def file(self, path, *, ref=None):
        try:
            return self.client._request(
                "GET",
                "/repository/files/" + quote(path, safe=""),
                query={"ref": ref or self.config.default_branch},
            )
        except GitLabRequestError as error:
            if error.status_code == 404:
                return None
            raise

    def commit_default(self, actions, message):
        self.fixture.preflight()
        value = self.client._request(
            "POST",
            "/repository/commits",
            body={
                "branch": self.config.default_branch,
                "commit_message": message,
                "actions": actions,
            },
        )
        self.fixture.expected_default_head = value["id"]
        self.record_evidence("default-branch-commit")
        return value["id"]

    def prepare_ci(self):
        previous = self.file(".gitlab-ci.yml")
        self.original_ci = (
            base64.b64decode(previous["content"]).decode() if previous else None
        )
        config = (
            "# " + self.fixture.prefix + "\n"
            "workflow:\n  rules:\n    - if: '$CI_PIPELINE_SOURCE == \"push\"'\n"
            "default:\n  image: python:3.12-slim\n"
            "variables:\n  GIT_STRATEGY: none\n"
            "flumic_contract:\n  script:\n"
            "    - python -c \"print('flumic qualification')\"\n"
        )
        action = {
            "action": "update" if previous else "create",
            "file_path": ".gitlab-ci.yml",
            "content": config,
        }
        if previous:
            action["last_commit_id"] = previous["last_commit_id"]
        # Mark preparation before sending: cleanup can inspect a known-effect
        # write whose HTTP reply was lost, rather than blindly repeat the write.
        self.ci_prepared = True
        self.record_evidence("preparing-ci")
        self.commit_default([action], "Prepare " + self.fixture.prefix)

    def wait_checks(self, sha, ref):
        deadline = time.monotonic() + 600
        while True:
            try:
                self.client.verify_commit_checks(sha, ref=ref)
            except RepositoryError:
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        "Required live GitLab CI proof did not succeed before the deadline"
                    ) from None
                time.sleep(5)
            else:
                self.checked.append({"sha": sha, "ref": ref})
                return

    def cleanup_live_resources(self):
        # Always attempt resource cleanup even when restoring the default-branch
        # files fails. Never reset a branch or overwrite an advanced human head.
        if self.ci_prepared:
            try:
                self.fixture.preflight()
                actions = []
                ci = self.file(".gitlab-ci.yml")
                if (
                    ci is None
                    or self.fixture.prefix
                    not in base64.b64decode(ci["content"]).decode()
                ):
                    raise ValueError(
                        "CI ownership changed; automatic restoration blocked"
                    )
                action = {
                    "action": "delete" if self.original_ci is None else "update",
                    "file_path": ".gitlab-ci.yml",
                    "last_commit_id": ci["last_commit_id"],
                }
                if self.original_ci is not None:
                    action["content"] = self.original_ci
                actions.append(action)
                for path in self.paths:
                    item = self.file(path)
                    if item is None:
                        continue
                    if (
                        self.fixture.nonce
                        not in base64.b64decode(item["content"]).decode()
                    ):
                        raise ValueError(
                            "Fixture file ownership changed; restoration blocked"
                        )
                    actions.append(
                        {
                            "action": "delete",
                            "file_path": path,
                            "last_commit_id": item["last_commit_id"],
                        }
                    )
                self.commit_default(actions, "Restore " + self.fixture.prefix)
            except Exception:
                self.cleanup_failures.append(
                    "default-branch restoration requires review"
                )
        self.cleanup_failures.extend(self.fixture.cleanup())
        self.record_evidence("cleanup")
        self.assertFalse(
            self.cleanup_failures,
            "Owned fixture cleanup incomplete; inspect the public cleanup inventory",
        )

    def record_evidence(self, stage):
        directory = os.environ.get("FLUMIC_LIVE_EVIDENCE_DIR")
        if not directory:
            return
        folder = Path(directory)
        folder.mkdir(parents=True, exist_ok=True)
        value = {
            "schema_version": 1,
            "stage": stage,
            "project_id": self.config.project_id,
            "repository": self.config.repository,
            "fixture": self.fixture.prefix,
            "writes": self.writes,
            "fake_coding_calls": self.fake_coding_calls,
            "physical_publications": self.physical_publications,
            "verified_refs": self.checked,
            "cleanup_remaining": self.cleanup_failures,
            "issues": self.fixture.issues,
            "merge_requests": self.fixture.mrs,
            "branches": list(self.fixture.branches),
            "branch_heads": dict(self.fixture.branches),
            "retained_merged_requests": list(self.fixture.retained_merged_requests),
            "labels": self.fixture.labels,
            "approved_default_sha": self.config.default_sha,
            "known_default_sha": self.fixture.expected_default_head,
            "dependencies": self.dependencies,
        }
        target = folder / ("gitlab-contract-" + self.fixture.nonce + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n")
        temporary.replace(target)

    def test_issue_plan_publication_feedback_ref_lease_merge_and_ci(self):
        import test_factory as factories

        names = {
            field: self.fixture.prefix + "-" + field.replace("_", "-")
            for field in Labels.model_fields
        }
        labels = Labels(**names)
        fake = factories.FactoryTests()
        fake.setUp()
        service = fake.service
        service.repository = self.config.repository
        service.repositories = lambda _: self.client
        service.labels = labels
        if self.resume_info:
            child = self.fixture.issues[1]
            self.record_evidence("resuming-owned-publication")
        else:
            self.prepare_ci()
            for name in names.values():
                self.fixture.labels.append(name)
                self.client.ensure_label(name)
            parent = self.client.create_issue(
                title=self.fixture.prefix,
                body=self.fixture.marker,
                labels=[labels.ready_for_agent],
            )
            self.fixture.issues.append(parent["number"])
            self.record_evidence("parent-created")
            fake.planner.plan_issue.return_value = IssueWorkPlan(
                tasks=[
                    PlannedSubtask(
                        title=self.fixture.prefix + " task",
                        objective="Qualify source publication. " + self.fixture.marker,
                        acceptance_criteria=[
                            "A fake-model patch passes the live CI job."
                        ],
                    )
                ]
            )
            root = IssueInput(
                repository=self.config.repository, issue_number=parent["number"]
            )
            plan = service.plan_issue(root, service.load_issue(root))
            self.assertEqual(len(plan.sub_issue_numbers), 1)
            child = plan.sub_issue_numbers[0]
            self.fixture.issues.append(child)
            self.record_evidence("child-created")
        self.assertIn(
            child, {self.client.get_issue(iid)["number"] for iid in self.fixture.issues}
        )
        command = IssueInput(repository=self.config.repository, issue_number=child)
        path = "src/flumic_contract_" + self.fixture.nonce + ".py"
        race_path = "src/flumic_race_" + self.fixture.nonce + ".py"
        self.paths = {path: True, race_path: True}
        fake.planner.triage.return_value = TriagePlan(
            needs_discussion=False, source_paths=[path]
        )

        def patch(version):
            return PatchPlan(
                pull_title=self.fixture.prefix,
                pull_body=self.fixture.marker,
                is_complete=True,
                progress="fixture complete",
                changes=[
                    FileChange(
                        path=path,
                        content="print('" + self.fixture.nonce + "-" + version + "')\n",
                    )
                ],
            )

        def generate(*args, **kwargs):
            self.fake_coding_calls += 1
            if self.fake_coding_calls > 2:
                raise AssertionError("Known-effect recovery repeated a fake model call")
            return patch(str(self.fake_coding_calls))

        fake.agent.generate.side_effect = generate
        triage = TriageState(status="ready", issue_number=child, source_paths=[])
        branch = "hatchet/issue-" + str(child)
        if not self.resume_info:
            self.assertIsNone(
                self.client._branch_head(branch),
                "Fresh fixture branch unexpectedly exists",
            )
        else:
            self.assertEqual(
                self.client._branch_head(branch), self.fixture.branches[branch]
            )
        publish = self.client.publisher.publish

        def tracked_publish(**values):
            self.physical_publications += 1
            return publish(**values)

        self.client.publisher.publish = tracked_publish
        if not self.resume_info:
            original = self.client._request
            lost = False

            def lose_success(method, endpoint, **kwargs):
                nonlocal lost
                result = original(method, endpoint, **kwargs)
                if method == "POST" and endpoint == "/merge_requests" and not lost:
                    lost = True
                    raise RepositoryError("Simulated successful publication reply loss")
                return result

            self.client._request = lose_success
            try:
                with self.assertRaises(RepositoryError):
                    service.generate_and_open_draft_pr(command, triage)
            finally:
                self.client._request = original
                head = self.client._branch_head(branch)
                if head:
                    self.fixture.branches[branch] = head
                existing = self.client.find_issue_pull_request(child)
                if existing:
                    self.fixture.mrs.append(existing["number"])
                self.record_evidence("publication-observed")
        recovered = service.generate_and_open_draft_pr(command, triage)
        self.assertEqual(self.fake_coding_calls, 1)
        self.assertEqual(self.physical_publications, 1)
        pull = PullInput(
            repository=self.config.repository,
            issue_number=child,
            pull_number=recovered.pull_number,
            pull_url=recovered.pull_url,
            branch=branch,
        )
        service.review_opened_pull_request(command, recovered)
        self.client._request(
            "POST",
            "/merge_requests/" + str(pull.pull_number) + "/notes",
            body={
                "body": "Contract feedback "
                + self.fixture.nonce
                + ": update the fixture version."
            },
        )
        service.process_pull_request_feedback(pull)
        self.fixture.branches[branch] = self.client._branch_head(branch)
        self.record_evidence("feedback-published")
        service.process_pull_request_feedback(pull)
        self.assertEqual(self.fake_coding_calls, 2)
        self.assertEqual(self.physical_publications, 2)
        before = self.client._branch_head(branch)
        advanced = self.client._request(
            "POST",
            "/repository/commits",
            body={
                "branch": branch,
                "commit_message": self.fixture.prefix + " human-race fixture",
                "actions": [
                    {
                        "action": "create",
                        "file_path": race_path,
                        "content": self.fixture.nonce + "\n",
                    }
                ],
            },
        )["id"]
        self.fixture.branches[branch] = advanced
        self.record_evidence("ref-race-fixture-created")
        with self.assertRaises(StaleRevisionError):
            publish(
                remote="https://gitlab.com/" + self.config.repository + ".git",
                branch=branch,
                expected_head=before,
                base_commit=before,
                changes=patch("stale").changes,
                marker=self.fixture.prefix,
                credentials={"gitlab": self.config.token},
            )
        self.assertEqual(self.client._branch_head(branch), advanced)
        self.wait_checks(advanced, branch)
        self.client._request(
            "PUT",
            "/merge_requests/" + str(pull.pull_number),
            body={"title": self.fixture.prefix},
        )
        self.fixture.preflight()
        self.client._request(
            "PUT",
            "/merge_requests/" + str(pull.pull_number) + "/merge",
            body={"sha": advanced, "should_remove_source_branch": False},
        )
        self.fixture.expected_default_head = self.client.resolve_revision(
            self.config.default_branch
        )
        self.record_evidence("merge-observed")
        merged = self.client.get_pull_request(pull.pull_number)
        self.assertTrue(merged["merged"])
        self.assertTrue(merged.get("merge_commit_sha"))
        self.wait_checks(merged["merge_commit_sha"], self.config.default_branch)
        result = service.verify_merged_pr(pull)
        self.assertEqual(result.status, "verified-merged")
        self.assertEqual(self.client.get_issue(child)["state"], "closed")


class LiveGitLabCleanupVerification(unittest.TestCase):
    """Independent read-only gate after a transport interruption or audit retention."""

    def test_completed_flow_and_cleanup(self):
        inventory_path = os.environ.get("FLUMIC_GITLAB_VERIFY_CLEANUP_INVENTORY")
        if not inventory_path:
            self.skipTest("opt-in completed GitLab cleanup verification")
        config = LiveGitLabConfig.from_env(os.environ)
        raw = Path(inventory_path).read_bytes()
        if len(raw) > 65536:
            raise ValueError("Qualification inventory exceeds its size limit")
        inventory = json.loads(raw)
        writes = inventory.get("writes")
        if type(writes) is not int or not 0 <= writes <= 128:
            raise ValueError("Qualification write counter is invalid")
        with secret_scope([config.token]):
            proof = GitLabLiveFixture.verify_completed_cleanup(config, inventory)
        self.assertTrue(proof["cleanup_verified"])
        self.assertTrue(proof["baseline_tree_matches"])
        directory = os.environ.get("FLUMIC_LIVE_EVIDENCE_DIR")
        if directory:
            import platform
            import subprocess
            from importlib.metadata import version

            safe = {
                "schema_version": 1,
                "stage": "qualified-cleanup-verified",
                "project_id": config.project_id,
                "repository": config.repository,
                "fixture": inventory["fixture"],
                "writes": writes,
                "fake_coding_calls": 2,
                "physical_publications": 2,
                "approved_default_sha": config.default_sha,
                "known_default_sha": inventory["known_default_sha"],
                "issues": inventory["issues"],
                "merge_requests": inventory["merge_requests"],
                "branches": inventory["branches"],
                "labels": inventory["labels"],
                "cleanup_remaining": [],
                **proof,
                "dependencies": {
                    "python": platform.python_version(),
                    "hatchet-sdk": version("hatchet-sdk"),
                    "pydantic": version("pydantic"),
                    "git": subprocess.check_output(
                        ["git", "--version"], text=True
                    ).strip(),
                },
            }
            target = Path(directory) / (
                "gitlab-contract-"
                + inventory["fixture"].removeprefix("flumic-contract-")
                + ".json"
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps(safe, indent=2) + "\n")
            temporary.replace(target)
