"""Trusted template installation and atomic project follow-up snapshot bundles.

These are service methods for the runtime API/composition (#11), not worker
request authority. An assignment-authenticated receiver must load bindings from
its stored context, never accept caller-selected snapshot IDs as authorization.
"""

from fastapi import HTTPException
from sqlalchemy import select
from app.database import (
    WorkflowDefinitionRecord,
    WorkflowRevision,
    RepositoryConnection,
)
from app.project_context import encrypt
from app.projects import require_owned
from app.workflow_definitions import (
    prepare_definition,
    graph_digest,
    pin_definition,
    read_definition_pin,
)
from factory.workflows.templates import project_templates
from factory.workflows.project_dispatch import ProjectBindings, WorkflowTarget


def seed_project_templates(
    sessions,
    scope,
    config,
    settings,
    *,
    repository,
    simple_pool,
    developer_pool,
    secrets=None,
):
    from app.workflow_state import transition_lock

    result = {}
    with sessions.begin() as db:
        transition_lock(db)
        for kind, graph in project_templates(
            repository, simple_pool, developer_pool, secrets=secrets
        ).items():
            existing = db.scalar(
                select(WorkflowDefinitionRecord).where(
                    WorkflowDefinitionRecord.project_id == scope.project_id,
                    WorkflowDefinitionRecord.name == graph.name,
                )
            )
            if existing:
                # Installation is create-only: never replace an operator's edits.
                result[kind] = existing.revision_id
                continue
            prepare_definition(db, scope, config, graph.model_dump(mode="json"))
            item = WorkflowDefinitionRecord(
                project_id=scope.project_id, name=graph.name
            )
            db.add(item)
            db.flush()
            document = graph.model_dump(mode="json")
            revision = WorkflowRevision(
                project_id=scope.project_id,
                definition_id=item.id,
                graph_ciphertext=encrypt(document, settings),
                digest=graph_digest(document),
            )
            db.add(revision)
            db.flush()
            item.revision_id = revision.id
            result[kind] = revision.id
    return result


def pin_project_bundle(db, scope, root_revision, bindings, config, settings):
    if not isinstance(bindings, dict) or not 1 <= len(bindings) <= 64:
        raise HTTPException(422, "Expected bounded repository workflow bindings")
    root = pin_definition(db, scope, root_revision, config, settings)
    base = read_definition_pin(db, scope, root.id, settings)
    pins = {root_revision: root}
    values = {root_revision: base}
    allowed = {item["id"] for item in base["repositories"].values()}
    for repository, revisions in bindings.items():
        require_owned(db, scope, RepositoryConnection, repository)
        if (
            not isinstance(revisions, dict)
            or repository not in allowed
            or set(revisions)
            != {
                "issue",
                "feedback",
                "verify",
            }
        ):
            raise HTTPException(
                422,
                "Bindings must cover issue, feedback and verification in a declared repository",
            )
        for revision in revisions.values():
            if not isinstance(revision, str) or not 1 <= len(revision) <= 120:
                raise HTTPException(422, "Expected a workflow revision identity")
            if revision not in pins:
                pins[revision] = pin_definition(
                    db, scope, revision, config, settings, frozen=base
                )
                values[revision] = read_definition_pin(
                    db, scope, pins[revision].id, settings
                )
            target = values[revision]
            if (
                target["repositories"][target["definition"]["default_repository"]]["id"]
                != repository
            ):
                raise HTTPException(
                    422, "Follow-up default repository does not match its binding"
                )
    frozen = {}
    for repository, revisions in bindings.items():
        frozen[repository] = ProjectBindings(
            project_id=scope.project_id,
            repository_id=repository,
            **{
                kind: WorkflowTarget(
                    snapshot_id=pins[revision].id, workflow_revision_id=revision
                )
                for kind, revision in revisions.items()
            },
        ).model_dump_json()
    for revision, value in values.items():
        for metadata in value["stages"].values():
            if (
                metadata["executor"] == "builtin@1"
                and metadata["action"] != "core.echo@1"
                and metadata["repository_id"] not in frozen
            ):
                raise HTTPException(
                    422, "Every builtin repository requires pinned follow-up bindings"
                )
            metadata["followup_bindings_json"] = frozen.get(
                metadata["repository_id"], "{}"
            )
        pins[revision].ciphertext = encrypt(value, settings)
    db.flush()
    return root
