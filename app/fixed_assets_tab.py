"""Fixed Assets tab — register, edit and review depreciable assets (#96).

Lists every asset (editable grid), adds one by hand, shows the per-year
depreciation schedule and the period charge the Modelo 130 uses, and keeps the
VAT capital-goods regularisation register. Assets are also created from an
expense invoice in the Invoice Ledger tab (``render_register_from_invoice``).
All logic lives in ``src/fixed_assets.py``.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Optional

import pandas as pd
import streamlit as st

from src.database import get_connection
from src.fixed_assets import (
    ASSET_CLASSES,
    DEFAULT_ASSET_CLASS,
    VAT_CAPITAL_GOOD_THRESHOLD_EUR,
    FixedAsset,
    add_fixed_asset,
    asset_from_invoice,
    asset_settings,
    assets_for_invoice,
    class_max_coefficient,
    compute_capital_goods_vat,
    compute_depreciation,
    delete_fixed_asset,
    depreciation_schedule,
    is_expensed,
    load_fixed_assets,
    load_vat_usage,
    register_asset_from_invoice,
    set_vat_usage,
    update_fixed_asset,
    vat_regularisation_register,
)
from src.logger import get_logger
from src.tax_engine import load_app_config

log = get_logger(__name__)

_CLASS_KEYS = list(ASSET_CLASSES)
_ISO_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
_GRID_COLUMNS = [
    "id", "description", "invoice_id", "acquisition_date", "start_of_use", "base_eur",
    "business_use_pct", "asset_class", "coefficient_pct", "vat_eur", "vat_business_pct",
    "vat_capital_good", "vat_deducted_eur", "disposal_date", "notes",
]
_GRID_READONLY = ["id", "invoice_id"]


def _class_label(key: str) -> str:
    c = ASSET_CLASSES[key]
    return f"{key} — {c.label} ({c.max_coefficient_pct:g}%, {c.max_years} y)"


def _to_date(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _flash(kind: str, message: str) -> None:
    st.session_state.setdefault("fa_flash", []).append((kind, message))


def _show_flash() -> None:
    for kind, message in st.session_state.pop("fa_flash", []):
        getattr(st, kind)(message)


def _grid_version() -> int:
    return st.session_state.setdefault("fa_grid_version", 0)


def _bump_grid_version() -> None:
    st.session_state["fa_grid_version"] = _grid_version() + 1


def _asset_form_fields(prefill: FixedAsset, k: str) -> FixedAsset:
    """Widgets for one asset (inside an ``st.form``); returns the asset as entered."""
    c1, c2, c3 = st.columns(3)
    with c1:
        description = st.text_input("Description", value=prefill.description, key=f"{k}_description")
        acquisition = st.date_input("Acquisition date", value=_to_date(prefill.acquisition_date),
                                    min_value=date(2000, 1, 1), format="YYYY-MM-DD", key=f"{k}_acquisition")
        start_of_use = st.date_input("Start of use (blank = acquisition date)",
                                     value=_to_date(prefill.start_of_use),
                                     min_value=date(2000, 1, 1), format="YYYY-MM-DD", key=f"{k}_start_of_use")
        notes = st.text_input("Notes", value=prefill.notes or "", key=f"{k}_notes")
    with c2:
        base = st.number_input("Unit base (EUR, ex-VAT)", min_value=0.0, value=float(prefill.base_eur),
                               format="%.2f", key=f"{k}_base")
        vat = st.number_input("VAT paid (EUR)", min_value=0.0, value=float(prefill.vat_eur),
                              format="%.2f", key=f"{k}_vat")
        business = st.number_input("Business use — depreciation %", min_value=0.0, max_value=100.0,
                                   value=float(prefill.business_use_pct), key=f"{k}_business")
        vat_business = st.number_input("Business use — VAT %", min_value=0.0, max_value=100.0,
                                       value=float(prefill.vat_business_pct), key=f"{k}_vat_business")
    with c3:
        asset_class = st.selectbox("Asset class", _CLASS_KEYS, index=_CLASS_KEYS.index(prefill.asset_class),
                                   format_func=_class_label, key=f"{k}_class")
        coefficient = st.number_input("Coefficient % (0 = class maximum)", min_value=0.0, max_value=100.0,
                                      value=0.0, key=f"{k}_coefficient")
        capital_good = st.selectbox(
            f"VAT capital good (unit base > {VAT_CAPITAL_GOOD_THRESHOLD_EUR:,.2f})",
            ["auto", "yes", "no"], key=f"{k}_capital_good",
        )
    return FixedAsset(
        invoice_id=prefill.invoice_id,
        description=description,
        acquisition_date=acquisition.isoformat() if acquisition else "",
        start_of_use=start_of_use.isoformat() if start_of_use else None,
        base_eur=base, vat_eur=vat, business_use_pct=business, vat_business_pct=vat_business,
        asset_class=asset_class,
        coefficient_pct=coefficient or class_max_coefficient(asset_class),
        vat_capital_good=None if capital_good == "auto" else capital_good == "yes",
        notes=notes or None,
    )


def render_register_from_invoice(invoice: dict) -> None:
    """Invoice Ledger hook: register an expense invoice as a fixed asset (prefilled form)."""
    if invoice.get("direction") != "in":
        return
    invoice_id = invoice["id"]
    conn = get_connection()
    try:
        existing = assets_for_invoice(conn, invoice_id)
        prefill = asset_from_invoice(conn, invoice_id)
    finally:
        conn.close()
    label = "Register as fixed asset" + (f" ({len(existing)} registered)" if existing else "")
    with st.expander(label, expanded=False):
        for a in existing:
            st.caption(f"Fixed asset #{a.id}: {a.description} · {a.asset_class} · base {a.base_eur:,.2f}")
        settings = asset_settings(load_app_config())
        st.caption(
            "Registering flags the invoice as a capital asset: the Modelo 130 stops expensing it and "
            f"depreciates it instead. Unit bases ≤ {settings.threshold_eur:,.2f} are expensed in the "
            "acquisition quarter. Several units on one invoice → register one asset per unit."
        )
        k = f"fa_inv_{invoice_id}"
        with st.form(key=f"{k}_form"):
            entered = _asset_form_fields(prefill, k)
            submitted = st.form_submit_button("Register as fixed asset", type="primary")
        if submitted:
            conn = get_connection()
            try:
                asset_id = register_asset_from_invoice(conn, entered)
            except (ValueError, KeyError) as exc:
                log.warning("⚠️ Fixed asset registration rejected for invoice %s — %s", invoice_id, exc)
                st.error(str(exc))
            else:
                st.session_state.setdefault("ledger_flash", []).append(
                    ("success", f"Registered fixed asset #{asset_id}; the invoice is now a capital asset.")
                )
                st.rerun()
            finally:
                conn.close()


def _render_grid(assets: list[FixedAsset]) -> None:
    st.markdown("**Assets**")
    st.caption("Edit cells, then **Save changes**. Changing the class resets the coefficient to the class "
               "maximum unless you also edit the coefficient.")
    df = pd.DataFrame([asdict(a) for a in assets])[_GRID_COLUMNS]
    editor_key = f"fa_grid_{_grid_version()}"
    st.data_editor(
        df, key=editor_key, width="stretch", hide_index=True, num_rows="fixed", disabled=_GRID_READONLY,
        column_config={
            "acquisition_date": st.column_config.TextColumn("acquisition_date", validate=_ISO_DATE_RE),
            "start_of_use": st.column_config.TextColumn("start_of_use", validate=_ISO_DATE_RE),
            "disposal_date": st.column_config.TextColumn("disposal_date", validate=_ISO_DATE_RE),
            "asset_class": st.column_config.SelectboxColumn("asset_class", options=_CLASS_KEYS),
            "base_eur": st.column_config.NumberColumn("base", format="%.2f", min_value=0),
            "vat_eur": st.column_config.NumberColumn("VAT", format="%.2f", min_value=0),
            "vat_deducted_eur": st.column_config.NumberColumn(
                "VAT deducted (override)", format="%.2f",
                help="Blank = VAT × VAT business %"),
            "business_use_pct": st.column_config.NumberColumn("business %", min_value=0, max_value=100),
            "vat_business_pct": st.column_config.NumberColumn("VAT business %", min_value=0, max_value=100),
            "coefficient_pct": st.column_config.NumberColumn("coefficient %", min_value=0, max_value=100),
            "vat_capital_good": st.column_config.CheckboxColumn("VAT capital good"),
        },
    )
    edited_rows: dict = st.session_state.get(editor_key, {}).get("edited_rows", {})
    col_save, col_info = st.columns([1, 4])
    with col_info:
        st.caption(f"{len(edited_rows)} row(s) with pending edits")
    with col_save:
        if st.button("Save changes", type="primary", key="fa_grid_save", disabled=not edited_rows):
            conn = get_connection()
            try:
                for pos, changes in edited_rows.items():
                    asset_id = int(df.iloc[int(pos)]["id"])
                    try:
                        changed = update_fixed_asset(conn, asset_id, changes)
                        if changed:
                            _flash("success", f"Asset #{asset_id}: saved {', '.join(changed)}.")
                    except (ValueError, KeyError) as exc:
                        log.warning("⚠️ Fixed asset %d edit rejected — %s", asset_id, exc)
                        _flash("error", f"Asset #{asset_id}: {exc}")
            finally:
                conn.close()
            _bump_grid_version()
            st.rerun()


def _render_add_form() -> None:
    with st.expander("Add an asset by hand", expanded=False):
        blank = FixedAsset(description="", acquisition_date=date.today().isoformat(), base_eur=0.0,
                           asset_class=DEFAULT_ASSET_CLASS)
        with st.form(key="fa_add_form"):
            entered = _asset_form_fields(blank, "fa_add")
            submitted = st.form_submit_button("Add asset", type="primary")
        if submitted:
            conn = get_connection()
            try:
                asset_id = add_fixed_asset(conn, entered)
            except ValueError as exc:
                st.error(str(exc))
            else:
                _flash("success", f"Added fixed asset #{asset_id}.")
                _bump_grid_version()
                st.rerun()
            finally:
                conn.close()


def _render_period(assets: list[FixedAsset], settings) -> None:
    st.markdown("**Depreciation by quarter (as the Modelo 130 sees it)**")
    this_year = date.today().year
    years = sorted({int(a.acquisition_date[:4]) for a in assets} | {this_year})
    c_year, c_info = st.columns([1, 3])
    with c_year:
        year = st.selectbox("Year", years, index=years.index(this_year), key="fa_period_year")
    with c_info:
        st.caption(f"Posting mode `{settings.posting_mode}` · threshold {settings.threshold_eur:,.2f} EUR "
                   "(config `assets.posting_mode`, `assets.threshold_eur`).")
    rows = []
    for q in (1, 2, 3, 4):
        quarter_only = compute_depreciation(assets, year, q, ytd=False, posting_mode=settings.posting_mode,
                                            threshold_eur=settings.threshold_eur)
        ytd = compute_depreciation(assets, year, q, ytd=True, posting_mode=settings.posting_mode,
                                   threshold_eur=settings.threshold_eur)
        rows.append({"quarter": f"Q{q}", "quarter charge": quarter_only.total_eur, "YTD (130 box 02)": ytd.total_eur})
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    breakdown = compute_depreciation(assets, year, 4, ytd=True, posting_mode=settings.posting_mode,
                                     threshold_eur=settings.threshold_eur)
    st.dataframe(pd.DataFrame([asdict(line) for line in breakdown.lines]), width="stretch", hide_index=True)


def _asset_label(a: FixedAsset) -> str:
    return f"#{a.id} · {a.acquisition_date} · {a.description} · {a.base_eur:,.2f}"


def _render_schedule(assets: list[FixedAsset], settings) -> None:
    st.markdown("**Schedule per year**")
    by_id = {a.id: a for a in assets}
    asset_id = st.selectbox("Asset", list(by_id), format_func=lambda i: _asset_label(by_id[i]),
                            key="fa_schedule_asset")
    a = by_id[asset_id]
    if is_expensed(a.base_eur, settings.threshold_eur):
        st.info(f"Unit base ≤ {settings.threshold_eur:,.2f}: expensed in full in the acquisition quarter.")
    st.dataframe(pd.DataFrame([asdict(r) for r in depreciation_schedule(a, settings.threshold_eur)]),
                 width="stretch", hide_index=True)
    if st.button("Delete this asset", key="fa_delete"):
        conn = get_connection()
        try:
            delete_fixed_asset(conn, asset_id)
        finally:
            conn.close()
        _flash("info", f"Deleted fixed asset #{asset_id}; its invoice is unflagged if no other asset uses it.")
        _bump_grid_version()
        st.rerun()


def _render_vat_register(assets: list[FixedAsset]) -> None:
    st.markdown("**VAT capital goods — Modelo 303 boxes 30/31 and 5-year regularisation**")
    goods = [a for a in assets if a.vat_capital_good]
    if not goods:
        st.caption(f"No capital goods (unit base > {VAT_CAPITAL_GOOD_THRESHOLD_EUR:,.2f}).")
        return
    boxes = []
    for a in goods:
        acq = date.fromisoformat(a.acquisition_date[:10])
        q = (acq.month - 1) // 3 + 1
        cg = compute_capital_goods_vat([a], acq.year, q)
        boxes.append({"asset": _asset_label(a), "period": f"{acq.year}-Q{q}",
                      "box 30 base": cg.box_30_base, "box 31 cuota": cg.box_31_cuota})
    st.dataframe(pd.DataFrame(boxes), width="stretch", hide_index=True)

    by_id = {a.id: a for a in goods}
    asset_id = st.selectbox("Capital good", list(by_id), format_func=lambda i: _asset_label(by_id[i]),
                            key="fa_vat_asset")
    a = by_id[asset_id]
    conn = get_connection()
    try:
        usage = load_vat_usage(conn, asset_id)
    finally:
        conn.close()
    register = vat_regularisation_register(a, usage)
    st.dataframe(pd.DataFrame([asdict(r) for r in register]), width="stretch", hide_index=True)
    st.caption("Adjustment = VAT borne / 5 × (% of the year − % of the acquisition year), only when the "
               "change exceeds 10 points (arts. 107-109 LIVA). It belongs in 303 box 44 of Q4.")
    years = [r.year for r in register][1:]
    if not years:
        return
    with st.form(key="fa_vat_usage_form"):
        c_y, c_p = st.columns(2)
        with c_y:
            year = st.selectbox("Year", years, key="fa_vat_usage_year")
        with c_p:
            pct = st.number_input("VAT business use % that year", min_value=0.0, max_value=100.0,
                                  value=float(a.vat_business_pct), key="fa_vat_usage_pct")
        submitted = st.form_submit_button("Record usage")
    if submitted:
        conn = get_connection()
        try:
            set_vat_usage(conn, asset_id, year, pct)
        finally:
            conn.close()
        _flash("success", f"Recorded {pct:g}% VAT business use for {year}.")
        st.rerun()


def render() -> None:
    """Render the Fixed Assets tab."""
    st.subheader("Fixed Assets — depreciation and VAT capital goods")
    st.caption(
        "Simplified depreciation table (Orden de 27 de marzo de 1998). Register assets from an expense invoice "
        "in the **Invoice Ledger** tab, or add one by hand below."
    )
    _show_flash()
    settings = asset_settings(load_app_config())
    conn = get_connection()
    try:
        assets = load_fixed_assets(conn)
    finally:
        conn.close()

    _render_add_form()
    if not assets:
        st.info("No fixed assets registered yet.")
        return

    m1, m2, m3 = st.columns(3)
    m1.metric("Assets", len(assets))
    m2.metric("Total base", f"{sum(a.base_eur for a in assets):,.2f}")
    m3.metric("VAT capital goods", sum(1 for a in assets if a.vat_capital_good))

    _render_grid(assets)
    st.markdown("---")
    _render_period(assets, settings)
    st.markdown("---")
    _render_schedule(assets, settings)
    st.markdown("---")
    _render_vat_register(assets)
