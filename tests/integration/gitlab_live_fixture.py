"""Explicit resource guards and ownership checks for opt-in real GitLab tests.

This module never reads .env, configures production workers or calls a model.
"""

from dataclasses import dataclass, field
import re
import unittest
import uuid
from urllib.parse import quote

from factory.adapters.gitlab import GitLabClient
from factory.domain.repositories import RepositoryIdentity


@dataclass(frozen=True)
class LiveGitLabConfig:
    repository: str
    project_id: int
    default_branch: str
    default_sha: str
    token: str = field(repr=False)

    @classmethod
    def from_env(cls, env):
        if env.get("FLUMIC_LIVE_GITLAB") != "1":
            raise unittest.SkipTest("opt-in live GitLab.com contract")
        required = (
            "FLUMIC_GITLAB_ALLOW_WRITES",
            "FLUMIC_GITLAB_ALLOW_DEFAULT_BRANCH_CHANGES",
            "FLUMIC_GITLAB_PROJECT_PATH",
            "FLUMIC_GITLAB_PROJECT_ID",
            "FLUMIC_GITLAB_DEFAULT_BRANCH",
            "FLUMIC_GITLAB_DEFAULT_SHA",
            "GITLAB_TOKEN",
        )
        if any(not env.get(name) for name in required):
            raise ValueError(
                "Live GitLab configuration is incomplete; see docs/gitlab.md"
            )
        if (
            env["FLUMIC_GITLAB_ALLOW_WRITES"] != "1"
            or env["FLUMIC_GITLAB_ALLOW_DEFAULT_BRANCH_CHANGES"] != "1"
        ):
            raise ValueError("Live GitLab requires both explicit mutation approvals")
        number = env["FLUMIC_GITLAB_PROJECT_ID"]
        if not re.fullmatch(r"[1-9][0-9]{0,18}", number):
            raise ValueError("Live GitLab requires a positive numeric project ID")
        owner, _, name = env["FLUMIC_GITLAB_PROJECT_PATH"].rpartition("/")
        try:
            identity = RepositoryIdentity(
                provider="gitlab",
                host="gitlab.com",
                owner=owner,
                name=name,
                project_id=int(number),
            )
        except ValueError:
            raise ValueError(
                "Live GitLab requires a namespace/project path on GitLab.com"
            ) from None
        branch, sha = (
            env["FLUMIC_GITLAB_DEFAULT_BRANCH"],
            env["FLUMIC_GITLAB_DEFAULT_SHA"],
        )
        if not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", branch
        ) or not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", sha):
            raise ValueError(
                "Pin the exact default branch name and full commit SHA before live qualification"
            )
        token = env["GITLAB_TOKEN"]
        if (
            not token
            or len(token) > 16384
            or any(ord(char) < 33 or ord(char) == 127 for char in token)
        ):
            raise ValueError("Live GitLab source credential is invalid")
        return cls(identity.path, identity.project_id, branch, sha, token)


class GitLabLiveFixture:
    def __init__(self, config, *, client=None):
        self.config = config
        self.client = client or GitLabClient(
            config.repository, project_id=config.project_id, token=config.token
        )
        self.nonce = uuid.uuid4().hex
        self.prefix = "flumic-contract-" + self.nonce
        self.marker = "<!-- flumic-contract:" + self.nonce + " -->"
        self.issues, self.mrs, self.labels = [], [], []
        self.branches = {}
        self.expected_default_head = config.default_sha
        self.preflight()

    def assert_project(self):
        value = self.client._request("GET", "")
        if (
            type(value.get("id")) is not int
            or value["id"] != self.config.project_id
            or value.get("path_with_namespace") != self.config.repository
            or value.get("default_branch") != self.config.default_branch
        ):
            raise ValueError(
                "Live GitLab project identity/default branch changed; no writes permitted"
            )
        return value

    def preflight(self):
        self.assert_project()
        if (
            self.client.resolve_revision(self.config.default_branch)
            != self.expected_default_head
        ):
            raise ValueError(
                "Live GitLab default head differs from its approved pin; no writes permitted"
            )

    def cleanup(self):
        """Best effort per resource, fail closed on ownership/head changes.

        Returns safe inventory labels, never vendor exceptions or source bodies.
        Default-branch file restoration is a separate guarded commit, not a ref
        reset, and must be completed by the live contract before this cleanup.
        """
        failures = []
        for kind, values, read, endpoint in (
            (
                "merge-request",
                self.mrs,
                self.client.get_pull_request,
                "/merge_requests/",
            ),
            ("issue", self.issues, self.client.get_issue, "/issues/"),
        ):
            for iid in reversed(values):
                try:
                    self.assert_project()
                    item = read(iid)
                    if self.marker not in str(item.get("body") or ""):
                        raise ValueError("ownership marker missing")
                    self.client._request("DELETE", endpoint + str(iid))
                except Exception:
                    failures.append(kind + "/" + str(iid))
        for branch, expected in self.branches.items():
            try:
                owned = branch.startswith(self.prefix) or branch in {
                    "hatchet/issue-" + str(iid) for iid in self.issues
                }
                if branch == self.config.default_branch or not owned:
                    raise ValueError("branch is not an owned disposable ref")
                self.assert_project()
                current = self.client._branch_head(branch)
                if current is None:
                    continue
                if current != expected:
                    raise ValueError("branch advanced or disappeared")
                self.client._request(
                    "DELETE", "/repository/branches/" + quote(branch, safe="")
                )
            except Exception:
                failures.append("branch/" + branch)
        for name in self.labels:
            try:
                self.assert_project()
                if not name.startswith(self.prefix + "-"):
                    raise ValueError("label ownership prefix missing")
                self.client._request("DELETE", "/labels/" + quote(name, safe=""))
            except Exception:
                failures.append("label/" + name)
        return failures
