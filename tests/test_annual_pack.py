"""Annual pack (#103): Modelo 390 from four synthetic 303 quarters, the Modelo 347
purchases threshold and the P&L per IAE activity tied to the Q4 Modelo 130.

All figures, names and NIFs are synthetic."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.annual_pack import build_annual_pack, csv_347, csv_390, csv_pl, main, to_markdown
from src.database import add_tax_entry, init_db
from src.filed_returns import FiledReturn, store_filed_return
from src.fixed_assets import FixedAsset, add_fixed_asset
from src.modelo_347 import EXCLUDED_349, EXCLUDED_WITHHOLDING, THRESHOLD_EUR, compute_modelo_347_purchases
from src.modelo_390 import compute_modelo_390
from src.pl_by_activity import compute_pl_by_activity
from src.tax_engine import compute_modelo_130, compute_modelo_303
from src.tax_models import Modelo303Result
from src.vendor_registry import Vendor, VendorRegistry

CFG: dict = {"tax": {}}
YEAR = 2025


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "annual.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _tx(conn, id, date, base, treatment="IVA_ES_21", activity="COACHING", geo="SPAIN"):
    vat = round(base * 0.21, 2) if treatment == "IVA_ES_21" else 0.0
    conn.execute(
        """INSERT INTO transactions (id, created_date, converted_amount, converted_amount_refunded,
               description, fee, currency, activity_type, geo_region, vat_treatment,
               vat_base_eur, vat_amount_eur)
           VALUES (?, ?, ?, 0, 'synthetic', 0, 'eur', ?, ?, ?, ?, ?)""",
        (id, f"{date}T10:00:00", base + vat, activity, geo, treatment, base, vat),
    )
    conn.commit()


def _inv(conn, id, direction, date, base, tax_treatment, iva=0.0, rate=None, geo="SPAIN", nif=None,
         name=None, irpf=None, activity=None, excluded=0):
    conn.execute(
        """INSERT INTO invoices (id, filename, direction, invoice_date, subtotal_eur, iva_rate, iva_amount,
               irpf_amount, tax_treatment, deductible_pct_vat, deductible_pct_irpf, geo_region,
               vendor_nif, vendor_name, client_nif, client_name, activity_type, excluded)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 100, 100, ?, ?, ?, ?, ?, ?, ?)""",
        (id, f"{id}.pdf", direction, date, base, rate, iva, irpf, tax_treatment, geo,
         nif if direction == "in" else None, name if direction == "in" else None,
         nif if direction == "out" else None, name if direction == "out" else None, activity, excluded),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Modelo 390
# ---------------------------------------------------------------------------

class TestModelo390FourQuarters:
    """One synthetic year touching every 390 section the taxpayer uses."""

    @pytest.fixture
    def year(self, conn):
        # Q1: Spanish sale 1,000 at 21 %; domestic purchases at 21 % and 10 %.
        _tx(conn, "s1", "2025-02-10", 1000.0)
        _inv(conn, "e21", "in", "2025-03-01", 200.0, "DOMESTIC", iva=42.0, rate=21)
        _inv(conn, "e10", "in", "2025-03-02", 100.0, "DOMESTIC", iva=10.0, rate=10)
        # Q2: intra-EU B2B service sold; intra-EU service bought (reverse charge, 21 %).
        _inv(conn, "o_eu", "out", "2025-05-05", 500.0, "EU_B2B", geo="EU_NOT_SPAIN", nif="IE1234567X")
        _inv(conn, "e_eu", "in", "2025-05-06", 100.0, "INTRA_EU_RC", rate=21, geo="EU_NOT_SPAIN",
             nif="IE7654321X")
        # Q3: non-EU customer (not subject by location); non-EU reverse-charge purchase.
        _tx(conn, "s_x", "2025-08-01", 300.0, treatment="IVA_EXPORT", geo="OUTSIDE_EU")
        _inv(conn, "e_rc", "in", "2025-08-02", 50.0, "NON_EU_RC", rate=21, geo="OUTSIDE_EU")
        # Q4: Spanish sale 200 and an exempt teaching invoice → pro-rata (art. 104 LIVA).
        _tx(conn, "s4", "2025-11-10", 200.0)
        _inv(conn, "o_ex", "out", "2025-11-20", 400.0, "EXEMPT_TEACHING")
        return [compute_modelo_303(YEAR, q, conn, CFG) for q in range(1, 5)]

    def test_accrued_by_rate(self, conn, year):
        b = compute_modelo_390(YEAR, conn, CFG, quarters=year).aeat_boxes()
        assert (b["05"], b["06"]) == (1200.0, 252.0)
        assert b["01"] == b["02"] == b["03"] == b["04"] == 0.0
        assert (b["551"], b["552"]) == (100.0, 21.0)          # intra-EU services at 21 %
        assert (b["27"], b["28"]) == (50.0, 10.5)             # other reverse charge
        assert (b["33"], b["34"], b["47"]) == (1350.0, 283.5, 283.5)

    def test_deductible_by_rate_and_totals(self, conn, year):
        b = compute_modelo_390(YEAR, conn, CFG, quarters=year).aeat_boxes()
        assert (b["603"], b["604"]) == (100.0, 10.0)
        assert (b["605"], b["606"]) == (250.0, 52.5)          # domestic 21 % + NON_EU_RC
        assert (b["48"], b["49"]) == (350.0, 62.5)
        assert (b["637"], b["638"], b["597"], b["598"]) == (100.0, 21.0, 100.0, 21.0)
        assert b["50"] == b["51"] == 0.0

    def test_prorrata_and_result_tie_to_the_quarters(self, conn, year):
        m = compute_modelo_390(YEAR, conn, CFG, quarters=year)
        b = m.aeat_boxes()
        # with right to deduct 1,200 + 500 + 300 = 2,000; exempt 400 → 83.3 % → 84 % (rounded up).
        assert m.prorrata_applies and m.prorrata_type == "G"
        assert (b["115"], b["116"], b["118"]) == (2400.0, 2000.0, 84.0)
        assert b["522"] == year[3].c44_regularizacion_prorrata == round(83.5 * (84 - 100) / 100, 2)
        assert b["64"] == round(b["49"] + b["51"] + b["598"] + b["63"] + b["522"], 2)
        assert b["65"] == round(b["47"] - b["64"], 2)
        assert b["65"] == round(sum(r.c46_resultado_regimen_general for r in year), 2)
        assert b["86"] == b["84"] == b["65"]                  # nothing carried in from 2024
        assert b["95"] == round(sum(max(0.0, r.c71_resultado_liquidacion) for r in year), 2)

    def test_volume_of_operations(self, conn, year):
        b = compute_modelo_390(YEAR, conn, CFG, quarters=year).aeat_boxes()
        assert (b["99"], b["103"], b["104"], b["105"], b["110"]) == (1200.0, 500.0, 0.0, 400.0, 300.0)
        assert b["108"] == 2400.0

    def test_computes_the_quarters_itself(self, conn, year):
        assert compute_modelo_390(YEAR, conn, CFG).aeat_boxes() == \
            compute_modelo_390(YEAR, conn, CFG, quarters=year).aeat_boxes()

    def test_audit_has_one_cell_per_box(self, conn, year):
        m = compute_modelo_390(YEAR, conn, CFG, quarters=year)
        assert [e.cell for e in m.audit] == [f"c{box}" for box in m.aeat_boxes()]


class TestModelo390RateRows:
    """#137: the 390 prefers the invoice's own iva_rate over inferring VAT ÷ base."""

    def test_prefers_stored_rate_over_misleading_ratio(self, conn):
        # The invoice's own rate is 10 %, but an iva_amount/subtotal_eur mismatch
        # (e.g. a blended-rate bill or OCR rounding) makes the raw ratio look
        # like 4.5 % — nearest 4 % — which would misroute the record.
        _inv(conn, "mixed", "in", "2025-03-03", 1000.0, "DOMESTIC", iva=45.0, rate=10)
        quarters = [compute_modelo_303(YEAR, q, conn, CFG) for q in range(1, 5)]
        b = compute_modelo_390(YEAR, conn, CFG, quarters=quarters).aeat_boxes()
        assert (b["603"], b["604"]) == (1000.0, 45.0)
        assert (b["190"], b["191"]) == (0.0, 0.0)

    def test_missing_rate_falls_back_to_ratio_and_is_counted(self, conn):
        # No stored iva_rate: still falls back to VAT ÷ base (21 %), and the
        # fallback is counted in the audit note.
        _inv(conn, "norate", "in", "2025-03-04", 100.0, "DOMESTIC", iva=21.0, rate=None)
        quarters = [compute_modelo_303(YEAR, q, conn, CFG) for q in range(1, 5)]
        m = compute_modelo_390(YEAR, conn, CFG, quarters=quarters)
        b = m.aeat_boxes()
        assert (b["605"], b["606"]) == (100.0, 21.0)
        assert "no stored VAT rate" in m.notes

    def test_off_rate_invoice_reported_not_forced(self, conn):
        # 7.5 % is not one of the modelled 4/10/21 rows: the nearest (10 %) is
        # used, but the off-rate is reported, not silently absorbed.
        _inv(conn, "seventyfive", "in", "2025-03-05", 200.0, "DOMESTIC", iva=15.0, rate=7.5)
        quarters = [compute_modelo_303(YEAR, q, conn, CFG) for q in range(1, 5)]
        m = compute_modelo_390(YEAR, conn, CFG, quarters=quarters)
        b = m.aeat_boxes()
        assert (b["603"], b["604"]) == (200.0, 15.0)
        assert "7.5" in m.notes

    def test_credit_note_lands_negative_on_its_invoices_rate_row(self, conn):
        # #161: a received rectificativa nets off on the rate row of the invoice it
        # corrects (here 10 %, in a later quarter), next to an untouched 21 % row.
        _inv(conn, "ten", "in", "2025-02-10", 100.0, "DOMESTIC", iva=10.0, rate=10)
        _inv(conn, "ten_cn", "in", "2025-05-10", -40.0, "DOMESTIC", iva=-4.0, rate=10)
        _inv(conn, "twenty_one", "in", "2025-05-11", 200.0, "DOMESTIC", iva=42.0, rate=21)
        quarters = [compute_modelo_303(YEAR, q, conn, CFG) for q in range(1, 5)]
        b = compute_modelo_390(YEAR, conn, CFG, quarters=quarters).aeat_boxes()
        assert (b["603"], b["604"]) == (60.0, 6.0)
        assert (b["605"], b["606"]) == (200.0, 42.0)
        assert (b["48"], b["49"]) == (260.0, 48.0)

    def test_platform_fees_in_reverse_charge_and_deductible_21_rows(self, conn):
        # #147: Stripe application fees are a NON_EU_RC purchase — 27/28 and the 21 % deductible row,
        # next to a 10 % domestic invoice that must stay in its own row.
        _tx(conn, "fee_q2", "2025-05-05", 100.0)
        _tx(conn, "fee_q3", "2025-08-05", 100.0)
        conn.execute("UPDATE transactions SET fee_application = 6.0 WHERE id = 'fee_q2'")
        conn.execute("UPDATE transactions SET fee_application = 4.0 WHERE id = 'fee_q3'")
        conn.commit()
        _inv(conn, "ten", "in", "2025-05-06", 50.0, "DOMESTIC", iva=5.0, rate=10)
        quarters = [compute_modelo_303(YEAR, q, conn, CFG) for q in range(1, 5)]
        b = compute_modelo_390(YEAR, conn, CFG, quarters=quarters).aeat_boxes()
        assert (b["27"], b["28"]) == (10.0, 2.1)
        assert (b["605"], b["606"]) == (10.0, 2.1)
        assert (b["603"], b["604"]) == (50.0, 5.0)
        assert (b["48"], b["49"]) == (60.0, 7.1)


