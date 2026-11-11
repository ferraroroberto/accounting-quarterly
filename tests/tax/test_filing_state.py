"""The filing state: one derive function, its table test, and the calendar that reads it.

Synthetic data, temp DB only.
"""
from __future__ import annotations

import logging
from datetime import date

import pytest

from src.database import (
    SNAPSHOT_COMPUTED,
    SNAPSHOT_FILED,
    filed_snapshot_periods,
    get_connection,
    init_db,
    upsert_filing_status,
)
from src.returns.filing_state import (
    STORED_COMPUTED,
    STORED_FILED,
    FilingObservation,
    FilingState,
    derive_filing_state,
)
from src.tax_engine import get_tax_calendar
from src.tax_models import FilingStatus

TODAY = date(2025, 7, 10)
FAR = date(2025, 9, 1)  # more than 15 days after TODAY
SOON = date(2025, 7, 20)  # within 15 days of TODAY
PAST = date(2025, 4, 20)  # before TODAY


def _obs(**kw) -> FilingObservation:
    return FilingObservation(deadline=kw.pop("deadline", FAR), today=TODAY, **kw)


# (case id, observation, expected state)
TABLE = [
    ("no row, far deadline", _obs(), FilingState.PENDING),
    ("no row, deadline within 15 days", _obs(deadline=SOON), FilingState.DUE),
    ("no row, deadline today", _obs(deadline=TODAY), FilingState.DUE),
    ("no row, deadline passed", _obs(deadline=PAST), FilingState.OVERDUE),
    ("stored PENDING row is date-derived", _obs(status_row="PENDING", deadline=PAST), FilingState.OVERDUE),
    ("COMPUTED row", _obs(status_row="COMPUTED", deadline=PAST), FilingState.COMPUTED),
    ("COMPUTED snapshot only", _obs(snapshot_status="COMPUTED", snapshot_version=1), FilingState.COMPUTED),
    ("FILED row, no snapshot", _obs(status_row="FILED", deadline=PAST), FilingState.FILED),
    ("FILED row and FILED snapshot", _obs(status_row="FILED", snapshot_status="FILED", snapshot_version=2),
     FilingState.FILED),
    ("FILED snapshot, stale COMPUTED row", _obs(status_row="COMPUTED", snapshot_status="FILED", snapshot_version=2),
     FilingState.FILED),
    ("FILED snapshot with a newer COMPUTED draft",
     _obs(status_row="FILED", snapshot_status="COMPUTED", snapshot_version=3, newer_draft=True),
     FilingState.FILED_WITH_NEWER_DRAFT),
    ("FILED row, COMPUTED snapshot, no filed version",
     _obs(status_row="FILED", snapshot_status="COMPUTED", snapshot_version=1), FilingState.FILED),
    ("unrecognised row value", _obs(status_row="FILED "), FilingState.UNKNOWN),
    ("unrecognised snapshot value", _obs(snapshot_status="DRAFT", snapshot_version=1), FilingState.UNKNOWN),
    ("newer draft flagged on a FILED snapshot", _obs(snapshot_status="FILED", snapshot_version=2, newer_draft=True),
     FilingState.UNKNOWN),
    ("nothing stored and no deadline", FilingObservation(), FilingState.UNKNOWN),
]


@pytest.mark.parametrize("obs,expected", [(o, e) for _, o, e in TABLE], ids=[c for c, _, _ in TABLE])
def test_derive_filing_state_table(obs, expected):
    result = derive_filing_state(obs)

    assert result.state is expected
    assert result.reason


def test_unknown_is_logged_at_info(caplog):
    obs = _obs(status_row="BOGUS")
    logger = logging.getLogger("src.returns.filing_state")
    logger.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.INFO, logger="src.returns.filing_state"):
            derive_filing_state(obs)
    finally:
        logger.removeHandler(caplog.handler)

    assert any("filing state unknown" in r.getMessage() and "BOGUS" in r.getMessage() for r in caplog.records)


def test_stored_vocabulary_matches_the_schema_and_the_calendar_type():
    assert (STORED_COMPUTED, STORED_FILED) == (SNAPSHOT_COMPUTED, SNAPSHOT_FILED)
    assert {s.value for s in FilingState} == set(FilingStatus.__args__)


# ---------------------------------------------------------------------------
# readers
# ---------------------------------------------------------------------------

YEAR = 2025


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return TODAY


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    monkeypatch.setattr("src.tax_engine.date", _FrozenDate)
    path = tmp_path / "accounting.db"
    init_db(path)
    return path


