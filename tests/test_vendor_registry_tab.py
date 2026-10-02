"""AppTest coverage for the Vendors tab and the ledger's unknown-vendor flag (#91).
Synthetic vendors only."""
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


def _render_vendors() -> None:
    from app import vendor_registry_tab

    vendor_registry_tab.render()


def _render_ledger() -> None:
    from app import invoice_ledger

    invoice_ledger.render()


@pytest.fixture
def vendors_db(tmp_path, monkeypatch, isolated_vendor_registry):
    isolated_vendor_registry.write_text(json.dumps({"vendors": [
        {"key": "example cloud", "country": "IE", "vat_id": "IE0000000XX",
         "default_tax_treatment": "INTRA_EU_RC", "default_deductible_pct_irpf": 50, "activity": "NEWSLETTER"},
    ]}), encoding="utf-8")
    path = tmp_path / "vendors_tab.db"
    init_db(path)
    upsert_invoice({"filename": "example cloud/2025-01.pdf", "direction": "in",
                    "invoice_date": "2025-01-10", "total_eur": 10.0}, db_path=path)
    upsert_invoice({"filename": "mystery/2025-01.pdf", "direction": "in",
                    "invoice_date": "2025-01-11", "vendor_name": "Mystery Vendor Ltd",
                    "total_eur": 20.0}, db_path=path)
    monkeypatch.setattr(database, "_DB_PATH", path)
    return path


def test_vendors_tab_renders_and_flags_unknown(vendors_db):
    at = AppTest.from_function(_render_vendors).run()
    assert not at.exception, f"Vendors tab raised: {at.exception}"
    assert any("1 expense invoice(s) match no registry vendor" in w.value for w in at.warning)


def test_apply_button_writes_registry_defaults(vendors_db):
    at = AppTest.from_function(_render_vendors).run()
    at.button(key="vendors_apply").click()
    at.run()
    assert not at.exception, f"Vendors tab raised: {at.exception}"
    row = get_invoice_by_filename("example cloud/2025-01.pdf", "in", db_path=vendors_db)
    assert row["tax_treatment"] == "INTRA_EU_RC"
    assert row["deductible_pct_irpf"] == 50.0
    assert row["activity_type"] == "NEWSLETTER"
    assert any("Matched 1 of 2" in s.value and "Updated per period: 2025 Q1: 1" in s.value for s in at.success)


def test_ledger_shows_unknown_vendor_metric(vendors_db):
    at = AppTest.from_function(_render_ledger).run()
    assert not at.exception, f"Invoice Ledger tab raised: {at.exception}"
    assert at.metric[4].label == "⚠ Unknown vendor"
    assert at.metric[4].value == "1"


def test_editor_frame_roundtrip():
    from app.vendor_registry_tab import _from_frame, _to_frame
    from src.vendor_registry import VendorRegistry

    reg = VendorRegistry.from_dict({"vendors": [
        {"key": "example cloud", "aliases": ["ExampleCloud", "Example Cloud Ltd"], "vat_id": "IE0000000XX",
         "default_deductible_pct_vat": 50},
        {"key": "sample ai", "activity": "NEWSLETTER"},
    ]})
    assert _from_frame(_to_frame(reg)).to_dict() == reg.to_dict()
