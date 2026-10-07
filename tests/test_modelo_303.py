"""Modelo 303 box model, pro-rata and credit chain (#97). All figures are synthetic."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.database import add_tax_entry, init_db
from src.filed_returns import FiledReturn, store_filed_return
from src.fixed_assets import FixedAsset, add_fixed_asset
from src.tax_engine import compute_modelo_303, prorrata_pct
from src.tax_snapshot_codec import decode_snapshot, encode_snapshot

# Box keys agreed with the reconciliation view (#100).
CONTRACT_BOXES = (
    "01", "03", "07", "09", "10", "11", "12", "13", "27", "28", "29", "30", "31", "36", "37",
    "44", "45", "46", "59", "60", "64", "65", "66", "69", "71", "72", "73", "78", "87", "110", "120",
)


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "m303.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _tx(conn, id, date, gross, treatment, base, vat, geo="SPAIN", activity="COACHING"):
    conn.execute(
        """INSERT INTO transactions (id, created_date, converted_amount, converted_amount_refunded,
               description, fee, currency, activity_type, geo_region, vat_treatment,
               vat_base_eur, vat_amount_eur)
           VALUES (?, ?, ?, 0, 'synthetic', 0, 'eur', ?, ?, ?, ?, ?)""",
        (id, date, gross, activity, geo, treatment, base, vat),
    )
    conn.commit()


def _inv(conn, id, direction, date, base, tax_treatment, iva=0.0, rate=None, pct=100.0, geo="SPAIN"):
    conn.execute(
        """INSERT INTO invoices (id, filename, direction, invoice_date, subtotal_eur, iva_rate,
               iva_amount, tax_treatment, deductible_pct_vat, deductible_pct_irpf, geo_region)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (id, f"{id}.pdf", direction, date, base, rate, iva, tax_treatment, pct, pct, geo),
    )
    conn.commit()


def _filed_303(conn, year, quarter, **boxes):
    store_filed_return(conn, FiledReturn(
        model="303", year=year, period=f"{quarter}T", justificante=f"SYN{year}{quarter}",
        source_file="synthetic.pdf", presented_at=f"{year}-0{quarter * 3}-20T10:00:00",
        boxes={k.lstrip("c"): v for k, v in boxes.items()},
    ))


def _capital_asset(conn, invoice_id, date, base, vat, pct):
    return add_fixed_asset(conn, FixedAsset(
        description="synthetic machine", acquisition_date=date, base_eur=base,
        business_use_pct=pct, vat_eur=vat, invoice_id=invoice_id,
    ))


# ---------------------------------------------------------------------------
# Golden quarter
# ---------------------------------------------------------------------------

