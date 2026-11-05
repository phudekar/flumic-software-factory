"""Offline credential migration. PostgreSQL changes commit together."""

import hashlib
import json
from hmac import compare_digest

from sqlalchemy import select, text

from app.credential_store import read_credential, store_credential
from app.database import Assignment, SecretVersion, WorkflowRun
from app.project_context import decrypt
from app.workflow_state import transition_lock


def inventory(db):
    rows = db.scalars(select(SecretVersion).order_by(SecretVersion.id)).all()
    digest = hashlib.sha256()
    for row in rows:
        encoded = json.dumps(
            [row.id, row.project_id, row.openbao_version, row.revoked_at,
             row.repository_id, row.purpose, row.ciphertext],
            separators=(",", ":"),
        ).encode()
        digest.update(encoded + b"\n")
    return {
        "count": len(rows),
        "database_only": sum(row.openbao_version is None for row in rows),
        "pending": sum(
            row.openbao_version == 0 and row.revoked_at is None for row in rows
        ),
        "pending_versions": [
            {"id": row.id, "project_id": row.project_id, "revoked": row.revoked_at is not None}
            for row in rows if row.openbao_version == 0
        ],
        "retained_ciphertext": sum(row.ciphertext is not None for row in rows),
        "fingerprint": digest.hexdigest(),
    }


def migrate_credentials(db, settings, *, expected_count, clear=False, fingerprint=None):
    transition_lock(db)
    if db.get_bind().dialect.name == "postgresql":
        db.execute(text("LOCK TABLE secret_versions IN SHARE ROW EXCLUSIVE MODE"))
    for model, column in ((WorkflowRun, WorkflowRun.state), (Assignment, Assignment.status)):
        if db.scalar(select(model.id).where(
            column.not_in(("completed", "failed", "cancelled"))
        ).limit(1)):
            raise ValueError("Drain workflow runs and assignments before credential migration")
    before = inventory(db)
    if (
        before["count"] != expected_count
        or before["pending"]
        or fingerprint is not None and before["fingerprint"] != fingerprint
    ):
        raise ValueError("Credential inventory changed or contains pending writes")
    for row in db.scalars(select(SecretVersion).with_for_update()):
        if row.openbao_version == 0:
            # Only explicitly revoked pending rows can reach this point.
            continue
        if row.openbao_version is None:
            if clear:
                raise ValueError("Every credential must be copied before clearing ciphertext")
            value = decrypt(row.ciphertext, settings)
            row.openbao_version = store_credential(row.project_id, row.id, value, settings)
        elif row.ciphertext is not None:
            value = decrypt(row.ciphertext, settings)
        else:
            # Already moved; verify the referenced secret is still available.
            read_credential(row, settings)
            continue
        actual = read_credential(row, settings)
        if not compare_digest(value.encode(), actual.encode()):
            raise ValueError("OpenBao read-back did not match the original credential")
        if clear:
            row.ciphertext = None
    db.flush()
    return inventory(db)
