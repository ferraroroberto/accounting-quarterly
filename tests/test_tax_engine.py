"""Unit tests for src/tax_engine.py and related tax classification."""
from __future__ import annotations

import sqlite3
from datetime import datetime

import pytest

from src.classifier import classify_vat
from src.database import _get_connection, init_db
from src.models import ClassifiedPayment
from src.tax_engine import (
    compute_modelo_130,
    compute_modelo_303,
    compute_modelo_347,
    compute_modelo_349,
    compute_oss_return,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cp(**kwargs) -> ClassifiedPayment:
    defaults = dict(
        id="test_01",
        created_date=datetime(2025, 1, 15),
        converted_amount=100.0,
        converted_amount_refunded=0.0,
        description="test",
        fee=0.0,
        currency="eur",
        activity_type="COACHING",
        geo_region="SPAIN",
    )
    defaults.update(kwargs)
    return ClassifiedPayment(**defaults)


def _insert_tx(conn: sqlite3.Connection, **kwargs) -> None:
    """Insert a minimal transaction row for tax engine tests."""
    defaults = dict(
        id="tx_01",
        created_date="2025-01-15T10:00:00",
        converted_amount=100.0,
        converted_amount_refunded=0.0,
        description="test",
        fee=0.0,
        currency="eur",
        activity_type="COACHING",
        geo_region="SPAIN",
        vat_treatment=None,
        vat_base_eur=None,
        vat_amount_eur=None,
        oss_country=None,
        buyer_vat_id=None,
    )
    defaults.update(kwargs)
    conn.execute(
        """INSERT OR REPLACE INTO transactions
           (id, created_date, converted_amount, converted_amount_refunded,
            description, fee, currency, activity_type, geo_region,
            vat_treatment, vat_base_eur, vat_amount_eur, oss_country, buyer_vat_id)
           VALUES (:id, :created_date, :converted_amount, :converted_amount_refunded,
                   :description, :fee, :currency, :activity_type, :geo_region,
                   :vat_treatment, :vat_base_eur, :vat_amount_eur, :oss_country, :buyer_vat_id)""",
        defaults,
    )
    conn.commit()


@pytest.fixture
def db_conn(tmp_path):
    """In-memory-like temp DB initialised with schema."""
    db_path = tmp_path / "test_tax.db"
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# VAT treatment classification tests
# ---------------------------------------------------------------------------

class TestClassifyVat:
    def test_newsletter_eu_not_spain_is_oss(self):
        cp = _make_cp(activity_type="NEWSLETTER", geo_region="EU_NOT_SPAIN", card_country="DE")
        result = classify_vat(cp)
        assert result.vat_treatment == "OSS_EU"
        assert result.oss_country == "DE"
        assert result.vat_amount_eur == pytest.approx(100.0 * 0.19, abs=0.01)

    def test_coaching_eu_not_spain_is_b2b(self):
        cp = _make_cp(activity_type="COACHING", geo_region="EU_NOT_SPAIN")
        result = classify_vat(cp)
        assert result.vat_treatment == "IVA_EU_B2B"
        assert result.vat_amount_eur == 0.0

    def test_any_outside_eu_is_export(self):
        for activity in ("COACHING", "NEWSLETTER", "ILLUSTRATIONS"):
            cp = _make_cp(activity_type=activity, geo_region="OUTSIDE_EU")
            result = classify_vat(cp)
            assert result.vat_treatment == "IVA_EXPORT"
            assert result.vat_amount_eur == 0.0

    def test_spain_is_iva_21(self):
        cp = _make_cp(activity_type="COACHING", geo_region="SPAIN")
        result = classify_vat(cp)
        assert result.vat_treatment == "IVA_ES_21"
        assert result.vat_amount_eur == pytest.approx(21.0, abs=0.01)

    def test_oss_fallback_rate_for_unknown_country(self):
        cp = _make_cp(activity_type="NEWSLETTER", geo_region="EU_NOT_SPAIN", card_country="XX")
        result = classify_vat(cp)
        assert result.vat_treatment == "OSS_EU"
        # DEFAULT_EU = 21%
        assert result.vat_amount_eur == pytest.approx(100.0 * 0.21, abs=0.01)


# ---------------------------------------------------------------------------
# Modelo 303 tests
# ---------------------------------------------------------------------------

class TestModelo303:
    def test_all_outside_eu_box_48_zero(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=500.0, geo_region="OUTSIDE_EU",
                   vat_treatment="IVA_EXPORT", vat_base_eur=500.0, vat_amount_eur=0.0)
        result = compute_modelo_303(2025, 1, db_conn)
        assert result.box_48_resultado == 0.0
        assert result.export_base == 500.0

    def test_spain_only_box_03_is_21pct(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=1000.0, geo_region="SPAIN",
                   vat_treatment="IVA_ES_21", vat_base_eur=1000.0, vat_amount_eur=210.0)
        result = compute_modelo_303(2025, 1, db_conn)
        assert result.box_01_base == pytest.approx(1000.0)
        assert result.box_03_cuota == pytest.approx(210.0)
        assert result.box_48_resultado == pytest.approx(210.0)

    def test_mixed_income_correct_allocation(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-10T10:00:00",
                   converted_amount=500.0, geo_region="SPAIN", activity_type="COACHING",
                   vat_treatment="IVA_ES_21", vat_base_eur=500.0, vat_amount_eur=105.0)
        _insert_tx(db_conn, id="t2", created_date="2025-01-20T10:00:00",
                   converted_amount=300.0, geo_region="EU_NOT_SPAIN", activity_type="COACHING",
                   vat_treatment="IVA_EU_B2B", vat_base_eur=300.0, vat_amount_eur=0.0)
        _insert_tx(db_conn, id="t3", created_date="2025-01-25T10:00:00",
                   converted_amount=200.0, geo_region="OUTSIDE_EU", activity_type="COACHING",
                   vat_treatment="IVA_EXPORT", vat_base_eur=200.0, vat_amount_eur=0.0)
        result = compute_modelo_303(2025, 1, db_conn)
        assert result.box_01_base == pytest.approx(500.0)
        assert result.box_03_cuota == pytest.approx(105.0)
        assert result.box_10_intracom == pytest.approx(300.0)
        assert result.export_base == pytest.approx(200.0)

    def test_iva_soportado_greater_than_devengado_gives_refund(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=100.0, geo_region="SPAIN",
                   vat_treatment="IVA_ES_21", vat_base_eur=100.0, vat_amount_eur=21.0)
        # Add IVA soportado entry: €500 deductible
        db_conn.execute(
            "INSERT INTO quarterly_tax_entries (year, quarter, entry_type, amount_eur) VALUES (2025, 1, 'IVA_SOPORTADO', 500.0)"
        )
        db_conn.commit()
        result = compute_modelo_303(2025, 1, db_conn)
        assert result.box_46_diferencia == pytest.approx(21.0 - 500.0)
        assert result.box_48_resultado < 0  # refund scenario


# ---------------------------------------------------------------------------
# Modelo 130 tests
# ---------------------------------------------------------------------------

class TestModelo130:
    def test_first_quarter_no_prior_payments(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=1000.0, activity_type="COACHING")
        result = compute_modelo_130(2025, 1, db_conn)
        assert result.box_14_pagos_anteriores == 0.0
        assert result.box_01_ingresos == pytest.approx(1000.0)
        assert result.box_05_base == pytest.approx(200.0)  # 20% of 1000

    def test_retenciones_greater_than_20pct_net_gives_zero(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=1000.0, activity_type="COACHING")
        # Retenciones YTD = 600 > 20% of 1000 = 200
        db_conn.execute(
            "INSERT INTO quarterly_tax_entries (year, quarter, entry_type, amount_eur) VALUES (2025, 1, 'RETENCIONES_SOPORTADAS', 600.0)"
        )
        db_conn.commit()
        result = compute_modelo_130(2025, 1, db_conn)
        assert result.box_16_resultado == 0.0

    def test_high_expenses_rendimiento_negative_gives_zero(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=500.0, activity_type="COACHING")
        db_conn.execute(
            "INSERT INTO quarterly_tax_entries (year, quarter, entry_type, amount_eur) VALUES (2025, 1, 'GASTOS_DEDUCIBLES', 2000.0)"
        )
        db_conn.commit()
        result = compute_modelo_130(2025, 1, db_conn)
        assert result.box_03_rendimiento < 0
        assert result.box_05_base == 0.0  # max(0, negative)
        assert result.box_16_resultado == 0.0

    def test_q2_accumulates_prior_q1_payment(self, db_conn):
        # Insert Q1 transaction
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=1000.0, activity_type="COACHING")
        # Insert Q2 transaction
        _insert_tx(db_conn, id="t2", created_date="2025-04-15T10:00:00",
                   converted_amount=1000.0, activity_type="COACHING")
        # Save Q1 Modelo 130 as COMPUTED with amount 200
        db_conn.execute(
            """INSERT INTO tax_filing_status (year, model, quarter, status, amount_eur)
               VALUES (2025, '130', 1, 'COMPUTED', 200.0)"""
        )
        db_conn.commit()
        result = compute_modelo_130(2025, 2, db_conn)
        assert result.box_01_ingresos == pytest.approx(2000.0)  # YTD
        assert result.box_14_pagos_anteriores == pytest.approx(200.0)
        # 20% of 2000 = 400 - 200 prior = 200
        assert result.box_16_resultado == pytest.approx(200.0)


