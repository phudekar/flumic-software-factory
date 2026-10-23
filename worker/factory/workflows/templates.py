"""Compatible initial project recipes; these are data, never per-run code imports."""

import hashlib
from factory.workflows.definitions import WorkflowDefinition


def project_templates(repository, simple_pool, developer_pool, *, secrets=None):
    command = {"issue_number": {"$ref": "input.issue_number"}}

    def stage(name, action, inputs, after=(), role="simple"):
        return dict(
            id=name,
            executor="builtin",
            action=action,
            role=role,
            pool_id=developer_pool if role == "developer" else simple_pool,
            inputs=inputs,
            after=list(after),
        )

    def parent(name):
        return {"$ref": f"stages.{name}.output"}

    stages = [
        stage("load", "issue.load@1", {"command": command}),
        stage(
            "plan",
            "issue.plan@1",
            {"command": command, "state": parent("load")},
            ["load"],
        ),
        stage(
            "triage",
            "issue.triage@1",
            {"command": command, "state": parent("load"), "plan": parent("plan")},
            ["load", "plan"],
        ),
        stage(
            "implement",
            "change.implement@1",
            {"command": command, "triage": parent("triage")},
            ["triage"],
            role="developer",
        ),
        stage(
            "review",
            "change.review@1",
            {"command": command, "publish": parent("implement")},
            ["implement"],
        ),
        stage(
            "record",
            "workflow.record@1",
            {"command": command, "result": parent("review")},
            ["review"],
        ),
    ]
    pull = {
        key: {"$ref": "input." + key}
        for key in ("issue_number", "pull_number", "pull_url", "branch")
    }
    recipes = {
        "issue": stages,
        "feedback": [
            stage("feedback", "pull.feedback@1", {"command": pull}, role="developer")
        ],
        "verify": [stage("verify", "pull.verify@1", {"command": pull})],
    }
    result = {}
    for kind, steps in recipes.items():
        suffix = hashlib.sha256(repository.encode()).hexdigest()[:8]
        result[kind] = WorkflowDefinition.model_validate(
            dict(
                schema_version=1,
                name=f"factory-{kind}-{repository[:60]}-{suffix}",
                repositories=[repository],
                default_repository=repository,
                secrets=secrets or {},
                stages=steps,
            )
        )
    return result
