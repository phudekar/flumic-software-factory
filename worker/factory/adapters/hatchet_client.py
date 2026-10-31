"""Hatchet transport construction shared by current runtime adapters."""

def create_client(settings):
    from hatchet_sdk import Hatchet
    from hatchet_sdk.config import ClientConfig, ClientTLSConfig

    return Hatchet(
        config=ClientConfig(
            tls_config=ClientTLSConfig(strategy=settings.hatchet_tls_strategy)
        )
    )
