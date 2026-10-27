"""Composition root. Custom factories are trusted, installed Python code."""

from contextlib import contextmanager
from importlib import import_module
from functools import partial

from factory.application.service import FactoryService
from factory.domain.policy import ExecutionPolicy


def load_factory(path):
    module_name, separator, attribute = path.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError("Adapter factory must use module:callable syntax")
    factory = getattr(import_module(module_name), attribute)
    if not callable(factory):
        raise ValueError("Adapter factory must be callable")
    return factory


def repository_adapter(identity, *, token, max_file_bytes, check_policy):
    if identity.provider == "github":
        from github_client import GitHubClient

        return GitHubClient(identity.path, token=token, max_file_bytes=max_file_bytes)
    from factory.adapters.gitlab import GitLabClient

    return GitLabClient(
        identity.path,
        project_id=identity.project_id,
        token=token,
        max_file_bytes=max_file_bytes,
        check_policy=check_policy,
    )


def settings_identity(settings):
    from factory.domain.repositories import RepositoryIdentity

    owner, _, name = settings.repository.rpartition("/")
    return RepositoryIdentity(
        provider=settings.repository_provider,
        host=settings.repository_host
        or {"github": "github.com", "gitlab": "gitlab.com"}[
            settings.repository_provider
        ],
        owner=owner,
        name=name,
        project_id=settings.gitlab_project_id,
    )


def build_service(settings, dispatch):
    from factory.domain.repositories import RepositoryCheckPolicy
    from factory.adapters.repository_state import source_scoped_state

    coding_path = (
        settings.coding_factory
        or {
            "api": "factory.adapters.agents:api_coding",
            "codex": "factory.adapters.agents:codex_coding",
            "claude": "factory.adapters.agents:claude_coding",
        }[settings.developer_agent]
    )
    observer = load_factory(settings.observer_factory)(settings)
    import os

    if os.environ.get("FACTORY_CONTROL_ENDPOINT"):
        from factory.adapters.control import ControlObserver

        observer = ControlObserver(observer, settings)

    def repository_client(repository):
        if repository != settings.repository:
            raise ValueError("Repository differs from worker configuration")
        return repository_adapter(
            settings_identity(settings),
            token=os.environ.get("GITLAB_TOKEN", "")
            if settings.repository_provider == "gitlab"
            else None,
            max_file_bytes=settings.max_file_bytes,
            check_policy=RepositoryCheckPolicy.model_validate_json(
                settings.repository_check_policy_json
            ),
        )

    # The conditional is at composition only; use cases know capabilities, not names.
    return FactoryService(
        repository=settings.repository,
        repositories=repository_client,
        planner=partial(load_factory(settings.planner_factory), settings),
        reviewer=partial(load_factory(settings.reviewer_factory), settings),
        coding_agent=partial(load_factory(coding_path), settings),
        dispatch=dispatch,
        policy=ExecutionPolicy(
            path_scope=settings.path_policy,
            max_files=settings.max_patch_iterations * settings.max_files_per_iteration,
            max_file_bytes=settings.max_file_bytes,
        ),
        state=source_scoped_state(
            load_factory(settings.state_factory)(settings), settings_identity(settings)
        ),
        observer=observer,
        labels=settings.labels,
        task_settings=settings.task,
    )


@contextmanager
def action_service(context, *, dispatch, state, observer=None, workspace_root=None):
    """Fresh service for a trusted resolved context; callers supply scoped state/dispatch.

    This is intentionally separate from legacy bootstrap: shared actions must not
    accidentally open the legacy global SQLite store or use environment credentials.
    Custom installed adapters must accept explicit credentials and agent_home.
    """
    from pathlib import Path
    from tempfile import TemporaryDirectory
    from factory.adapters.telemetry import LoggingObserver
    from factory.workflows.redaction import secret_scope, redact

    settings = context.settings
    credentials = context.credentials
    coding_path = (
        settings.coding_factory
        or {
            "api": "factory.adapters.agents:api_coding",
            "codex": "factory.adapters.agents:codex_coding",
            "claude": "factory.adapters.agents:claude_coding",
        }[settings.developer_agent]
    )
    with (
        secret_scope(credentials.values()),
        TemporaryDirectory(prefix="flumic-action-", dir=workspace_root) as directory,
    ):
        home = Path(directory)
        (home / "workspaces").mkdir()
        # In worker containers the CLI runs as the unprivileged agent user.
        import os

        if os.geteuid() == 0:
            from cli_agent import AGENT_UID, AGENT_GID

            os.chown(home, AGENT_UID, AGENT_GID)
            os.chown(home / "workspaces", AGENT_UID, AGENT_GID)
        kwargs = {"credentials": credentials, "agent_home": home}

        def repository_client(repository):
            if repository != settings.repository:
                raise ValueError("Repository does not match action context")
            identity = context.repository_identity
            return repository_adapter(
                identity,
                max_file_bytes=settings.max_file_bytes,
                token=credentials.get(identity.credential_purpose, ""),
                check_policy=context.repository_check_policy,
            )

        try:
            yield FactoryService(
                repository=settings.repository,
                repositories=repository_client,
                planner=partial(
                    load_factory(settings.planner_factory), settings, **kwargs
                ),
                reviewer=partial(
                    load_factory(settings.reviewer_factory), settings, **kwargs
                ),
                coding_agent=partial(load_factory(coding_path), settings, **kwargs),
                dispatch=dispatch,
                state=state,
                observer=observer or LoggingObserver(),
                policy=ExecutionPolicy(
                    path_scope=settings.path_policy,
                    max_files=settings.max_patch_iterations
                    * settings.max_files_per_iteration,
                    max_file_bytes=settings.max_file_bytes,
                ),
                labels=settings.labels,
                task_settings=settings.task,
            )
        except Exception as error:
            # Preserve ordinary domain failures and retry semantics. If an adapter
            # echoes a grant, replace the exception at the action boundary.
            safe = redact(str(error))
            if safe != str(error):
                from factory.workflows.contracts import ActionExecutionError

                raise ActionExecutionError(safe[:4000]) from None
            raise


def run_builtin(
    action, context, inputs, transport, *, observer=None, service_factory=action_service
):
    """Compose one installed action from authenticated pinned state/dispatch ports.

    This does not turn worker request labels into authority. The transport must
    authenticate the assignment lease, use stored provenance, and durably accept
    follow-up intents (#11). Never fall back to legacy SQLite/static dispatch.
    """
    from factory.workflows.actions import ActionCatalog
    from factory.workflows.project_dispatch import ProjectDispatch, ProjectBindings
    from factory.adapters.state import ExecutionStateStore

    if action == "core.echo@1":
        return ActionCatalog(service_factory=service_factory).execute(
            action, context, inputs, dispatch=None, state=None, observer=observer
        )
    bindings = ProjectBindings.model_validate_json(context.followup_bindings_json)
    dispatch = ProjectDispatch(context, bindings, transport)
    state = ExecutionStateStore(context, transport)
    return ActionCatalog(service_factory=service_factory).execute(
        action, context, inputs, dispatch=dispatch, state=state, observer=observer
    )
