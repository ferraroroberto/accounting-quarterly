"""Filing / direct-debit deadlines and the Obligations calendar dates (#165)."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from src.tax_deadlines import _easter_sunday, calendar_deadline, filing_deadline
from src.tax_engine import get_tax_calendar


@pytest.mark.parametrize("year,quarter,last_day,debit", [
    (2026, 1, date(2026, 4, 20), date(2026, 4, 15)),
    (2026, 2, date(2026, 7, 20), date(2026, 7, 15)),
    (2026, 3, date(2026, 10, 20), date(2026, 10, 15)),
    (2025, 4, date(2026, 1, 30), date(2026, 1, 27)),   # AEAT 2026 calendar: debit 1–27 January
    (2025, 2, date(2025, 7, 21), date(2025, 7, 16)),   # 20 July 2025 was a Sunday
    (2026, 4, date(2027, 2, 1), date(2027, 1, 27)),    # 30 January 2027 is a Saturday
])
def test_deadlines(year, quarter, last_day, debit):
    for model in ("303", "130"):
        d = filing_deadline(model, year, quarter)
        assert (d.last_day, d.direct_debit_last_day) == (last_day, debit), model
    d349 = filing_deadline("349", year, quarter)
    assert d349.last_day == last_day and d349.direct_debit_last_day is None


def test_deadline_extra_holidays_and_easter():
    assert _easter_sunday(2024) == date(2024, 3, 31)
    assert _easter_sunday(2025) == date(2025, 4, 20)
    assert _easter_sunday(2026) == date(2026, 4, 5)
    d = filing_deadline("303", 2026, 3, extra_holidays=[date(2026, 10, 20)])
    assert d.last_day == date(2026, 10, 21) and d.nominal == date(2026, 10, 20)
    with pytest.raises(ValueError):
        filing_deadline("390", 2026, 4)


@pytest.mark.parametrize("model,year,quarter,expected", [
    ("303", 2026, 1, date(2026, 4, 20)),
    ("130", 2025, 2, date(2025, 7, 21)),    # 20 July 2025 was a Sunday
    ("349", 2026, 4, date(2027, 2, 1)),     # 30 January 2027 is a Saturday
    ("OSS", 2026, 1, date(2026, 4, 30)),
    ("OSS", 2025, 2, date(2025, 7, 31)),
    ("OSS", 2026, 3, date(2026, 11, 2)),    # 31 October 2026 is a Saturday
    ("390", 2025, 1, date(2026, 1, 30)),
    ("390", 2026, 1, date(2027, 2, 1)),     # 30 January 2027 is a Saturday
    ("347", 2025, 1, date(2026, 3, 2)),  # 28 Feb 2026 is a Saturday
])
def test_calendar_deadline(model, year, quarter, expected):
    assert calendar_deadline(model, year, quarter) == expected


def test_calendar_deadline_rejects_unknown_model():
    with pytest.raises(ValueError):
        calendar_deadline("100", 2026, 1)


def test_calendar_agrees_with_the_filing_sheet():
    """The Obligations calendar and the filing sheet show the same date for every quarterly return."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE tax_filing_status (model TEXT, quarter INTEGER, year INTEGER, status TEXT, amount_eur REAL)")
    for year in (2025, 2026, 2027):
        calendar = {(d.model, d.quarter): d.deadline for d in get_tax_calendar(year, db_conn=conn)}
        for model in ("303", "130", "349"):
            for quarter in (1, 2, 3, 4):
                assert calendar[(model, quarter)] == filing_deadline(model, year, quarter).last_day