class TestGoldenQuarter:
    """2025 Q2 with every treatment type and a credit carried in from a filed Q1."""

    @pytest.fixture
    def result(self, db, conn):
        # Stripe: Spain 121 gross (100 + 21), EU consumer at Spanish 21% (D7) 60.50
        # gross (50 + 10.50), non-EU consumer 200 (not subject, box 120).
        _tx(conn, "s_es", "2025-04-10T10:00:00", 121.0, "IVA_ES_21", 100.0, 21.0)
        _tx(conn, "s_b2c", "2025-05-10T10:00:00", 60.5, "EU_B2C_ES21", 50.0, 10.5,
            geo="EU_NOT_SPAIN", activity="NEWSLETTER")
        _tx(conn, "s_us", "2025-06-10T10:00:00", 200.0, "IVA_EXPORT", 200.0, 0.0, geo="OUTSIDE_EU")
        # Issued invoices.
        _inv(conn, "o_es", "out", "2025-04-15", 1000.0, "ES_21", iva=210.0, rate=21)
        _inv(conn, "o_eu", "out", "2025-04-16", 300.0, "EU_B2B", geo="EU_NOT_SPAIN")
        _inv(conn, "o_us", "out", "2025-04-17", 400.0, "NON_EU_NOT_SUBJECT", geo="OUTSIDE_EU")
        _inv(conn, "o_edu", "out", "2025-04-18", 500.0, "EXEMPT_TEACHING")
        # Received invoices.
        _inv(conn, "i_dom", "in", "2025-04-20", 200.0, "DOMESTIC", iva=42.0, rate=21)
        _inv(conn, "i_half", "in", "2025-04-21", 100.0, "DOMESTIC", iva=21.0, rate=21, pct=50)
        _inv(conn, "i_eu", "in", "2025-04-22", 150.0, "INTRA_EU_RC", geo="EU_NOT_SPAIN")
        _inv(conn, "i_us", "in", "2025-04-23", 80.0, "NON_EU_RC", geo="OUTSIDE_EU")
        _inv(conn, "i_cap", "in", "2025-05-02", 4000.0, "DOMESTIC_CAPITAL", iva=840.0, rate=21, pct=20)
        _capital_asset(conn, "i_cap", "2025-05-02", 4000.0, 840.0, 20.0)
        add_tax_entry(2025, 2, "IVA_SOPORTADO", 10.0, "synthetic ticket", db_path=db, vat_rate=10)
        # Filed Q1: 87 = 50 pending + 72 = 30 generated → 110 = 80.
        _filed_303(conn, 2025, 1, c87=50.0, c72=30.0)
        return compute_modelo_303(2025, 2, conn)

    def test_accrued(self, result):
        # 07 = Stripe 100 + 50 + invoice 1000 = 1150 ; 09 = 21 + 10.50 + 210 = 241.50
        assert result.c07_base == pytest.approx(1150.0)
        assert result.c09_cuota == pytest.approx(241.50)
        assert result.c01_base == result.c04_base == 0.0
        # 10/11 = INTRA_EU_RC 150 × 21% = 31.50 ; 12/13 = NON_EU_RC 80 × 21% = 16.80
        assert (result.c10_base, result.c11_cuota) == (pytest.approx(150.0), pytest.approx(31.50))
        assert (result.c12_base, result.c13_cuota) == (pytest.approx(80.0), pytest.approx(16.80))
        # 27 = 241.50 + 31.50 + 16.80 = 289.80
        assert result.c27_total_devengado == pytest.approx(289.80)

    def test_deductible(self, result):
        # 28 = 200 + 100 × 50% + NON_EU_RC 80 + manual 10 / 10% = 430
        # 29 = 42 + 21 × 50% + 16.80 + 10 = 79.30  (capital-good VAT NOT here)
        assert result.c28_base == pytest.approx(430.0)
        assert result.c29_cuota == pytest.approx(79.30)
        # 30/31 = capital good at 20%: 4000 × 20% = 800 / 840 × 20% = 168
        assert (result.c30_base, result.c31_cuota) == (pytest.approx(800.0), pytest.approx(168.0))
        # 36/37 = intra-EU acquisition deductible 150 / 31.50
        assert (result.c36_base, result.c37_cuota) == (pytest.approx(150.0), pytest.approx(31.50))
        assert result.c44_regularizacion_prorrata == 0.0  # not Q4
        # 45 = 79.30 + 168 + 31.50 = 278.80 ; 46 = 289.80 − 278.80 = 11.00
        assert result.c45_total_deducir == pytest.approx(278.80)
        assert result.c46_resultado_regimen_general == pytest.approx(11.0)
        assert result.prorrata_provisional_pct == 100.0

    def test_informational(self, result):
        assert result.c59_entregas_intracom == pytest.approx(300.0)
        # 120 = Stripe non-EU 200 + invoice 400 ; 60 stays 0 (no exports of goods)
        assert result.c120_no_sujetas_localizacion == pytest.approx(600.0)
        assert result.c60_exportaciones == 0.0
        assert result.exempt_base == pytest.approx(500.0)

    def test_result_and_credit(self, result):
        # 64 = 66 = 11 ; 110 = 50 + 30 = 80 ; 78 = min(80, 11) = 11 ; 87 = 69 ; 69 = 71 = 0
        assert result.c64_suma_resultados == pytest.approx(11.0)
        assert result.c65_pct_atribuible_estado == 100.0
        assert result.c66_atribuible_estado == pytest.approx(11.0)
        assert result.c110_pendiente_anteriores == pytest.approx(80.0)
        assert result.c110_source == "filed"
        assert result.c78_aplicadas_periodo == pytest.approx(11.0)
        assert result.c87_pendiente_posteriores == pytest.approx(69.0)
        assert result.c69_resultado_autoliquidacion == 0.0
        assert result.c71_resultado_liquidacion == 0.0
        assert result.c72_a_compensar == result.c73_a_devolver == 0.0
        assert result.credit_carry_forward == pytest.approx(69.0)

    def test_aeat_boxes_and_audit(self, result):
        boxes = result.aeat_boxes()
        assert set(CONTRACT_BOXES) <= set(boxes)
        assert boxes["09"] == pytest.approx(241.50) and boxes["110"] == pytest.approx(80.0)
        cells = {a.cell for a in result.audit}
        from src.tax_models import MODELO303_BOX_FIELDS
        assert set(MODELO303_BOX_FIELDS.values()) <= cells


