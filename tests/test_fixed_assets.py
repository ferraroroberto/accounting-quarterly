"""Fixed assets (#96): simplified depreciation table, threshold, day proration,
cap, posting modes, business-use %, VAT capital goods (303 boxes 30/31 and the
5-year regularisation register) and the Modelo 130 hook. Synthetic data only."""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import src.database as database  # noqa: E402
from src.database import get_invoice_by_filename, init_db, update_invoice_fields, upsert_invoice  # noqa: E402
from src.fixed_assets import (  # noqa: E402
    ASSET_CLASSES,
    FixedAsset,
    add_fixed_asset,
    asset_from_invoice,
    asset_settings,
    assets_for_invoice,
    compute_capital_goods_vat,
    compute_depreciation,
    delete_fixed_asset,
    depreciation_for_period,
    depreciation_schedule,
    ensure_fixed_assets_schema,
    get_fixed_asset,
    is_expensed,
    is_vat_capital_good,
    load_vat_usage,
    register_asset_from_invoice,
    set_vat_usage,
    update_fixed_asset,
    vat_regularisation_register,
)
from src.tax_engine import compute_modelo_130  # noqa: E402

QUARTERLY = {"assets": {"posting_mode": "quarterly"}}


def _asset(**kw) -> FixedAsset:
    base = {"description": "Test asset", "acquisition_date": "2025-01-01", "base_eur": 1000.0,
            "asset_class": "installations"}
    base.update(kw)
    return FixedAsset(**base)


def _charge(asset: FixedAsset, year: int, quarter: int, *, ytd: bool = True,
            mode: str = "quarterly", threshold: float = 300.0) -> float:
    return compute_depreciation([asset], year, quarter, ytd=ytd, posting_mode=mode,
                                threshold_eur=threshold).total_eur


# ---------------------------------------------------------------------------
# Table, settings, thresholds
# ---------------------------------------------------------------------------

class TestRules:
    @pytest.mark.parametrize("key, coef, years", [
        ("installations", 10.0, 20), ("machinery", 12.0, 18), ("it_equipment", 26.0, 10),
        ("tools", 30.0, 8), ("other", 10.0, 20), ("vehicles", 16.0, 14), ("buildings", 3.0, 68),
    ])
    def test_simplified_table(self, key, coef, years):
        assert ASSET_CLASSES[key].max_coefficient_pct == coef
        assert ASSET_CLASSES[key].max_years == years
        assert _asset(asset_class=key).coefficient_pct == coef

    def test_unknown_class_falls_back_to_other(self):
        a = _asset(asset_class="Spaceship")
        assert a.asset_class == "other" and a.coefficient_pct == 10.0
        assert _asset(asset_class="IT equipment").asset_class == "it_equipment"

    def test_settings_defaults_and_bad_mode(self):
        assert asset_settings(None).threshold_eur == 300.0
        assert asset_settings(None).posting_mode == "annual_q4"
        assert asset_settings({"assets": {"posting_mode": "weekly"}}).posting_mode == "annual_q4"
        assert asset_settings({"assets": {"threshold_eur": 1500}}).threshold_eur == 1500.0

    def test_threshold_boundary(self):
        assert is_expensed(300.00) is True
        assert is_expensed(300.01) is False

    def test_expensed_item_charged_in_full_in_acquisition_quarter(self):
        cheap = _asset(base_eur=300.00, acquisition_date="2025-05-10", asset_class="it_equipment")
        for mode in ("annual_q4", "quarterly"):
            assert _charge(cheap, 2025, 1, mode=mode) == 0.0
            assert _charge(cheap, 2025, 2, ytd=False, mode=mode) == 300.0
            assert _charge(cheap, 2025, 3, ytd=False, mode=mode) == 0.0
            assert _charge(cheap, 2025, 4, mode=mode) == 300.0          # YTD
        line = compute_depreciation([cheap], 2025, 2, posting_mode="annual_q4").lines[0]
        assert line.expensed and line.fully_depreciated

    def test_just_above_threshold_is_depreciated(self):
        asset = _asset(base_eur=300.01, acquisition_date="2025-05-10", asset_class="it_equipment")
        assert _charge(asset, 2025, 2, ytd=False, mode="annual_q4") == 0.0
        assert 0 < _charge(asset, 2025, 4, mode="annual_q4") < 300.01

    def test_threshold_is_configurable(self):
        asset = _asset(base_eur=1200.0, acquisition_date="2025-05-10", asset_class="it_equipment")
        assert _charge(asset, 2025, 2, ytd=False, mode="annual_q4", threshold=1500.0) == 1200.0


