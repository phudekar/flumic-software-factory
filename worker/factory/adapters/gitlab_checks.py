"""Fail-closed GitLab check selection for an exact merged commit and target ref."""

import re
from factory.domain.models import RepositoryError


def verify_checks(client, sha, *, ref=None):
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise ValueError("A full merged commit OID is required")
    if (
        not isinstance(ref, str)
        or not ref
        or len(ref) > 255
        or any(ord(char) < 32 for char in ref)
    ):
        raise ValueError("The merged change target ref is required")
    checked = {}
    active = set()

    def identifier(value):
        if type(value) is not int or value < 1:
            raise RepositoryError("GitLab check evidence is missing a numeric identity")
        return value

    def pipeline(identity, expected_source=None):
        identifier(identity)
        if identity in active or len(checked) + len(active) >= 256:
            raise RepositoryError(
                "GitLab downstream evidence is cyclic or exceeds bounds"
            )
        if identity in checked:
            if expected_source is not None and checked[identity][0] != expected_source:
                raise RepositoryError(
                    "GitLab pipeline source differs from required policy"
                )
            return checked[identity][1]
        active.add(identity)
        value = client._request("GET", f"/pipelines/{identity}")
        if (
            value.get("id") != identity
            or value.get("project_id") != client.project_id
            or value.get("sha") != sha
            or value.get("ref") != ref
            or value.get("status") != "success"
        ):
            raise RepositoryError(
                "GitLab pipeline lacks successful exact-commit evidence"
            )
        if expected_source is not None and value.get("source") != expected_source:
            raise RepositoryError("GitLab pipeline source differs from required policy")
        jobs = client._pages(
            f"/pipelines/{identity}/jobs", query={"include_retried": "false"}
        )
        latest = {}
        for job in jobs:
            identifier(job.get("id"))
            key = (job.get("stage"), job.get("name"))
            if not isinstance(key[1], str) or not key[1]:
                raise RepositoryError("GitLab job has no stable name")
            if key not in latest or latest[key]["id"] < job["id"]:
                latest[key] = job
        successful_required = False
        for job in latest.values():
            optional = job.get("allow_failure", False)
            if type(optional) is not bool:
                raise RepositoryError("Invalid GitLab job failure policy")
            if not optional:
                if job.get("status") not in {"success", "skipped"}:
                    raise RepositoryError("GitLab required job is not successful")
                successful_required = (
                    job.get("status") == "success" or successful_required
                )
        try:
            triggers = client._pages(f"/pipelines/{identity}/trigger_jobs")
        except RepositoryError as error:
            # GitLab renamed bridges in 19.2. Never turn an unavailable endpoint
            # into empty downstream evidence; only the known 404 gets fallback.
            if getattr(error, "status_code", None) != 404:
                raise
            triggers = client._pages(f"/pipelines/{identity}/bridges")
        for trigger in triggers:
            if trigger.get("allow_failure", False) is True and trigger.get(
                "status"
            ) in {"manual", "skipped"}:
                continue
            child = trigger.get("downstream_pipeline")
            if (
                trigger.get("status") != "success"
                or not isinstance(child, dict)
                or child.get("project_id") != client.project_id
            ):
                raise RepositoryError(
                    "GitLab downstream evidence unavailable in repository scope"
                )
            successful_required = (
                pipeline(identifier(child.get("id"))) or successful_required
            )
        if not successful_required:
            raise RepositoryError("GitLab pipeline has no successful required job")
        active.remove(identity)
        checked[identity] = (value.get("source"), successful_required)
        return successful_required

    for source in client.check_policy.required_pipeline_sources:
        values = client._pages(
            "/pipelines", query={"sha": sha, "ref": ref, "source": source}
        )
        candidates = [
            value
            for value in values
            if value.get("sha") == sha
            and value.get("ref") == ref
            and value.get("source") == source
        ]
        if not candidates:
            raise RepositoryError(
                "Required GitLab pipeline source has no exact-commit evidence"
            )
        selected = max(candidates, key=lambda value: identifier(value.get("id")))
        pipeline(selected["id"], expected_source=source)

    latest_status = {}
    for value in client._pages(
        f"/repository/commits/{sha}/statuses", query={"ref": ref, "all": "true"}
    ):
        if value.get("sha") != sha or value.get("ref") != ref:
            continue
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise RepositoryError("GitLab status has no stable name")
        identifier(value.get("id"))
        if name not in latest_status or latest_status[name]["id"] < value["id"]:
            latest_status[name] = value
    for name in client.check_policy.required_status_names:
        if name not in latest_status or latest_status[name].get("status") != "success":
            raise RepositoryError("Required GitLab status has no successful evidence")
    for value in latest_status.values():
        if value.get("allow_failure", False) is not True and value.get("status") in {
            "failed",
            "canceled",
        }:
            raise RepositoryError("GitLab commit has a failed status")
