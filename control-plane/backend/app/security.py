"""No raw credentials or model content in normal logs."""

import hashlib
import os
import re
from factory.domain.safety import find_sensitive_categories


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def redact(value):
    if isinstance(value, dict):
        return {str(k): redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    for key, secret in os.environ.items():
        if (
            any(part in key for part in ("TOKEN", "PASSWORD", "SECRET", "API_KEY"))
            and len(secret) >= 8
        ):
            value = value.replace(secret, "[REDACTED]")
    value = re.sub(
        r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{12,}", "Bearer [REDACTED]", value
    )
    # Entire affected lines are removed; do not retain partial private keys.
    value = re.sub(
        r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
        "[REDACTED PRIVATE KEY]",
        value,
        flags=re.S,
    )
    value = "\n".join(
        "[REDACTED sensitive line]" if find_sensitive_categories(line) else line
        for line in value.splitlines()
    )
    raw = value.encode("utf-8")
    return raw[:65536].decode("utf-8", errors="ignore") + (
        "\n[TRUNCATED]" if len(raw) > 65536 else ""
    )
