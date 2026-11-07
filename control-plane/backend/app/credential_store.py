"""Resolve credential values without granting ownership or assignment authority."""

from pathlib import Path

from fastapi import HTTPException

from factory.adapters.openbao import OpenBao, OpenBaoError, credential_path


def openbao_client(settings):
    if not settings.openbao_address or not settings.openbao_token_file:
        raise OpenBaoError("OpenBao credential store is unavailable")
    with Path(settings.openbao_token_file).open(encoding="utf-8") as stream:
        token = stream.read(4097).strip()
    if not token or len(token) > 4096 or any(char.isspace() for char in token):
        raise OpenBaoError("OpenBao credential store authentication is unavailable")
    return OpenBao(
        settings.openbao_address,
        token=token,
        ca_file=settings.openbao_ca_file,
    )


def read_credential(secret, settings):
    if secret.openbao_version is None:
        from app.project_context import decrypt

        return decrypt(secret.ciphertext, settings)
    if secret.openbao_version < 1:
        raise HTTPException(409, "Credential storage is pending")
    try:
        client = openbao_client(settings)
        path = credential_path(settings.openbao_mount, secret.project_id, secret.id)
        return client.read(path, secret.openbao_version)
    except (OpenBaoError, ValueError, OSError):
        raise HTTPException(503, "OpenBao credential is unavailable") from None


def wrap_credential(secret, settings, *, seconds_remaining):
    ttl = min(30, int(seconds_remaining))
    if ttl < 1:
        raise HTTPException(409, "Assignment lease has insufficient time remaining")
    try:
        path = credential_path(settings.openbao_mount, secret.project_id, secret.id)
        token = openbao_client(settings).wrap(path, secret.openbao_version, ttl)
        return {"path": path, "version": secret.openbao_version, "token": token}
    except (OpenBaoError, ValueError, OSError):
        raise HTTPException(503, "OpenBao credential delivery is unavailable") from None


def store_credential(project_id, credential_id, value, settings):
    """Create a credential once, reconciling an ambiguous write by exact value."""
    from hmac import compare_digest

    try:
        client = openbao_client(settings)
        path = credential_path(settings.openbao_mount, project_id, credential_id)
        try:
            return client.create(path, value)
        except OpenBaoError:
            existing = client.read(path, 1)
            if not compare_digest(existing.encode(), value.encode()):
                raise HTTPException(409, "OpenBao credential conflicts with the intended value")
            return 1
    except (OpenBaoError, ValueError, OSError):
        raise HTTPException(503, "OpenBao credential storage is unavailable") from None
