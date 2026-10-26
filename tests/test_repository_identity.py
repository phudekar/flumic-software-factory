"""Source-host identity is explicit; historical GitHub claims remain stable."""

import json
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))
from factory.workflows.contracts import ActionContext
from factory.workflows.resources import canonical_repository, canonical_resource


class RepositoryIdentityTests(unittest.TestCase):
    def identity(self, **values):
        from factory.domain.repositories import RepositoryIdentity

        return RepositoryIdentity(**values)

    def test_github_canonical_bytes_unchanged(self):
        identity = self.identity(
            provider="github", host="github.com", owner="Owner", name="Repo"
        )
        self.assertEqual(identity.path, "Owner/Repo")
        self.assertEqual(identity.canonical_key, "github.com/owner/repo")
        self.assertEqual(identity.credential_purpose, "github")
        self.assertEqual(
            canonical_repository("github.com", "Owner", "Repo"), identity.canonical_key
        )
        self.assertEqual(
            canonical_resource("github.com", "Owner", "Repo", "issue", 7),
            "github.com/owner/repo/issue/7",
        )
        self.assertEqual(
            canonical_resource("github.com", "Owner", "Repo", "pull", 7),
            "github.com/owner/repo/issue/7",
        )

    def test_gitlab_nested_namespace_and_numeric_identity(self):
        identity = self.identity(
            provider="gitlab",
            host="gitlab.com",
            owner="Group/Subgroup",
            name="Project",
            project_id=123,
        )
        self.assertEqual(identity.path, "Group/Subgroup/Project")
        self.assertEqual(identity.canonical_key, "gitlab.com/project/123")
        self.assertEqual(identity.credential_purpose, "gitlab")
        self.assertEqual(
            canonical_repository(
                "gitlab.com", "Group/Subgroup", "Project", project_id=123
            ),
            identity.canonical_key,
        )
        renamed = self.identity(
            provider="gitlab",
            host="gitlab.com",
            owner="New",
            name="Name",
            project_id=123,
        )
        reused = self.identity(
            provider="gitlab",
            host="gitlab.com",
            owner="Group/Subgroup",
            name="Project",
            project_id=124,
        )
        self.assertEqual(renamed.canonical_key, identity.canonical_key)
        self.assertNotEqual(reused.canonical_key, identity.canonical_key)

    def test_issue_mr_iid_collision_is_not_same_claim(self):
        issue = canonical_resource(
            "gitlab.com", "g/sub", "repo", "issue", 7, project_id=123
        )
        mr = canonical_resource(
            "gitlab.com", "g/sub", "repo", "pull", 7, project_id=123
        )
        self.assertEqual(issue, "gitlab.com/project/123/issue/7")
        self.assertEqual(mr, "gitlab.com/project/123/merge-request/7")

    def test_host_provider_mismatch_and_traversal_rejected(self):
        bad = [
            {"host": "evil.com"},
            {"host": "https://gitlab.com"},
            {"host": "gitlab.com:443"},
            {"host": "token@gitlab.com"},
            {"provider": "github"},
            {"owner": "g/../sub"},
            {"owner": "g//sub"},
            {"owner": "g/%2fsub"},
            {"owner": "/group"},
            {"name": ".."},
            {"name": "r?token=secret"},
            {"project_id": None},
            {"project_id": True},
            {"project_id": 0},
            {"project_id": "123"},
            {"project_id": 1.5},
            {"project_id": 2**63},
        ]
        for changes in bad:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.identity(
                    **{
                        "provider": "gitlab",
                        "host": "gitlab.com",
                        "owner": "g/sub",
                        "name": "repo",
                        "project_id": 123,
                        **changes,
                    }
                )
        with self.assertRaises(ValueError):
            self.identity(
                provider="github", host="github.com", owner="g/sub", name="repo"
            )

    def context(self, **extra):
        return ActionContext(
            project_id="p",
            repository_id="r",
            repository_revision_id="rr",
            config_revision_id="c",
            run_id="run",
            stage_id="load",
            execution_id="e",
            attempt=1,
            assignment_id="a",
            namespace="n",
            settings_json=json.dumps({"repository": "owner/repo"}),
            **extra,
        )

    def test_old_context_defaults_to_github(self):
        context = self.context()
        self.assertEqual(context.repository_identity.provider, "github")
        self.assertEqual(
            context.repository_identity.canonical_key, "github.com/owner/repo"
        )
        self.assertEqual(
            context.repository_check_policy.required_pipeline_sources, ("push",)
        )
        self.assertNotIn("repository_identity_json", context.model_dump())
        self.assertNotIn("repository_check_policy_json", context.model_dump())
        self.assertNotIn(
            "repository_identity_json", json.loads(context.model_dump_json())
        )
        self.assertEqual(
            ActionContext.model_validate(
                {**context.model_dump(), "settings_json": context.settings_json}
            ).repository_identity,
            context.repository_identity,
        )

    def test_context_pins_identity_and_checks_without_secret_values(self):
        identity = {
            "provider": "gitlab",
            "host": "gitlab.com",
            "owner": "g/sub",
            "name": "repo",
            "project_id": 123,
        }
        context = self.context(
            repository_identity_json=json.dumps(identity),
            repository_check_policy_json=json.dumps(
                {
                    "required_pipeline_sources": ["push"],
                    "required_status_names": ["security"],
                }
            ),
        )
        self.assertEqual(context.repository_identity.credential_purpose, "gitlab")
        self.assertEqual(
            context.repository_check_policy.required_status_names, ("security",)
        )
        restored = ActionContext.model_validate(
            {**context.model_dump(), "settings_json": context.settings_json}
        )
        self.assertEqual(restored.repository_identity, context.repository_identity)
        with self.assertRaises(ValueError):
            self.context(repository_identity_json='{"provider":"evil"}')
        with self.assertRaises(ValueError):
            self.context(
                repository_check_policy_json='{"required_pipeline_sources":[]}'
            )

    def test_check_policy_is_bounded_and_explicit(self):
        from factory.domain.repositories import RepositoryCheckPolicy

        self.assertEqual(RepositoryCheckPolicy().required_pipeline_sources, ("push",))
        for values in (
            {"required_pipeline_sources": []},
            {"required_pipeline_sources": ["push", "push"]},
            {"required_pipeline_sources": ["unknown-source"]},
            {"required_status_names": [""]},
            {"required_status_names": ["a"] * 33},
            {"unknown": "value"},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                RepositoryCheckPolicy(**values)
