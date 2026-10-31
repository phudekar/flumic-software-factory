"""Shared worker entry point; importing creates no clients or registrations."""


def main():
    import os

    protocol = os.environ.get("FACTORY_WORKER_PROTOCOL", "shared-v1")
    if protocol != "shared-v1":
        raise ValueError(
            "Static worker execution is retired. Provision a shared-v1 worker "
            "through a Control Pane worker pool."
        )
    from factory.shared_worker import start_shared

    return start_shared()


if __name__ == "__main__":
    main()
