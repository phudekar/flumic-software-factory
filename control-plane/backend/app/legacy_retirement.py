"""Read-only provenance inventory for explicit legacy retirement maintenance.

No startup hook calls this module. Applying deletion requires additional backup,
manifest and quiescence checks; inventory alone never authorizes deletion.
"""
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
    return {
        'version': 1,
        'worker_ids': worker_ids,
        'event_ids': event_ids,
        'conversation_ids': sorted(db.scalars(select(Conversation.id).where(
            Conversation.id.in_(event_ids))),
        ),
        'operation_ids': sorted(db.scalars(select(Operation.id).where(
            Operation.worker_id.in_(worker_ids))),
        ),
    }