class TestModelo390Compensation:
    def test_carried_in_credit_and_q4_boxes(self, conn):
        store_filed_return(conn, FiledReturn(
            model="303", year=2024, period="4T", justificante="SYN2024", source_file="synthetic.pdf",
            presented_at="2025-01-20T10:00:00", boxes={"72": 300.0, "87": 0.0, "71": -300.0},
        ))
        _tx(conn, "a", "2025-02-01", 1000.0)                                    # Q1 +210, uses 210 of 300
        _tx(conn, "b", "2025-05-01", 500.0)                                     # Q2 +105, uses the last 90
        _inv(conn, "big", "in", "2025-08-01", 1000.0, "DOMESTIC", iva=210.0, rate=21)  # Q3 −210 → 72
        b = compute_modelo_390(YEAR, conn, CFG).aeat_boxes()
        assert b["65"] == 105.0
        assert b["85"] == 300.0                        # credit of 2024 applied in 2025
        assert b["86"] == -195.0
        assert b["95"] == 15.0                         # only Q2 paid (105 − 90)
        assert b["97"] == 0.0 and b["98"] == 0.0       # Q4 result 0: its 110 is still pending (87)
        assert b["662"] == 210.0                       # Q3's credit, never applied in 2025
        assert "115" not in b                          # no exempt operations → no pro-rata section


