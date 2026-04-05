"""Tax Obligations tab — Spanish autónomo quarterly and annual filings."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path

import streamlit as st

from src.config import load_config, reload_config
from src.database import (
    add_tax_entry,
    delete_tax_entry,
    get_all_filing_statuses,
    get_tax_entries,
    upsert_filing_status,
    _get_connection,
)
from src.tax_engine import (
    compute_modelo_130,
    compute_modelo_303,
    compute_modelo_347,
    compute_modelo_349,
    compute_oss_return,
    get_tax_calendar,
)
from src.tax_models import TaxDeadline

_DISCLAIMER = (
    "> **This tool pre-fills tax data for review purposes only. It does not constitute tax advice. "
    "Always review outputs with a qualified gestor or asesor fiscal before filing. "
    "Regulatory changes (IVA rates, IRPF thresholds, OSS rules) are not automatically tracked — "
    "verify current rules with the Agencia Tributaria each filing period.**"
)

_STATUS_COLOURS = {
    "FILED": "🟢",
    "DUE": "🟡",
    "OVERDUE": "🔴",
    "PENDING": "⚪",
}


def _quarter_label(q: int) -> str:
    return f"Q{q} ({['Jan–Mar', 'Apr–Jun', 'Jul–Sep', 'Oct–Dec'][q - 1]})"


def _fmt_eur(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}€{abs(value):,.2f}"


def _get_conn() -> sqlite3.Connection:
    return _get_connection()


# ---------------------------------------------------------------------------
# Sub-section A: Tax Calendar
# ---------------------------------------------------------------------------

def _render_tax_calendar(year: int) -> None:
    st.subheader("A. Tax Calendar")
    conn = _get_conn()
    try:
        deadlines = get_tax_calendar(year, db_conn=conn)
    finally:
        conn.close()

    cols = st.columns([1, 3, 2, 1, 2, 2])
    cols[0].markdown("**Model**")
    cols[1].markdown("**Name**")
    cols[2].markdown("**Deadline**")
    cols[3].markdown("**Status**")
    cols[4].markdown("**Amount**")
    cols[5].markdown("**Action**")
    st.divider()

    for dl in deadlines:
        period = f"Q{dl.quarter}" if dl.quarter else "Annual"
        key_base = f"{dl.model}_{dl.quarter or 'annual'}_{year}"
        cols = st.columns([1, 3, 2, 1, 2, 2])
        cols[0].markdown(f"**{dl.model}** ({period})")
        cols[1].markdown(dl.name)
        cols[2].markdown(dl.deadline.strftime("%d %b %Y"))
        cols[3].markdown(f"{_STATUS_COLOURS.get(dl.status, '⚪')} {dl.status}")
        cols[4].markdown(_fmt_eur(dl.amount_eur))

        if dl.status != "FILED":
            if cols[5].button("Mark Filed", key=f"file_{key_base}"):
                upsert_filing_status(
                    year=year, model=dl.model, quarter=dl.quarter,
                    status="FILED", amount_eur=dl.amount_eur,
                    filed_at=datetime.now().isoformat(),
                )
                st.rerun()
        else:
            cols[5].markdown("✅ Filed")


# ---------------------------------------------------------------------------
# Sub-section B: Modelo 303
# ---------------------------------------------------------------------------

def _render_modelo_303(year: int, quarter: int) -> None:
    st.subheader("B. Modelo 303 — IVA Trimestral")
    conn = _get_conn()
    try:
        result = compute_modelo_303(year, quarter, conn)
    finally:
        conn.close()

    st.markdown(f"**Period:** {_quarter_label(quarter)} {year}")
    st.divider()

    st.markdown("##### DEVENGADO (IVA collected)")
    col1, col2 = st.columns(2)
    col1.metric("Box 01 — Base imponible al 21%", _fmt_eur(result.box_01_base))
    col2.metric("Box 03 — Cuota (21% × Box 01)", _fmt_eur(result.box_03_cuota))
    st.metric("Box 10 — Entregas intracom. exentas (IVA_EU_B2B base, informative)",
              _fmt_eur(result.box_10_intracom))

    if result.oss_base > 0:
        st.info(
            f"OSS income (not in Modelo 303): base {_fmt_eur(result.oss_base)}, "
            f"VAT {_fmt_eur(result.oss_vat)} — declare separately via OSS portal."
        )

    st.divider()
    st.markdown("##### DEDUCIBLE (IVA paid on expenses)")
    col1, col2 = st.columns(2)
    col1.metric("Box 28 — Cuota IVA soportado", _fmt_eur(result.box_28_iva_soportado))
    col2.metric("Box 29 — Base correspondiente", _fmt_eur(result.box_29_base_soportado))

    st.divider()
    st.markdown("##### RESULTADO")
    col1, col2 = st.columns(2)
    col1.metric("Box 46 — Diferencia (03 − 28)", _fmt_eur(result.box_46_diferencia))
    result_val = result.box_48_resultado
    delta_color = "inverse" if result_val < 0 else "normal"
    col2.metric(
        "Box 48 — Resultado a ingresar / devolver",
        _fmt_eur(result_val),
        delta="Refund" if result_val < 0 else "To pay",
        delta_color=delta_color,
    )

    st.divider()
    _save_filing_button("303", year, quarter, result.box_48_resultado)

    with st.expander("⚠️ Caveats"):
        st.markdown(
            "- `IVA_EU_B2B` transactions require a valid NIF-IVA verified in VIES — "
            "the system cannot verify this automatically.\n"
            "- OSS income is shown for reference only; file it separately through the AEAT OSS portal.\n"
            "- Manual override: enter corrected IVA soportado via **Manual Entries** below."
        )


# ---------------------------------------------------------------------------
# Sub-section C: Modelo 130
# ---------------------------------------------------------------------------

def _render_modelo_130(year: int, quarter: int, config: dict) -> None:
    st.subheader("C. Modelo 130 — IRPF Trimestral")
    conn = _get_conn()
    try:
        result = compute_modelo_130(year, quarter, conn)
    finally:
        conn.close()

    irpf_rate = config.get("tax", {}).get("irpf_retention_rate", 0.15)
    st.markdown(f"**Period:** {_quarter_label(quarter)} {year} — YTD cumulative | IRPF retention rate: {irpf_rate:.0%}")
    st.divider()

    st.markdown("##### INGRESOS Y GASTOS (year-to-date)")
    col1, col2, col3 = st.columns(3)
    col1.metric("Box 01 — Ingresos del periodo", _fmt_eur(result.box_01_ingresos))
    col2.metric("Box 02 — Gastos deducibles", _fmt_eur(result.box_02_gastos))
    col3.metric("Box 03 — Rendimiento neto (01 − 02)", _fmt_eur(result.box_03_rendimiento))

    st.divider()
    st.markdown("##### CÁLCULO")
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Box 05 — 20% de Box 03", _fmt_eur(result.box_05_base))
    col2.metric("Box 07 — Retenciones soportadas YTD", _fmt_eur(result.box_07_retenciones))
    col3.metric("Box 14 — Pagos fraccionados anteriores", _fmt_eur(result.box_14_pagos_anteriores))
    col4.metric("Box 16 — Resultado a ingresar", _fmt_eur(result.box_16_resultado),
                delta="min €0" if result.box_16_resultado == 0 else None)

    st.divider()
    _save_filing_button("130", year, quarter, result.box_16_resultado)

    with st.expander("ℹ️ Notes"):
        st.markdown(
            "- Box 02 comes from **Gastos Deducibles** entries in Manual Entries below.\n"
            "- Box 07 comes from **Retenciones Soportadas** entries in Manual Entries below.\n"
            "- Box 14 is auto-filled from previously saved Modelo 130 amounts.\n"
            "- Stripe does not capture IRPF retentions — enter them manually."
        )


# ---------------------------------------------------------------------------
# Sub-section D: Manual Entries
# ---------------------------------------------------------------------------

def _render_manual_entries(year: int, quarter: int) -> None:
    st.subheader("D. Manual Entries & Adjustments")
    st.markdown(f"**Period:** {_quarter_label(quarter)} {year}")

    entries = get_tax_entries(year, quarter)

    if entries:
        import pandas as pd
        df = pd.DataFrame(entries)[["id", "entry_type", "amount_eur", "description", "notes", "created_at"]]
        df.columns = ["ID", "Type", "Amount (€)", "Description", "Notes", "Created"]
        st.dataframe(df, width="stretch", hide_index=True)

        delete_id = st.number_input("Delete entry by ID", min_value=0, step=1, value=0,
                                    key=f"del_entry_{year}_{quarter}")
        if st.button("Delete Entry", key=f"del_btn_{year}_{quarter}"):
            if delete_id > 0:
                deleted = delete_tax_entry(int(delete_id))
                if deleted:
                    st.success(f"Entry {delete_id} deleted.")
                    st.rerun()
                else:
                    st.error("Entry not found.")
    else:
        st.info("No manual entries for this period.")

    st.divider()
    st.markdown("##### Add New Entry")
    with st.form(key=f"add_entry_{year}_{quarter}"):
        col1, col2 = st.columns(2)
        entry_type = col1.selectbox(
            "Type",
            ["IVA_SOPORTADO", "GASTOS_DEDUCIBLES", "RETENCIONES_SOPORTADAS", "OTHER"],
        )
        amount = col2.number_input("Amount (€)", min_value=0.0, step=0.01, format="%.2f")
        description = st.text_input("Description")
        notes = st.text_area("Notes", height=70)
        if st.form_submit_button("Add Entry"):
            if amount > 0:
                add_tax_entry(year, quarter, entry_type, amount, description, notes)
                st.success("Entry added.")
                st.rerun()
            else:
                st.warning("Amount must be greater than 0.")


# ---------------------------------------------------------------------------
# Sub-section E: OSS Return
# ---------------------------------------------------------------------------

def _render_oss_return(year: int, quarter: int) -> None:
    st.subheader("E. OSS Return (One Stop Shop)")
    conn = _get_conn()
    try:
        result = compute_oss_return(year, quarter, conn)
    finally:
        conn.close()

    if not result.rows:
        st.info("No OSS transactions in this period.")
        return

    st.markdown(f"**Period:** {_quarter_label(quarter)} {year}")

    import pandas as pd
    rows = [
        {
            "Country": r.country,
            "Transactions": r.transactions,
            "Base (€)": r.base_eur,
            "VAT Rate": f"{r.vat_rate:.0%}",
            "VAT Amount (€)": r.vat_amount_eur,
        }
        for r in result.rows
    ]
    rows.append({
        "Country": "**TOTAL**",
        "Transactions": result.total_transactions,
        "Base (€)": result.total_base,
        "VAT Rate": "",
        "VAT Amount (€)": result.total_vat,
    })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

    oss_deadline_map = {1: "April 30", 2: "July 31", 3: "October 31", 4: "January 31 (next year)"}
    st.caption(f"Filing due: {oss_deadline_map.get(quarter, '—')}")

    csv_data = pd.DataFrame([
        {"country": r.country, "base_eur": r.base_eur, "vat_rate": r.vat_rate,
         "vat_amount_eur": r.vat_amount_eur}
        for r in result.rows
    ]).to_csv(index=False)
    st.download_button("Export OSS Return CSV", data=csv_data,
                       file_name=f"oss_return_{year}_Q{quarter}.csv", mime="text/csv")

    _save_filing_button("OSS", year, quarter, result.total_vat)


# ---------------------------------------------------------------------------
# Sub-section F: Modelo 347
# ---------------------------------------------------------------------------

def _render_modelo_347(year: int) -> None:
    st.subheader("F. Modelo 347 — Operaciones con Terceros (Annual)")
    conn = _get_conn()
    try:
        result = compute_modelo_347(year, conn)
    finally:
        conn.close()

    if not result.rows:
        st.info(f"No Spain counterparties exceed the €{result.threshold:,.2f} threshold in {year}.")
        return

    import pandas as pd
    rows = []
    for r in result.rows:
        qb = " | ".join(f"Q{q}: {_fmt_eur(v)}" for q, v in sorted(r.quarter_breakdown.items()))
        rows.append({
            "Counterparty": r.counterparty_name,
            "NIF/CIF": r.counterparty_nif or "⚠️ Enter manually",
            "Total Operations": r.total_operations,
            "Quarter Breakdown": qb,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption(
        "NIF/CIF must be entered manually — Stripe does not store tax IDs. "
        "Only Spain-based counterparties (geo_region = SPAIN) appear here."
    )

    _save_filing_button("347", year, None, None)


# ---------------------------------------------------------------------------
# Shared: save filing status button
# ---------------------------------------------------------------------------

def _save_filing_button(model: str, year: int, quarter: int | None, amount: float | None) -> None:
    key = f"save_{model}_{year}_{quarter or 'annual'}"
    period_label = f"Q{quarter}" if quarter else "Annual"
    col1, col2 = st.columns([2, 1])
    notes = col1.text_input(f"Notes for Modelo {model} {period_label}", key=f"notes_{key}")
    if col2.button(f"Save Computed ({model} {period_label})", key=f"btn_{key}"):
        upsert_filing_status(year=year, model=model, quarter=quarter,
                             status="COMPUTED", amount_eur=amount, notes=notes)
        st.success(f"Modelo {model} {period_label} saved as COMPUTED (€{amount:.2f}).")


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("Tax Obligations")
    st.markdown(_DISCLAIMER)
    st.divider()

    config = reload_config()
    tax_cfg = config.get("tax", {})

    if "tax" not in config:
        st.warning(
            "Tax configuration is incomplete. Go to **Configuration** tab and fill in the `tax` section "
            "(NIF, regime, OSS registration, etc.)."
        )

    col1, col2 = st.columns(2)
    current_year = date.today().year
    year = col1.selectbox("Year", list(range(current_year, current_year - 5, -1)), index=0,
                          key="tax_year")
    quarter = col2.selectbox("Quarter", [1, 2, 3, 4],
                             format_func=_quarter_label, index=0, key="tax_quarter")

    st.divider()

    (tab_calendar, tab_303, tab_130, tab_manual,
     tab_oss, tab_347) = st.tabs([
        "Tax Calendar",
        "Modelo 303 — IVA",
        "Modelo 130 — IRPF",
        "Manual Entries",
        "OSS Return",
        "Modelo 347",
    ])

    with tab_calendar:
        _render_tax_calendar(year)

    with tab_303:
        _render_modelo_303(year, quarter)

    with tab_130:
        _render_modelo_130(year, quarter, config)

    with tab_manual:
        _render_manual_entries(year, quarter)

    with tab_oss:
        _render_oss_return(year, quarter)

    with tab_347:
        _render_modelo_347(year)
