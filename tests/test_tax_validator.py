"""Unit tests for src/tax_validator.py.

Focus: the Modelo 390 "130/01" cross-check line must compare the M130 computed
annual income against the income actually *filed* on Modelo 130 (box 01 YTD,
carried by the Q4 filing) — never against the Modelo 390 total-operations-volume
figure (`108_total_volumen`), which is a different quantity (it includes exports
and intracom that are not M130 income).
"""
from __future__ import annotations

import sqlite3

import pytest

from src.database import init_db
from src.tax_engine import compute_modelo_130
from src.tax_validator import validate_modelo_390


@pytest.fixture
def db_conn(tmp_path):
    """Temp DB initialised with schema."""
    db_path = tmp_path / "test_validator.db"
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


def _insert_tx(conn: sqlite3.Connection, **kwargs) -> None:
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
        vat_treatment="IVA_ES_21",
        vat_base_eur=100.0,
        vat_amount_eur=21.0,
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


def _filings(*, m130_ingresos: float, volume_390: float) -> list[dict]:
    """Build a minimal filings list with a 130 Q4 filing and a 390 annual filing.

    `m130_ingresos` is the income filed on Modelo 130; `volume_390` is the 390
    total-operations volume — deliberately different so a test can prove the
    cross-check ignores it.
    """
    return [
        {
            "model": "130",
            "year": 2025,
            "quarter": 4,
            "filed_date": "2026-01-30",
            "values": {"01_ingresos_ytd": m130_ingresos},
        },
        {
            "model": "390",
            "year": 2025,
            "quarter": None,
            "filed_date": "2026-01-30",
            "values": {"108_total_volumen": volume_390},
        },
    ]


def _cross_check_line(result):
    line = next(ln for ln in result.lines if ln.casilla == "130/01")
    return line


class TestModelo390CrossCheck:
    def test_cross_check_reads_m130_filed_income_not_390_volume(self, db_conn):
        """The filed value on the 130/01 line is the M130 filed income, and the
        390 volume figure is *not* used even when it differs."""
        _insert_tx(db_conn, converted_amount=1000.0)
        m130_filed = 1000.0
        result = validate_modelo_390(
            2025, db_conn,
            _filings(m130_ingresos=m130_filed, volume_390=999999.0),
        )
        line = _cross_check_line(result)
        # Filed reference is the M130 filing's income, not the 390 volume.
        assert line.filed == pytest.approx(m130_filed)
        assert line.filed != pytest.approx(999999.0)

    def test_matching_m130_income_is_ok(self, db_conn):
        """Computed M130 income == filed M130 income → line matches (OK)."""
        _insert_tx(db_conn, converted_amount=1000.0)
        # Take the engine's actual annual (Q4 YTD) income as the filed figure so
        # the cross-check reconciles exactly, independent of the engine's VAT math.
        computed_income = compute_modelo_130(2025, 4, db_conn).box_01_ingresos
        result = validate_modelo_390(
            2025, db_conn,
            _filings(m130_ingresos=computed_income, volume_390=12345.0),
        )
        line = _cross_check_line(result)
        assert line.computed == pytest.approx(computed_income)
        assert line.filed == pytest.approx(computed_income)
        assert line.match is True
        assert line.status == "OK"

    def test_mismatching_m130_income_is_flagged(self, db_conn):
        """Computed M130 income != filed M130 income → line is flagged."""
        _insert_tx(db_conn, converted_amount=1000.0)
        computed_income = compute_modelo_130(2025, 4, db_conn).box_01_ingresos
        filed_income = computed_income + 500.0  # gestor filed 500 more → discrepancy
        result = validate_modelo_390(
            2025, db_conn,
            _filings(m130_ingresos=filed_income, volume_390=computed_income),
        )
        line = _cross_check_line(result)
        assert line.computed == pytest.approx(computed_income)
        assert line.filed == pytest.approx(filed_income)
        assert line.match is False
        # Computed < filed → DB_LOW.
        assert line.status == "DB_LOW"
        # The old bug compared computed against the 390 volume (here set equal to
        # the computed income) → spurious OK. Prove the filed ref is NOT the volume.
        assert line.filed != pytest.approx(computed_income)
