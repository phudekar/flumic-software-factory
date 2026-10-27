"""Deterministic GitLab wire fixture; never performs network or model calls."""

from copy import deepcopy
import re
from urllib.parse import unquote
from factory.adapters.gitlab import GitLabClient, GitLabRequestError
from factory.domain.models import StaleRevisionError


class GitLabProtocolFixture:
    def __init__(self):
        self.client = GitLabClient("owner/repo", project_id=123, token="fixture-gitlab")
        self.client._request = self.request
        self.client.publisher = self
        self.base = "a" * 40
        self.merge = "b" * 40
        self.files = {"src/app.py": "value = 0\n"}
        self.issues = {
            2: self.issue(2, "Parent"),
            4: {**self.issue(4, "Unrelated issue"), "state": "closed"},
        }
        self.mrs = {}
        self.notes = {}
        self.links = {}
        self.labels = set()
        self.branches = {"main": self.base}
        self.messages = {self.base: "base"}
        self.publications = []
        self.writes = []
        self.lose_mr_response = False

    @staticmethod
    def issue(iid, title, description="", labels=()):
        return {
            "id": 500 + iid,
            "iid": iid,
            "project_id": 123,
            "title": title,
            "description": description,
            "state": "opened",
            "labels": list(labels),
            "web_url": f"https://gitlab.com/owner/repo/-/issues/{iid}",
        }

    def publish(self, **values):
        branch = values["branch"]
        expected = values["expected_head"]
        actual = self.branches.get(branch, "")
        if actual != expected:
            if self.messages.get(actual) == values["marker"]:
                return actual
            raise StaleRevisionError("fixture branch changed")
        self.publications.append(deepcopy(values))
        sha = f"{len(self.publications):040x}"
        self.branches[branch] = sha
        self.messages[sha] = values["marker"]
        for change in values["changes"]:
            self.files[change.path] = change.content
        for mr in self.mrs.values():
            if mr["source_branch"] == branch:
                mr["sha"] = sha
        return sha

    def request(self, method, path, *, body=None, query=None, raw=False):
        if method != "GET":
            self.writes.append((method, path, deepcopy(body)))
        if path == "":
            return {
                "id": 123,
                "path_with_namespace": "owner/repo",
                "default_branch": "main",
            }
        if path.startswith("/repository/branches/"):
            branch = unquote(path.removeprefix("/repository/branches/"))
            if branch not in self.branches:
                raise GitLabRequestError(404)
            return {"commit": {"id": self.branches[branch]}}
        if path.startswith("/repository/commits/") and not path.endswith("/statuses"):
            ref = unquote(path.removeprefix("/repository/commits/"))
            sha = self.branches.get(ref, ref)
            return {"id": sha, "message": self.messages.get(sha, "merged")}
        if path == "/repository/tree":
            return [{"path": name, "type": "blob"} for name in self.files]
        if path.startswith("/repository/files/"):
            import base64

            name = unquote(path.removeprefix("/repository/files/"))
            return {
                "encoding": "base64",
                "content": base64.b64encode(self.files[name].encode()).decode(),
            }
        if path == "/labels":
            if method == "GET":
                return [{"name": name} for name in self.labels]
            self.labels.add(body["name"])
            return {"name": body["name"]}
        note = re.fullmatch(r"/(issues|merge_requests)/(\d+)/notes(?:/(\d+))?", path)
        if note:
            key = (note[1], int(note[2]))
            values = self.notes.setdefault(key, [])
            if method == "GET":
                return deepcopy(values)
            if method == "POST":
                result = {
                    "id": len(values) + 1,
                    "body": body["body"],
                    "system": False,
                    "author": {"username": "factory"},
                }
                values.append(result)
                return deepcopy(result)
            existing = next(value for value in values if value["id"] == int(note[3]))
            existing["body"] = body["body"]
            return deepcopy(existing)
        link = re.fullmatch(r"/issues/(\d+)/links", path)
        if link:
            values = self.links.setdefault(int(link[1]), set())
            if method == "GET":
                return [deepcopy(self.issues[number]) for number in values]
            values.add(body["target_issue_iid"])
            return {"id": 1}
        discussion = re.fullmatch(r"/merge_requests/(\d+)/discussions", path)
        if discussion:
            return [
                {
                    "id": "fixture-thread",
                    "notes": deepcopy(
                        self.notes.get(("merge_requests", int(discussion[1])), [])
                    ),
                }
            ]
        if path.endswith("/diffs"):
            return [
                {
                    "old_path": "src/app.py",
                    "new_path": "src/app.py",
                    "diff": "@@ -1 +1 @@\n-value = 0\n+value = 1",
                }
            ]
        if path in {"/issues", "/merge_requests"}:
            table = self.issues if path == "/issues" else self.mrs
            if method == "GET":
                return [
                    deepcopy(value)
                    for value in table.values()
                    if not query
                    or query.get("state") == "all"
                    or value["state"] == "opened"
                ]
            if path == "/issues":
                iid = max(20, max(table) + 1)
                value = self.issue(
                    iid, body["title"], body["description"], body["labels"].split(",")
                )
            else:
                iid = 4
                value = {
                    "id": 900,
                    "iid": iid,
                    "project_id": 123,
                    "source_project_id": 123,
                    "target_project_id": 123,
                    "title": body["title"],
                    "description": body["description"],
                    "state": "opened",
                    "draft": True,
                    "source_branch": body["source_branch"],
                    "target_branch": body["target_branch"],
                    "sha": self.branches[body["source_branch"]],
                    "labels": [],
                    "web_url": f"https://gitlab.com/owner/repo/-/merge_requests/{iid}",
                }
            table[iid] = value
            if path == "/merge_requests" and self.lose_mr_response:
                self.lose_mr_response = False
                from factory.domain.models import RepositoryError

                raise RepositoryError("Fixture lost successful MR creation response")
            return deepcopy(value)
        item = re.fullmatch(r"/(issues|merge_requests)/(\d+)", path)
        if item:
            table = self.issues if item[1] == "issues" else self.mrs
            value = table[int(item[2])]
            if body:
                for name in body.get("add_labels", "").split(","):
                    if name and name not in value["labels"]:
                        value["labels"].append(name)
                for name in body.get("remove_labels", "").split(","):
                    if name in value["labels"]:
                        value["labels"].remove(name)
                if body.get("state_event") == "close":
                    value["state"] = "closed"
            return deepcopy(value)
        pipeline = {
            "id": 10,
            "project_id": 123,
            "sha": self.merge,
            "ref": "main",
            "source": "push",
            "status": "success",
        }
        if path == "/pipelines":
            return [pipeline]
        if path == "/pipelines/10":
            return pipeline
        if path == "/pipelines/10/jobs":
            return [
                {
                    "id": 20,
                    "name": "tests",
                    "stage": "test",
                    "status": "success",
                    "allow_failure": False,
                }
            ]
        if path == "/pipelines/10/trigger_jobs" or path.endswith("/statuses"):
            return []
        raise AssertionError((method, path, body, query))