def _q303(quarter: int, c110: float, c78: float, c72: float = 0.0, source: str = "none") -> Modelo303Result:
    """A synthetic 303 quarter carrying only the credit-chain boxes the 390 reads (#136)."""
    return Modelo303Result(year=YEAR, quarter=quarter, c110_pendiente_anteriores=c110,
                           c78_aplicadas_periodo=c78, c87_pendiente_posteriores=round(c110 - c78, 2),
                           c72_a_compensar=c72, c110_source=source)


def _audit_of(m, cell: str) -> dict:
    return json.loads(next(e for e in m.audit if e.cell == cell).inputs_json)


class TestModelo390CreditWalk:
    """Box 85 holds only credit of earlier years; credit generated in the year goes to 662 (#136)."""

    def test_intra_year_credit_applied_later_is_not_in_85(self, conn):
        qs = [_q303(1, 0.0, 0.0), _q303(2, 0.0, 0.0, c72=80.0), _q303(3, 80.0, 80.0), _q303(4, 0.0, 0.0)]
        b = compute_modelo_390(YEAR, conn, CFG, quarters=qs).aeat_boxes()
        assert b["85"] == 0.0 and b["662"] == 0.0

    def test_carried_in_credit_used_up_before_intra_year_credit(self, conn):
        qs = [_q303(1, 100.0, 60.0, source="filed"), _q303(2, 40.0, 40.0),
              _q303(3, 0.0, 0.0, c72=70.0), _q303(4, 70.0, 70.0)]
        b = compute_modelo_390(YEAR, conn, CFG, quarters=qs).aeat_boxes()
        assert b["85"] == 100.0 and b["662"] == 0.0

    def test_662_is_the_intra_year_credit_still_pending(self, conn):
        # 100 carried in: Q1 uses 60; Q2 generates 50; Q3 uses 30 (all from 2024's credit, FIFO).
        qs = [_q303(1, 100.0, 60.0, source="filed"), _q303(2, 40.0, 0.0, c72=50.0),
              _q303(3, 90.0, 30.0), _q303(4, 60.0, 0.0)]
        m = compute_modelo_390(YEAR, conn, CFG, quarters=qs)
        b = m.aeat_boxes()
        assert b["85"] == 90.0                         # 10 of 2024's credit is still pending
        assert b["662"] == 50.0                        # Q2's credit, none of it applied
        walk = _audit_of(m, "c85")["walk"]
        assert [s["from_earlier_years"] for s in walk] == [60.0, 0.0, 30.0, 0.0]
        assert _audit_of(m, "c85")["flags"] == []

    def test_credit_shrunk_mid_year_is_not_refilled_by_intra_year_credit(self, conn):
        # Q2's 110 (from a filed Q1 that diverges from the app) leaves only 30 of 2024's
        # credit; Q3 then generates 50, which Q4 applies — that 50 is not 2024 credit.
        qs = [_q303(1, 100.0, 0.0, source="filed"), _q303(2, 30.0, 30.0),
              _q303(3, 0.0, 0.0, c72=50.0), _q303(4, 50.0, 50.0)]
        m = compute_modelo_390(YEAR, conn, CFG, quarters=qs)
        b = m.aeat_boxes()
        assert b["85"] == 30.0
        assert b["86"] == round(b["84"] - 30.0, 2)
        assert _audit_of(m, "c85")["flags"] == ["chain_break_Q2"]
        assert "Q2" in m.notes

    def test_flags_carried_in_credit_from_the_app_chain(self, conn):
        qs = [_q303(1, 100.0, 100.0, source="app_chain"), _q303(2, 0.0, 0.0),
              _q303(3, 0.0, 0.0), _q303(4, 0.0, 0.0)]
        m = compute_modelo_390(YEAR, conn, CFG, quarters=qs)
        inputs = _audit_of(m, "c85")
        assert inputs["carried_in_source"] == "app_chain"
        assert inputs["flags"] == ["carried_in_from_app_chain"]
        assert _audit_of(m, "c662")["flags"] == ["carried_in_from_app_chain"]
        assert "app's own chain" in m.notes


