"""Explicit GitLab.com REST boundary; wire IDs never masquerade as issue IIDs."""

import base64
import binascii
import json
import re
import hashlib
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener

from factory.domain.models import RepositoryError
from factory.domain.repositories import RepositoryIdentity, RepositoryCheckPolicy
from factory.domain.safety import (
    MAX_SOURCE_FILE_BYTES,
    assert_safe_text,
    is_allowed_source_path,
    is_safe_repository_path,
    validate_source_inventory,
)


class GitLabRequestError(RepositoryError):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"GitLab request failed (HTTP {status_code})")


MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024


class NoGitLabRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitLabClient:
    def __init__(
        self,
        repository,
        *,
        project_id,
        token,
        max_file_bytes=MAX_SOURCE_FILE_BYTES,
        check_policy=None,
    ):
        owner, _, name = repository.rpartition("/")
        self.identity = RepositoryIdentity(
            provider="gitlab",
            host="gitlab.com",
            owner=owner,
            name=name,
            project_id=project_id,
        )
        if (
            not isinstance(token, str)
            or not token
            or any(ord(char) < 32 for char in token)
        ):
            raise ValueError("Explicit GitLab credential required")
        if (
            type(max_file_bytes) is not int
            or not 0 < max_file_bytes <= MAX_RESPONSE_BYTES
        ):
            raise ValueError("Invalid source file byte limit")
        self.repository = self.identity.path
        self.project_id = project_id
        self._token = token
        self.max_file_bytes = max_file_bytes
        self.check_policy = check_policy or RepositoryCheckPolicy()

    def _request(self, method, path, *, body=None, query=None, raw=False):
        if (
            (path and not path.startswith("/"))
            or path.startswith("//")
            or "?" in path
            or "#" in path
        ):
            raise ValueError("Invalid GitLab API path")
        url = f"https://gitlab.com/api/v4/projects/{self.project_id}" + path
        if query:
            url += "?" + urlencode(query)
        data = None
        headers = {"PRIVATE-TOKEN": self._token, "Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, allow_nan=False).encode()
            headers["Content-Type"] = "application/json"
        request = Request(url, data=data, headers=headers, method=method)
        limit = MAX_ARCHIVE_BYTES if raw else MAX_RESPONSE_BYTES
        try:
            with build_opener(NoGitLabRedirects()).open(
                request, timeout=30
            ) as response:
                payload = response.read(limit + 1)
        except HTTPError as error:
            status = error.code
            error.close()
            raise GitLabRequestError(status) from None
        except (URLError, TimeoutError, OSError):
            raise RepositoryError(
                "GitLab request failed; no response body retained"
            ) from None
        if len(payload) > limit:
            raise RepositoryError("GitLab response exceeded byte limit")
        if raw:
            return payload
        if not payload:
            return None
        try:
            return json.loads(payload)
        except (UnicodeDecodeError, ValueError):
            raise RepositoryError("GitLab returned invalid JSON") from None

    def _pages(self, path, *, query=None):
        result = []
        for page in range(1, 101):
            values = self._request(
                "GET", path, query={**(query or {}), "page": page, "per_page": 100}
            )
            if not isinstance(values, list) or len(values) > 100:
                raise RepositoryError("Invalid GitLab page")
            result.extend(values)
            if len(values) < 100:
                return result
        raise RepositoryError("GitLab pagination limit reached; results incomplete")

    @staticmethod
    def _number(number):
        if type(number) is not int or number < 1:
            raise ValueError("Expected a positive GitLab IID")
        return str(number)

    @staticmethod
    def _issue(value):
        if (
            value.get("state") not in {"opened", "closed"}
            or type(value.get("iid")) is not int
            or value["iid"] < 1
        ):
            raise RepositoryError("Invalid GitLab issue identity or state")
        return {
            **value,
            "number": value["iid"],
            "body": value.get("description") or "",
            "html_url": value.get("web_url"),
            "state": "open" if value["state"] == "opened" else "closed",
            "labels": [{"name": label} for label in value.get("labels", [])],
        }

    @staticmethod
    def _change(value):
        if (
            value.get("state") not in {"opened", "closed", "merged"}
            or type(value.get("iid")) is not int
            or value["iid"] < 1
        ):
            raise RepositoryError("Invalid GitLab change identity or state")
        return {
            **value,
            "number": value["iid"],
            "body": value.get("description") or "",
            "html_url": value.get("web_url"),
            "state": "open" if value["state"] == "opened" else "closed",
            "merged": value["state"] == "merged",
            "labels": [{"name": label} for label in value.get("labels", [])],
            "draft": bool(value.get("draft", False)),
            "head": {"sha": value.get("sha"), "ref": value.get("source_branch")},
            "base": {"ref": value.get("target_branch")},
            "merge_commit_sha": value.get("merge_commit_sha")
            or value.get("squash_commit_sha"),
        }

    def get_issue(self, number):
        return self._issue(self._request("GET", "/issues/" + self._number(number)))

    def get_issue_comments(self, number):
        return [
            str(note.get("body") or "")
            for note in self._pages("/issues/" + self._number(number) + "/notes")
            if not note.get("system")
        ]

    def list_open_issues(self):
        return [
            self._issue(value)
            for value in self._pages("/issues", query={"state": "opened"})
        ]

    def list_open_pull_requests(self):
        return [
            self._change(value)
            for value in self._pages("/merge_requests", query={"state": "opened"})
        ]

    def get_pull_request(self, number):
        return self._change(
            self._request("GET", "/merge_requests/" + self._number(number))
        )

    def get_source_files(self, paths, branch=None):
        ref = branch or self.resolve_revision()
        result = {}
        for path in paths:
            if not is_safe_repository_path(path):
                raise ValueError("Invalid source path")
            item = self._request(
                "GET", "/repository/files/" + quote(path, safe=""), query={"ref": ref}
            )
            try:
                if item.get("encoding") != "base64":
                    raise ValueError()
                content = base64.b64decode(item["content"], validate=True)
                if len(content) > self.max_file_bytes:
                    raise ValueError()
                result[path] = content.decode("utf-8")
            except (ValueError, KeyError, UnicodeDecodeError, binascii.Error):
                raise RepositoryError(
                    "GitLab source file is invalid or exceeds byte limit"
                ) from None
        return result

    def list_repository_paths(self, branch=None):
        values = self._pages(
            "/repository/tree",
            query={"ref": branch or self.resolve_revision(), "recursive": "true"},
        )
        paths = {item["path"] for item in values if item.get("type") == "blob"}
        if any(not is_safe_repository_path(path) for path in paths):
            raise RepositoryError("GitLab inventory contains invalid paths")
        return paths

    def list_source_paths(self, branch=None):
        return validate_source_inventory(
            sorted(
                path
                for path in self.list_repository_paths(branch)
                if is_allowed_source_path(path)
            )
        )

    def resolve_revision(self, ref=None):
        if ref is None:
            # A trailing slash addresses the project itself without permitting
            # an arbitrary root/path in the transport boundary.
            project = self._request("GET", "")
            if project.get("id") != self.project_id:
                raise RepositoryError("GitLab project identity mismatch")
            ref = project.get("default_branch")
        if not isinstance(ref, str) or not ref:
            raise RepositoryError("GitLab repository has no revision")
        value = self._request("GET", "/repository/commits/" + quote(ref, safe=""))
        return str(value["id"])

    def list_sub_issue_ids(self, parent_number):
        marker = (
            f"<!-- factory-parent:{self.project_id}:{self._number(parent_number)} -->"
        )
        return {
            value["id"]
            for value in self._pages(
                "/issues/" + self._number(parent_number) + "/links"
            )
            if marker in str(value.get("description") or "")
        }

    def get_pull_request_files(self, number):
        values = self._pages("/merge_requests/" + self._number(number) + "/diffs")
        result = []
        for value in values:
            if value.get("too_large") or value.get("collapsed"):
                raise RepositoryError("GitLab diff is incomplete")
            path = value["new_path"]
            if not is_safe_repository_path(path) or not is_safe_repository_path(
                value["old_path"]
            ):
                raise RepositoryError("GitLab diff contains invalid paths")
            result.append(
                {
                    "path": path,
                    "patch": value.get("diff") or "",
                    "status": "removed"
                    if value.get("deleted_file")
                    else "added"
                    if value.get("new_file")
                    else "renamed"
                    if value.get("renamed_file")
                    else "modified",
                }
            )
        return result

    @staticmethod
    def _label(label):
        if (
            not isinstance(label, str)
            or not label
            or len(label) > 255
            or "," in label
            or any(ord(c) < 32 for c in label)
        ):
            raise ValueError("Invalid GitLab label")
        return label

    def create_issue(self, *, title, body, labels):
        assert_safe_text(title, body)
        for parent in set(
            re.findall(
                r"<!-- hatchet-parent-issue-([1-9][0-9]*)-step-[1-9][0-9]* -->", body
            )
        ):
            body += f"\n\n<!-- factory-parent:{self.project_id}:{parent} -->"
        value = self._request(
            "POST",
            "/issues",
            body={
                "title": title,
                "description": body,
                "labels": ",".join(self._label(label) for label in labels),
            },
        )
        return self._issue(value)

    def find_issues_by_markers(self, markers):
        found = {}
        for value in self._pages("/issues", query={"state": "all", "scope": "all"}):
            for marker in markers:
                if marker in str(value.get("description") or ""):
                    if marker in found and found[marker]["id"] != value["id"]:
                        raise RepositoryError(
                            "Multiple GitLab issues have the same factory marker"
                        )
                    found[marker] = {"number": value["iid"], "id": value["id"]}
        return found

    def add_sub_issue(self, parent_number, sub_issue_id):
        if type(sub_issue_id) is not int or sub_issue_id < 1:
            raise ValueError("Expected a global GitLab issue ID")
        marker = (
            f"<!-- factory-parent:{self.project_id}:{self._number(parent_number)} -->"
        )
        children = [
            value
            for value in self._pages("/issues", query={"scope": "all", "state": "all"})
            if value.get("id") == sub_issue_id
        ]
        if (
            len(children) != 1
            or children[0].get("project_id") != self.project_id
            or marker not in str(children[0].get("description") or "")
        ):
            raise RepositoryError(
                "GitLab related task is not a marked issue in this project"
            )
        if sub_issue_id in self.list_sub_issue_ids(parent_number):
            return
        self._request(
            "POST",
            "/issues/" + self._number(parent_number) + "/links",
            body={
                "target_project_id": self.project_id,
                "target_issue_iid": children[0]["iid"],
                "link_type": "relates_to",
            },
        )

    def ensure_label(self, label):
        label = self._label(label)
        existing = self._pages("/labels", query={"search": label})
        if not any(value.get("name") == label for value in existing):
            self._request(
                "POST",
                "/labels",
                body={
                    "name": label,
                    "color": "#5319e7",
                    "description": "Managed by Flumic AI",
                },
            )

    def add_label(self, number, label):
        self._request(
            "PUT",
            "/issues/" + self._number(number),
            body={"add_labels": self._label(label)},
        )

    def remove_label(self, number, label):
        if label:
            self._request(
                "PUT",
                "/issues/" + self._number(number),
                body={"remove_labels": self._label(label)},
            )

    def comment_once(self, number, *, marker, comment):
        assert_safe_text(comment, marker)
        notes = self.get_issue_comments(number)
        if not any(marker in value for value in notes):
            self._request(
                "POST",
                "/issues/" + self._number(number) + "/notes",
                body={"body": comment + "\n\n" + marker},
            )

    def close_issue(self, number):
        self._request(
            "PUT", "/issues/" + self._number(number), body={"state_event": "close"}
        )

    def find_issue_pull_request(self, issue_number):
        marker = re.compile(
            r"(?:^|\n)Closes #" + self._number(issue_number) + r"(?:\s|$)"
        )
        found = [
            value
            for value in self.list_open_pull_requests()
            if marker.search(value["body"])
        ]
        if len(found) > 1:
            raise RepositoryError("Multiple open GitLab changes match this issue")
        return found[0] if found else None

    def get_repository_archive(self, ref=None):
        return self._request(
            "GET",
            "/repository/archive.tar.gz",
            query={"sha": ref or self.resolve_revision()},
            raw=True,
        )

    @property
    def publisher(self):
        from factory.adapters.git_publish import GitPublisher

        if not hasattr(self, "_publisher"):
            self._publisher = GitPublisher()
        return self._publisher

    @publisher.setter
    def publisher(self, value):
        self._publisher = value

    def _branch_head(self, branch):
        try:
            value = self._request(
                "GET", "/repository/branches/" + quote(branch, safe="")
            )
            return value["commit"]["id"]
        except GitLabRequestError as error:
            if error.status_code == 404:
                return None
            raise

    @staticmethod
    def _patch_marker(base, changes):
        content = json.dumps(
            {
                "base": base,
                "changes": [
                    {"path": change.path, "content": change.content}
                    for change in changes
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return "factory-patch:" + hashlib.sha256(content.encode()).hexdigest()

    def _publication_project(self):
        project = self._request("GET", "")
        expected_url = "https://gitlab.com/" + self.repository + ".git"
        if (
            type(project.get("id")) is not int
            or project["id"] != self.project_id
            or project.get("path_with_namespace") != self.repository
            or project.get("http_url_to_repo", expected_url) != expected_url
            or not project.get("default_branch")
        ):
            raise RepositoryError(
                "Pinned GitLab project path changed; reconcile before publication"
            )
        return project

    def create_draft_pull_request(
        self, *, issue_number, title, body, branch, changes, expected_base_sha=None
    ):
        assert_safe_text(title, body, *(change.content for change in changes))
        self._number(issue_number)
        project = self._publication_project()
        current = self._branch_head(branch)
        base = (
            expected_base_sha
            or current
            or self.resolve_revision(project["default_branch"])
        )
        if current is None and self.resolve_revision(project["default_branch"]) != base:
            from factory.domain.models import StaleRevisionError

            raise StaleRevisionError("Default branch changed during generation")
        marker = self._patch_marker(base, changes)
        self.publisher.publish(
            remote="https://gitlab.com/" + self.repository + ".git",
            branch=branch,
            expected_head=base if current else "",
            base_commit=base,
            changes=changes,
            marker=marker,
            credentials={"gitlab": self._token},
        )
        self._publication_project()
        existing = self.find_issue_pull_request(issue_number)
        if existing:
            if existing["head"]["ref"] != branch or marker not in existing["body"]:
                raise RepositoryError(
                    "Existing GitLab change does not match publication"
                )
            return existing
        value = self._request(
            "POST",
            "/merge_requests",
            body={
                "source_branch": branch,
                "target_branch": project["default_branch"],
                "title": title
                if title.lower().startswith("draft:")
                else "Draft: " + title,
                "description": body.rstrip()
                + f"\n\nCloses #{issue_number}\n\n<!-- {marker} -->",
            },
        )
        return self._change(value)

    def update_pull_request_branch(
        self, *, branch, issue_number, feedback_cursor, changes, expected_base_sha=None
    ):
        self._publication_project()
        self._number(issue_number)
        base = expected_base_sha or self._branch_head(branch)
        if not base:
            raise RepositoryError("GitLab feedback branch is missing")
        cursor = []
        for source in ("conversation", "inline review", "review"):
            identity = feedback_cursor.get(source, 0)
            if type(identity) is not int or not 0 <= identity <= 2**63 - 1:
                raise ValueError("Invalid feedback cursor")
            cursor.append(source.replace(" ", "-") + "=" + str(identity))
        marker = (
            self._patch_marker(base, changes)
            + " <!-- hatchet-pr-feedback-cursor:"
            + ";".join(cursor)
            + " -->"
        )
        result = self.publisher.publish(
            remote="https://gitlab.com/" + self.repository + ".git",
            branch=branch,
            expected_head=base,
            changes=changes,
            marker=marker,
            credentials={"gitlab": self._token},
        )
        self._publication_project()
        return result

    def get_pull_comments(self, number):
        return [
            str(note.get("body") or "")
            for note in self._pages(
                "/merge_requests/" + self._number(number) + "/notes"
            )
            if not note.get("system")
        ]

    def comment_pull_once(self, number, *, marker, comment):
        assert_safe_text(comment, marker)
        if not any(marker in value for value in self.get_pull_comments(number)):
            self._request(
                "POST",
                "/merge_requests/" + self._number(number) + "/notes",
                body={"body": comment + "\n\n" + marker},
            )

    def add_pull_label(self, number, label):
        self._request(
            "PUT",
            "/merge_requests/" + self._number(number),
            body={"add_labels": self._label(label)},
        )

    def get_pull_feedback(self, number):
        feedback = {}
        for thread in self._pages(
            "/merge_requests/" + self._number(number) + "/discussions"
        ):
            notes = thread.get("notes")
            if not isinstance(notes, list):
                raise RepositoryError("Invalid GitLab discussion notes")
            for note in notes:
                body = note.get("body")
                if (
                    note.get("system")
                    or not isinstance(body, str)
                    or not body.strip()
                    or any(
                        marker in body
                        for marker in ("<!-- hatchet-", "<!-- factory-", "<!-- flumic-")
                    )
                ):
                    continue
                identity = note.get("id")
                if type(identity) is not int or identity < 1:
                    raise RepositoryError("GitLab feedback is missing a stable note ID")
                position = note.get("position") or {}
                feedback[identity] = {
                    "id": identity,
                    "source": "inline review"
                    if note.get("type") == "DiffNote" or position
                    else "conversation",
                    "body": body,
                    "path": position.get("new_path") or position.get("old_path"),
                    "line": position.get("new_line") or position.get("old_line"),
                    "author": (note.get("author") or {}).get("username"),
                    "created_at": note.get("created_at"),
                }
        return [feedback[key] for key in sorted(feedback)]

    @staticmethod
    def _cursor(value):
        result = {"conversation": 0, "inline review": 0, "review": 0}
        for match in re.finditer(
            r"<!-- hatchet-pr-feedback-cursor:([^ >]+) -->", value
        ):
            for item in match.group(1).split(";"):
                source, _, identity = item.partition("=")
                source = source.replace("-", " ")
                if (
                    source not in result
                    or not identity.isdecimal()
                    or len(identity) > 19
                ):
                    raise RepositoryError("Invalid GitLab feedback recovery cursor")
                result[source] = max(result[source], int(identity))
        return result

    def pull_feedback_cursor(self, number, branch):
        comments = self.get_pull_comments(number)
        head = self._branch_head(branch)
        if not head:
            raise RepositoryError("GitLab feedback branch unavailable")
        commit = self._request("GET", "/repository/commits/" + quote(head, safe=""))
        values = [*comments, str(commit.get("message") or "")]
        result = {"conversation": 0, "inline review": 0, "review": 0}
        for value in values:
            for source, identity in self._cursor(value).items():
                result[source] = max(result[source], identity)
        return result

    def record_pull_feedback(self, number, cursor, summary):
        assert_safe_text(summary)
        values = []
        for source in ("conversation", "inline review", "review"):
            identity = cursor.get(source, 0)
            if type(identity) is not int or not 0 <= identity <= 2**63 - 1:
                raise ValueError("Invalid feedback cursor")
            values.append(source.replace(" ", "-") + "=" + str(identity))
        marker = "<!-- hatchet-pr-feedback-cursor:" + ";".join(values) + " -->"
        path = "/merge_requests/" + self._number(number) + "/notes"
        notes = self._pages(path)
        for note in notes:
            if "<!-- hatchet-pr-feedback-cursor:" in str(note.get("body") or ""):
                self._request(
                    "PUT",
                    path + "/" + self._number(note["id"]),
                    body={"body": summary.rstrip() + "\n\n" + marker},
                )
                return
        self._request("POST", path, body={"body": summary.rstrip() + "\n\n" + marker})

    def verify_commit_checks(self, sha, *, ref=None):
        from factory.adapters.gitlab_checks import verify_checks

        return verify_checks(self, sha, ref=ref)
