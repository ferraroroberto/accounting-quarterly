"""Save Computed never un-files a FILED period (#187). Synthetic data, temp DB only."""
from __future__ import annotations

import functools
from datetime import date

import pytest
from streamlit.testing.v1 import AppTest

from src.database import get_connection, init_db, upsert_filing_status
from src.tax_engine import get_tax_calendar

YEAR, QUARTER = 2025, 1


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "accounting.db"
    init_db(path)
    return path


def _status_row(db_path):
    conn = get_connection(db_path)
    try:
        return conn.execute(
            "SELECT status, amount_eur, notes, filed_at FROM tax_filing_status "
            "WHERE year = ? AND model = '303' AND quarter = ?", (YEAR, QUARTER),
        ).fetchone()
    finally:
        conn.close()


def _calendar_303_q1(db_path):
    conn = get_connection(db_path)
    try:
        return next(d for d in get_tax_calendar(YEAR, db_conn=conn)
                    if d.model == "303" and d.quarter == QUARTER)
    finally:
        conn.close()


def _mark_filed(db_path) -> None:
    upsert_filing_status(YEAR, "303", QUARTER, "FILED", amount_eur=50.0,
                         notes="justificante 3030000000001",
                         filed_at=date(2025, 4, 18).isoformat(), db_path=db_path)


def test_computed_upsert_does_not_downgrade_a_filed_row(db_path):
    _mark_filed(db_path)
    written = upsert_filing_status(YEAR, "303", QUARTER, "COMPUTED", amount_eur=99.0,
                                   notes="later draft", db_path=db_path)

    assert written is False
    assert tuple(_status_row(db_path)) == ("FILED", 50.0, "justificante 3030000000001", "2025-04-18")
    assert _calendar_303_q1(db_path).status == "FILED"


def test_computed_and_filed_upserts_still_write(db_path):
    assert upsert_filing_status(YEAR, "303", QUARTER, "COMPUTED", amount_eur=10.0,
                                db_path=db_path) is True
    assert upsert_filing_status(YEAR, "303", QUARTER, "COMPUTED", amount_eur=20.0,
                                db_path=db_path) is True
    assert _status_row(db_path)["amount_eur"] == 20.0
    _mark_filed(db_path)
    assert _status_row(db_path)["status"] == "FILED"


def _save_computed_button() -> None:
    from app.views.tax_obligations import _save_filing_button

    _save_filing_button("303", 2025, 1, 99.0)


def test_save_computed_button_keeps_a_filed_period_filed(db_path, monkeypatch):
    import app.views.tax_obligations as tab

    _mark_filed(db_path)
    monkeypatch.setattr(tab, "upsert_filing_status",
                        functools.partial(upsert_filing_status, db_path=db_path))

    at = AppTest.from_function(_save_computed_button).run()
    at.button(key="btn_save_303_2025_1").click().run()
    assert not at.exception, at.exception

    assert _status_row(db_path)["status"] == "FILED"
    assert _calendar_303_q1(db_path).status == "FILED"
    assert not at.success
    assert [w.value for w in at.warning] == [
        "Modelo 303 Q1 is already filed — status left unchanged."
    ]
