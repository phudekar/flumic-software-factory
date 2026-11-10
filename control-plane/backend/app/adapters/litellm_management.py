"""Read-only LiteLLM catalog discovery."""
import json
from time import monotonic

import httpx

from app.ports import ConnectionCapabilities, GatewayVerificationError


class LiteLLMAdministration:
    def verify(self, endpoint: str, aliases: tuple[str, ...], token: str) -> ConnectionCapabilities:
        deadline = monotonic() + 5

        try:
            with httpx.Client(trust_env=False, follow_redirects=False, timeout=5) as client:
                with client.stream(
                    "GET", endpoint + "/model/info",
                    headers={"Authorization": "Bearer " + token, "Accept-Encoding": "identity"},
                ) as response:
                    response.raise_for_status()
                    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                        raise ValueError("Unsupported encoding")
                    payload = bytearray()
                    for chunk in response.iter_raw():
                        if monotonic() >= deadline or len(payload) + len(chunk) > 1024 * 1024:
                            raise ValueError("Catalog too large")
                        payload.extend(chunk)
            rows = json.loads(payload)["data"]
            if not isinstance(rows, list):
                raise ValueError("Expected model list")
            names = {row["model_name"] for row in rows if isinstance(row.get("model_name"), str)}
        except (AttributeError, KeyError, TypeError, ValueError, httpx.HTTPError):
            raise GatewayVerificationError("Gateway model discovery failed") from None

        configured = set(aliases)
        return ConnectionCapabilities(
            available_aliases=tuple(sorted(configured & names)),
            missing_aliases=tuple(sorted(configured - names)),
        )
