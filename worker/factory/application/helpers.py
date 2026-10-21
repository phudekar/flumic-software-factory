from __future__ import annotations

import base64
import re
from typing import Any
from pydantic import ValidationError
from factory.domain.models import (
    IssueState,
    IssueWorkPlan,
    ModelError,
    PullInput,
    PullRequestReview,
)
from factory.domain.safety import assert_safe_text, find_sensitive_categories

NEEDS_DISCUSSION = "needs-discussion"
READY_FOR_AGENT = "ready-for-agent"
PR_OPEN = "agent-pr-open"
PR_REVIEWED = "agent-pr-reviewed"
PLAN_CREATED = "agent-plan-created"
PLAN_SUBTASK = "agent-subtask"
PLAN_MARKER_PREFIX = "<!-- hatchet-issue-work-plan:"


def labels_for(issue: dict[str, Any]) -> set[str]:
    return {
        str(label.get("name", ""))
        for label in issue.get("labels", [])
        if isinstance(label, dict)
    }


def _issue_snapshot(issue: dict[str, Any]) -> IssueState:
    return IssueState(
        issue_number=int(issue["number"]),
        state=str(issue["state"]),
        labels=sorted(labels_for(issue)),
    )


def _pull_input_from_api(
    github, pull: dict[str, Any], *, pr_open_label=PR_OPEN
) -> PullInput | None:
    number = pull.get("number")
    url = pull.get("html_url")
    branch = (pull.get("head") or {}).get("ref")
    body = str(pull.get("body") or "")
    linked_issue = re.search(
        r"(?i)\b(?:close[sd]?|fix(?:es|ed)?|resolve[sd]?)\s+#(\d+)\b",
        body,
    )
    if (
        not isinstance(number, int)
        or not isinstance(url, str)
        or not isinstance(branch, str)
        or linked_issue is None
    ):
        return None
    issue_number = int(linked_issue.group(1))
    issue = github.get_issue(issue_number)
    if issue.get("state") != "open" or pr_open_label not in labels_for(issue):
        return None
    return PullInput(
        repository=github.repository,
        issue_number=issue_number,
        pull_number=number,
        pull_url=url,
        branch=branch,
    )


def _format_review_feedback(item: dict[str, Any]) -> str:
    body = re.sub(
        r"\s*<!-- hatchet-issue-agent-review-\d+ -->",
        "",
        str(item["body"]),
    ).strip()
    location = ""
    if item.get("path"):
        location = f" on {item['path']}"
        if item.get("line") is not None:
            location += f" at line {item['line']}"
    author = str(item.get("author") or "unknown")
    return f"Comment {item['id']} ({item['source']}) by {author}{location}:\n{body}"


def _sub_issue_marker(parent_number: int, step_number: int) -> str:
    return f"<!-- hatchet-parent-issue-{parent_number}-step-{step_number} -->"


def _stored_issue_plan(comments: list[str]) -> IssueWorkPlan | None:
    marker_pattern = re.compile(r"<!-- hatchet-issue-work-plan:([A-Za-z0-9_-]+) -->")
    for comment in comments:
        match = marker_pattern.search(comment)
        if not match:
            continue
        encoded = match.group(1)
        try:
            padding = "=" * (-len(encoded) % 4)
            plan_json = base64.urlsafe_b64decode(encoded + padding)
            return IssueWorkPlan.model_validate_json(plan_json)
        except (ValueError, ValidationError):
            raise ModelError("Stored issue plan is invalid") from None
    return None


def _format_issue_plan_comment(plan: IssueWorkPlan) -> str:
    lines = ["Automated implementation plan; each subtask can be worked independently:"]
    for index, task in enumerate(plan.tasks, start=1):
        lines.append(f"{index}. **{task.title}** — {task.objective}")
        lines.extend(f"   - {criterion}" for criterion in task.acceptance_criteria)
    encoded = (
        base64.urlsafe_b64encode(plan.model_dump_json().encode("utf-8"))
        .decode("ascii")
        .rstrip("=")
    )
    return "\n".join(lines) + f"\n\n{PLAN_MARKER_PREFIX}{encoded} -->"


def _format_sub_issue_body(
    *,
    parent_number: int,
    parent_url: str,
    objective: str,
    acceptance_criteria: list[str],
    marker: str,
) -> str:
    parent_reference = (
        f"[#{parent_number}]({parent_url})" if parent_url else f"#{parent_number}"
    )
    lines = [
        f"Part of parent issue {parent_reference}.",
        "",
        "## Objective",
        objective,
        "",
        "## Acceptance criteria",
        *(f"- {criterion}" for criterion in acceptance_criteria),
        "",
        marker,
    ]
    body = "\n".join(lines)
    assert_safe_text(body)
    return body


def _pending_pull_feedback(
    feedback: list[dict[str, Any]],
    cursor: dict[str, int],
    pull_number: int,
) -> list[dict[str, Any]]:
    review_marker = f"<!-- hatchet-issue-agent-review-{pull_number} -->"
    pending = []
    for item in feedback:
        body = str(item["body"])
        is_worker_review = review_marker in body
        has_agent_marker = "<!-- hatchet-" in body
        if int(item["id"]) > cursor[str(item["source"])] and (
            not has_agent_marker or is_worker_review
        ):
            pending.append(item)
    return pending


def _log_title(title: str) -> str:
    if find_sensitive_categories(title):
        return "[redacted]"
    return re.sub(r"\s+", " ", title).strip()[:160]


def _format_pull_request_review(review: PullRequestReview) -> str:
    if not review.findings:
        return (
            "Automated review completed. No actionable defects were identified "
            "in the pull-request diff."
        )
    lines = ["Automated review found the following actionable concerns:"]
    for finding in review.findings:
        location = f"`{finding.path}`"
        if finding.line is not None:
            location += f" (line {finding.line})"
        lines.append(
            f"- **{finding.kind.upper()} / {finding.severity.upper()}** "
            f"{location}: {finding.description}"
        )
    comment = "\n".join(lines)
    assert_safe_text(comment)
    return comment