# ---------------------------------------------------------------------------
# Modelo 347 — purchases
# ---------------------------------------------------------------------------

class TestModelo347Purchases:
    def test_threshold_is_strictly_above_3005_06(self, conn):
        # VAT-inclusive totals: 2,483.52 + 521.54 = 3,005.06 (not declared); 3,005.07 is declared.
        _inv(conn, "at", "in", "2025-02-01", 2483.52, "DOMESTIC", iva=521.54, nif="B12345678", name="At Threshold")
        _inv(conn, "ov1", "in", "2025-02-01", 1000.00, "DOMESTIC", iva=210.00, nif="B87654321", name="Over One Cent")
        _inv(conn, "ov2", "in", "2025-10-01", 1483.53, "DOMESTIC", iva=311.54, nif="B87654321", name="Over One Cent")
        r = compute_modelo_347_purchases(YEAR, conn, VendorRegistry())
        assert THRESHOLD_EUR == 3005.06
        assert [(x.nif, x.total) for x in r.rows] == [("B87654321", 3005.07)]
        assert r.rows[0].quarter_breakdown == {1: 1210.0, 4: 1795.07}
        assert r.below_threshold == 1

    def test_exclusions(self, conn):
        _inv(conn, "eu", "in", "2025-03-01", 5000.0, "INTRA_EU_RC", rate=21, geo="EU_NOT_SPAIN",
             nif="IE1234567X", name="EU Cloud")
        _inv(conn, "wh", "in", "2025-03-01", 4000.0, "DOMESTIC", iva=840.0, irpf=600.0, nif="12345678Z",
             name="Withheld Professional")
        _inv(conn, "xx", "in", "2025-03-01", 9000.0, "DOMESTIC", iva=1890.0, nif="B11111111", name="Dup",
             excluded=1)
        _inv(conn, "nonif", "in", "2025-03-01", 4000.0, "DOMESTIC", iva=840.0, name="No Nif Shop")
        _inv(conn, "us", "in", "2025-03-01", 4000.0, "NON_EU_RC", rate=21, geo="OUTSIDE_EU", name="US Saas")
        r = compute_modelo_347_purchases(YEAR, conn, VendorRegistry())
        assert r.rows == []
        assert {(e["id"], e["reason"]) for e in r.excluded} == {("eu", EXCLUDED_349), ("wh", EXCLUDED_WITHHOLDING)}
        assert [u.name for u in r.unidentified] == ["No Nif Shop"]

    def test_registry_supplies_the_missing_nif(self, conn):
        _inv(conn, "a", "in", "2025-06-01", 3000.0, "DOMESTIC", iva=630.0, name="Acme Installers SL")
        registry = VendorRegistry(vendors=[Vendor(key="acme installers", vat_id="B22222222", country="ES",
                                                  legal_entity="Acme Installers SL")])
        r = compute_modelo_347_purchases(YEAR, conn, registry)
        assert [(x.nif, x.name, x.total) for x in r.rows] == [("B22222222", "Acme Installers SL", 3630.0)]

    def test_activity_start_date_excludes_purchases_before_it(self, conn):
        # #133: a purchase dated before the business existed doesn't belong
        # to it either, same floor as the sales side and the quarterly models.
        _inv(conn, "before", "in", "2025-01-10", 3000.0, "DOMESTIC", iva=630.0, nif="B87654321",
             name="Early Vendor")
        cfg = {"tax": {"activity_start_date": "2025-02-01"}}
        r = compute_modelo_347_purchases(YEAR, conn, VendorRegistry(), cfg)
        assert r.rows == []
        # sanity: without the floor it clears the threshold
        r_nofloor = compute_modelo_347_purchases(YEAR, conn, VendorRegistry(), {"tax": {}})
        assert len(r_nofloor.rows) == 1


