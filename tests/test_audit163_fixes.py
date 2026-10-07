"""Regression tests for the #163 audit bug findings (synthetic data only)."""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import src.database as database
from src import invoice_ocr
from src.config import save_stripe_api_key
from src.database import derive_geo_region_from_nif, get_uploaded_files, init_db, record_upload
from src.exceptions import ConfigError, StaleClassificationError


# --- Greek VAT prefix -------------------------------------------------------

@pytest.mark.parametrize("vat", ["EL123456789", "el123456789", "GR123456789"])
def test_greek_vat_ids_are_eu(vat):
    assert derive_geo_region_from_nif(vat) == "EU_NOT_SPAIN"


def test_non_eu_prefix_is_still_outside_eu():
    assert derive_geo_region_from_nif("US123456789") == "OUTSIDE_EU"


# --- OCR number strings -----------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("42.50", 42.5),
    ("1.0832", 1.0832),
    ("1.234,56", 1234.56),
    ("1,234.56", 1234.56),
    ("42,50", 42.5),
    ("1.234", 1234.0),          # three digits after a lone dot: Spanish thousands
    ("1.234.567", 1234567.0),
    ("1,234,567", 1234567.0),
    (" 100 ", 100.0),
    ("21", 21.0),
])
def test_parse_number_string(raw, expected):
    assert invoice_ocr._parse_number_string(raw) == expected


def test_parse_number_string_rejects_text():
    with pytest.raises(ValueError):
        invoice_ocr._parse_number_string("n/a")


def test_extract_invoice_normalises_string_fields_in_both_locales(tmp_path, monkeypatch):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    raw = ('{"subtotal_eur": "42.50", "fx_rate": "1.0832", "total_eur": "1.234,56", "iva_rate": "n/a", '
           '"iva_breakdown": [{"base_imponible": "100.00", "iva_amount": "21,00"}]}')
    monkeypatch.setattr(invoice_ocr, "_extract_via_hub", lambda *a, **k: raw)
    data = invoice_ocr.extract_invoice(pdf)
    assert data["subtotal_eur"] == 42.5
    assert data["fx_rate"] == 1.0832
    assert data["total_eur"] == 1234.56
    assert data["iva_rate"] is None
    assert data["iva_breakdown"] == [{"base_imponible": 100.0, "iva_amount": 21.0}]


# --- failed uploads stay pending -------------------------------------------

def test_failed_upload_row_is_not_uploaded_and_is_replaced_on_retry(tmp_path):
    from app.invoice_upload import _get_new_invoices

    db = tmp_path / "up.db"
    init_db(db)
    record_upload("a.pdf", "in", api_response="ok", db_path=db)
    record_upload("b.pdf", "in", api_response="ERROR: boom", db_path=db)   # legacy failed row
    uploaded = get_uploaded_files("in", db_path=db)
    assert _get_new_invoices(["a.pdf", "b.pdf", "c.pdf"], uploaded) == ["b.pdf", "c.pdf"]

    assert record_upload("b.pdf", "in", api_response="ok", db_path=db) is True
    assert record_upload("b.pdf", "in", api_response="ok", db_path=db) is False
    rows = {r["filename"]: r["api_response"] for r in get_uploaded_files("in", db_path=db)}
    assert rows["b.pdf"] == "ok"


# --- Stripe key save --------------------------------------------------------

def test_save_stripe_api_key_replaces_only_the_exact_line(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OTHER=1\nSTRIPE_API_KEY=old\nSTRIPE_API_KEY_RESTRICTED=rk_keep\n", encoding="utf-8")
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)
    save_stripe_api_key("  sk_test_new  ", env_path=env)
    lines = env.read_text(encoding="utf-8").splitlines()
    assert lines == ["OTHER=1", "STRIPE_API_KEY_RESTRICTED=rk_keep", "STRIPE_API_KEY=sk_test_new"]
    import os
    assert os.environ["STRIPE_API_KEY"] == "sk_test_new"
    monkeypatch.delenv("STRIPE_API_KEY", raising=False)


@pytest.mark.parametrize("empty", ["", "   "])
def test_save_stripe_api_key_refuses_empty(tmp_path, empty):
    env = tmp_path / ".env"
    env.write_text("STRIPE_API_KEY=keep\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        save_stripe_api_key(empty, env_path=env)
    assert env.read_text(encoding="utf-8") == "STRIPE_API_KEY=keep\n"


# --- Excel download helper --------------------------------------------------

def _render_download() -> None:
    from app.excel_download import render_excel_download

    render_excel_download([], 2025, 1, "r.xlsx", download_key="dl")


def test_excel_download_shows_stale_error_and_removes_temp_file(monkeypatch, tmp_path):
    import app.excel_download as mod

    made: list[str] = []

    def boom(payments, path, year, quarter, label):
        made.append(path)
        raise StaleClassificationError("stale quarter")

    monkeypatch.setattr(mod, "create_excel_report", boom)
    at = AppTest.from_function(_render_download).run()
    assert not at.exception
    assert [e.value for e in at.error] == ["stale quarter"]
    import os
    assert made and not os.path.exists(made[0])


# --- flash helper -----------------------------------------------------------

def _flash_page() -> None:
    import streamlit as st

    from app.flash import flash, show_flash

    show_flash("t")
    if st.button("go", key="go"):
        flash("t", "error", "failed thing")
        st.rerun()


def test_flash_survives_the_rerun_and_shows_once():
    at = AppTest.from_function(_flash_page).run()
    assert not at.error
    at.button(key="go").click().run()
    assert [e.value for e in at.error] == ["failed thing"]
    at.run()
    assert not at.error


def test_flash_rejects_unknown_kind():
    from app.flash import flash

    with pytest.raises(ValueError):
        flash("t", "bogus", "x")


# --- tab regressions (loaders stubbed, no DB) -------------------------------

def _quarter_report_page() -> None:
    from app import quarter_report

    quarter_report.render()


def test_quarter_report_custom_range_then_since_inception_does_not_crash(monkeypatch):
    import app.quarter_report as tab

    monkeypatch.setattr(tab, "first_data_year", lambda: 2024)
    monkeypatch.setattr(tab, "get_classified_for_period", lambda *a, **k: [])
    at = AppTest.from_function(_quarter_report_page).run()
    assert not at.exception
    at.checkbox(key="qr_custom").check().run()
    assert not at.exception
    at.selectbox(key="qr_year").select("Since inception").run()
    assert not at.exception, f"Quarter Report raised: {at.exception}"


def _browser_page() -> None:
    from app import transaction_browser

    transaction_browser.render()


def test_transaction_browser_reloads_when_the_period_changes(monkeypatch):
    import app.transaction_browser as tab

    calls: list[tuple] = []

    def loader(year, quarter, start, end, input_mode="db"):
        calls.append((year, quarter))
        return []

    monkeypatch.setattr(tab, "first_data_year", lambda: 2024)
    monkeypatch.setattr(tab, "get_classified_for_period", loader)
    monkeypatch.setattr(tab, "get_transaction_count_db", lambda *a, **k: 0)
    monkeypatch.setattr(tab, "search_transactions_raw", lambda *a, **k: [])
    at = AppTest.from_function(_browser_page).run()
    assert not at.exception, at.exception
    first = calls[-1]
    at.radio(key="tb_quarter").set_value("Q2").run()
    assert not at.exception, at.exception
    assert calls[-1] == (first[0], 2) and len(calls) == 2
    at.run()
    assert len(calls) == 2    # an unchanged period does not reload