# ---------------------------------------------------------------------------
# OSS Return tests
# ---------------------------------------------------------------------------

class TestOSSReturn:
    def test_no_oss_transactions_returns_empty(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   geo_region="SPAIN", vat_treatment="IVA_ES_21")
        result = compute_oss_return(2025, 1, db_conn)
        assert result.rows == []
        assert result.total_base == 0.0

    def test_oss_groups_by_country(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-10T10:00:00",
                   converted_amount=100.0, geo_region="EU_NOT_SPAIN",
                   activity_type="NEWSLETTER", vat_treatment="OSS_EU",
                   vat_base_eur=100.0, vat_amount_eur=19.0,
                   oss_country="DE", card_country="DE")
        _insert_tx(db_conn, id="t2", created_date="2025-01-20T10:00:00",
                   converted_amount=200.0, geo_region="EU_NOT_SPAIN",
                   activity_type="NEWSLETTER", vat_treatment="OSS_EU",
                   vat_base_eur=200.0, vat_amount_eur=40.0,
                   oss_country="FR", card_country="FR")
        result = compute_oss_return(2025, 1, db_conn)
        assert len(result.rows) == 2
        countries = {r.country for r in result.rows}
        assert countries == {"DE", "FR"}
        assert result.total_base == pytest.approx(300.0)


# ---------------------------------------------------------------------------
# Modelo 349 tests
# ---------------------------------------------------------------------------

class TestModelo349:
    def test_only_b2b_included(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-10T10:00:00",
                   converted_amount=500.0, geo_region="EU_NOT_SPAIN",
                   activity_type="COACHING", vat_treatment="IVA_EU_B2B",
                   vat_base_eur=500.0, email_meta="client@eu.com", buyer_vat_id="DE123")
        _insert_tx(db_conn, id="t2", created_date="2025-01-20T10:00:00",
                   converted_amount=300.0, geo_region="EU_NOT_SPAIN",
                   activity_type="NEWSLETTER", vat_treatment="OSS_EU",
                   vat_base_eur=300.0, email_meta="sub@eu.com")
        result = compute_modelo_349(2025, 1, db_conn)
        assert len(result.rows) == 1
        assert result.rows[0].total_amount == pytest.approx(500.0)
        assert result.total == pytest.approx(500.0)