# ---------------------------------------------------------------------------
# P&L per IAE activity
# ---------------------------------------------------------------------------

class TestPLByActivity:
    @pytest.fixture
    def year(self, conn, db):
        _tx(conn, "c1", "2025-02-01", 3000.0, activity="COACHING")
        _tx(conn, "n1", "2025-05-01", 800.0, treatment="IVA_EU_B2B", activity="NEWSLETTER", geo="EU_NOT_SPAIN")
        _tx(conn, "i1", "2025-08-01", 1200.0, treatment="IVA_EXPORT", activity="ILLUSTRATIONS", geo="OUTSIDE_EU")
        _inv(conn, "teach", "out", "2025-10-01", 400.0, "EXEMPT_TEACHING")                 # → COACHING
        _inv(conn, "misc", "out", "2025-11-01", 100.0, "ES_21", iva=21.0, rate=21)         # no activity
        _inv(conn, "ill", "in", "2025-03-01", 150.0, "DOMESTIC", iva=31.5, rate=21, activity="ILLUSTRATIONS")
        _inv(conn, "acme", "in", "2025-04-01", 90.0, "DOMESTIC", iva=18.9, rate=21, name="Acme Mailer Ltd")
        _inv(conn, "anon", "in", "2025-04-02", 60.0, "DOMESTIC", iva=12.6, rate=21)       # no activity
        conn.execute("INSERT INTO social_security_payments (payment_date, amount_eur) VALUES ('2025-01-31', 300)")
        conn.commit()
        add_fixed_asset(conn, FixedAsset(description="synthetic laptop", acquisition_date="2025-01-01",
                                         base_eur=2000.0, asset_class="it_equipment"))
        add_tax_entry(YEAR, 4, "GASTOS_DEDUCIBLES", 50.0, db_path=db)
        return VendorRegistry(vendors=[Vendor(key="acme mailer", activity="NEWSLETTER")])

    def test_totals_tie_to_the_q4_130(self, conn, year):
        pl = compute_pl_by_activity(YEAR, conn, CFG, year)
        m130 = compute_modelo_130(YEAR, 4, conn, CFG)
        assert pl.total_income == m130.c01_ingresos == 5500.0
        assert pl.total_expenses == m130.gastos_reales
        assert pl.gastos_dificil_justificacion == m130.gastos_dificil_justificacion > 0
        assert pl.m130_c02 == round(pl.total_expenses + pl.gastos_dificil_justificacion, 2)
        assert pl.ties_to_130
        assert round(sum(a.income for a in pl.activities), 2) == pl.total_income
        assert round(sum(a.total_expenses for a in pl.activities), 2) == pl.total_expenses

    def test_default_allocation_sends_shared_items_to_826(self, conn, year):
        pl = compute_pl_by_activity(YEAR, conn, CFG, year)
        by = {a.iae: a for a in pl.activities}
        assert by["826"].income == 3000.0 + 400.0 + 100.0
        assert by["751"].income == 800.0 and by["861"].income == 1200.0
        assert by["861"].expenses_invoices == 150.0
        assert by["751"].expenses_invoices == 90.0            # activity from the vendor registry
        assert by["826"].reta == 300.0 and by["826"].depreciation > 0
        assert by["826"].other_expenses == 60.0 + 50.0        # no-activity invoice + manual entry
        assert by["751"].reta == by["861"].reta == 0.0

    def test_by_income_allocation(self, conn, year):
        cfg = {"tax": {"pl_allocation": {"reta": "BY_INCOME", "depreciation": "NEWSLETTER"}}}
        pl = compute_pl_by_activity(YEAR, conn, cfg, year)
        by = {a.activity: a for a in pl.activities}
        # Direct income: coaching 3,400 (incl. the teaching invoice), newsletter 800, illustrations 1,200.
        assert by["COACHING"].reta == round(300 * 3400 / 5400, 2)
        assert by["NEWSLETTER"].reta == round(300 * 800 / 5400, 2)
        assert by["NEWSLETTER"].depreciation > 0 and by["COACHING"].depreciation == 0.0
        assert pl.ties_to_130

    def test_invalid_allocation_falls_back_to_826(self, conn, year):
        pl = compute_pl_by_activity(YEAR, conn, {"tax": {"pl_allocation": {"reta": "PAINTING"}}}, year)
        assert pl.allocation["reta"] == "COACHING" and "PAINTING" in pl.notes

    def test_platform_fee_goes_to_the_charge_activity_and_ties(self, conn, year):
        # #135: the newsletter charge's platform (application) fee is a real expense of IAE 751.
        conn.execute("UPDATE transactions SET fee_stripe = 0.4, fee_application = 12.0 WHERE id = 'n1'")
        conn.commit()
        pl = compute_pl_by_activity(YEAR, conn, CFG, year)
        by = {a.iae: a for a in pl.activities}
        assert by["751"].other_expenses == 12.0
        assert pl.total_expenses == compute_modelo_130(YEAR, 4, conn, CFG).gastos_reales
        assert pl.ties_to_130


