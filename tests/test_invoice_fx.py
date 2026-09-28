"""FX resolution for invoices (issue #93): ECB conversion at ingestion, the EUR
actually charged winning for expenses, `eur_received` winning for income, a
missing rate being flagged (never silently left unconverted), AUD support, and
the exchange-difference hook into Modelo 130 income. All data is synthetic;
the network is always mocked.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date
from unittest.mock import patch

import pytest

from src.database import (
    get_connection,
    init_db,
    update_invoice_fields,
    upsert_invoice,
)
from src.fx_rates import (
    SUPPORTED_CURRENCIES,
    get_exchange_differences,
    get_currencies_in_use,
    recompute_stored_invoice_fx,
    record_exchange_difference,
    resolve_invoice_amounts,
    store_rates,
)
from src.tax_engine import compute_modelo_130, compute_modelo_303

ES_CIF = "B00000000"
US_EIN = "00-0000000"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "fx.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _store_usd_rate(db_path, rate_date="2025-01-15", rate=1.0280):
    store_rates({rate_date: {"USD": rate}}, db_path)


def _extracted(**overrides) -> dict:
    """Shape of the dict `invoice_ocr.extract_invoice` returns."""
    data = {
        "invoice_date": "2025-01-15",
        "currency": "USD",
        "original_currency": "USD",
        "original_amount": 100.0,
        "subtotal_eur": 95.0,   # the LLM's own (wrong) EUR guess
        "iva_amount": 0.0,
        "total_eur": 95.0,
        "charged_eur": None,
    }
    data.update(overrides)
    return data


# ---------------------------------------------------------------------------
# resolve_invoice_amounts
# ---------------------------------------------------------------------------

class TestResolveInvoiceAmounts:
    def test_native_eur_passes_through(self, db):
        data = _extracted(currency="EUR", original_currency=None, original_amount=None,
                          subtotal_eur=100.0, total_eur=121.0)
        r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_source == "NATIVE_EUR"
        assert r.subtotal_eur == 100.0 and r.total_eur == 121.0
        assert r.fx_rate_used is None and not r.fx_stale

    def test_usd_expense_converted_with_stored_ecb_rate(self, db):
        _store_usd_rate(db)
        data = _extracted()
        r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_source == "ECB"
        assert r.fx_rate_used == 1.0280
        assert r.total_eur == round(100.0 / 1.0280, 2)
        assert r.subtotal_eur == r.total_eur  # no VAT breakdown on the LLM guess -> whole amount is base
        assert not r.fx_stale

    def test_charged_eur_wins_for_expense(self, db):
        _store_usd_rate(db)  # present, but must lose to the stated charge
        data = _extracted(charged_eur=42.50, original_amount=50.0, subtotal_eur=45.0, total_eur=45.0)
        r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_source == "CHARGED_EUR"
        assert r.total_eur == 42.50
        assert r.fx_rate_used == pytest.approx(50.0 / 42.50, rel=1e-4)

    def test_missing_rate_is_flagged_not_silent(self, db):
        with patch("src.fx_rates.fetch_single_date", side_effect=Exception("no network")):
            data = _extracted(invoice_date="2020-01-01")  # nothing stored before this date
            r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_source == "NO_RATE"
        assert r.fx_stale is True
        assert r.fx_warning is not None and "No ECB rate" in r.fx_warning

    def test_cross_check_warns_above_one_percent(self, db):
        _store_usd_rate(db)
        # ECB conversion = 97.28; LLM guessed 90.00 -> off by > 1%.
        data = _extracted(subtotal_eur=90.0, total_eur=90.0)
        r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_cross_check_diff_pct > 1.0
        assert r.fx_warning is not None and "differs" in r.fx_warning

    def test_cross_check_silent_within_one_percent(self, db):
        _store_usd_rate(db)
        expected_total = round(100.0 / 1.0280, 2)
        data = _extracted(subtotal_eur=expected_total, total_eur=expected_total)
        r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_warning is None

    def test_stale_fallback_flagged(self, db):
        _store_usd_rate(db, rate_date="2025-01-01", rate=1.05)
        data = _extracted(invoice_date="2025-01-20")  # 19 days after the only stored rate
        r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_source == "ECB"
        assert r.fx_stale is True

    def test_aud_conversion_works(self, db):
        store_rates({"2025-01-15": {"AUD": 1.6200}}, db)
        data = _extracted(currency="AUD", original_currency="AUD", original_amount=162.0)
        r = resolve_invoice_amounts("in", data, db_path=db)
        assert r.fx_source == "ECB"
        assert r.fx_rate_used == 1.6200
        assert r.total_eur == 100.0

    def test_aud_in_supported_currencies(self):
        assert "AUD" in SUPPORTED_CURRENCIES


# ---------------------------------------------------------------------------
# Ingestion → storage (mirrors src/invoice_ingest.extract_and_save)
# ---------------------------------------------------------------------------

def _ingest(direction: str, db_path, **data_overrides) -> str:
    data = _extracted(**data_overrides)
    fx = resolve_invoice_amounts(direction, data, db_path=db_path)
    record = {
        "filename": data_overrides.get("filename", "vendor/inv.pdf"),
        "direction": direction,
        "invoice_date": data["invoice_date"],
        "vendor_nif": ES_CIF if direction == "in" else None,
        "client_nif": None if direction == "in" else US_EIN,  # non-EU customer, like ILL20260617
        "original_currency": data.get("original_currency"),
        "original_amount": data.get("original_amount"),
        "charged_eur": data.get("charged_eur"),
        "subtotal_eur": fx.subtotal_eur,
        "iva_amount": fx.iva_amount,
        "total_eur": fx.total_eur,
        "fx_rate_used": fx.fx_rate_used,
        "fx_rate_date": fx.fx_rate_date,
        "fx_source": fx.fx_source,
        "fx_stale": fx.fx_stale,
        "fx_cross_check_diff_pct": fx.fx_cross_check_diff_pct,
    }
    return upsert_invoice(record, db_path=db_path)


class TestIngestionStoresFxMetadata:
    def test_expense_stores_resolved_amounts_and_provenance(self, db):
        _store_usd_rate(db)
        rid = _ingest("in", db, filename="vendor/usd.pdf")
        conn = get_connection(db)
        row = dict(conn.execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
        conn.close()
        assert row["fx_source"] == "ECB"
        assert row["fx_rate_used"] == 1.0280
        assert row["subtotal_eur"] == round(100.0 / 1.0280, 2)
        assert row["fx_stale"] == 0

    def test_charged_eur_is_editable_and_lockable(self, db):
        rid = _ingest("in", db, filename="vendor/saas_card.pdf", charged_eur=42.50,
                      original_amount=50.0, subtotal_eur=45.0, total_eur=45.0)
        row = dict(get_connection(db).execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
        assert row["charged_eur"] == 42.50
        changed = update_invoice_fields(rid, {"charged_eur": 44.50}, db_path=db)
        assert changed == ["charged_eur"]


# ---------------------------------------------------------------------------
# Engine: eur_received wins for income
# ---------------------------------------------------------------------------

class TestIncomeEurReceived:
    def test_eur_received_wins_over_ecb_booked_value_in_modelo130(self, db, conn):
        _store_usd_rate(db)
        rid = _ingest("out", db, filename="out/usd-income.pdf")
        row = dict(get_connection(db).execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
        assert row["subtotal_eur"] is not None
        result_before = compute_modelo_130(2025, 1, conn)
        assert result_before.box_01_ingresos == pytest.approx(row["subtotal_eur"])

        update_invoice_fields(rid, {"eur_received": 91.11}, db_path=db)
        result_after = compute_modelo_130(2025, 1, conn)
        assert result_after.box_01_ingresos == pytest.approx(91.11)

    def test_eur_received_wins_in_modelo303_export_base(self, db, conn):
        _store_usd_rate(db)
        rid = _ingest("out", db, filename="out/usd-income-303.pdf")
        update_invoice_fields(rid, {"eur_received": 80.0}, db_path=db)
        result = compute_modelo_303(2025, 1, conn)
        assert result.export_base == pytest.approx(80.0)

    def test_no_eur_received_uses_ecb_booked_value_as_final(self, db, conn):
        _store_usd_rate(db)
        _ingest("out", db, filename="out/usd-income-final.pdf")
        result = compute_modelo_130(2025, 1, conn)
        expected = round(100.0 / 1.0280, 2)
        assert result.box_01_ingresos == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Exchange differences
# ---------------------------------------------------------------------------

class TestExchangeDifferences:
    def test_record_and_list(self, db):
        diff_id = record_exchange_difference(
            conversion_date="2025-05-10", currency="USD", foreign_amount=1000.0,
            eur_obtained=880.00, booked_eur=860.00, notes="USD balance conversion",
            db_path=db,
        )
        assert diff_id > 0
        rows = get_exchange_differences(db_path=db)
        assert len(rows) == 1
        assert rows[0]["gain_loss_eur"] == pytest.approx(20.00)

    def test_feeds_modelo130_income_in_conversion_quarter_only(self, db, conn):
        _store_usd_rate(db)
        rid = _ingest("out", db, filename="out/usd-conversion.pdf")
        booked = dict(get_connection(db).execute(
            "SELECT subtotal_eur FROM invoices WHERE id = ?", (rid,)
        ).fetchone())["subtotal_eur"]

        q1_before = compute_modelo_130(2025, 1, conn)
        assert q1_before.box_01_ingresos == pytest.approx(booked)

        record_exchange_difference(
            conversion_date="2025-05-15", currency="USD", foreign_amount=100.0,
            eur_obtained=booked + 5.0, booked_eur=booked, invoice_id=rid, db_path=db,
        )

        # Q1 (before the conversion date) is unaffected.
        q1_after = compute_modelo_130(2025, 1, conn)
        assert q1_after.box_01_ingresos == pytest.approx(booked)

        # Q2 YTD picks up the original booked income (still, invoice-date keyed)
        # PLUS the +5.00 gain realised on conversion.
        q2 = compute_modelo_130(2025, 2, conn)
        assert q2.box_01_ingresos == pytest.approx(booked + 5.0)

    def test_loss_reduces_modelo130_income(self, db, conn):
        _store_usd_rate(db)
        rid = _ingest("out", db, filename="out/usd-loss.pdf")
        booked = dict(get_connection(db).execute(
            "SELECT subtotal_eur FROM invoices WHERE id = ?", (rid,)
        ).fetchone())["subtotal_eur"]
        record_exchange_difference(
            conversion_date="2025-02-01", currency="USD", foreign_amount=100.0,
            eur_obtained=booked - 10.0, booked_eur=booked, invoice_id=rid, db_path=db,
        )
        q1 = compute_modelo_130(2025, 1, conn)
        assert q1.box_01_ingresos == pytest.approx(booked - 10.0)


# ---------------------------------------------------------------------------
# Currencies in use / backfill
# ---------------------------------------------------------------------------

def _seed_invoice_currency(db_path, currency: str, filename: str) -> None:
    """Insert a minimal invoice row with a foreign `original_currency`, without
    going through FX resolution (no network) — just to make the currency show
    up for `get_currencies_in_use`."""
    upsert_invoice({
        "filename": filename, "direction": "in", "invoice_date": "2025-01-15",
        "vendor_nif": ES_CIF, "original_currency": currency, "original_amount": 50.0,
        "subtotal_eur": 30.0, "total_eur": 30.0,
    }, db_path=db_path)


class TestBackfill:
    def test_currencies_in_use_includes_supported_and_stored(self, db):
        _seed_invoice_currency(db, "AUD", "vendor/aud.pdf")
        found = get_currencies_in_use(db)
        assert set(SUPPORTED_CURRENCIES) <= set(found)
        assert "AUD" in found

    @patch("src.fx_rates.requests.get")
    def test_backfill_to_today_stores_rates_for_currencies_in_use(self, mock_get, db):
        from src.fx_rates import backfill_to_today

        _seed_invoice_currency(db, "AUD", "vendor/aud2.pdf")

        mock_response = mock_get.return_value
        mock_response.json.return_value = {"rates": {"2025-06-01": {"USD": 1.1, "GBP": 0.9, "CHF": 0.95, "AUD": 1.6}}}
        mock_response.raise_for_status = lambda: None

        stored = backfill_to_today(db_path=db)
        assert stored > 0
        assert mock_get.called

    def test_backfill_never_raises_on_network_failure(self, db):
        from src.fx_rates import backfill_to_today

        with patch("src.fx_rates.requests.get", side_effect=Exception("offline")):
            stored = backfill_to_today(db_path=db)
        assert stored == 0


# ---------------------------------------------------------------------------
# recompute_stored_invoice_fx: correcting invoices stored before the resolver
# existed (or before a later fix to it) — the real follow-up to issue #93.
# ---------------------------------------------------------------------------

def _seed_wrong_invoice(db_path, filename="vendor/wrong.pdf", direction="in",
                        wrong_total=90.0, invoice_date="2025-01-15",
                        locked_fields=None) -> str:
    """Insert an invoice the way pre-fix ingestion would have: the LLM's own
    (wrong) EUR guess stored verbatim, no fx_* provenance — bypasses
    `resolve_invoice_amounts` entirely, exactly like an invoice extracted
    before the FX resolver existed."""
    record = {
        "filename": filename, "direction": direction, "invoice_date": invoice_date,
        "vendor_nif": ES_CIF if direction == "in" else None,
        "client_nif": None if direction == "in" else US_EIN,
        "original_currency": "USD", "original_amount": 100.0,
        "subtotal_eur": wrong_total, "iva_amount": 0.0, "total_eur": wrong_total,
    }
    rid = upsert_invoice(record, db_path=db_path)
    if locked_fields:
        conn = get_connection(db_path)
        conn.execute("UPDATE invoices SET locked_fields = ? WHERE id = ?",
                    (json.dumps(locked_fields), rid))
        conn.commit()
        conn.close()
    return rid


class TestRecomputeStoredInvoiceFx:
    def test_dry_run_detects_change_and_writes_nothing(self, db):
        _store_usd_rate(db)
        rid = _seed_wrong_invoice(db)
        expected = round(100.0 / 1.0280, 2)

        result = recompute_stored_invoice_fx(db_path=db, dry_run=True)
        assert result.dry_run is True
        assert result.scanned == 1
        assert result.changed == 1
        assert len(result.rows) == 1
        assert result.rows[0].old_total_eur == 90.0
        assert result.rows[0].new_total_eur == expected

        # Nothing written — the stored row is still the wrong value.
        row = dict(get_connection(db).execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
        assert row["total_eur"] == 90.0
        assert row["fx_source"] is None

    def test_apply_corrects_the_stored_eur_value(self, db):
        _store_usd_rate(db)
        rid = _seed_wrong_invoice(db)
        expected = round(100.0 / 1.0280, 2)

        result = recompute_stored_invoice_fx(db_path=db, dry_run=False)
        assert result.changed == 1

        row = dict(get_connection(db).execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
        assert row["total_eur"] == expected
        assert row["subtotal_eur"] == expected
        assert row["fx_source"] == "ECB"
        assert row["fx_rate_used"] == 1.0280
        assert row["fx_stale"] == 0

    def test_second_run_is_idempotent(self, db):
        _store_usd_rate(db)
        _seed_wrong_invoice(db)
        recompute_stored_invoice_fx(db_path=db, dry_run=False)

        second = recompute_stored_invoice_fx(db_path=db, dry_run=False)
        assert second.scanned == 1
        assert second.changed == 0
        assert second.rows == []

    def test_locked_total_eur_is_never_overwritten(self, db):
        _store_usd_rate(db)
        rid = _seed_wrong_invoice(db, locked_fields=["total_eur"])

        result = recompute_stored_invoice_fx(db_path=db, dry_run=False)
        assert result.changed == 0
        assert result.locked_skipped == 1
        assert result.rows[0].locked_skipped is True

        row = dict(get_connection(db).execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
        assert row["total_eur"] == 90.0  # untouched

    def test_locked_subtotal_or_iva_also_guards_the_row(self, db):
        _store_usd_rate(db)
        _seed_wrong_invoice(db, filename="vendor/wrong2.pdf", locked_fields=["subtotal_eur"])
        result = recompute_stored_invoice_fx(db_path=db, dry_run=False)
        assert result.locked_skipped == 1

    def test_eur_received_is_never_touched(self, db, conn):
        _store_usd_rate(db)
        rid = _seed_wrong_invoice(db, direction="out", wrong_total=90.0)
        update_invoice_fields(rid, {"eur_received": 91.11}, db_path=db)

        recompute_stored_invoice_fx(db_path=db, dry_run=False)

        row = dict(get_connection(db).execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
        assert row["eur_received"] == 91.11
        # subtotal_eur is corrected, but eur_received (not this function's
        # concern) still wins in the engine per `_income_invoice_eur`.
        assert compute_modelo_130(2025, 1, conn).box_01_ingresos == pytest.approx(91.11)

    def test_since_filters_by_invoice_date(self, db):
        _store_usd_rate(db)
        _seed_wrong_invoice(db, filename="vendor/old.pdf", invoice_date="2024-01-01")
        _seed_wrong_invoice(db, filename="vendor/new.pdf", invoice_date="2025-01-15")

        result = recompute_stored_invoice_fx(db_path=db, dry_run=True, since="2025-01-01")
        assert result.scanned == 1
        assert result.rows[0].filename == "vendor/new.pdf"

    def test_correct_invoice_is_not_reported_as_changed(self, db):
        _store_usd_rate(db)
        expected = round(100.0 / 1.0280, 2)
        _seed_wrong_invoice(db, wrong_total=expected)  # already correct

        result = recompute_stored_invoice_fx(db_path=db, dry_run=True)
        assert result.changed == 0
        assert result.rows == []