# ---------------------------------------------------------------------------
# Depreciation arithmetic
# ---------------------------------------------------------------------------

class TestDepreciation:
    def test_partial_year_day_proration(self):
        a = _asset(start_of_use="2025-07-01")                 # 1000 × 10% = 100/yr
        # 1 Jul – 31 Dec = 184 days → 100 × 184/365
        assert _charge(a, 2025, 4, mode="annual_q4") == pytest.approx(50.41)
        # Q3 alone = 92 days
        assert _charge(a, 2025, 3, ytd=False) == pytest.approx(25.21)
        assert _charge(a, 2025, 2) == 0.0

    def test_start_of_use_defaults_to_acquisition(self):
        a = _asset(acquisition_date="2025-10-01")             # 92 days in 2025
        assert _charge(a, 2025, 4) == pytest.approx(round(100 * 92 / 365, 2))

    def test_full_leap_year_is_exactly_the_coefficient(self):
        a = _asset(acquisition_date="2027-01-01")
        assert _charge(a, 2028, 4, mode="annual_q4") == 100.0

    def test_annual_q4_vs_quarterly_posting(self):
        a = _asset(base_eur=2000.0, asset_class="it_equipment")   # 520/yr from 1 Jan 2025
        assert [_charge(a, 2025, q, mode="annual_q4") for q in (1, 2, 3, 4)] == [0.0, 0.0, 0.0, 520.0]
        assert _charge(a, 2025, 4, ytd=False, mode="annual_q4") == 520.0
        # quarterly YTD: 90, 181, 273 days of 365
        assert [_charge(a, 2025, q) for q in (1, 2, 3, 4)] == [
            pytest.approx(128.22), pytest.approx(257.86), pytest.approx(388.93), 520.0]

    def test_business_use_share(self):
        a = _asset(base_eur=4000.0, business_use_pct=20.0, acquisition_date="2026-05-15")
        # 4000 × 20% × 10% = 80/yr; 15 May – 31 Dec = 231 days
        assert _charge(a, 2026, 4, mode="annual_q4") == pytest.approx(round(80 * 231 / 365, 2))
        assert _charge(a, 2027, 4, mode="annual_q4") == 80.0

    def test_cap_at_fully_depreciated(self):
        a = _asset(base_eur=1000.0, asset_class="it_equipment", acquisition_date="2020-01-01")
        sched = depreciation_schedule(a)
        assert [r.year for r in sched] == [2020, 2021, 2022, 2023]
        assert [r.charge_eur for r in sched] == [260.0, 260.0, 260.0, 220.0]
        assert sched[-1].accumulated_eur == 1000.0 and sched[-1].net_book_value_eur == 0.0
        line = compute_depreciation([a], 2024, 4, posting_mode="annual_q4").lines[0]
        assert line.charge_eur == 0.0 and line.fully_depreciated

    def test_cap_uses_business_share(self):
        a = _asset(base_eur=1000.0, business_use_pct=50.0, asset_class="tools",
                   acquisition_date="2020-01-01")                    # 150/yr, cap 500
        assert sum(r.charge_eur for r in depreciation_schedule(a)) == 500.0

    def test_disposal_stops_depreciation(self):
        a = _asset(acquisition_date="2025-01-01", disposal_date="2025-07-01")   # in use Jan–Jun = 181 days
        assert _charge(a, 2025, 4, mode="annual_q4") == pytest.approx(round(100 * 181 / 365, 2))
        assert _charge(a, 2026, 4, mode="annual_q4") == 0.0
        assert depreciation_schedule(a)[-1].year == 2025

    def test_breakdown_lines_for_audit(self):
        a = _asset(id=7, invoice_id="inv-1")
        res = compute_depreciation([a], 2025, 4, posting_mode="annual_q4")
        rec = res.records()[0]
        assert rec["asset_id"] == 7 and rec["invoice_id"] == "inv-1"
        assert rec["days"] == 365 and rec["charge_eur"] == 100.0
        json.dumps(res.records())   # serialisable for AuditEntry.inputs_json

    def test_unknown_posting_mode_rejected(self):
        with pytest.raises(ValueError):
            compute_depreciation([], 2025, 1, posting_mode="weekly")


