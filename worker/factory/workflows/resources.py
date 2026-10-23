"""Canonical external-resource identity shared by workers and trusted state."""

import re


def canonical_repository(host, owner, name):
    if host.lower() != "github.com" or any(
        not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", value) or value in {".", ".."}
        for value in (owner, name)
    ):
        raise ValueError("Unsupported canonical GitHub repository")
    return f"github.com/{owner.lower()}/{name.lower()}"


def canonical_resource(host, owner, name, kind="repository", number=None):
    repository = canonical_repository(host, owner, name)
    if kind == "repository" and number is None:
        return repository
    if kind not in {"issue", "pull"} or type(number) is not int or number < 1:
        raise ValueError("Expected a positive canonical issue/pull number")
    # GitHub issues and PRs share an issue number namespace. They must not acquire
    # two independent locks for the same external work item.
    return repository + "/issue/" + str(number)
