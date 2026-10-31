"""Read-only provenance inventory for explicit legacy retirement maintenance.

No startup hook calls this module. Applying deletion requires additional backup,
manifest and quiescence checks; inventory alone never authorizes deletion.
"""
import hashlib
import json

from sqlalchemy import select
from app.database import (
    Worker, Event, Conversation, Operation, Assignment, RuntimeResourceBinding,
)


def inspect_retirement(db):
    """Return deterministic identities only; refuse ambiguous execution ownership."""
    workers = db.scalars(select(Worker).where(Worker.protocol == 'legacy')).all()
    worker_ids = sorted(worker.id for worker in workers)
    if any(worker.desired_state not in {'stopped', 'removed'} for worker in workers):
        raise ValueError('Retired workers must be stopped or removed')
    if db.scalar(select(Operation.id).where(
        Operation.worker_id.in_(worker_ids),
        Operation.status.in_(['pending', 'running']),
    ).limit(1)):
        raise ValueError('Retired workers have pending lifecycle operations')
    for model in (Assignment, RuntimeResourceBinding):
        if db.scalar(select(model.id).where(model.worker_id.in_(worker_ids)).limit(1)):
            raise ValueError('Retired workers are referenced by retained runtime data')
    events = db.scalars(select(Event).where(Event.worker_id.in_(worker_ids))).all()
    if any(event.assignment_id or event.execution_id for event in events):
        raise ValueError('Retired events have project execution attribution')
    event_ids = sorted(event.id for event in events)
    conversations = db.scalars(select(Conversation).where(
        Conversation.id.in_(event_ids))).all()
    operations = db.scalars(select(Operation).where(
        Operation.worker_id.in_(worker_ids))).all()
    # Hash complete candidate rows so content changes invalidate an approved
    # manifest without disclosing credentials, model payloads or ciphertext.
    digest = hashlib.sha256()
    for records in (workers, events, conversations, operations):
        for record in sorted(records, key=lambda item: item.id):
            value = {
                column.name: getattr(record, column.name)
                for column in record.__table__.columns
            }
            encoded = json.dumps(
                [record.__tablename__, value], sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode()
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return {
        'fingerprint': digest.hexdigest(),
        'version': 1,
        'worker_ids': worker_ids,
        'event_ids': event_ids,
        'conversation_ids': sorted(item.id for item in conversations),
        'operation_ids': sorted(item.id for item in operations),
    }
