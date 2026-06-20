"""Unit tests for src/tax_engine.py and related tax classification."""
from __future__ import annotations

import sqlite3

import pytest

from src.database import (
    TAX_SNAPSHOT_QUARTER_ANNUAL,
    _get_connection,
    init_db,
    load_tax_snapshots_for_period,
)
from src.tax_engine import (
    compute_and_persist_tax_snapshots,
    compute_modelo_130,
    compute_modelo_303,
    compute_modelo_347,
    compute_modelo_349,
    compute_oss_return,
)
from src.tax_snapshot_codec import decode_snapshot


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
        assert result.box_59_intracom_entregas == pytest.approx(300.0)
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
        # €1000 gross IVA_ES_21 → ex-VAT income base 1000 / 1.21 = 826.45
        assert result.box_01_ingresos == pytest.approx(826.45)
        # 5% of 826.45 = 41.32 gastos de difícil justificación
        assert result.gastos_dificil_justificacion == pytest.approx(41.32)
        # 826.45 − 41.32 = 785.13
        assert result.rendimiento_neto == pytest.approx(785.13)
        # 20% of 785.13 = 157.03
        assert result.box_05_base == pytest.approx(157.03)

    def test_retenciones_greater_than_20pct_net_gives_zero(self, db_conn):
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=1000.0, activity_type="COACHING")
        # Retenciones YTD = 600 > 20% of 950 (after 5% deduction) = 190
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
        assert result.gastos_dificil_justificacion == 0.0  # No deduction on negative rendimiento
        assert result.box_05_base == 0.0  # max(0, negative)
        assert result.box_16_resultado == 0.0

    def test_q2_accumulates_prior_q1_payment(self, db_conn):
        # Insert Q1 transaction
        _insert_tx(db_conn, id="t1", created_date="2025-01-15T10:00:00",
                   converted_amount=1000.0, activity_type="COACHING")
        # Insert Q2 transaction
        _insert_tx(db_conn, id="t2", created_date="2025-04-15T10:00:00",
                   converted_amount=1000.0, activity_type="COACHING")
        # Save Q1 Modelo 130 as COMPUTED with amount 190 (after 5% deduction)
        db_conn.execute(
            """INSERT INTO tax_filing_status (year, model, quarter, status, amount_eur)
               VALUES (2025, '130', 1, 'COMPUTED', 190.0)"""
        )
        db_conn.commit()
        result = compute_modelo_130(2025, 2, db_conn)
        # Two €1000 gross IVA_ES_21 tx → ex-VAT base each 1000 / 1.21 = 826.45,
        # YTD income = 826.45 + 826.45 = 1652.90
        assert result.box_01_ingresos == pytest.approx(1652.90)  # YTD
        assert result.box_14_pagos_anteriores == pytest.approx(190.0)
        # rendimiento neto previo = 1652.90, 5% = 82.65, rendimiento neto = 1570.25
        # 20% of 1570.25 = 314.05 - 190 prior = 124.05
        assert result.gastos_dificil_justificacion == pytest.approx(82.65)
        assert result.rendimiento_neto == pytest.approx(1570.25)
        assert result.box_16_resultado == pytest.approx(124.05)


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


class TestTaxSnapshotPersistence:
    def test_persist_and_load_roundtrip(self, db_conn):
        _insert_tx(db_conn)
        ts = compute_and_persist_tax_snapshots(2025, 1, db_conn)
        assert len(ts) > 0
        rows = load_tax_snapshots_for_period(2025, 1, db_conn)
        by_model = {r["model"]: r for r in rows}
        assert set(by_model) >= {"303", "130", "OSS", "347", "349"}
        assert by_model["347"]["quarter"] == TAX_SNAPSHOT_QUARTER_ANNUAL
        m303 = decode_snapshot("303", by_model["303"]["payload_json"])
        # €100 gross IVA_ES_21 → ex-VAT base 100 / 1.21 = 82.64
        assert m303.box_01_base == pytest.approx(82.64)
        m347 = decode_snapshot("347", by_model["347"]["payload_json"])
        assert m347.year == 2025
