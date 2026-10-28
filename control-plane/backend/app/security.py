"""No raw credentials or model content in normal logs."""

import hashlib
import os
import re
from factory.domain.safety import find_sensitive_categories


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def redact(value):
    from factory.workflows.redaction import redact as action_redact

    value = action_redact(value)
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


def redact_truncated_secret_tail(value, secrets):
    """Mask a known credential cut at the read boundary, in linear bounded work."""
    for secret in sorted(set(filter(None, secrets)), key=len, reverse=True):
        if not isinstance(secret, str) or value.endswith(secret):
            continue
        # Prefix-function matching avoids building thousands of regex prefixes
        # for the largest allowed grant. None cannot collide with a character.
        sequence = (
            list(secret)
            + [None]
            + list(value[-max(0, len(secret) - 1) :] if len(secret) > 1 else "")
        )
        prefixes = [0] * len(sequence)
        for index in range(1, len(sequence)):
            matched = prefixes[index - 1]
            while matched and sequence[index] != sequence[matched]:
                matched = prefixes[matched - 1]
            if sequence[index] == sequence[matched]:
                matched += 1
            prefixes[index] = matched
        overlap = prefixes[-1]
        if overlap:
            value = value[:-overlap] + "[REDACTED]"
    return value
