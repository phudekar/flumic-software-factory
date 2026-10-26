"""Canonical external-resource identity shared by workers and trusted state."""

from factory.domain.repositories import RepositoryIdentity


def canonical_repository(host, owner, name, *, project_id=None):
    provider = {"github.com": "github", "gitlab.com": "gitlab"}.get(host.lower())
    if provider is None:
        raise ValueError("Unsupported canonical repository host")
    return RepositoryIdentity(
        provider=provider, host=host, owner=owner, name=name, project_id=project_id
    ).canonical_key


def canonical_resource(
    host, owner, name, kind="repository", number=None, *, project_id=None
):
    repository = canonical_repository(host, owner, name, project_id=project_id)
    if kind == "repository" and number is None:
        return repository
    if kind not in {"issue", "pull"} or type(number) is not int or number < 1:
        raise ValueError("Expected a positive canonical issue/pull number")
    # GitHub issue/PR numbers share a namespace. GitLab issue/MR IIDs do not.
    resource = (
        "merge-request" if host.lower() == "gitlab.com" and kind == "pull" else "issue"
    )
    return repository + "/" + resource + "/" + str(number)
