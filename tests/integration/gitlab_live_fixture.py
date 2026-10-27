"""Explicit resource guards and ownership checks for opt-in real GitLab tests.

This module never reads .env, configures production workers or calls a model.
"""

from dataclasses import dataclass, field, replace
import base64
import re
import unittest
import uuid
from urllib.parse import quote

from factory.adapters.gitlab import GitLabClient, GitLabRequestError
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
        self.retained_merged_requests = []
        self.expected_default_head = config.default_sha
        self.preflight()

    @classmethod
    def resume(cls, config, inventory, *, client=None):
        """Resume only a known draft publication; never create replacement resources."""
        prefix = inventory.get("fixture", "")
        known = inventory.get("known_default_sha", "")
        if (
            inventory.get("project_id") != config.project_id
            or inventory.get("repository") != config.repository
            or inventory.get("approved_default_sha") != config.default_sha
            or not isinstance(prefix, str)
            or not re.fullmatch(r"flumic-contract-[a-f0-9]{32}", prefix)
            or not isinstance(known, str)
            or not re.fullmatch(r"[a-f0-9]{40}", known)
            or inventory.get("fake_coding_calls") != 1
            or inventory.get("physical_publications") != 1
            or inventory.get("verified_refs")
        ):
            raise ValueError(
                "Only the original approved draft-publication checkpoint can resume"
            )
        issues, mrs = inventory.get("issues", []), inventory.get("merge_requests", [])
        if (
            len(issues) != 2
            or len(set(issues)) != 2
            or len(mrs) != 1
            or any(type(value) is not int or value <= 0 for value in [*issues, *mrs])
        ):
            raise ValueError(
                "Resume requires the two original issues and one merge request"
            )
        from factory.configuration import Labels

        expected_labels = {
            prefix + "-" + name.replace("_", "-") for name in Labels.model_fields
        }
        if set(inventory.get("labels", [])) != expected_labels:
            raise ValueError("Resume label inventory is not the original owned set")
        branch = "hatchet/issue-" + str(issues[1])
        if inventory.get("branches") != [branch]:
            raise ValueError("Resume branch is not the original issue branch")
        value = cls(replace(config, default_sha=known), client=client)
        value.config = config
        value.nonce, value.prefix = prefix.removeprefix("flumic-contract-"), prefix
        value.marker = "<!-- flumic-contract:" + value.nonce + " -->"
        for iid in issues:
            issue = value.client.get_issue(iid)
            if (
                issue.get("number") != iid
                or issue.get("project_id") != config.project_id
                or value.marker not in str(issue.get("body") or "")
            ):
                raise ValueError("Resume issue ownership changed")
        pull = value.client.get_pull_request(mrs[0])
        if (
            pull.get("number") != mrs[0]
            or pull.get("state") != "open"
            or pull.get("merged")
            or pull.get("source_project_id") != config.project_id
            or pull.get("target_project_id") != config.project_id
            or value.marker not in str(pull.get("body") or "")
            or pull.get("head", {}).get("ref") != branch
        ):
            raise ValueError("Resume merge request ownership changed")
        head = value.client._branch_head(branch)
        if not head or head != pull.get("head", {}).get("sha"):
            raise ValueError("Resume branch does not match the owned change")
        expected = inventory.get("branch_heads", {}).get(branch)
        if expected:
            if head != expected:
                raise ValueError(
                    "Resume branch advanced after its recorded publication"
                )
        else:
            # Version-one inventories recorded only branch names. Recover the
            # exact original tree using parent, complete diff and file bytes;
            # a marker alone never authorizes an advanced/human head.
            from factory.domain.models import FileChange

            path = "src/flumic_contract_" + value.nonce + ".py"
            content = "print('" + value.nonce + "-1')\n"
            commit = value.client._request("GET", "/repository/commits/" + head)
            differences = value.client._pages("/repository/commits/" + head + "/diff")
            marker = GitLabClient._patch_marker(
                known, [FileChange(path=path, content=content)]
            )
            if (
                commit.get("parent_ids") != [known]
                or str(commit.get("message", "")).strip()
                != "Flumic publication\n\n" + marker
                or len(differences) != 1
                or differences[0].get("new_path") != path
                or differences[0].get("deleted_file") is True
                or value.client.get_source_files([path], branch=head) != {path: content}
            ):
                raise ValueError("Legacy resume publication tree or parent changed")
        ci = value.client._request(
            "GET",
            "/repository/files/.gitlab-ci.yml",
            query={"ref": config.default_branch},
        )
        if (
            not base64.b64decode(ci["content"])
            .decode()
            .startswith("# " + prefix + "\n")
        ):
            raise ValueError("Resume CI ownership changed")
        names = {
            item.get("name")
            for item in value.client._pages("/labels", query={"search": prefix})
        }
        if not expected_labels <= names:
            raise ValueError("Resume labels were removed during cleanup")
        value.issues, value.mrs, value.labels = (
            list(issues),
            list(mrs),
            list(inventory["labels"]),
        )
        value.branches = {branch: head}
        return value

    @classmethod
    def verify_completed_cleanup(cls, config, inventory, *, client=None):
        """Read-only proof for a completed continuation and its cleanup/audit record."""
        prefix, known = (
            inventory.get("fixture", ""),
            inventory.get("known_default_sha", ""),
        )
        issues, mrs = inventory.get("issues", []), inventory.get("merge_requests", [])
        if (
            inventory.get("project_id") != config.project_id
            or inventory.get("repository") != config.repository
            or inventory.get("approved_default_sha") != config.default_sha
            or not isinstance(prefix, str)
            or not re.fullmatch(r"flumic-contract-[a-f0-9]{32}", prefix)
            or not isinstance(known, str)
            or not re.fullmatch(r"[a-f0-9]{40}", known)
            or inventory.get("fake_coding_calls") != 2
            or inventory.get("physical_publications") != 2
            or len(issues) != 2
            or len(set(issues)) != 2
            or len(mrs) != 1
            or any(type(iid) is not int or iid <= 0 for iid in [*issues, *mrs])
        ):
            raise ValueError("Completed qualification inventory is invalid")
        branch = "hatchet/issue-" + str(issues[1])
        refs = inventory.get("verified_refs", [])
        if (
            len(refs) != 2
            or {entry.get("ref") for entry in refs} != {branch, config.default_branch}
            or any(
                not re.fullmatch(r"[a-f0-9]{40}", entry.get("sha", ""))
                for entry in refs
            )
        ):
            raise ValueError(
                "Both exact source and merged-commit CI proofs are required"
            )
        from factory.configuration import Labels

        labels = {prefix + "-" + name.replace("_", "-") for name in Labels.model_fields}
        if set(inventory.get("labels", [])) != labels or inventory.get("branches") != [
            branch
        ]:
            raise ValueError("Completed inventory differs from the owned fixture scope")
        retained = inventory.get("retained_merged_requests", [])
        if set(retained) - set(mrs):
            raise ValueError("Retained audit record is outside the owned fixture")
        value = cls(replace(config, default_sha=known), client=client)
        client = value.client
        marker = (
            "<!-- flumic-contract:" + prefix.removeprefix("flumic-contract-") + " -->"
        )
        for entry in refs:
            client.verify_commit_checks(entry["sha"], ref=entry["ref"])

        def tree(ref):
            return sorted(
                (item["path"], item["id"], item["mode"], item["type"])
                for item in client._pages(
                    "/repository/tree", query={"ref": ref, "recursive": "true"}
                )
            )

        if tree(config.default_sha) != tree(known):
            raise ValueError(
                "Cleanup did not restore the exact original repository tree"
            )
        for iid in issues:
            try:
                client.get_issue(iid)
            except GitLabRequestError as error:
                if error.status_code == 404:
                    continue
                raise
            raise ValueError("An owned issue remains after cleanup")
        audit = []
        for iid in mrs:
            try:
                item = client.get_pull_request(iid)
            except GitLabRequestError as error:
                if error.status_code == 404:
                    continue
                raise
            if (
                iid not in retained
                or item.get("number") != iid
                or item.get("merged") is not True
                or item.get("source_project_id") != config.project_id
                or item.get("target_project_id") != config.project_id
                or marker not in str(item.get("body") or "")
                or item.get("head", {}).get("ref") != branch
            ):
                raise ValueError(
                    "Only the explicitly retained owned merged audit record may remain"
                )
            audit.append(iid)
        if client._branch_head(branch) is not None:
            raise ValueError("Owned source branch remains after cleanup")
        if any(
            item.get("name") in labels
            for item in client._pages("/labels", query={"search": prefix})
        ):
            raise ValueError("Owned fixture labels remain after cleanup")
        return {
            "cleanup_verified": True,
            "baseline_tree_matches": True,
            "retained_merged_requests": audit,
            "verified_refs": [
                {"sha": entry["sha"], "ref": entry["ref"]} for entry in refs
            ],
        }

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
        previously_retained = set(self.retained_merged_requests)
        self.retained_merged_requests = []
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
                project_checked, item = False, None
                try:
                    self.assert_project()
                    project_checked = True
                    item = read(iid)
                    if self.marker not in str(item.get("body") or ""):
                        raise ValueError("ownership marker missing")
                    if (
                        kind == "merge-request"
                        and item.get("merged") is True
                        and iid in previously_retained
                    ):
                        self.retained_merged_requests.append(iid)
                        continue
                    self.client._request("DELETE", endpoint + str(iid))
                except GitLabRequestError as error:
                    if project_checked and error.status_code == 404:
                        continue
                    if (
                        project_checked
                        and error.status_code == 403
                        and kind == "merge-request"
                        and item
                        and item.get("merged") is True
                    ):
                        self.retained_merged_requests.append(iid)
                        continue
                    failures.append(kind + "/" + str(iid))
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
            project_checked = False
            try:
                self.assert_project()
                project_checked = True
                if not name.startswith(self.prefix + "-"):
                    raise ValueError("label ownership prefix missing")
                self.client._request("DELETE", "/labels/" + quote(name, safe=""))
            except GitLabRequestError as error:
                if not project_checked or error.status_code != 404:
                    failures.append("label/" + name)
            except Exception:
                failures.append("label/" + name)
        return failures
