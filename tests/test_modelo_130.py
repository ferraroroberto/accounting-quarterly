"""Modelo 130 box model, box 13 reduction and negative carry (#98). All figures are synthetic."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.filed_returns import FiledReturn, store_filed_return
from src.database import init_db
from src.reconciliation import audit_entries_for_box
from src.tax_engine import compute_modelo_130, minoracion_art_110_3_c
from src.tax_models import MODELO130_BOX_FIELDS
from src.tax_snapshot_codec import decode_snapshot, encode_snapshot

BOXES = tuple(f"{n:02d}" for n in range(1, 20))


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "m130.db"
    init_db(path)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _stripe(conn, id, date, base):
    """A Spanish Stripe sale already split into base + 21% VAT."""
    conn.execute(
        """INSERT INTO transactions (id, created_date, converted_amount, converted_amount_refunded,
               description, fee, currency, activity_type, geo_region, vat_treatment,
               vat_base_eur, vat_amount_eur)
           VALUES (?, ?, ?, 0, 'synthetic', 0, 'eur', 'COACHING', 'SPAIN', 'IVA_ES_21', ?, ?)""",
        (id, f"{date}T10:00:00", round(base * 1.21, 2), base, round(base * 0.21, 2)),
    )
    conn.commit()


def _invoice(conn, id, direction, date, subtotal, irpf=None, pct_irpf=100.0, excluded=0):
    conn.execute(
        """INSERT INTO invoices (id, filename, direction, invoice_date, subtotal_eur, irpf_amount,
               tax_treatment, deductible_pct_vat, deductible_pct_irpf, geo_region, excluded)
           VALUES (?, ?, ?, ?, ?, ?, 'ES_21', 100, ?, 'SPAIN', ?)""",
        (id, f"{id}.pdf", direction, date, subtotal, irpf, pct_irpf, excluded),
    )
    conn.commit()


def _filed_130(conn, year, quarter, **boxes):
    store_filed_return(conn, FiledReturn(
        model="130", year=year, period=f"{quarter}T", justificante=f"SYN130{year}{quarter}",
        source_file="synthetic.pdf", presented_at=f"{year}-0{min(quarter * 3, 9)}-20T10:00:00",
        boxes={k.lstrip("c"): v for k, v in boxes.items()},
    ))


# ---------------------------------------------------------------------------
# Golden two-quarter scenario (same shape as a real filed year: a negative
# Q1 through box 13, deducted in Q2's box 15 until the result is 0)
# ---------------------------------------------------------------------------

class TestGoldenTwoQuarters:
    @pytest.fixture
    def year(self, conn):
        # Q1: Stripe base 500; expense invoices 900.
        _stripe(conn, "s1", "2025-02-10", 500.0)
        _invoice(conn, "e1", "in", "2025-03-01", 900.0)
        # Q2: Stripe base 5,400; issued invoice 600 gross with 90.00 withheld (15 %);
        # expense invoices 4,100.
        _stripe(conn, "s2", "2025-05-10", 5400.0)
        _invoice(conn, "o1", "out", "2025-05-20", 600.0, irpf=90.0)
        _invoice(conn, "e2", "in", "2025-06-01", 4100.0)
        return conn

    def test_q1_negative_net_still_takes_box_13(self, year):
        r = compute_modelo_130(2025, 1, year)
        # 01 = 500; real expenses 900; 01 − real < 0 → no 5 % allowance; 02 = 900
        # 03 = 500 − 900 = −400; 04 = 0; 05 = 0; 06 = 0; 07 = 0; 12 = 0
        # 13 = 100 (no 2024 activity → previous-year net 0 ≤ 9,000)
        # 14 = 0 − 100 = −100; 15 = 0 (14 not positive); 17 = −100; 19 = −100
        assert r.aeat_boxes() == {
            **{b: 0.0 for b in BOXES},
            "01": 500.0, "02": 900.0, "03": -400.0, "13": 100.0, "14": -100.0, "17": -100.0, "19": -100.0,
        }
        assert r.negativos_pendientes_posteriores == 100.0

    def test_q2_uses_the_q1_negative_in_box_15(self, year):
        r = compute_modelo_130(2025, 2, year)
        # 01 = 500 + 5,400 + 600 = 6,500; real expenses = 900 + 4,100 = 5,000
        # 5 % allowance = 5 % × (6,500 − 5,000) = 75.00 → 02 = 5,075.00
        # 03 = 6,500 − 5,075 = 1,425.00; 04 = 20 % × 1,425 = 285.00
        # 05 = Σ positive 07 of Q1 = 0; 06 = 90.00; 07 = 285 − 0 − 90 = 195.00
        # 12 = 195.00; 13 = 100; 14 = 95.00
        # 15 = min(95.00, 100 pending from Q1) = 95.00; 17 = 0; 19 = 0; 5.00 left for Q3
        assert r.gastos_reales == 5000.0 and r.gastos_dificil_justificacion == 75.0
        assert r.aeat_boxes() == {
            **{b: 0.0 for b in BOXES},
            "01": 6500.0, "02": 5075.0, "03": 1425.0, "04": 285.0, "06": 90.0, "07": 195.0,
            "12": 195.0, "13": 100.0, "14": 95.0, "15": 95.0,
        }
        assert r.c05_source == "app_chain"
        assert r.negativos_pendientes_anteriores == 100.0
        assert r.negativos_pendientes_posteriores == 5.0

    def test_audit_has_a_cell_for_every_box(self, year):
        r = compute_modelo_130(2025, 2, year)
        entries = [{"cell": e.cell, "value": e.value} for e in r.audit]
        for box in BOXES:
            hits = audit_entries_for_box(entries, "130", box)
            assert hits and hits[0]["cell"] == MODELO130_BOX_FIELDS[box]
        # The box 02 split is drillable under 02.
        cells_02 = {e["cell"] for e in audit_entries_for_box(entries, "130", "02")}
        assert {"c02_gastos_reales", "c02_gastos_dificil_justificacion", "c02_amortizaciones"} <= cells_02


# ---------------------------------------------------------------------------
# Individual rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("net, expected", [
    (-5000.0, 100.0), (9000.0, 100.0), (9000.01, 75.0), (10000.0, 75.0), (10000.01, 50.0),
    (11000.0, 50.0), (11000.01, 25.0), (12000.0, 25.0), (12000.01, 0.0),
])
def test_reduction_scale_boundaries(net, expected):
    assert minoracion_art_110_3_c(net) == expected


class TestPreviousYearNetYield:
    def test_filed_q4_box_03_wins_over_config(self, conn):
        _filed_130(conn, 2024, 4, c01=20000.0, c02=10500.0, c03=9500.0)
        r = compute_modelo_130(2025, 1, conn, {"tax": {"previous_year_net_yield": 1000.0}})
        assert (r.c13_minoracion, r.previous_year_net_source, r.previous_year_net_yield) == (75.0, "filed", 9500.0)

    def test_config_number_and_per_year_mapping(self, conn):
        assert compute_modelo_130(2025, 1, conn, {"tax": {"previous_year_net_yield": 10500.0}}).c13_minoracion == 50.0
        per_year = {"tax": {"previous_year_net_yield": {"2024": 11500.0, "2023": 1.0}}}
        r = compute_modelo_130(2025, 1, conn, per_year)
        assert (r.c13_minoracion, r.previous_year_net_source) == (25.0, "config")

    def test_app_previous_year_when_nothing_else(self, conn):
        # 2024 app net: 20,000 − 5,000 real − 5 % × 15,000 = 750 allowance = 14,250 → no reduction.
        _stripe(conn, "old", "2024-06-10", 20000.0)
        _invoice(conn, "old_e", "in", "2024-06-11", 5000.0)
        r = compute_modelo_130(2025, 1, conn)
        assert (r.previous_year_net_yield, r.previous_year_net_source, r.c13_minoracion) == (14250.0, "app", 0.0)


def test_negative_net_keeps_its_sign(conn):
    _stripe(conn, "s1", "2025-01-10", 300.0)
    _invoice(conn, "e1", "in", "2025-01-11", 1000.0)
    _invoice(conn, "o1", "out", "2025-01-12", 200.0, irpf=30.0)
    r = compute_modelo_130(2025, 1, conn)
    # 01 = 500; 02 = 1,000 (no allowance on a loss); 03 = −500; 04 = 0; 06 = 30
    # 07 = 0 − 0 − 30 = −30 (negative allowed); 12 = max(0, −30) = 0; 14 = −100; 19 = −100
    assert (r.c03_rendimiento_neto, r.c04_veinte_pct, r.c07_pago_fraccionado) == (-500.0, 0.0, -30.0)
    assert (r.c12_suma_pagos, r.c14_diferencia, r.c15_negativos_anteriores, r.c19_resultado) == (
        0.0, -100.0, 0.0, -100.0)


def test_gdj_cap_2000(conn):
    _stripe(conn, "s1", "2025-02-10", 60000.0)
    _invoice(conn, "e1", "in", "2025-02-11", 10000.0)
    r = compute_modelo_130(2025, 1, conn)
    # 5 % × (60,000 − 10,000) = 2,500 → capped at 2,000; 02 = 12,000; 03 = 48,000
    assert r.gastos_dificil_justificacion == 2000.0
    assert (r.c02_gastos, r.c03_rendimiento_neto) == (12000.0, 48000.0)
    cell = next(e for e in r.audit if e.cell == "c02_gastos_dificil_justificacion")
    inputs = json.loads(cell.inputs_json)
    assert inputs["cap_applied"] is True and inputs["raw_5pct"] == 2500.0


def test_gdj_only_in_simplified_regime(conn):
    _stripe(conn, "s1", "2025-02-10", 1000.0)
    r = compute_modelo_130(2025, 1, conn, {"tax": {"regime": "estimacion_directa_normal"}})
    assert (r.gastos_dificil_justificacion, r.c02_gastos) == (0.0, 0.0)


def test_withholdings_from_gross_booked_invoices(conn):
    # Issued invoices booked gross (D12): income 450 + 337.50, withholdings 67.50 + 50.63 to the cent.
    _invoice(conn, "o1", "out", "2025-04-01", 450.0, irpf=67.5)
    _invoice(conn, "o2", "out", "2025-05-16", 337.5, irpf=50.63)
    _invoice(conn, "o3", "out", "2025-05-17", 999.0, irpf=99.0, excluded=1)   # excluded: ignored
    r = compute_modelo_130(2025, 2, conn)
    assert r.c01_ingresos == 787.5
    assert r.c06_retenciones == 118.13


def test_expenses_use_the_irpf_business_share(conn):
    _stripe(conn, "s1", "2025-02-10", 100.0)
    _invoice(conn, "e1", "in", "2025-02-11", 1000.0, pct_irpf=2.07)     # utilities, office share
    assert compute_modelo_130(2025, 1, conn).gastos_reales == 20.7


class TestChainSources:
    """Box 05 and the negative carry for 15: filed returns first, else the app's own quarters."""

    @pytest.fixture
    def data(self, conn):
        # App Q1: 01 = 2,000 → allowance 100 → 03 = 1,900 → 04 = 380 = 07.
        _stripe(conn, "s1", "2025-02-10", 2000.0)
        # Q2 adds 1,000 → 01 = 3,000, allowance 150, 03 = 2,850, 04 = 570.
        _stripe(conn, "s2", "2025-05-10", 1000.0)
        return conn

    def test_app_chain(self, data):
        r = compute_modelo_130(2025, 2, data)
        # App Q1: 07 = 380 → 12 = 380, 13 = 100, 14 = 280, 19 = 280 (no negatives).
        # Q2: 05 = 380; 07 = 570 − 380 = 190; 12 = 190; 14 = 90; 15 = 0; 19 = 90.
        assert (r.c05_pagos_anteriores, r.c05_source) == (380.0, "app_chain")
        assert (r.c07_pago_fraccionado, r.c14_diferencia, r.c15_negativos_anteriores, r.c19_resultado) == (
            190.0, 90.0, 0.0, 90.0)

    def test_filed_chain_wins(self, data):
        # Filed Q1 differs from the app: 07 = 40, then 13 = 100 → 14 = 17 = 19 = −60.
        _filed_130(data, 2025, 1, c07=40.0, c12=40.0, c13=100.0, c14=-60.0, c17=-60.0, c19=-60.0)
        r = compute_modelo_130(2025, 2, data)
        # 05 = 40 (filed positive 07); 07 = 570 − 40 = 530; 12 = 530; 14 = 430
        # 15 = min(430, 60 pending from the filed Q1) = 60; 17 = 19 = 370
        assert (r.c05_pagos_anteriores, r.c05_source) == (40.0, "filed")
        assert (r.c07_pago_fraccionado, r.c14_diferencia) == (530.0, 430.0)
        assert (r.c15_negativos_anteriores, r.c19_resultado, r.negativos_pendientes_posteriores) == (
            60.0, 370.0, 0.0)

    def test_mixed_chain_and_used_negatives(self, data):
        # Filed Q1 left 100 of negatives, filed Q2 used 75.73 of them; Q3 not filed → the app computes it.
        _filed_130(data, 2025, 1, c13=100.0, c14=-100.0, c17=-100.0, c19=-100.0)
        _filed_130(data, 2025, 2, c07=175.73, c12=175.73, c13=100.0, c14=75.73, c15=75.73)
        q4 = compute_modelo_130(2025, 4, data)
        q3 = compute_modelo_130(2025, 3, data)
        assert q3.negativos_pendientes_anteriores == 24.27
        # Q3 (app): 04 = 570; 05 = 175.73; 07 = 394.27; 14 = 294.27; 15 = 24.27; 19 = 270.00
        assert (q3.c05_pagos_anteriores, q3.c15_negativos_anteriores, q3.c19_resultado) == (175.73, 24.27, 270.0)
        # Q4: 05 = 175.73 (filed Q2) + 394.27 (app Q3) = 570; nothing pending.
        assert (q4.c05_pagos_anteriores, q4.c05_source) == (570.0, "mixed")
        assert q4.negativos_pendientes_anteriores == 0.0


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def test_old_snapshot_field_names_still_decode():
    legacy = {
        "year": 2024, "quarter": 2, "box_01_ingresos": 1000.0, "box_02_gastos": 400.0,
        "box_03_rendimiento": 600.0, "gastos_dificil_justificacion": 30.0, "rendimiento_neto": 570.0,
        "box_05_base": 114.0, "box_07_retenciones": 15.0, "box_14_pagos_anteriores": 20.0,
        "box_16_resultado": 79.0, "notes": "",
    }
    r = decode_snapshot("130", json.dumps(legacy))
    boxes = r.aeat_boxes()
    assert (boxes["01"], boxes["02"], boxes["03"], boxes["04"]) == (1000.0, 430.0, 570.0, 114.0)
    assert (boxes["05"], boxes["06"], boxes["07"]) == (20.0, 15.0, 79.0)
    assert (boxes["12"], boxes["14"], boxes["17"], boxes["19"]) == (79.0, 79.0, 79.0, 79.0)
    assert r.gastos_reales == 400.0


