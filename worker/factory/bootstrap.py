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


def build_service(settings, dispatch):
    from github_client import GitHubClient

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
    # The conditional is at composition only; use cases know capabilities, not names.
    return FactoryService(
        repository=settings.repository,
        repositories=partial(GitHubClient, max_file_bytes=settings.max_file_bytes),
        planner=partial(load_factory(settings.planner_factory), settings),
        reviewer=partial(load_factory(settings.reviewer_factory), settings),
        coding_agent=partial(load_factory(coding_path), settings),
        dispatch=dispatch,
        policy=ExecutionPolicy(
            path_scope=settings.path_policy,
            max_files=settings.max_patch_iterations * settings.max_files_per_iteration,
            max_file_bytes=settings.max_file_bytes,
        ),
        state=load_factory(settings.state_factory)(settings),
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
    from github_client import GitHubClient
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
            return GitHubClient(
                repository,
                max_file_bytes=settings.max_file_bytes,
                token=credentials.get("github", ""),
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
