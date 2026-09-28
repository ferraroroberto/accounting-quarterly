"""AppTest smoke coverage for the Duplicate Review tab (#92): renders against a
temp DB, scans for a synthetic file-hash duplicate, and applies the exclusion
through the UI button. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.database as database  # noqa: E402
from src.database import get_invoices, init_db, upsert_invoice  # noqa: E402


def _render_dedupe() -> None:
    from app import invoice_dedupe_tab

    invoice_dedupe_tab.render()


def _use_db(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(database, "_DB_PATH", path)


@pytest.fixture
def dedupe_db(tmp_path, monkeypatch):
    path = tmp_path / "dedupe_tab.db"
    init_db(path)
    for n, fname in ((1, "a.pdf"), (2, "b.pdf")):
        upsert_invoice({
            "filename": fname, "direction": "in", "file_hash": "SAMEHASH",
            "invoice_number": f"N{n}", "invoice_date": "2025-02-10",
            "vendor_name": "Example Vendor SL", "vendor_nif": "B00000000",
            "subtotal_eur": 100.0, "iva_amount": 21.0, "total_eur": 121.0,
        }, db_path=path)
    _use_db(monkeypatch, path)
    return path


def test_tab_renders_and_scans(dedupe_db):
    at = AppTest.from_function(_render_dedupe).run()
    assert not at.exception, f"Duplicate Review tab raised: {at.exception}"

    at.button(key="dedupe_scan").click().run()
    assert not at.exception, f"Duplicate Review tab raised: {at.exception}"
    assert at.metric[0].value == "1"  # one candidate group


def test_apply_all_excludes_the_loser(dedupe_db):
    at = AppTest.from_function(_render_dedupe).run()
    at.button(key="dedupe_scan").click().run()
    at.button(key="dedupe_apply_all").click().run()
    assert not at.exception, f"Duplicate Review tab raised: {at.exception}"

    rows = get_invoices(db_path=dedupe_db)
    excluded = [r for r in rows if r["excluded"]]
    assert len(excluded) == 1
    assert excluded[0]["excluded_reason"] == "duplicate"


def test_empty_db_shows_guidance(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    init_db(path)
    _use_db(monkeypatch, path)
    at = AppTest.from_function(_render_dedupe).run()
    assert not at.exception
    assert any("Invoice OCR" in i.value for i in at.info)
