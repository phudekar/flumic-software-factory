"""Composition root. Custom factories are trusted, installed Python code."""

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