def test_snapshot_roundtrip(conn):
    _stripe(conn, "s1", "2025-02-10", 800.0)
    r = compute_modelo_130(2025, 1, conn)
    back = decode_snapshot("130", encode_snapshot("130", r))
    assert back.aeat_boxes() == r.aeat_boxes()
    assert back.previous_year_net_source == r.previous_year_net_source

# ---------------------------------------------------------------------------
# Platform (application) fees in box 02 (#135)
# ---------------------------------------------------------------------------

def _stripe_fee(conn, id, date, base, fee_stripe, fee_application, refunded=0.0):
    """A Spanish Stripe sale whose balance-transaction fee is split (NULL split = legacy row)."""
    _stripe(conn, id, date, base)
    known = [x for x in (fee_stripe, fee_application) if x is not None]
    conn.execute("UPDATE transactions SET fee = ?, fee_stripe = ?, fee_application = ?, "
                 "converted_amount_refunded = ? WHERE id = ?",
                 (round(sum(known), 2) if known else 1.4, fee_stripe, fee_application, refunded, id))
    conn.commit()


def _cell(r, cell):
    entry = next(e for e in r.audit if e.cell == cell)
    return entry.value, json.loads(entry.inputs_json)


class TestPlatformFees:
    def test_application_fee_is_expensed_stripe_fee_is_not(self, conn):
        _stripe_fee(conn, "pf1", "2025-02-10", 100.0, fee_stripe=0.40, fee_application=1.00)
        r = compute_modelo_130(2025, 1, conn)
        # Stripe's 0.40 is on the Stripe invoices; only the platform's 1.00 enters 02.
        assert r.gastos_reales == 1.0
        value, inputs = _cell(r, "c02_platform_fees")
        assert value == 1.0 and inputs["fee_split_unknown"] == 0
        assert [(x["charge_id"], x["fee_application"]) for x in inputs["records"]] == [("pf1", 1.0)]
        assert _cell(r, "c02_gastos_reales")[1]["platform_fees"] == 1.0

    def test_no_application_fee_adds_nothing(self, conn):
        _stripe_fee(conn, "pf2", "2025-02-10", 100.0, fee_stripe=0.40, fee_application=0.0)
        r = compute_modelo_130(2025, 1, conn)
        assert r.gastos_reales == 0.0
        assert _cell(r, "c02_platform_fees") == (0.0, {"fee_split_unknown": 0, "records": []})
        assert "Fee split unknown" not in r.notes

    def test_unknown_split_is_counted_never_guessed(self, conn):
        _stripe_fee(conn, "pf3", "2025-02-10", 100.0, fee_stripe=None, fee_application=None)
        _stripe_fee(conn, "pf4", "2025-03-10", 100.0, fee_stripe=0.40, fee_application=1.00)
        r = compute_modelo_130(2025, 1, conn)
        assert r.gastos_reales == 1.0                     # the legacy row's 1.40 fee is not expensed
        assert _cell(r, "c02_platform_fees")[1]["fee_split_unknown"] == 1
        assert "Fee split unknown for 1 Stripe charge(s); re-fetch" in r.notes

    def test_ytd_by_charge_date_and_refunds_follow_the_balance_transaction(self, conn):
        _stripe_fee(conn, "pf5", "2025-02-10", 100.0, fee_stripe=0.40, fee_application=1.00)
        # Refunded in full: the platform did not return its fee on the charge's balance transaction.
        _stripe_fee(conn, "pf6", "2025-05-10", 50.0, fee_stripe=0.30, fee_application=0.50, refunded=60.5)
        _stripe_fee(conn, "pf7", "2025-07-01", 80.0, fee_stripe=0.30, fee_application=0.80)   # Q3: not yet
        assert compute_modelo_130(2025, 1, conn).gastos_reales == 1.0
        r2 = compute_modelo_130(2025, 2, conn)
        assert r2.gastos_reales == 1.5
        assert [x["refunded_eur"] for x in _cell(r2, "c02_platform_fees")[1]["records"]] == [0.0, 60.5]