def _calendar(db_path) -> dict[tuple[str, int | None], tuple[str, float | None]]:
    conn = get_connection(db_path)
    try:
        return {(d.model, d.quarter): (d.status, d.amount_eur)
                for d in get_tax_calendar(YEAR, db_conn=conn, config={"tax": {"oss_registered": True}})}
    finally:
        conn.close()


def _put(db_path, model, quarter, status, amount=None):
    upsert_filing_status(YEAR, model, quarter, status, amount_eur=amount, db_path=db_path)


def test_calendar_without_rows_is_date_derived(db_path):
    cal = _calendar(db_path)

    assert cal[("303", 1)] == ("OVERDUE", None)  # deadline 2025-04-20 < TODAY
    assert cal[("303", 2)] == ("DUE", None)  # deadline 2025-07-20 is 10 days out
    assert cal[("390", None)][0] == "PENDING"


def test_calendar_filed_row_is_filed_for_quarterly_and_annual(db_path):
    _put(db_path, "303", 1, "FILED", 50.0)
    _put(db_path, "OSS", 2, "FILED", 7.0)
    _put(db_path, "347", None, "FILED", 9.0)

    cal = _calendar(db_path)

    assert cal[("303", 1)] == ("FILED", 50.0)
    assert cal[("OSS", 2)] == ("FILED", 7.0)
    assert cal[("347", None)] == ("FILED", 9.0)
    assert cal[("130", 1)][0] == "OVERDUE"


def test_calendar_stored_pending_row_is_still_date_derived(db_path):
    _put(db_path, "349", 1, "PENDING")

    assert _calendar(db_path)[("349", 1)][0] == "OVERDUE"


def test_calendar_shows_computed_for_a_period_saved_as_computed(db_path):
    _put(db_path, "303", 1, "COMPUTED", 123.0)

    assert _calendar(db_path)[("303", 1)] == ("COMPUTED", 123.0)


def test_calendar_shows_unknown_for_an_unrecognised_stored_value(db_path):
    conn = get_connection(db_path)
    try:
        conn.execute("INSERT INTO tax_filing_status (year, model, quarter, status) VALUES (?, '130', 1, 'BOGUS')",
                     (YEAR,))
        conn.commit()
    finally:
        conn.close()

    assert _calendar(db_path)[("130", 1)][0] == "UNKNOWN"


# --- the vendor / close guard -----------------------------------------------

def _snapshot(conn, quarter, model, version, status):
    conn.execute(
        "INSERT INTO tax_computation_snapshots (year, quarter, model, snapshot_version, status, payload_json, computed_at) "
        "VALUES (?, ?, ?, ?, ?, '{}', '2025-01-01T00:00:00')", (YEAR, quarter, model, version, status))


def test_filed_guard_locks_filed_and_filed_with_newer_draft_but_not_a_draft(db_path):
    conn = get_connection(db_path)
    try:
        _snapshot(conn, 1, "303", 1, "COMPUTED")
        _snapshot(conn, 1, "303", 2, "FILED")  # Q1 filed
        _snapshot(conn, 2, "303", 1, "COMPUTED")
        _snapshot(conn, 2, "303", 2, "FILED")
        _snapshot(conn, 2, "303", 3, "COMPUTED")  # Q2 filed, with a newer draft above it
        _snapshot(conn, 3, "303", 1, "COMPUTED")  # Q3 only a draft
        _snapshot(conn, 0, "347", 1, "COMPUTED")
        _snapshot(conn, 0, "347", 2, "FILED")  # annual filed: covers the four quarters of its year
        conn.commit()
        locked = filed_snapshot_periods(conn)
    finally:
        conn.close()

    assert locked == {(YEAR, 1), (YEAR, 2), (YEAR, 3), (YEAR, 4)}


def test_filed_guard_ignores_drafts_and_status_only_filings(db_path):
    _put(db_path, "OSS", 1, "FILED", 5.0)  # a status-only filing is out of the guard's scope
    conn = get_connection(db_path)
    try:
        _snapshot(conn, 3, "303", 1, "COMPUTED")
        conn.commit()
        assert filed_snapshot_periods(conn) == set()
    finally:
        conn.close()


# --- state-machine-proof: filing-state --------------------------------------

def test_illegal_transition_is_refused_a_filed_period_is_never_reopened(db_path):
    """state-machine-proof: filing-state"""
    _put(db_path, "303", 1, "FILED", 50.0)

    assert upsert_filing_status(YEAR, "303", 1, "COMPUTED", amount_eur=99.0, db_path=db_path) is False
    assert _calendar(db_path)[("303", 1)] == ("FILED", 50.0)

    # an observation that claims a newer draft above a snapshot that is not a draft has no state
    assert derive_filing_state(_obs(snapshot_status="FILED", snapshot_version=2, newer_draft=True)).state \
        is FilingState.UNKNOWN
