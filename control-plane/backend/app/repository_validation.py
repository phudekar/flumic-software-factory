"""Read remote identity outside DB locks; append an owned immutable validation pin."""

import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener

from fastapi import Depends, HTTPException, Request as APIRequest
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.database import RepositoryConnection, RepositoryRevision
from app.projects import (
    request_scope,
    require_owned,
    project_parameter,
    repository_public,
)
from app.project_context import require_secret, decrypt
from factory.domain.repositories import RepositoryLocation, RepositoryIdentity
from factory.adapters.gitlab import NoGitLabRedirects
from factory.domain.models import RepositoryError


class ValidateRepository(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parent_id: str = Field(min_length=1, max_length=120)


def resolve_gitlab_project(owner, name, token):
    location = RepositoryLocation(
        provider="gitlab", host="gitlab.com", owner=owner, name=name
    )
    request = Request(
        "https://gitlab.com/api/v4/projects/" + quote(location.path, safe=""),
        headers={"PRIVATE-TOKEN": token, "Accept": "application/json"},
    )
    try:
        with build_opener(NoGitLabRedirects()).open(request, timeout=30) as response:
            raw = response.read(1048577)
        if len(raw) > 1048576:
            raise ValueError()
        value = json.loads(raw)
        path = value["path_with_namespace"]
        if not isinstance(path, str) or path.lower() != location.path.lower():
            raise ValueError()
        namespace, _, project = path.rpartition("/")
        return RepositoryIdentity(
            provider="gitlab",
            host="gitlab.com",
            owner=namespace,
            name=project,
            project_id=value["id"],
        )
    except HTTPError as error:
        error.close()
        raise RepositoryError(
            "GitLab repository identity could not be verified"
        ) from None
    except (URLError, OSError, TimeoutError, ValueError, KeyError, TypeError):
        raise RepositoryError(
            "GitLab repository identity could not be verified"
        ) from None


def install(app, sessions, settings, row):
    @app.post(
        "/api/v1/projects/{project_id}/repositories/{identifier}/validate",
        dependencies=[Depends(project_parameter)],
    )
    def validate(identifier: str, body: ValidateRepository, request: APIRequest):
        with sessions() as db:
            scope = request_scope(request, db)
            item = require_owned(db, scope, RepositoryConnection, identifier)
            parent = require_owned(db, scope, RepositoryRevision, body.parent_id)
            if parent.repository_id != item.id:
                raise HTTPException(404, "Resource not found")
            if item.revision_id != parent.id:
                raise HTTPException(409, "Repository changed; reload before validation")
            document = dict(parent.document)
            if item.provider != "gitlab":
                raise HTTPException(422, "Remote identity validation is for GitLab.com")
            credential = document.get("credential_id")
            if not credential:
                raise HTTPException(
                    409, "Configure a GitLab credential before validation"
                )
            secret = require_secret(db, scope, credential, "gitlab", item.id)
            token = decrypt(secret.ciphertext, settings)
        try:
            identity = resolve_gitlab_project(
                document["owner"], document["name"], token
            )
        except (RepositoryError, ValueError):
            raise HTTPException(
                409, "GitLab repository identity could not be verified"
            ) from None
        expected = (document.get("identity") or {}).get("project_id") or document.get(
            "expected_project_id"
        )
        if expected is not None and identity.project_id != expected:
            raise HTTPException(
                409,
                "Path now resolves to another GitLab project; create a new connection",
            )
        with sessions.begin() as db:
            item = db.scalar(
                select(RepositoryConnection)
                .where(
                    RepositoryConnection.id == identifier,
                    RepositoryConnection.project_id == scope.project_id,
                )
                .with_for_update()
            )
            if item is None:
                raise HTTPException(404, "Resource not found")
            if item.revision_id != body.parent_id:
                raise HTTPException(409, "Repository changed during validation; reload")
            require_secret(db, scope, credential, "gitlab", item.id)
            document.update(
                identity=identity.model_dump(mode="json"),
                owner=identity.owner,
                name=identity.name,
            )
            document.pop("expected_project_id", None)
            revision = RepositoryRevision(
                project_id=scope.project_id,
                repository_id=item.id,
                parent_id=body.parent_id,
                document=document,
            )
            db.add(revision)
            db.flush()
            item.owner, item.name, item.revision_id = (
                identity.owner,
                identity.name,
                revision.id,
            )
            db.flush()
            return repository_public(db, row, item)
