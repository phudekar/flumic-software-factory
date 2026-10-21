"""Worker entry point. Importing this module creates no clients or workflows."""

from factory.settings import Settings


def main():
    from factory.adapters.telemetry import configure_logging

    import os
    from factory.configuration import load_managed_settings

    settings = (
        load_managed_settings(os.environ)
        if os.environ.get("FACTORY_CONTROL_ENDPOINT")
        else Settings.load()
    )
    configure_logging(settings)
    from factory.startup import run_startup_script

    run_startup_script(settings)
    if settings.orchestrator == "local":
        from factory.adapters.orchestration.local import main as local_main

        return local_main(settings)

    from factory.adapters.orchestration.hatchet import (
        HatchetDispatch,
        build_worker,
        create_client,
    )

    from factory.bootstrap import build_service

    dispatch = HatchetDispatch()
    service = build_service(settings, dispatch)
    try:
        build_worker(create_client(settings), service, settings, dispatch).start()
    finally:
        shutdown = getattr(service.observer, "shutdown", None)
        if shutdown:
            shutdown()


if __name__ == "__main__":
    main()