# ---------------------------------------------------------------------------
# VAT capital goods
# ---------------------------------------------------------------------------

class TestCapitalGoodsVat:
    def test_capital_good_boundary(self):
        assert is_vat_capital_good(3005.06) is False
        assert is_vat_capital_good(3005.07) is True
        assert _asset(base_eur=3005.07).vat_capital_good is True
        assert _asset(base_eur=3005.07, vat_capital_good=False).vat_capital_good is False

    def test_boxes_30_31_at_business_share(self):
        a = _asset(base_eur=4000.0, vat_eur=840.0, business_use_pct=20.0, acquisition_date="2026-05-15")
        q2 = compute_capital_goods_vat([a], 2026, 2)
        assert q2.box_30_base == 800.0 and q2.box_31_cuota == 168.0
        assert len(q2.lines) == 1
        assert compute_capital_goods_vat([a], 2026, 3).box_30_base == 0.0

    def test_non_capital_good_not_in_30_31(self):
        a = _asset(base_eur=2000.0, vat_eur=420.0, acquisition_date="2026-05-15")
        assert compute_capital_goods_vat([a], 2026, 2).lines == []

    def test_vat_deducted_override(self):
        a = _asset(base_eur=4000.0, vat_eur=840.0, vat_business_pct=20.0, vat_deducted_eur=150.0,
                   acquisition_date="2026-05-15")
        assert compute_capital_goods_vat([a], 2026, 2).box_31_cuota == 150.0

    def test_regularisation_register(self):
        a = _asset(base_eur=4000.0, vat_eur=840.0, business_use_pct=20.0, acquisition_date="2026-05-15")
        reg = vat_regularisation_register(a, {2027: 25.0, 2028: 50.0, 2030: 5.0})
        assert [r.year for r in reg] == [2026, 2027, 2028, 2029, 2030]
        by_year = {r.year: r for r in reg}
        assert by_year[2027].applies is False and by_year[2027].adjustment_eur == 0.0   # 5 points
        assert by_year[2028].applies and by_year[2028].adjustment_eur == pytest.approx(50.40)  # 840/5 × 30%
        assert by_year[2029].recorded is False and by_year[2029].pct_used == 20.0
        assert by_year[2030].adjustment_eur == pytest.approx(-25.20)                    # 840/5 × −15%

    def test_register_stops_at_disposal_and_skips_non_capital(self):
        a = _asset(base_eur=4000.0, vat_eur=840.0, acquisition_date="2026-05-15", disposal_date="2027-06-01")
        assert [r.year for r in vat_regularisation_register(a)] == [2026, 2027]
        assert vat_regularisation_register(_asset(base_eur=1000.0)) == []


# ---------------------------------------------------------------------------
# Storage + Modelo 130 hook
# ---------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    path = tmp_path / "assets.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    yield c
    c.close()


def _invoice(db, filename: str, subtotal: float, when: str = "2025-01-15", **kw) -> str:
    rec = {"filename": filename, "direction": "in", "invoice_date": when,
           "vendor_name": "Example Vendor SL", "vendor_nif": "B00000000",
           "description": "Laptop", "subtotal_eur": subtotal, "iva_rate": 21.0,
           "iva_amount": round(subtotal * 0.21, 2), "total_eur": round(subtotal * 1.21, 2)}
    rec.update(kw)
    return upsert_invoice(rec, db_path=db)