# ---------------------------------------------------------------------------
# Rate rows
# ---------------------------------------------------------------------------

def test_reduced_rate_sales_use_their_own_rows(conn):
    _inv(conn, "o4", "out", "2025-01-10", 100.0, "ES_21", iva=4.0, rate=4)
    _inv(conn, "o10", "out", "2025-01-11", 200.0, "ES_21", iva=20.0, rate=0.10)
    r = compute_modelo_303(2025, 1, conn)
    assert (r.c01_base, r.c03_cuota) == (pytest.approx(100.0), pytest.approx(4.0))
    assert (r.c04_base, r.c06_cuota) == (pytest.approx(200.0), pytest.approx(20.0))
    assert r.c07_base == 0.0
    assert r.c27_total_devengado == pytest.approx(24.0)


# ---------------------------------------------------------------------------
# Pro-rata
# ---------------------------------------------------------------------------

class TestProrrata:
    @pytest.fixture
    def year_2025(self, conn):
        # Q1: taxed sale 1000 (+210), expense 500 (+105).
        _inv(conn, "o_q1", "out", "2025-02-01", 1000.0, "ES_21", iva=210.0, rate=21)
        _inv(conn, "i_q1", "in", "2025-02-02", 500.0, "DOMESTIC", iva=105.0, rate=21)
        # Q4: exempt teaching 300, expense 100 (+21).
        _inv(conn, "o_q4", "out", "2025-11-01", 300.0, "EXEMPT_TEACHING")
        _inv(conn, "i_q4", "in", "2025-11-02", 100.0, "DOMESTIC", iva=21.0, rate=21)
        return conn

    def test_rounding_up_to_the_unit(self):
        assert prorrata_pct(1000.0, 300.0) == 77.0      # 76.92 → 77
        assert prorrata_pct(800.0, 200.0) == 80.0       # exact stays
        assert prorrata_pct(500.0, 0.0) == 100.0
        assert prorrata_pct(0.0, 0.0) is None

    def test_provisional_100_leaves_q1_to_q3_unchanged(self, year_2025):
        r1 = compute_modelo_303(2025, 1, year_2025)
        assert r1.prorrata_provisional_pct == 100.0
        assert "no operations in 2024" in r1.prorrata_provisional_source
        assert r1.c29_cuota == pytest.approx(105.0)
        assert r1.c44_regularizacion_prorrata == 0.0
        assert r1.c46_resultado_regimen_general == r1.c46_sin_prorrata == pytest.approx(105.0)

    def test_q4_box_44_applies_the_definitive_pct(self, year_2025):
        r4 = compute_modelo_303(2025, 4, year_2025)
        # Definitive = ceil(1000 / (1000 + 300) × 100) = ceil(76.92) = 77
        assert r4.prorrata_definitive_pct == 77.0
        # Q4 deducts at the provisional 100%: 29 = 21
        assert r4.c29_cuota == pytest.approx(21.0)
        # 44 = (77 − 100)% × year deductible (105 + 21 = 126) = −28.98
        assert r4.c44_regularizacion_prorrata == pytest.approx(-28.98)
        # 45 = 21 − 28.98 = −7.98 ; 46 = 0 − (−7.98) = 7.98 ; gestor mode = −21
        assert r4.c45_total_deducir == pytest.approx(-7.98)
        assert r4.c46_resultado_regimen_general == pytest.approx(7.98)
        assert r4.c46_sin_prorrata == pytest.approx(-21.0)

    def test_next_year_uses_the_definitive_pct_as_provisional(self, year_2025):
        _inv(year_2025, "i_26", "in", "2026-01-15", 100.0, "DOMESTIC", iva=21.0, rate=21)
        r = compute_modelo_303(2026, 1, year_2025)
        assert r.prorrata_provisional_pct == 77.0
        # 21 × 77% = 16.17 ; 100 × 77% = 77.00
        assert r.c29_cuota == pytest.approx(16.17)
        assert r.c28_base == pytest.approx(77.0)

    def test_stored_definitive_pct_wins(self, year_2025):
        _inv(year_2025, "i_26", "in", "2026-01-15", 100.0, "DOMESTIC", iva=21.0, rate=21)
        cfg = {"tax": {"prorrata": {"definitive_pct_by_year": {"2025": 90}}}}
        r = compute_modelo_303(2026, 1, year_2025, cfg)
        assert r.prorrata_provisional_pct == 90.0
        assert r.c29_cuota == pytest.approx(18.90)

    def test_disabled_means_no_regularisation(self, year_2025):
        r4 = compute_modelo_303(2025, 4, year_2025, {"tax": {"prorrata": {"enabled": False}}})
        assert r4.prorrata_definitive_pct is None
        assert r4.c44_regularizacion_prorrata == 0.0
        assert r4.c45_total_deducir == pytest.approx(21.0)


