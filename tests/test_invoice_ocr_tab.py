"""AppTest smoke coverage for the Invoice OCR tab after its save path moved to
``src/invoice_ingest.py`` (#102): the "Extract new/changed" button extracts a
synthetic PDF (OCR mocked) into a temp DB. Synthetic data only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

import src.database as database
import src.invoice_ingest as invoice_ingest
import src.invoice_scanner as invoice_scanner
from src.database import get_invoices, init_db


def _render_ocr_tab() -> None:
    from app import invoice_ocr_tab

    invoice_ocr_tab.render()


@pytest.fixture
def ocr_env(tmp_path, monkeypatch):
    db = tmp_path / "ocr_tab.db"
    init_db(db)
    monkeypatch.setattr(database, "_DB_PATH", db)
    inv_in = tmp_path / "in"
    inv_in.mkdir()
    (inv_in / "inv-001.pdf").write_bytes(b"%PDF-1.4 synthetic")
    cfg = {"app": {"invoice_in_dir": str(inv_in), "invoice_out_dir": str(tmp_path / "out")}}
    monkeypatch.setattr(invoice_scanner, "load_config", lambda: cfg)
    import app.invoice_ocr_tab as tab
    monkeypatch.setattr(tab, "load_config", lambda: cfg)
    monkeypatch.setenv("INVOICE_OCR_PROVIDER", "hub")

    def extract(pdf_path, api_key=None, provider=None, model=None):
        data = {"invoice_number": "A-1", "invoice_date": "2025-02-10", "vendor_name": "Example Vendor SL",
                "subtotal_eur": 100.0, "iva_rate": 21.0, "iva_amount": 21.0, "total_eur": 121.0,
                "currency": "EUR", "_file_hash": hashlib.md5(Path(pdf_path).read_bytes()).hexdigest()}
        data["_raw_response"] = json.dumps(data)
        return data

    monkeypatch.setattr(invoice_ingest, "extract_invoice", extract)
    return db


def test_extract_new_button_saves_the_invoice(ocr_env):
    at = AppTest.from_function(_render_ocr_tab).run()
    assert not at.exception, f"Invoice OCR tab raised: {at.exception}"

    at.button(key="extract_all_in").click().run()
    assert not at.exception, f"Invoice OCR tab raised: {at.exception}"
    rows = get_invoices(db_path=ocr_env)
    assert [(r["filename"], r["total_eur"]) for r in rows] == [("inv-001.pdf", 121.0)]
