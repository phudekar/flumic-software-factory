from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from factory.domain.models import RepositoryError, StaleRevisionError

from factory.domain.safety import (
    MAX_SOURCE_FILE_BYTES,
    assert_safe_text,
    is_allowed_source_path,
    validate_source_inventory,
)

_API_ROOT = "https://api.github.com"
MAX_REPOSITORY_ARCHIVE_BYTES = 128 * 1024 * 1024
logger = logging.getLogger("issue-worker.github")

GitHubError = RepositoryError


class GitHubClient:
    max_file_bytes = MAX_SOURCE_FILE_BYTES

    def __init__(
        self,
        repository: str,
        *,
        max_file_bytes: int = MAX_SOURCE_FILE_BYTES,
        token: str | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("GITHUB_REPOSITORY must be in owner/name form")
        token = os.environ.get("GITHUB_TOKEN") if token is None else token
        if not token:
            raise GitHubError("GITHUB_TOKEN is required")
        self.max_file_bytes = max_file_bytes
        self.repository = repository
        self._token = token
        self._base = f"{_API_ROOT}/repos/{repository}"

    def get_issue(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/issues/{number}")

    def get_issue_comments(self, number: int) -> list[str]:
        comments = self._request(
            "GET",
            f"/issues/{number}/comments",
            query={"per_page": "50", "sort": "updated", "direction": "desc"},
        )
        if not isinstance(comments, list):
            raise GitHubError("GitHub returned an invalid issue-comment list")
        return [
            str(comment.get("body") or "")
            for comment in reversed(comments)
            if comment.get("body")
        ]

    def list_open_issues(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for page in range(1, 6):
            items = self._request(
                "GET",
                "/issues",
                query={
                    "state": "open",
                    "per_page": "100",
                    "page": str(page),
                    "sort": "created",
                    "direction": "asc",
                },
            )
            if not isinstance(items, list):
                raise GitHubError("GitHub returned an invalid issue list")
            result.extend(item for item in items if "pull_request" not in item)
            if len(items) < 100:
                break
        return result

    def find_issues_by_markers(self, markers: set[str]) -> dict[str, dict[str, int]]:
        found: dict[str, dict[str, int]] = {}
        for page in range(1, 11):
            items = self._request(
                "GET",
                "/issues",
                query={
                    "state": "all",
                    "per_page": "100",
                    "page": str(page),
                    "sort": "created",
                    "direction": "desc",
                },
            )
            if not isinstance(items, list):
                raise GitHubError("GitHub returned an invalid issue list")
            for item in items:
                if "pull_request" in item:
                    continue
                body = str(item.get("body") or "")
                for marker in markers - found.keys():
                    if marker in body:
                        number = item.get("number")
                        issue_id = item.get("id")
                        if not isinstance(number, int) or not isinstance(issue_id, int):
                            raise GitHubError(
                                "GitHub returned a planned issue without numeric IDs"
                            )
                        found[marker] = {"number": number, "id": issue_id}
            if markers <= found.keys() or len(items) < 100:
                return found
        if markers - found.keys():
            raise GitHubError("Could not safely check for existing planned sub-issues")
        return found

    def create_issue(
        self, *, title: str, body: str, labels: list[str]
    ) -> dict[str, Any]:
        assert_safe_text(title, body)
        issue = self._request(
            "POST",
            "/issues",
            body={"title": title, "body": body, "labels": labels},
        )
        if (
            not isinstance(issue, dict)
            or not isinstance(issue.get("number"), int)
            or not isinstance(issue.get("id"), int)
        ):
            raise GitHubError("GitHub returned an invalid created-issue response")
        return issue

    def list_sub_issue_ids(self, parent_number: int) -> set[int]:
        result = self._request(
            "GET", f"/issues/{parent_number}/sub_issues", query={"per_page": "100"}
        )
        if not isinstance(result, list):
            raise GitHubError("GitHub returned an invalid sub-issue list")
        issue_ids = [item.get("id") for item in result]
        if any(not isinstance(issue_id, int) for issue_id in issue_ids):
            raise GitHubError("GitHub returned a sub-issue without a numeric ID")
        return set(issue_ids)

    def add_sub_issue(self, parent_number: int, sub_issue_id: int) -> None:
        self._request(
            "POST",
            f"/issues/{parent_number}/sub_issues",
            body={"sub_issue_id": sub_issue_id},
        )

    def list_source_paths(self, branch: str | None = None) -> list[str]:
        entries = (
            self._get_branch_tree(branch) if branch else self._get_default_branch_tree()
        )
        paths = [
            entry["path"]
            for entry in entries[:20_000]
            if entry.get("type") == "blob"
            and isinstance(entry.get("path"), str)
            and is_allowed_source_path(entry["path"])
            and isinstance(entry.get("size"), int)
            and entry["size"] <= self.max_file_bytes
        ]
        return validate_source_inventory(paths)

    def list_repository_paths(self, branch: str | None = None) -> set[str]:
        return {
            entry["path"]
            for entry in (
                self._get_branch_tree(branch)
                if branch
                else self._get_default_branch_tree()
            )
            if entry.get("type") == "blob" and isinstance(entry.get("path"), str)
        }

    def get_repository_archive(self, ref: str | None = None) -> bytes:
        if ref is None:
            repository = self._request("GET", "")
            ref = repository.get("default_branch")
            if not isinstance(ref, str) or not ref:
                raise GitHubError("Repository has no configured default branch")
        encoded_ref = urllib.parse.quote(ref, safe="")
        request = urllib.request.Request(
            f"{self._base}/tarball/{encoded_ref}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "hatchet-issue-agent",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read(MAX_REPOSITORY_ARCHIVE_BYTES + 1)
        except urllib.error.HTTPError as error:
            raise GitHubError(
                f"GitHub repository archive request failed with HTTP {error.code}"
            ) from None
        except urllib.error.URLError:
            raise GitHubError(
                "GitHub repository archive request failed due to a network error"
            ) from None
        if len(raw) > MAX_REPOSITORY_ARCHIVE_BYTES:
            raise GitHubError("GitHub repository archive exceeds the size limit")
        if not raw:
            raise GitHubError("GitHub returned an empty repository archive")
        return raw

    def resolve_revision(self, ref: str | None = None) -> str:
        if ref is None:
            repository = self._request("GET", "")
            ref = repository.get("default_branch")
            if not isinstance(ref, str) or not ref:
                raise GitHubError("Repository has no configured default branch")
        if re.fullmatch(r"[0-9a-f]{40}", ref):
            return ref
        commit = self._request("GET", f"/commits/{urllib.parse.quote(ref, safe='')}")
        sha = commit.get("sha")
        if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
            raise GitHubError("Could not resolve repository revision")
        return sha

    def _get_branch_tree(self, branch: str) -> list[dict[str, Any]]:
        sha = self.resolve_revision(branch)
        tree = self._request(
            "GET",
            f"/git/trees/{sha}",
            query={"recursive": "1"},
        )
        if tree.get("truncated"):
            raise GitHubError(
                "Repository tree is too large to validate safe file paths"
            )
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise GitHubError("GitHub returned an invalid repository tree")
        return entries

    def _get_default_branch_tree(self) -> list[dict[str, Any]]:
        repository = self._request("GET", "")
        branch = repository.get("default_branch")
        if not isinstance(branch, str):
            raise GitHubError("Repository has no configured default branch")
        encoded_branch = urllib.parse.quote(branch, safe="")
        commit = self._request("GET", f"/branches/{encoded_branch}")
        sha = commit.get("commit", {}).get("sha")
        if not isinstance(sha, str):
            raise GitHubError("Could not determine default-branch commit")
        tree = self._request(
            "GET",
            f"/git/trees/{sha}",
            query={"recursive": "1"},
        )
        if tree.get("truncated"):
            raise GitHubError(
                "Repository tree is too large to validate safe file paths"
            )
        entries = tree.get("tree")
        if not isinstance(entries, list):
            raise GitHubError("GitHub returned an invalid repository tree")
        return entries

    def get_source_files(
        self, paths: list[str], branch: str | None = None
    ) -> dict[str, str]:
        if branch:
            entries_data = self._get_branch_tree(branch)
        else:
            entries_data = self._get_default_branch_tree()
        entries = {
            entry["path"]: entry["sha"]
            for entry in entries_data
            if entry.get("type") == "blob"
        }
        files: dict[str, str] = {}
        for path in paths:
            if path not in entries:
                raise GitHubError("Selected source path no longer exists")
            blob = self._request("GET", f"/git/blobs/{entries[path]}")
            content = base64.b64decode(blob["content"], validate=False)
            logger.info(
                "Received repository file: path=%s size_bytes=%d max_file_bytes=%d",
                path,
                len(content),
                self.max_file_bytes,
            )
            if len(content) > self.max_file_bytes:
                raise GitHubError(
                    f"Selected source file exceeds the size limit: {path} ({len(content)} bytes; maximum {self.max_file_bytes} bytes)"
                )
            try:
                decoded = content.decode("utf-8")
            except UnicodeDecodeError:
                logger.warning("Skipped selected file that is not UTF-8 text")
                continue
            files[path] = decoded
        return files

    def list_open_pull_requests(self) -> list[dict[str, Any]]:
        pulls: list[dict[str, Any]] = []
        for page in range(1, 6):
            items = self._request(
                "GET",
                "/pulls",
                query={"state": "open", "per_page": "100", "page": str(page)},
            )
            if not isinstance(items, list):
                raise GitHubError("GitHub returned an invalid pull-request list")
            pulls.extend(items)
            if len(items) < 100:
                break
        return pulls

    def get_pull_comments(self, number):
        return self.get_issue_comments(number)

    def comment_pull_once(self, number, *, marker, comment):
        return self.comment_once(number, marker=marker, comment=comment)

    def add_pull_label(self, number, label):
        return self.add_label(number, label)

    def get_pull_feedback(self, pull_number: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        endpoints = (
            (f"/issues/{pull_number}/comments", "conversation"),
            (f"/pulls/{pull_number}/comments", "inline review"),
            (f"/pulls/{pull_number}/reviews", "review"),
        )
        for path, source in endpoints:
            for page in range(1, 11):
                response = self._request(
                    "GET",
                    path,
                    query={"per_page": "100", "page": str(page)},
                )
                if not isinstance(response, list):
                    raise GitHubError(
                        "GitHub returned an invalid pull-request feedback list"
                    )
                for item in response:
                    body = item.get("body")
                    comment_id = item.get("id")
                    if not isinstance(body, str) or not body.strip():
                        continue
                    if not isinstance(comment_id, int):
                        raise GitHubError(
                            "GitHub returned feedback without a numeric ID"
                        )
                    items.append(
                        {
                            "id": comment_id,
                            "source": source,
                            "body": body,
                            "path": item.get("path"),
                            "line": item.get("line") or item.get("original_line"),
                            "author": (item.get("user") or {}).get("login"),
                            "created_at": item.get("created_at"),
                        }
                    )
                if len(response) < 100:
                    break
        return sorted(items, key=lambda item: item["id"])

    def pull_feedback_cursor(self, pull_number: int, branch: str) -> dict[str, int]:
        marker = re.compile(r"<!-- hatchet-pr-feedback-cursor:([^ >]+) -->")
        cursor = {"conversation": 0, "inline review": 0, "review": 0}
        comments = self._list_issue_comments(pull_number)
        for comment in comments:
            body = str(comment.get("body") or "")
            match = marker.search(body)
            if match:
                _merge_feedback_cursor(cursor, _parse_feedback_cursor(match.group(1)))
        branch_sha = self._get_branch_sha(branch, allow_missing=False)
        if branch_sha is not None:
            commit = self._request("GET", f"/git/commits/{branch_sha}")
            match = marker.search(str(commit.get("message") or ""))
            if match:
                _merge_feedback_cursor(cursor, _parse_feedback_cursor(match.group(1)))
        return cursor

    def record_pull_feedback(
        self, pull_number: int, cursor: dict[str, int], summary: str
    ) -> None:
        assert_safe_text(summary)
        marker = (
            f"<!-- hatchet-pr-feedback-cursor:{_format_feedback_cursor(cursor)} -->"
        )
        comments = self._list_issue_comments(pull_number)
        for comment in comments:
            body = str(comment.get("body") or "")
            if re.search(r"<!-- hatchet-pr-feedback-cursor:[^ >]+ -->", body):
                self._request(
                    "PATCH",
                    f"/issues/comments/{comment['id']}",
                    body={"body": f"{summary.rstrip()}\n\n{marker}"},
                )
                return
        self._request(
            "POST",
            f"/issues/{pull_number}/comments",
            body={"body": f"{summary.rstrip()}\n\n{marker}"},
        )

    def _list_issue_comments(self, number: int) -> list[dict[str, Any]]:
        comments: list[dict[str, Any]] = []
        for page in range(1, 11):
            items = self._request(
                "GET",
                f"/issues/{number}/comments",
                query={
                    "per_page": "100",
                    "page": str(page),
                    "sort": "updated",
                    "direction": "desc",
                },
            )
            if not isinstance(items, list):
                raise GitHubError("GitHub returned an invalid issue-comment list")
            comments.extend(items)
            if len(items) < 100:
                break
        return comments

    def ensure_label(self, label: str) -> None:
        try:
            self._request(
                "GET",
                f"/labels/{urllib.parse.quote(label, safe='')}",
            )
        except GitHubError as error:
            if "HTTP 404" not in str(error):
                raise
            colors = {
                "needs-discussion": "d93f0b",
                "ready-for-agent": "0e8a16",
                "agent-pr-open": "5319e7",
                "agent-plan-created": "1d76db",
                "agent-subtask": "fbca04",
                "agent-pr-reviewed": "0e8a16",
            }
            self._request(
                "POST",
                "/labels",
                body={
                    "name": label,
                    "color": colors.get(label, "ededed"),
                    "description": "Managed by the Hatchet issue agent",
                },
            )

    def add_label(self, number: int, label: str) -> None:
        self._request(
            "POST",
            f"/issues/{number}/labels",
            body={"labels": [label]},
        )

    def remove_label(self, number: int, label: str) -> None:
        if not label:
            return
        try:
            self._request(
                "DELETE",
                f"/issues/{number}/labels/{urllib.parse.quote(label, safe='')}",
            )
        except GitHubError as error:
            if "HTTP 404" not in str(error):
                raise

    def comment_once(self, number: int, *, marker: str, comment: str) -> None:
        assert_safe_text(comment)
        comments = self.get_issue_comments(number)
        if any(marker in body for body in comments):
            return
        self._request(
            "POST",
            f"/issues/{number}/comments",
            body={"body": f"{comment}\n\n{marker}"},
        )

    def find_issue_pull_request(self, issue_number: int) -> dict[str, Any] | None:
        pulls = self._request(
            "GET", "/pulls", query={"state": "open", "per_page": "100"}
        )
        if not isinstance(pulls, list):
            raise GitHubError("GitHub returned an invalid pull-request list")
        marker = f"Closes #{issue_number}"
        for pull in pulls:
            body = str(pull.get("body") or "")
            if marker in body:
                return pull
        return None

    def create_draft_pull_request(
        self,
        *,
        issue_number: int,
        title: str,
        body: str,
        branch: str,
        changes: list[Any],
        expected_base_sha: str | None = None,
    ) -> dict[str, Any]:
        assert_safe_text(title, body, *(change.content for change in changes))
        repository = self._request("GET", "")
        default_branch = str(repository["default_branch"])
        branch_sha = self._get_branch_sha(branch, allow_missing=True)
        base_sha = branch_sha
        if base_sha is None:
            default_data = self._request(
                "GET",
                f"/branches/{urllib.parse.quote(default_branch, safe='')}",
            )
            base_sha = str(default_data["commit"]["sha"])
        marker = None
        recovered = False
        if expected_base_sha is not None:
            fingerprint = hashlib.sha256(
                json.dumps(
                    {
                        "base": expected_base_sha,
                        "changes": [
                            {"path": c.path, "content": c.content} for c in changes
                        ],
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            marker = f"<!-- factory-patch:{fingerprint} -->"
            if branch_sha and branch_sha != expected_base_sha:
                commit = self._request("GET", f"/git/commits/{branch_sha}")
                # Recover a branch commit that succeeded before PR creation failed.
                recovered = marker in str(commit.get("message") or "")
            if not recovered and base_sha != expected_base_sha:
                raise StaleRevisionError(
                    "Repository revision changed during generation"
                )
        if not recovered:
            self._commit_branch_changes(
                branch=branch,
                base_sha=base_sha,
                issue_number=issue_number,
                changes=changes,
                message=f"Propose changes for issue #{issue_number}\n\n{marker}"
                if marker
                else None,
            )
        pr_body = f"{body.rstrip()}\n\nCloses #{issue_number}"
        return self._request(
            "POST",
            "/pulls",
            body={
                "title": title,
                "body": pr_body,
                "head": branch,
                "base": default_branch,
                "draft": True,
            },
        )

    def update_pull_request_branch(
        self,
        *,
        branch: str,
        issue_number: int,
        feedback_cursor: dict[str, int],
        changes: list[Any],
        expected_base_sha: str | None = None,
    ) -> str:
        base_sha = self._get_branch_sha(branch, allow_missing=False)
        if base_sha is None:
            raise GitHubError("Pull-request branch no longer exists")
        if expected_base_sha is not None and base_sha != expected_base_sha:
            raise StaleRevisionError("Pull-request branch changed during generation")
        return self._commit_branch_changes(
            branch=branch,
            base_sha=base_sha,
            issue_number=issue_number,
            message=(
                f"Address pull-request feedback for issue #{issue_number}\n\n"
                f"<!-- hatchet-pr-feedback-cursor:"
                f"{_format_feedback_cursor(feedback_cursor)} -->"
            ),
            changes=changes,
        )

    def _get_branch_sha(self, branch: str, *, allow_missing: bool) -> str | None:
        encoded_branch = urllib.parse.quote(branch, safe="")
        try:
            existing_ref = self._request("GET", f"/git/ref/heads/{encoded_branch}")
        except GitHubError as error:
            if not allow_missing or "HTTP 404" not in str(error):
                raise
            return None
        sha = existing_ref.get("object", {}).get("sha")
        if not isinstance(sha, str):
            raise GitHubError("GitHub returned an invalid branch reference")
        return sha

    def _commit_branch_changes(
        self,
        *,
        branch: str,
        base_sha: str,
        issue_number: int,
        message: str | None = None,
        changes: list[Any],
    ) -> str:
        assert_safe_text(*(change.content for change in changes))
        encoded_branch = urllib.parse.quote(branch, safe="")
        base_commit = self._request("GET", f"/git/commits/{base_sha}")
        base_tree = str(base_commit["tree"]["sha"])
        tree_items = []
        for change in changes:
            blob = self._request(
                "POST",
                "/git/blobs",
                body={
                    "content": base64.b64encode(change.content.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "encoding": "base64",
                },
            )
            tree_items.append(
                {
                    "path": change.path,
                    "mode": "100644",
                    "type": "blob",
                    "sha": blob["sha"],
                }
            )
        tree = self._request(
            "POST",
            "/git/trees",
            body={"base_tree": base_tree, "tree": tree_items},
        )
        commit = self._request(
            "POST",
            "/git/commits",
            body={
                "message": message or f"Propose changes for issue #{issue_number}",
                "tree": tree["sha"],
                "parents": [base_sha],
            },
        )
        branch_sha = self._get_branch_sha(branch, allow_missing=True)
        if branch_sha is None:
            self._request(
                "POST",
                "/git/refs",
                body={"ref": f"refs/heads/{branch}", "sha": commit["sha"]},
            )
        else:
            if branch_sha != base_sha:
                raise StaleRevisionError("Branch changed before publication")
            self._request(
                "PATCH",
                f"/git/refs/heads/{encoded_branch}",
                body={"sha": commit["sha"], "force": False},
            )
        return str(commit["sha"])

    def get_pull_request(self, number: int) -> dict[str, Any]:
        return self._request("GET", f"/pulls/{number}")

    def get_pull_request_files(self, number: int) -> list[dict[str, str]]:
        files: list[dict[str, str]] = []
        for page in range(1, 4):
            response = self._request(
                "GET",
                f"/pulls/{number}/files",
                query={"per_page": "100", "page": str(page)},
            )
            if not isinstance(response, list):
                raise GitHubError("GitHub returned an invalid pull-request file list")
            for item in response:
                path = item.get("filename")
                patch = item.get("patch")
                status = item.get("status")
                if (
                    not isinstance(path, str)
                    or not isinstance(patch, str)
                    or not isinstance(status, str)
                ):
                    raise GitHubError(
                        "Pull-request diff contains a file without a text patch"
                    )
                files.append({"path": path, "patch": patch, "status": status})
            if len(response) < 100:
                break
        if len(files) == 300:
            raise GitHubError("Pull request exceeds the supported review diff size")
        return files

    def verify_commit_checks(self, sha: str, *, ref: str | None = None) -> None:
        checks = self._request(
            "GET",
            f"/commits/{sha}/check-runs",
            query={"per_page": "100"},
        )
        runs = checks.get("check_runs", [])
        if any(
            run.get("status") != "completed" or run.get("conclusion") != "success"
            for run in runs
        ):
            raise GitHubError("One or more merge-commit checks have not succeeded")
        status = self._request("GET", f"/commits/{sha}/status")
        if status.get("state") not in {"success", "pending"}:
            raise GitHubError("Merge commit has a failing combined status")
        if status.get("state") == "pending":
            raise GitHubError("Merge commit checks are still pending")

    def close_issue(self, number: int) -> None:
        self._request("PATCH", f"/issues/{number}", body={"state": "closed"})

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self._base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "hatchet-issue-agent",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            raise GitHubError(
                f"GitHub API request failed with HTTP {error.code}"
            ) from None
        except urllib.error.URLError:
            raise GitHubError(
                "GitHub API request failed due to a network error"
            ) from None
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise GitHubError("GitHub returned an invalid JSON response") from None


def _parse_feedback_cursor(value: str) -> dict[str, int]:
    cursor: dict[str, int] = {}
    for entry in value.split(";"):
        source, separator, raw_id = entry.partition("=")
        if separator and raw_id.isdecimal():
            cursor[source.replace("-", " ")] = int(raw_id)
    return cursor


def _format_feedback_cursor(cursor: dict[str, int]) -> str:
    return ";".join(
        f"{source.replace(' ', '-')}={cursor.get(source, 0)}"
        for source in ("conversation", "inline review", "review")
    )


def _merge_feedback_cursor(target: dict[str, int], source: dict[str, int]) -> None:
    for kind, comment_id in source.items():
        if kind in target:
            target[kind] = max(target[kind], comment_id)
