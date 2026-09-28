"""AppTest coverage for the Invoice Ledger tab (#90): renders against a temp DB,
and a per-invoice form edit persists and locks the field. Synthetic data only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.database as database  # noqa: E402
from src.database import get_invoice_by_filename, init_db, upsert_invoice  # noqa: E402


def _render_ledger() -> None:
    """AppTest script body (runs in-process, so the fixture's DB-path patch applies)."""
    from app import invoice_ledger

    invoice_ledger.render()


def _use_db(monkeypatch, path: Path) -> None:
    monkeypatch.setattr(database, "_DB_PATH", path)


@pytest.fixture
def ledger_db(tmp_path, monkeypatch):
    path = tmp_path / "ledger_tab.db"
    init_db(path)
    for n, day in ((1, "10"), (2, "11")):
        upsert_invoice({
            "filename": f"vendor/inv-{n}.pdf", "direction": "in",
            "invoice_date": f"2025-02-{day}", "vendor_name": "Example Vendor SL",
            "vendor_nif": "B00000000", "subtotal_eur": 100.0, "iva_amount": 21.0,
            "total_eur": 121.0,
        }, db_path=path)
    _use_db(monkeypatch, path)
    return path


def test_tab_renders_bulk_editor_and_form(ledger_db):
    at = AppTest.from_function(_render_ledger).run()
    assert not at.exception, f"Invoice Ledger tab raised: {at.exception}"
    assert at.metric[0].value == "2"
    assert at.selectbox(key="ledger_select_in") is not None


def test_form_edit_persists_and_locks(ledger_db):
    at = AppTest.from_function(_render_ledger).run()
    invoice_id = at.selectbox(key="ledger_select_in").value
    at.selectbox(key=f"ledger_{invoice_id}_tax_treatment").set_value("INTRA_EU_RC")
    at.number_input(key=f"ledger_{invoice_id}_deductible_pct_irpf").set_value(20.0)
    at.button(key=f"FormSubmitter:ledger_{invoice_id}_form-Save & mark reviewed").click()
    at.run()
    assert not at.exception, f"Invoice Ledger tab raised: {at.exception}"

    rows = {r: get_invoice_by_filename(f"vendor/inv-{r}.pdf", "in", db_path=ledger_db) for r in (1, 2)}
    rec = next(r for r in rows.values() if r["id"] == invoice_id)
    assert rec["tax_treatment"] == "INTRA_EU_RC"
    assert rec["deductible_pct_irpf"] == 20.0
    assert json.loads(rec["locked_fields"]) == ["deductible_pct_irpf", "tax_treatment"]
    assert rec["reviewed_at"] is not None
    assert any("Saved" in s.value for s in at.success)


def test_empty_db_shows_guidance(tmp_path, monkeypatch):
    path = tmp_path / "empty.db"
    init_db(path)
    _use_db(monkeypatch, path)
    at = AppTest.from_function(_render_ledger).run()
    assert not at.exception
    assert any("Invoice OCR" in i.value for i in at.info)
