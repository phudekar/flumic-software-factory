"""Pure source-observation policy shared by webhook and polling acceptance."""

from dataclasses import dataclass, replace
from typing import Literal


@dataclass(frozen=True)
class Observation:
    eligible: bool = False
    epoch: int = 0
    last_dispatched_epoch: int = 0
    last_stamp: str = ""
    dispatched_stamp: str = ""


def observe(
    kind: str,
    previous: Observation,
    *,
    eligible: bool,
    stamp: str,
    active: bool,
) -> tuple[Observation, Literal["accepted", "coalesced", "ignored"]]:
    """Keep pending changes while an existing run owns the work item.

    Ready issues use observed eligibility edges: source comments and timestamp
    changes cannot cause an endless agent loop. Feedback and verification use
    immutable note IDs and merge commit IDs supplied by a fresh source read.
    """
    if kind not in {"issue-ready", "feedback", "verify"}:
        raise ValueError("unsupported repository trigger kind")
    current = replace(
        previous,
        eligible=eligible,
        epoch=previous.epoch + int(eligible and not previous.eligible),
        last_stamp=stamp,
    )
    if kind == "issue-ready":
        changed = current.epoch > previous.last_dispatched_epoch
    elif kind == "feedback":
        if stamp and (not stamp.isascii() or not stamp.isdecimal() or len(stamp) > 19):
            raise ValueError("feedback requires a bounded numeric note ID")
        changed = bool(stamp) and int(stamp) > int(previous.dispatched_stamp or "0")
    else:
        changed = bool(stamp) and stamp != previous.dispatched_stamp
    pending = eligible and changed
    if not pending:
        return current, "ignored"
    if active:
        return current, "coalesced"
    return replace(
        current,
        last_dispatched_epoch=current.epoch,
        dispatched_stamp=stamp,
    ), "accepted"