# ---------------------------------------------------------------------------
# Credit chain (110 / 78 / 87 / 72 / 73)
# ---------------------------------------------------------------------------

class TestCreditChain:
    @pytest.fixture
    def two_quarters(self, conn):
        # Q1: only an expense → 46 = −210 → 72 = 210. Q2: a sale 100 (+21).
        _inv(conn, "i1", "in", "2025-02-01", 1000.0, "DOMESTIC", iva=210.0, rate=21)
        _inv(conn, "o2", "out", "2025-05-01", 100.0, "ES_21", iva=21.0, rate=21)
        return conn

    def test_app_chain_when_nothing_filed(self, two_quarters):
        q1 = compute_modelo_303(2025, 1, two_quarters)
        assert q1.c110_source == "none"
        assert (q1.c71_resultado_liquidacion, q1.c72_a_compensar) == (pytest.approx(-210.0), pytest.approx(210.0))
        q2 = compute_modelo_303(2025, 2, two_quarters)
        # 110 = app Q1 87 (0) + 72 (210) ; 78 = min(210, 21) ; 87 = 189 ; 71 = 0
        assert q2.c110_source == "app_chain"
        assert q2.c110_pendiente_anteriores == pytest.approx(210.0)
        assert q2.c78_aplicadas_periodo == pytest.approx(21.0)
        assert q2.c87_pendiente_posteriores == pytest.approx(189.0)
        assert q2.c71_resultado_liquidacion == 0.0

    def test_filed_previous_return_wins(self, two_quarters):
        # The filed Q1 is the legally operative figure, even when it differs from the app's.
        _filed_303(two_quarters, 2025, 1, c87=40.0, c72=110.0)
        q2 = compute_modelo_303(2025, 2, two_quarters)
        assert q2.c110_source == "filed"
        assert q2.c110_pendiente_anteriores == pytest.approx(150.0)   # 40 + 110
        assert q2.c78_aplicadas_periodo == pytest.approx(21.0)
        assert q2.c87_pendiente_posteriores == pytest.approx(129.0)

    def test_q4_compensate_vs_refund(self, conn):
        _inv(conn, "o4", "out", "2025-10-01", 100.0, "ES_21", iva=30.0, rate=21)
        _filed_303(conn, 2025, 3, c87=100.0)
        comp = compute_modelo_303(2025, 4, conn)
        # 66 = 30 ; 78 = min(100, 30) = 30 ; 87 = 70 ; 71 = 0
        assert (comp.c78_aplicadas_periodo, comp.c87_pendiente_posteriores) == (30.0, 70.0)
        assert comp.c71_resultado_liquidacion == comp.c73_a_devolver == 0.0
        ref = compute_modelo_303(2025, 4, conn, q4_negative_result="refund")
        # Refund: 78 = the whole 110 = 100 ; 69 = 30 − 100 = −70 → 73 = 70, 72 = 0
        assert ref.c78_aplicadas_periodo == pytest.approx(100.0)
        assert ref.c87_pendiente_posteriores == 0.0
        assert ref.c71_resultado_liquidacion == pytest.approx(-70.0)
        assert (ref.c73_a_devolver, ref.c72_a_compensar) == (pytest.approx(70.0), 0.0)
        # Config sets the same choice.
        cfg = {"tax": {"modelo303_q4_negative_result": "refund"}}
        assert compute_modelo_303(2025, 4, conn, cfg).c73_a_devolver == pytest.approx(70.0)

    def test_refund_option_ignored_outside_q4(self, two_quarters):
        q1 = compute_modelo_303(2025, 1, two_quarters, q4_negative_result="refund")
        assert (q1.c72_a_compensar, q1.c73_a_devolver) == (pytest.approx(210.0), 0.0)

    def test_invalid_option_rejected(self, conn):
        with pytest.raises(ValueError):
            compute_modelo_303(2025, 4, conn, q4_negative_result="keep")


