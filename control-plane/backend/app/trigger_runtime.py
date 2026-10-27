"""Durable source acceptance: remote reads precede a short atomic SQL fence."""

from dataclasses import asdict
import hashlib
import time

from fastapi import HTTPException
from sqlalchemy import select
from factory.adapters.gitlab_events import GitLabEvent
from app.database import identity

from app.database import (
    RepositoryTrigger,
    RepositoryConnection,
    RepositoryRevision,
    TriggerToken,
    TriggerReceipt,
    TriggerObservation,
    WorkflowDefinitionRecord,
    WorkflowRun,
)
from app.projects import ProjectScope, require_owned, effective_configuration
from app.project_context import decrypt, require_secret
from app.trigger_policy import Observation, observe
from app.workflow_definitions import pin_definition
from app.workflow_state import WorkflowState, transition_lock, TERMINAL
from factory.domain.repositories import RepositoryIdentity
from factory.adapters.gitlab import GitLabClient
from factory.application.helpers import _pull_input_from_api


def receipt_public(item):
    return {
        key: getattr(item, key)
        for key in (
            "id",
            "event_id",
            "status",
            "source_key",
            "run_id",
            "workflow_revision_id",
            "repository_revision_id",
            "config_revision_id",
            "received_at",
        )
    }


class TriggerRuntime:
    def __init__(self, sessions, settings, config):
        self.sessions, self.settings, self.config = sessions, settings, config
        self.store = WorkflowState(sessions, settings, config)

    def source(self, project_id, repository_id, revision_id):
        with self.sessions() as db:
            scope = ProjectScope(project_id)
            repo = require_owned(db, scope, RepositoryConnection, repository_id)
            revision = require_owned(db, scope, RepositoryRevision, revision_id)
            document = revision.document
            secret = require_secret(
                db, scope, document.get("credential_id"), "gitlab", repo.id
            )
            token = decrypt(secret.ciphertext, self.settings)
            settings, _, _ = effective_configuration(
                db, scope, self.config, repo.alias, repository_revision_id=revision.id
            )
            identity = RepositoryIdentity.model_validate(document["identity"])
            client = GitLabClient(
                identity.path,
                project_id=identity.project_id,
                token=token,
                timeout_seconds=5,
                max_pages=2,
            )
            labels = settings.labels.model_dump()
        # Fresh numeric lookup also detects a path rename before any work is accepted.
        client._publication_project()
        return client, labels

    @staticmethod
    def check_proof(db, item, proof):
        if proof is None:
            return
        token = db.get(TriggerToken, proof.token_version_id)
        if (
            proof.binding_id != item.id
            or proof.project_id != item.project_id
            or item.current_token_id != proof.token_version_id
            or not token
            or token.revoked_at is not None
            or token.binding_id != item.id
            or not item.enabled
        ):
            raise HTTPException(401, "Invalid trigger credential")
        if item.revision_id != proof.configuration_revision_id:
            raise HTTPException(409, "Trigger changed during acceptance")

    def candidate(self, kind, event, client, labels, policy):
        if event.object_kind == "note" and event.system:
            return None
        if (
            kind == "issue-ready"
            and event.object_kind == "issue"
            and event.issue_number
        ):
            issue = client.get_issue(event.issue_number)
            if issue.get("number") != event.issue_number:
                raise HTTPException(409, "Source issue does not match requested IID")
            names = {value["name"] for value in issue.get("labels", [])}
            eligible = issue.get("state") == "open" and not (
                {labels["pr_open"], labels["plan_created"]} & names
            )
            eligible = eligible and (
                not policy.get("require_ready_label", True)
                or labels["ready_for_agent"] in names
            )
            return (
                "issue/" + str(event.issue_number),
                event.issue_number,
                eligible,
                "",
                {"issue_number": event.issue_number},
            )
        if (
            kind in {"feedback", "verify"}
            and event.pull_number
            and event.object_kind in {"note", "merge_request"}
        ):
            pull = client.get_pull_request(event.pull_number)
            expected_url = (
                "https://gitlab.com/"
                + client.repository
                + "/-/merge_requests/"
                + str(event.pull_number)
            )
            if (
                pull.get("html_url") != expected_url
                or pull.get("number") != event.pull_number
                or pull.get("source_project_id") != client.project_id
                or pull.get("target_project_id") != client.project_id
            ):
                raise HTTPException(
                    409, "Source change does not match configured repository"
                )
            inputs = _pull_input_from_api(
                client,
                pull,
                pr_open_label=labels["pr_open"],
                allow_closed_issue=kind == "verify" and pull.get("merged") is True,
            )
            eligible = inputs is not None
            stamp = ""
            if kind == "feedback":
                eligible = (
                    eligible and pull.get("state") == "open" and not pull.get("merged")
                )
                if eligible:
                    notes = client.get_pull_feedback(event.pull_number)
                    stamp = (
                        str(max((note["id"] for note in notes), default=0))
                        if notes
                        else ""
                    )
            else:
                stamp = pull.get("merge_commit_sha") or ""
                eligible = eligible and pull.get("merged") is True and bool(stamp)
            return (
                "merge-request/" + str(event.pull_number),
                event.pull_number,
                eligible,
                stamp,
                inputs.model_dump(mode="json") if inputs else {},
            )
        return None

    def accept_event(self, binding_id, event, *, proof=None):
        receipt_id = hashlib.sha256(
            (binding_id + "\0" + event.event_id).encode()
        ).hexdigest()
        with self.sessions() as db:
            item = db.get(RepositoryTrigger, binding_id)
            if not item or not item.enabled:
                raise HTTPException(409, "Trigger is unavailable")
            self.check_proof(db, item, proof)
            existing = db.get(TriggerReceipt, receipt_id)
            if existing:
                if existing.payload_digest != event.payload_digest:
                    raise HTTPException(
                        409, "Delivery identity already has another payload"
                    )
                return receipt_public(existing)
            scope = ProjectScope(item.project_id)
            repository = require_owned(
                db, scope, RepositoryConnection, item.repository_id
            )
            revision = require_owned(
                db, scope, RepositoryRevision, repository.revision_id
            )
            identity = RepositoryIdentity.model_validate(revision.document["identity"])
            if identity.provider != "gitlab" or event.project_id != identity.project_id:
                raise HTTPException(
                    422, "Event does not match configured source project"
                )
            if proof and proof.repository_revision_id != revision.id:
                raise HTTPException(409, "Repository changed during acceptance")
            captured = (
                item.project_id,
                item.repository_id,
                revision.id,
                item.revision_id,
                item.definition_id,
                item.event_kind,
            )
            policy = decrypt(item.configuration_ciphertext, self.settings)
            expected_key = (
                "issue/" + str(event.issue_number)
                if item.event_kind == "issue-ready" and event.issue_number
                else "merge-request/" + str(event.pull_number)
                if event.pull_number
                else None
            )
            observed = (
                db.get(TriggerObservation, (scope.project_id, item.id, expected_key))
                if expected_key
                else None
            )
            observed_version = observed.last_checked if observed else None
        # Client construction decrypts only this repository's scoped source grant.
        # No database transaction is held while making remote requests.
        client, labels = self.source(*captured[:3])
        candidate = self.candidate(captured[5], event, client, labels, policy)
        with self.sessions.begin() as db:
            transition_lock(db)
            item = db.get(RepositoryTrigger, binding_id)
            if not item or not item.enabled:
                raise HTTPException(409, "Trigger is unavailable")
            self.check_proof(db, item, proof)
            existing = db.get(TriggerReceipt, receipt_id)
            if existing:
                if existing.payload_digest != event.payload_digest:
                    raise HTTPException(
                        409, "Delivery identity already has another payload"
                    )
                return receipt_public(existing)
            repository = require_owned(
                db, scope, RepositoryConnection, item.repository_id
            )
            if item.revision_id != captured[3] or repository.revision_id != captured[2]:
                raise HTTPException(
                    409, "Trigger or repository changed during source read"
                )
            # Revalidate scoped credential revocation before creating any new pin.
            revision = require_owned(db, scope, RepositoryRevision, captured[2])
            if revision.document.get("credential_id"):
                require_secret(
                    db,
                    scope,
                    revision.document["credential_id"],
                    "gitlab",
                    repository.id,
                )
            receipt = TriggerReceipt(
                id=receipt_id,
                project_id=scope.project_id,
                binding_id=item.id,
                event_id=event.event_id,
                payload_digest=event.payload_digest,
                config_revision_id=item.revision_id,
                status="ignored",
                repository_id=repository.id,
                repository_revision_id=revision.id,
                auth_token_version=proof.token_version_id if proof else None,
                received_at=time.time(),
            )
            if candidate:
                source_key, iid, eligible, stamp, run_input = candidate
                receipt.source_key = source_key
                key = (scope.project_id, item.id, source_key)
                observation = db.get(TriggerObservation, key)
                if (
                    observation.last_checked if observation else None
                ) != observed_version:
                    raise HTTPException(
                        409, "Source observation changed; retry with a fresh read"
                    )
                previous = (
                    Observation(
                        **{
                            field: getattr(observation, field)
                            for field in Observation.__dataclass_fields__
                        }
                    )
                    if observation
                    else Observation()
                )
                active_run = (
                    db.get(WorkflowRun, observation.run_id)
                    if observation and observation.run_id
                    else None
                )
                current, status = observe(
                    item.event_kind,
                    previous,
                    eligible=eligible,
                    stamp=stamp,
                    active=bool(active_run and active_run.state not in TERMINAL),
                )
                receipt.status = status
                if observation is None:
                    observation = TriggerObservation(
                        project_id=scope.project_id,
                        binding_id=item.id,
                        source_key=source_key,
                        remote_iid=iid,
                    )
                    db.add(observation)
                for field, value in asdict(current).items():
                    setattr(observation, field, value)
                observation.last_checked = time.time()
                if status == "accepted":
                    definition = require_owned(
                        db, scope, WorkflowDefinitionRecord, item.definition_id
                    )
                    # Revalidate the current graph root: editing a definition must
                    # not silently redirect an existing repository binding.
                    from app.repository_triggers import validate_definition

                    validate_definition(
                        db, scope, repository, definition.id, self.settings
                    )
                    if policy.get("followups"):
                        from app.builtin_workflows import pin_project_bundle

                        bindings = {
                            repo_id: {
                                kind: require_owned(
                                    db, scope, WorkflowDefinitionRecord, definition_id
                                ).revision_id
                                for kind, definition_id in definitions.items()
                            }
                            for repo_id, definitions in policy["followups"].items()
                        }
                        pin = pin_project_bundle(
                            db,
                            scope,
                            definition.revision_id,
                            bindings,
                            self.config,
                            self.settings,
                        )
                    else:
                        pin = pin_definition(
                            db,
                            scope,
                            definition.revision_id,
                            self.config,
                            self.settings,
                        )
                    inputs = {**policy.get("input_defaults", {}), **run_input}
                    run = self.store.start_pinned_run(
                        scope, pin.id, inputs, key="gitlab-trigger/" + receipt_id, db=db
                    )
                    observation.run_id = run.id
                    receipt.run_id = run.id
                    receipt.workflow_revision_id = definition.revision_id
                elif active_run:
                    receipt.run_id = active_run.id
            db.add(receipt)
            db.flush()
            return receipt_public(receipt)

    def poll_once(self, *, force=False):
        """Lease one binding and inspect at most five new and five tracked items."""
        lease = identity()
        now = time.time()
        with self.sessions.begin() as db:
            transition_lock(db)
            query = select(RepositoryTrigger).where(
                RepositoryTrigger.enabled.is_(True),
                (
                    RepositoryTrigger.lease_until.is_(None)
                    | (RepositoryTrigger.lease_until < now)
                ),
            )
            if not force:
                query = query.where(RepositoryTrigger.next_poll_at <= now)
            item = db.scalar(
                query.order_by(
                    RepositoryTrigger.next_poll_at, RepositoryTrigger.id
                ).limit(1)
            )
            if item is None:
                return 0
            item.lease_owner, item.lease_until = lease, now + 600
            binding_id, project_id, repository_id = (
                item.id,
                item.project_id,
                item.repository_id,
            )
            repo = require_owned(
                db, ProjectScope(project_id), RepositoryConnection, repository_id
            )
            revision_id = repo.revision_id
            revision = require_owned(
                db, ProjectScope(project_id), RepositoryRevision, revision_id
            )
            source_identity = RepositoryIdentity.model_validate(
                revision.document["identity"]
            )
            kind, cursor = item.event_kind, item.poll_cursor
            tracked = list(
                db.scalars(
                    select(TriggerObservation)
                    .where(
                        TriggerObservation.project_id == project_id,
                        TriggerObservation.binding_id == binding_id,
                    )
                    .order_by(TriggerObservation.last_checked)
                    .limit(5)
                )
            )
            tracked_iids = [record.remote_iid for record in tracked]
        try:
            client, _ = self.source(project_id, repository_id, revision_id)
            path = "/issues" if kind == "issue-ready" else "/merge_requests"
            state = "merged" if kind == "verify" else "opened"
            values = client._request(
                "GET",
                path,
                query={
                    "state": state,
                    "page": cursor + 1,
                    "per_page": 5,
                    "order_by": "updated_at",
                    "sort": "desc",
                },
            )
            if not isinstance(values, list) or len(values) > 5:
                raise ValueError("Invalid source page")
            candidates = {}
            for value in values:
                iid = value.get("iid")
                if type(iid) is not int or iid <= 0:
                    raise ValueError("Invalid source IID")
                candidates[iid] = str(value.get("updated_at") or "")[:120]
            for iid in tracked_iids:
                candidates.setdefault(iid, "")
            for iid, stamp in candidates.items():
                with self.sessions() as db:
                    binding = db.get(RepositoryTrigger, binding_id)
                    if (
                        not binding
                        or not binding.enabled
                        or binding.lease_owner != lease
                    ):
                        return 1
                    key = (
                        "issue/" if kind == "issue-ready" else "merge-request/"
                    ) + str(iid)
                    observation = db.get(
                        TriggerObservation, (project_id, binding_id, key)
                    )
                    run = (
                        db.get(WorkflowRun, observation.run_id)
                        if observation and observation.run_id
                        else None
                    )
                    facts = [
                        lease,
                        binding.revision_id,
                        key,
                        stamp,
                        observation.epoch if observation else 0,
                        observation.eligible if observation else False,
                        observation.last_stamp if observation else "",
                        run.id if run else "",
                        run.state if run else "",
                    ]
                import json

                digest = hashlib.sha256(
                    json.dumps(facts, separators=(",", ":")).encode()
                ).hexdigest()
                event = GitLabEvent(
                    event_id="poll/" + digest,
                    payload_digest=digest,
                    project_id=source_identity.project_id,
                    repository=source_identity.path,
                    object_kind="issue" if kind == "issue-ready" else "merge_request",
                    action="poll",
                    issue_number=iid if kind == "issue-ready" else None,
                    pull_number=iid if kind != "issue-ready" else None,
                )
                self.accept_event(binding_id, event)
            with self.sessions.begin() as db:
                transition_lock(db)
                item = db.get(RepositoryTrigger, binding_id)
                if item and item.lease_owner == lease:
                    item.poll_cursor = cursor + 1 if len(values) == 5 else 0
                    item.last_error = None
            return 1
        except Exception:
            # Do not persist vendor exceptions: they may contain credentials,
            # source text or caller-controlled response bodies.
            with self.sessions.begin() as db:
                transition_lock(db)
                item = db.get(RepositoryTrigger, binding_id)
                if item and item.lease_owner == lease:
                    item.last_error = (
                        "GitLab polling could not complete; retry scheduled"
                    )
            return 1
        finally:
            with self.sessions.begin() as db:
                transition_lock(db)
                item = db.get(RepositoryTrigger, binding_id)
                if item and item.lease_owner == lease:
                    item.lease_owner = item.lease_until = None
                    item.next_poll_at = time.time() + 30

    def poll(self, stopped):
        while not stopped.is_set():
            try:
                self.poll_once()
            except Exception:
                # A transient database outage must not kill the recovery thread.
                pass
            stopped.wait(2)
