"""Merged-commit checks require explicit exact-SHA/ref evidence."""

from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.adapters.gitlab import GitLabClient
from factory.domain.repositories import RepositoryCheckPolicy
from factory.domain.models import RepositoryError


class GitLabCheckTests(unittest.TestCase):
    def setUp(self):
        self.client = GitLabClient("g/r", project_id=123, token="fixture")
        self.sha = "a" * 40
        self.pipeline = {
            "id": 10,
            "project_id": 123,
            "sha": self.sha,
            "ref": "main",
            "source": "push",
            "status": "success",
        }
        self.jobs = [
            {
                "id": 20,
                "name": "tests",
                "stage": "test",
                "status": "success",
                "allow_failure": False,
            }
        ]
        self.statuses = []
        self.bridges = []

        def pages(path, **kwargs):
            if path == "/pipelines":
                return [self.pipeline]
            if path == "/pipelines/10/jobs":
                return self.jobs
            if path == "/pipelines/10/trigger_jobs":
                return self.bridges
            if path.endswith("/statuses"):
                return self.statuses
            raise AssertionError(path)

        self.client._pages = Mock(side_effect=pages)
        self.client._request = Mock(return_value=self.pipeline)

    def test_exact_commit_success_with_nonoptional_job(self):
        self.client.verify_commit_checks(self.sha, ref="main")
        self.client._pages.assert_any_call(
            "/pipelines", query={"sha": self.sha, "ref": "main", "source": "push"}
        )

    def test_missing_wrong_sha_or_wrong_ref_pipeline_is_not_success(self):
        for replacement in ({"sha": "b" * 40}, {"ref": "other"}, {"source": "web"}):
            with self.subTest(replacement=replacement):
                self.pipeline.update(replacement)
                with self.assertRaises(RepositoryError):
                    self.client.verify_commit_checks(self.sha, ref="main")
                self.pipeline.update(sha=self.sha, ref="main", source="push")
        self.client._pages = Mock(return_value=[])
        with self.assertRaises(RepositoryError):
            self.client.verify_commit_checks(self.sha, ref="main")

    def test_pending_skipped_manual_cancelled_failed_and_empty_jobs_block(self):
        for state in ("pending", "running", "manual", "skipped", "canceled", "failed"):
            with self.subTest(state=state):
                self.pipeline["status"] = state
                with self.assertRaises(RepositoryError):
                    self.client.verify_commit_checks(self.sha, ref="main")
        self.pipeline["status"] = "success"
        for jobs in (
            [],
            [
                {
                    "id": 20,
                    "name": "optional",
                    "status": "success",
                    "allow_failure": True,
                }
            ],
            [{"id": 20, "name": "manual", "status": "manual", "allow_failure": False}],
        ):
            self.jobs = jobs
            with self.assertRaises(RepositoryError):
                self.client.verify_commit_checks(self.sha, ref="main")

    def test_required_external_status_and_failed_observed_context(self):
        self.client.check_policy = RepositoryCheckPolicy(
            required_status_names=["security"]
        )
        with self.assertRaises(RepositoryError):
            self.client.verify_commit_checks(self.sha, ref="main")
        self.statuses = [
            {
                "id": 1,
                "name": "security",
                "sha": self.sha,
                "ref": "main",
                "status": "success",
                "allow_failure": False,
            }
        ]
        self.client.verify_commit_checks(self.sha, ref="main")
        self.statuses.append(
            {
                "id": 2,
                "name": "other",
                "sha": self.sha,
                "ref": "main",
                "status": "failed",
                "allow_failure": False,
            }
        )
        with self.assertRaises(RepositoryError):
            self.client.verify_commit_checks(self.sha, ref="main")

    def test_latest_pipeline_and_external_status_win(self):
        old = {**self.pipeline, "id": 9, "status": "failed"}
        original = self.client._pages.side_effect
        self.client._pages.side_effect = lambda path, **kwargs: (
            [old, self.pipeline] if path == "/pipelines" else original(path, **kwargs)
        )
        self.statuses = [
            {
                "id": 1,
                "name": "security",
                "sha": self.sha,
                "ref": "main",
                "status": "failed",
            },
            {
                "id": 2,
                "name": "security",
                "sha": self.sha,
                "ref": "main",
                "status": "success",
            },
        ]
        self.client.verify_commit_checks(self.sha, ref="main")

    def test_child_pipeline_and_inaccessible_downstream_block(self):
        self.bridges = [
            {
                "id": 30,
                "status": "success",
                "downstream_pipeline": {
                    "id": 11,
                    "project_id": 123,
                    "sha": self.sha,
                    "ref": "main",
                    "status": "running",
                },
            }
        ]
        with self.assertRaises(RepositoryError):
            self.client.verify_commit_checks(self.sha, ref="main")
        self.bridges[0]["downstream_pipeline"]["project_id"] = 999
        with self.assertRaises(RepositoryError):
            self.client.verify_commit_checks(self.sha, ref="main")

    def test_missing_target_ref_cannot_fall_back_to_current_default(self):
        with self.assertRaises((RepositoryError, ValueError)):
            self.client.verify_commit_checks(self.sha)

    def test_trigger_only_parent_requires_successful_nonoptional_child_job(self):
        child = {**self.pipeline, "id": 11, "source": "parent_pipeline"}
        self.bridges = [{"id": 30, "status": "success", "downstream_pipeline": child}]
        original = self.client._pages.side_effect

        def pages(path, **kwargs):
            if path == "/pipelines/10/jobs":
                return []
            if path == "/pipelines/11/jobs":
                return self.jobs
            if path == "/pipelines/11/trigger_jobs":
                return []
            return original(path, **kwargs)

        self.client._pages.side_effect = pages
        self.client._request.side_effect = lambda method, path: (
            child if path == "/pipelines/11" else self.pipeline
        )
        self.client.verify_commit_checks(self.sha, ref="main")

    def test_optional_failures_and_old_retried_jobs_do_not_override_success(self):
        self.jobs.insert(0, {**self.jobs[0], "id": 19, "status": "failed"})
        self.jobs.append(
            {
                "id": 21,
                "name": "optional",
                "stage": "lint",
                "status": "failed",
                "allow_failure": True,
            }
        )
        self.statuses = [
            {
                "id": 1,
                "name": "optional",
                "sha": self.sha,
                "ref": "main",
                "status": "failed",
                "allow_failure": True,
            }
        ]
        self.client.verify_commit_checks(self.sha, ref="main")

    def test_every_configured_pipeline_source_is_required(self):
        self.client.check_policy = RepositoryCheckPolicy(
            required_pipeline_sources=["push", "web"]
        )
        with self.assertRaises(RepositoryError):
            self.client.verify_commit_checks(self.sha, ref="main")

    def test_successful_pipeline_can_have_conditionally_skipped_jobs(self):
        self.jobs.append(
            {
                "id": 21,
                "name": "conditional",
                "stage": "deploy",
                "status": "skipped",
                "allow_failure": False,
            }
        )
        self.client.verify_commit_checks(self.sha, ref="main")

    def test_pipeline_details_must_confirm_selected_source(self):
        details = {**self.pipeline, "source": "web"}
        self.client._request.return_value = details
        with self.assertRaises(RepositoryError):
            self.client.verify_commit_checks(self.sha, ref="main")
