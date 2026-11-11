"""The filing state of one return (one model, one period): the single place that maps facts to a state.

Two tables record whether a return is filed, and the calendar adds the date. Every reader hands
what it observed to ``derive_filing_state``; none re-derives the state from a stored value. The
rules, the state table and who reads it are in ``docs/filing-state.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Optional

from src.logger import get_logger

log = get_logger(__name__)

# Stored vocabulary: ``tax_filing_status.status`` and ``tax_computation_snapshots.status``. The
# snapshot table's CHECK constraint allows only the last two; a test pins them to the schema's.
STORED_PENDING = "PENDING"
STORED_DUE = "DUE"
STORED_OVERDUE = "OVERDUE"
STORED_COMPUTED = "COMPUTED"
STORED_FILED = "FILED"

# A deadline this close (in days) reads as due rather than pending.
DUE_SOON_DAYS = 15


class FilingState(str, Enum):
    """The closed set of states a return can be in. The value is what the calendar displays."""

    PENDING = "PENDING"  # not filed, deadline more than DUE_SOON_DAYS away
    DUE = "DUE"  # not filed, deadline within DUE_SOON_DAYS
    OVERDUE = "OVERDUE"  # not filed, deadline passed
    COMPUTED = "COMPUTED"  # a draft exists (saved or snapshotted); nothing filed yet
    FILED = "FILED"  # filed; the filed figures are the latest
    FILED_WITH_NEWER_DRAFT = "FILED_WITH_NEWER_DRAFT"  # filed, and a recomputed draft sits above it
    UNKNOWN = "UNKNOWN"  # the facts are missing or contradict each other; the reason says which

    @property
    def is_filed(self) -> bool:
        """True when a filed return exists for the period, whether or not a newer draft sits above it."""
        return self in (FilingState.FILED, FilingState.FILED_WITH_NEWER_DRAFT)


@dataclass(frozen=True)
class FilingObservation:
    """What a reader saw for one return. ``None`` means the reader has no such fact, not "no".

    ``status_row`` is the ``tax_filing_status.status`` value, ``snapshot_status`` and
    ``snapshot_version`` the latest ``tax_computation_snapshots`` row, and ``newer_draft`` whether
    that latest snapshot is a COMPUTED draft above a FILED version. ``deadline`` is needed only to
    tell pending, due and overdue apart; a reader that asks only whether the return is filed may
    leave it out.
    """

    status_row: Optional[str] = None
    snapshot_status: Optional[str] = None
    snapshot_version: Optional[int] = None
    newer_draft: bool = False
    deadline: Optional[date] = None
    today: Optional[date] = None


@dataclass(frozen=True)
class FilingStateResult:
    """A derived state and the one-line reason, so a bad stored value is diagnosable from logs."""

    state: FilingState
    reason: str


_KNOWN_ROW_VALUES = frozenset({STORED_PENDING, STORED_DUE, STORED_OVERDUE, STORED_COMPUTED, STORED_FILED})
_KNOWN_SNAPSHOT_VALUES = frozenset({STORED_COMPUTED, STORED_FILED})


def _unknown(reason: str) -> FilingStateResult:
    log.info("ℹ️ filing state unknown: %s", reason)
    return FilingStateResult(FilingState.UNKNOWN, reason)


def derive_filing_state(obs: FilingObservation) -> FilingStateResult:
    """Map one observation to a filing state. Pure: the only code that does."""
    if obs.status_row is not None and obs.status_row not in _KNOWN_ROW_VALUES:
        return _unknown(f"filing status row holds an unrecognised value {obs.status_row!r}")
    if obs.snapshot_status is not None and obs.snapshot_status not in _KNOWN_SNAPSHOT_VALUES:
        return _unknown(f"latest snapshot holds an unrecognised status {obs.snapshot_status!r}")
    if obs.newer_draft and obs.snapshot_status != STORED_COMPUTED:
        return _unknown("a newer draft is flagged but the latest snapshot is not a COMPUTED draft")

    if obs.newer_draft:
        return FilingStateResult(FilingState.FILED_WITH_NEWER_DRAFT, "a FILED version has a newer COMPUTED draft above it")
    if obs.snapshot_status == STORED_FILED:
        return FilingStateResult(FilingState.FILED, "the latest snapshot is FILED")
    if obs.status_row == STORED_FILED:
        # OSS, 347 and 390 are filed from the calendar and have no snapshot.
        return FilingStateResult(FilingState.FILED, "the filing status row is FILED")
    if obs.status_row == STORED_COMPUTED or obs.snapshot_status == STORED_COMPUTED:
        return FilingStateResult(FilingState.COMPUTED, "a draft is saved and no version is filed")

    if obs.deadline is None or obs.today is None:
        return _unknown("nothing is stored and no deadline or date was given to derive from")
    if obs.deadline < obs.today:
        return FilingStateResult(FilingState.OVERDUE, "not filed and the deadline has passed")
    if (obs.deadline - obs.today).days <= DUE_SOON_DAYS:
        return FilingStateResult(FilingState.DUE, f"not filed and the deadline is within {DUE_SOON_DAYS} days")
    return FilingStateResult(FilingState.PENDING, "not filed and the deadline is more than "
                             f"{DUE_SOON_DAYS} days away")