class TestStorage:
    def test_schema_is_idempotent(self, conn):
        ensure_fixed_assets_schema(conn)
        ensure_fixed_assets_schema(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(fixed_assets)")}
        assert {"invoice_id", "start_of_use", "coefficient_pct", "vat_capital_good",
                "vat_deducted_eur", "disposal_date"} <= cols

    def test_register_from_invoice_prefills_and_flags(self, db, conn):
        rid = _invoice(db, "v/laptop.pdf", 2000.0)
        update_invoice_fields(rid, {"deductible_pct_irpf": 80, "deductible_pct_vat": 60}, db_path=db)
        prefill = asset_from_invoice(conn, rid)
        assert prefill.base_eur == 2000.0 and prefill.vat_eur == 420.0
        assert prefill.business_use_pct == 80.0 and prefill.vat_business_pct == 60.0
        assert prefill.acquisition_date == "2025-01-15"
        prefill.asset_class = "it_equipment"
        prefill.coefficient_pct = None
        prefill.__post_init__()
        asset_id = register_asset_from_invoice(conn, prefill)
        stored = get_fixed_asset(conn, asset_id)
        assert stored.coefficient_pct == 26.0 and stored.invoice_id == rid
        inv = get_invoice_by_filename("v/laptop.pdf", "in", db_path=db)
        assert inv["is_capital_asset"] == 1 and inv["asset_class"] == "it_equipment"

    def test_income_invoice_cannot_be_an_asset(self, db, conn):
        rid = _invoice(db, "o/sale.pdf", 2000.0, direction="out")
        with pytest.raises(ValueError):
            asset_from_invoice(conn, rid)

    @pytest.mark.parametrize("kw", [
        {"description": ""}, {"base_eur": 0.0}, {"business_use_pct": 120.0},
        {"asset_class": "it_equipment", "coefficient_pct": 30.0},
        {"disposal_date": "2024-01-01"}, {"acquisition_date": "15/01/2025"},
    ])
    def test_invalid_assets_rejected(self, conn, kw):
        with pytest.raises(ValueError):
            add_fixed_asset(conn, _asset(**kw))

    def test_update_class_resets_coefficient(self, conn):
        asset_id = add_fixed_asset(conn, _asset(asset_class="installations", coefficient_pct=8.0))
        assert update_fixed_asset(conn, asset_id, {"asset_class": "tools"}) == ["asset_class", "coefficient_pct"]
        assert get_fixed_asset(conn, asset_id).coefficient_pct == 30.0
        with pytest.raises(ValueError):
            update_fixed_asset(conn, asset_id, {"coefficient_pct": 31.0})
        with pytest.raises(ValueError):
            update_fixed_asset(conn, asset_id, {"invoice_id": "x"})

    def test_delete_unflags_invoice_and_usage(self, db, conn):
        rid = _invoice(db, "v/ac.pdf", 4000.0)
        asset_id = register_asset_from_invoice(conn, asset_from_invoice(conn, rid))
        set_vat_usage(conn, asset_id, 2026, 50.0)
        assert load_vat_usage(conn, asset_id) == {2026: 50.0}
        delete_fixed_asset(conn, asset_id)
        assert get_fixed_asset(conn, asset_id) is None
        assert conn.execute("SELECT COUNT(*) FROM fixed_asset_vat_usage").fetchone()[0] == 0
        assert get_invoice_by_filename("v/ac.pdf", "in", db_path=db)["is_capital_asset"] == 0


class TestModelo130Hook:
    def _setup(self, db, conn) -> str:
        _invoice(db, "v/paper.pdf", 100.0, description="Stationery")
        rid = _invoice(db, "v/laptop.pdf", 2000.0)
        asset = asset_from_invoice(conn, rid)
        asset.asset_class, asset.coefficient_pct = "it_equipment", 26.0     # 520/yr
        register_asset_from_invoice(conn, asset)
        return rid

    def test_before_registration_invoice_is_expensed(self, db, conn):
        _invoice(db, "v/paper.pdf", 100.0)
        _invoice(db, "v/laptop.pdf", 2000.0)
        assert compute_modelo_130(2025, 1, conn).c02_gastos == 2100.0

    def test_capital_invoice_excluded_and_depreciation_added_annual_q4(self, db, conn):
        self._setup(db, conn)
        assert compute_modelo_130(2025, 1, conn).c02_gastos == 100.0
        q4 = compute_modelo_130(2025, 4, conn)
        # 15 Jan – 31 Dec = 351 days → 520 × 351/365
        assert q4.c02_gastos == pytest.approx(100.0 + round(520 * 351 / 365, 2))
        audit = {a.cell: a for a in q4.audit}
        assert audit["c02_amortizaciones"].value == pytest.approx(round(520 * 351 / 365, 2))
        assert len(json.loads(audit["c02_amortizaciones"].inputs_json)["records"]) == 1
        assert audit["c02_capital_assets_excluded"].value == 2000.0
        assert json.loads(audit["c02_gastos_reales"].inputs_json)["amortizaciones"] == audit["c02_amortizaciones"].value

    def test_quarterly_posting_in_130(self, db, conn):
        self._setup(db, conn)
        # 15 Jan – 31 Mar = 76 days
        assert compute_modelo_130(2025, 1, conn, config=QUARTERLY).c02_gastos == pytest.approx(
            100.0 + round(520 * 76 / 365, 2))

    def test_flagged_invoice_without_asset_is_reported(self, db, conn):
        rid = _invoice(db, "v/laptop.pdf", 2000.0)
        update_invoice_fields(rid, {"is_capital_asset": True}, db_path=db)
        r = compute_modelo_130(2025, 1, conn)
        assert r.c02_gastos == 0.0
        audit = {a.cell: a for a in r.audit}
        assert json.loads(audit["c02_capital_assets_excluded"].inputs_json)["unregistered_invoice_ids"] == [rid]

    def test_depreciation_for_period_reads_config(self, db, conn):
        self._setup(db, conn)
        assert depreciation_for_period(2025, 1, conn).total_eur == 0.0
        assert depreciation_for_period(2025, 1, conn, config=QUARTERLY).total_eur == pytest.approx(
            round(520 * 76 / 365, 2))


# ---------------------------------------------------------------------------
# UI (AppTest)
# ---------------------------------------------------------------------------

def _render_fixed_assets() -> None:
    from app import fixed_assets_tab

    fixed_assets_tab.render()


def _render_ledger() -> None:
    from app import invoice_ledger

    invoice_ledger.render()


@pytest.fixture
def ui_db(db, monkeypatch):
    monkeypatch.setattr(database, "_DB_PATH", db)
    return db


def test_tab_renders_empty(ui_db):
    at = AppTest.from_function(_render_fixed_assets).run()
    assert not at.exception, f"Fixed Assets tab raised: {at.exception}"
    assert any("No fixed assets" in i.value for i in at.info)


def test_tab_renders_assets_schedule_and_register(ui_db):
    c = database.get_connection(ui_db)
    try:
        add_fixed_asset(c, _asset(base_eur=4000.0, vat_eur=840.0, business_use_pct=20.0,
                                  acquisition_date=f"{date.today().year}-01-10"))
    finally:
        c.close()
    at = AppTest.from_function(_render_fixed_assets).run()
    assert not at.exception, f"Fixed Assets tab raised: {at.exception}"
    assert at.metric[0].value == "1"
    assert at.selectbox(key="fa_vat_asset") is not None


def test_ledger_registers_invoice_as_asset(ui_db):
    rid = _invoice(ui_db, "v/laptop.pdf", 2000.0)
    at = AppTest.from_function(_render_ledger).run()
    assert not at.exception, f"Invoice Ledger tab raised: {at.exception}"
    at.selectbox(key=f"fa_inv_{rid}_class").set_value("it_equipment")
    at.button(key=f"FormSubmitter:fa_inv_{rid}_form-Register as fixed asset").click()
    at.run()
    assert not at.exception, f"Invoice Ledger tab raised: {at.exception}"
    inv = get_invoice_by_filename("v/laptop.pdf", "in", db_path=ui_db)
    assert inv["is_capital_asset"] == 1
    c = database.get_connection(ui_db)
    try:
        (asset,) = assets_for_invoice(c, rid)
    finally:
        c.close()
    assert asset.asset_class == "it_equipment" and asset.coefficient_pct == 26.0
    assert asset.base_eur == 2000.0