# ---------------------------------------------------------------------------
# Pack assembly, exports, CLI
# ---------------------------------------------------------------------------

def test_pack_exports_and_cli(conn, db, tmp_path, capsys, monkeypatch):
    _tx(conn, "s1", "2025-02-10", 1000.0)
    _inv(conn, "big", "in", "2025-03-01", 3000.0, "DOMESTIC", iva=630.0, rate=21, nif="B12345678", name="Installer")
    monkeypatch.setattr("src.annual_pack.load_app_config", lambda: CFG)
    pack = build_annual_pack(YEAR, conn, CFG)
    md = to_markdown(pack)
    assert "# Annual pack 2025" in md and "| 05 |" in md and "B12345678" in md and "ties ✅" in md
    assert csv_390(pack).splitlines()[0] == "box,description,value"
    assert "A,purchases,B12345678,Installer,3630.0" in csv_347(pack)
    assert csv_pl(pack).splitlines()[1].startswith("826,COACHING,1000.0")

    main(["--year", "2025", "--db", str(db), "--out", str(tmp_path / "out")])
    written = capsys.readouterr().out.split()
    assert {p.rsplit("\\", 1)[-1].rsplit("/", 1)[-1] for p in written} == {
        "annual_pack_2025.md", "modelo_390_2025.csv", "modelo_347_2025.csv",
        "pl_by_activity_2025.csv", "pl_by_activity_lines_2025.csv"}