# ---------------------------------------------------------------------------
# Capital goods, manual entries, snapshots
# ---------------------------------------------------------------------------

def test_capital_good_at_20pct_in_30_31_not_in_28_29(conn):
    # Treatment left as DOMESTIC, but the register links a capital good to the invoice.
    _inv(conn, "ac", "in", "2025-06-09", 3500.0, "DOMESTIC", iva=735.0, rate=21)
    _capital_asset(conn, "ac", "2025-06-09", 3500.0, 735.0, 20.0)
    r = compute_modelo_303(2025, 2, conn)
    assert (r.c28_base, r.c29_cuota) == (0.0, 0.0)
    # 3500 × 20% = 700 ; 735 × 20% = 147
    assert (r.c30_base, r.c31_cuota) == (pytest.approx(700.0), pytest.approx(147.0))


def test_rectificativa_nets_into_28_29(conn):
    # #161: a received credit note (negative base and VAT) reduces the deductible
    # VAT of its quarter instead of being skipped.
    _inv(conn, "inv", "in", "2025-05-02", 100.0, "DOMESTIC", iva=21.0, rate=21)
    _inv(conn, "cn", "in", "2025-05-20", -50.0, "DOMESTIC", iva=-10.5, rate=21)
    r = compute_modelo_303(2025, 2, conn)
    assert (r.c28_base, r.c29_cuota) == (pytest.approx(50.0), pytest.approx(10.5))


def test_rectificativa_applies_the_deductible_pct(conn):
    _inv(conn, "inv", "in", "2025-05-02", 100.0, "DOMESTIC", iva=21.0, rate=21, pct=50)
    _inv(conn, "cn", "in", "2025-05-20", -50.0, "DOMESTIC", iva=-10.5, rate=21, pct=50)
    r = compute_modelo_303(2025, 2, conn)
    assert (r.c28_base, r.c29_cuota) == (pytest.approx(25.0), pytest.approx(5.25))


def test_zero_vat_domestic_row_adds_nothing_to_28_29(conn):
    _inv(conn, "zero", "in", "2025-05-02", 100.0, "DOMESTIC", iva=0.0, rate=0)
    r = compute_modelo_303(2025, 2, conn)
    assert (r.c28_base, r.c29_cuota) == (0.0, 0.0)


def test_unregistered_capital_invoice_uses_the_invoice(conn):
    _inv(conn, "cap", "in", "2025-06-09", 4000.0, "DOMESTIC_CAPITAL", iva=840.0, rate=21, pct=50)
    r = compute_modelo_303(2025, 2, conn)
    assert (r.c30_base, r.c31_cuota) == (pytest.approx(2000.0), pytest.approx(420.0))
    assert r.c29_cuota == 0.0
    assert "not in the fixed-asset register" in r.notes


def test_registered_non_capital_good_invoice_uses_28_29(conn):
    # #134: flagged DOMESTIC_CAPITAL, but the linked asset is registered below the
    # art. 108 LIVA threshold (€3,005.06) so it is not a VAT capital good — the
    # invoice must be deducted like DOMESTIC in 28/29, not fall back to 30/31.
    _inv(conn, "small_cap", "in", "2025-06-09", 1000.0, "DOMESTIC_CAPITAL", iva=210.0, rate=21, pct=50)
    _capital_asset(conn, "small_cap", "2025-06-09", 1000.0, 210.0, 50.0)
    r = compute_modelo_303(2025, 2, conn)
    # 1000 × 50% = 500 ; 210 × 50% = 105
    assert (r.c28_base, r.c29_cuota) == (pytest.approx(500.0), pytest.approx(105.0))
    assert (r.c30_base, r.c31_cuota) == (0.0, 0.0)
    assert "not in the fixed-asset register" not in r.notes
    assert "not a VAT capital good" in r.notes


def test_manual_entry_needs_a_rate(db, conn):
    add_tax_entry(2025, 1, "IVA_SOPORTADO", 21.0, "with rate", db_path=db, vat_rate=21)
    add_tax_entry(2025, 1, "IVA_SOPORTADO", 5.0, "legacy, no rate", db_path=db)
    r = compute_modelo_303(2025, 1, conn)
    assert r.c29_cuota == pytest.approx(26.0)       # both cuotas count
    assert r.c28_base == pytest.approx(100.0)       # only 21 / 21% — no assumed rate for the other
    assert "no VAT rate" in r.notes


def test_old_snapshot_field_names_still_decode():
    legacy = {
        "year": 2024, "quarter": 3,
        "box_01_base": 1000.0, "box_03_cuota": 210.0, "box_59_intracom_entregas": 50.0,
        "box_28_base_soportado": 400.0, "box_29_cuota_soportado": 84.0,
        "box_46_diferencia": 126.0, "box_48_resultado": 126.0,
        "oss_base": 0.0, "oss_vat": 0.0, "export_base": 70.0, "notes": "",
    }
    r = decode_snapshot("303", json.dumps(legacy))
    assert (r.c07_base, r.c09_cuota) == (1000.0, 210.0)
    assert (r.c28_base, r.c29_cuota) == (400.0, 84.0)
    assert r.c120_no_sujetas_localizacion == 70.0
    assert r.c27_total_devengado == 210.0 and r.c45_total_deducir == 84.0
    assert r.c71_resultado_liquidacion == 126.0
    assert r.aeat_boxes()["46"] == 126.0
    # Pre-#42 keys (swapped 28/29) map straight to the current fields too.
    pre42 = decode_snapshot("303", json.dumps({"year": 2024, "quarter": 1,
                                               "box_28_iva_soportado": 15.0,
                                               "box_29_base_soportado": 60.0}))
    assert (pre42.c28_base, pre42.c29_cuota) == (60.0, 15.0)
    # Legacy read-only aliases (still used by the validator until #100).
    assert r.c07_base == 1000.0 and r.c120_no_sujetas_localizacion == 70.0


def test_snapshot_roundtrip(conn):
    _inv(conn, "o", "out", "2025-01-10", 100.0, "ES_21", iva=21.0, rate=21)
    r = compute_modelo_303(2025, 1, conn)
    back = decode_snapshot("303", encode_snapshot("303", r))
    assert back.aeat_boxes() == r.aeat_boxes()


# ---------------------------------------------------------------------------
# Stripe platform (application) fees as a non-EU reverse charge (#147)
# ---------------------------------------------------------------------------

def _fee_tx(conn, id, date, fee_application):
    """A Spanish Stripe sale of 100 + 21 whose connected platform kept ``fee_application``
    (``None`` = fee split unknown, a row fetched before the split was stored)."""
    _tx(conn, id, f"{date}T10:00:00", 121.0, "IVA_ES_21", 100.0, 21.0)
    conn.execute("UPDATE transactions SET fee_application = ? WHERE id = ?", (fee_application, id))
    conn.commit()


def _cell(r, cell):
    entry = next(e for e in r.audit if e.cell == cell)
    return entry.value, json.loads(entry.inputs_json)


class TestPlatformFeeReverseCharge:
    @pytest.fixture
    def fees(self, conn):
        _fee_tx(conn, "pf_q1", "2025-03-31", 3.00)       # Q1: not in Q2
        _fee_tx(conn, "pf_a", "2025-04-10", 6.00)
        _fee_tx(conn, "pf_b", "2025-06-30", 4.00)
        _fee_tx(conn, "pf_zero", "2025-05-10", 0.0)      # no platform on this charge
        _fee_tx(conn, "pf_q3", "2025-07-01", 5.00)       # Q3: not in Q2
        return conn

    def test_accrued_in_12_13_and_deducted_in_28_29(self, fees):
        r = compute_modelo_303(2025, 2, fees)
        assert (r.c12_base, r.c13_cuota) == (pytest.approx(10.0), pytest.approx(2.10))
        assert (r.c28_base, r.c29_cuota) == (pytest.approx(10.0), pytest.approx(2.10))
        # 27 = 3 Stripe sales × 21 + 2.10 ; neutral at 100%: 46 = the sales VAT only
        assert r.c27_total_devengado == pytest.approx(65.10)
        assert r.c46_resultado_regimen_general == pytest.approx(63.0)
        value, inputs = _cell(r, "c12_base")
        assert value == 10.0 and inputs["platform_fee_vat_treatment"] == "NON_EU_RC"
        assert [(x["source"], x["charge_id"], x["fee_application"], x["rate_pct"])
                for x in inputs["records"]] == [("platform_fee", "pf_a", 6.0, 21.0),
                                                ("platform_fee", "pf_b", 4.0, 21.0)]
        assert [x["charge_id"] for x in _cell(r, "c28_base")[1]["records"]] == ["pf_a", "pf_b"]

    def test_other_quarters_count_their_own_charges(self, fees):
        assert compute_modelo_303(2025, 1, fees).c12_base == pytest.approx(3.0)
        assert compute_modelo_303(2025, 3, fees).c12_base == pytest.approx(5.0)

    def test_deduction_takes_the_provisional_prorrata(self, fees):
        r = compute_modelo_303(2025, 2, fees, {"tax": {"prorrata": {"definitive_pct_by_year": {"2024": 80}}}})
        assert (r.c12_base, r.c13_cuota) == (pytest.approx(10.0), pytest.approx(2.10))
        assert (r.c28_base, r.c29_cuota) == (pytest.approx(8.0), pytest.approx(1.68))

    def test_none_leaves_the_303_unchanged(self, fees):
        r = compute_modelo_303(2025, 2, fees, {"tax": {"platform_fee_vat_treatment": "NONE"}})
        assert (r.c12_base, r.c13_cuota, r.c28_base, r.c29_cuota) == (0.0, 0.0, 0.0, 0.0)
        assert r.c27_total_devengado == pytest.approx(63.0)
        assert _cell(r, "c12_base")[1]["records"] == []

    def test_invalid_option_rejected(self, fees):
        with pytest.raises(ValueError, match="platform_fee_vat_treatment"):
            compute_modelo_303(2025, 2, fees, {"tax": {"platform_fee_vat_treatment": "EU_RC"}})

    def test_unknown_split_is_counted_never_guessed(self, conn):
        _fee_tx(conn, "pf_legacy", "2025-04-11", None)
        _fee_tx(conn, "pf_ok", "2025-04-12", 1.00)
        r = compute_modelo_303(2025, 2, conn)
        assert (r.c12_base, r.c13_cuota) == (pytest.approx(1.0), pytest.approx(0.21))
        assert _cell(r, "c12_base")[1]["fee_split_unknown"] == 1
        assert "Fee split unknown for 1 Stripe charge(s); re-fetch" in r.notes
        r_none = compute_modelo_303(2025, 2, conn, {"tax": {"platform_fee_vat_treatment": "NONE"}})
        assert "Fee split unknown" not in r_none.notes

    def test_activity_start_date_excludes_earlier_charges(self, fees):
        r = compute_modelo_303(2025, 2, fees, {"tax": {"activity_start_date": "2025-05-01"}})
        assert (r.c12_base, r.c13_cuota) == (pytest.approx(4.0), pytest.approx(0.84))
