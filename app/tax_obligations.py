"""Tax Obligations tab — Spanish autónomo quarterly and annual filings."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any

import streamlit as st

from app.annual_pack_tab import render as render_annual_pack
from app.flash import flash, show_flash
from app.year_picker import year_choices
from src.config import reload_config
from src.database import (
    add_tax_entry,
    delete_tax_entry,
    get_tax_entries,
    load_tax_snapshots_for_period,
    upsert_filing_status,
    get_connection,
)
from src.declared_reports import get_declared_report
from src.tax_engine import (
    compute_and_persist_tax_snapshots,
    compute_eu_b2c_threshold,
    get_tax_calendar,
)
from src.tax_snapshot_codec import decode_snapshot
from src.tax_models import (
    EUB2CThresholdResult,
    Modelo130Result,
    Modelo303Result,
    Modelo347Result,
    Modelo349Result,
    OSSReturnResult,
    TaxDeadline,
)

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
    return get_connection()


def _load_snapshot_bundle(year: int, quarter: int) -> dict[str, tuple[Any, str]]:
    """Decode stored tax snapshots for the period (quarterly + annual 347)."""
    conn = _get_conn()
    try:
        rows = load_tax_snapshots_for_period(year, quarter, conn)
        out: dict[str, tuple[Any, str]] = {}
        for row in rows:
            m = row["model"]
            obj = decode_snapshot(m, row["payload_json"])
            out[m] = (obj, row["computed_at"])
        return out
    finally:
        conn.close()


def _missing_snapshot_banner() -> None:
    st.info("No saved calculation for this year and quarter. Click **Calculate tax** above.")


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

_M303_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("IVA devengado", (
        ("01", "Base 4%"), ("03", "Cuota 4%"), ("04", "Base 10%"), ("06", "Cuota 10%"),
        ("07", "Base 21%"), ("09", "Cuota 21%"),
        ("10", "Adq. intracomunitarias — base"), ("11", "Adq. intracomunitarias — cuota"),
        ("12", "Otras operaciones con ISP — base"), ("13", "Otras operaciones con ISP — cuota"),
        ("27", "Total cuota devengada"),
    )),
    ("IVA deducible", (
        ("28", "Interiores corrientes — base"), ("29", "Interiores corrientes — cuota"),
        ("30", "Bienes de inversión — base"), ("31", "Bienes de inversión — cuota"),
        ("36", "Adq. intracom. corrientes — base"), ("37", "Adq. intracom. corrientes — cuota"),
        ("43", "Regularización bienes de inversión"), ("44", "Regularización prorrata definitiva"),
        ("45", "Total a deducir"), ("46", "Resultado régimen general (27 − 45)"),
    )),
    ("Información adicional", (
        ("59", "Entregas intracomunitarias"), ("60", "Exportaciones"),
        ("120", "No sujetas por reglas de localización"), ("123", "No sujetas — OSS"),
    )),
    ("Resultado", (
        ("64", "Suma de resultados"), ("65", "% atribuible al Estado"), ("66", "Atribuible al Estado"),
        ("110", "Cuotas a compensar pendientes (periodos anteriores)"),
        ("78", "Cuotas a compensar aplicadas"), ("87", "Pendientes para periodos posteriores"),
        ("69", "Resultado de la autoliquidación"), ("71", "Resultado"),
        ("72", "A compensar"), ("73", "A devolver"),
    )),
)


def _render_modelo_303(year: int, quarter: int, bundle: dict[str, tuple[Any, str]]) -> None:
    st.subheader("B. Modelo 303 — IVA Trimestral")
    pair = bundle.get("303")
    if not pair:
        _missing_snapshot_banner()
        return
    result, computed_at = pair
    assert isinstance(result, Modelo303Result)

    st.caption(f"Stored calculation: {computed_at}")
    st.markdown(f"**Period:** {_quarter_label(quarter)} {year}")

    result_val = result.c71_resultado_liquidacion
    col1, col2, col3 = st.columns(3)
    col1.metric("Box 71 — Resultado", _fmt_eur(result_val),
                delta="Refund / carry" if result_val < 0 else "To pay",
                delta_color="inverse" if result_val < 0 else "normal")
    col2.metric("Credit carried to next period (87 + 72)", _fmt_eur(result.credit_carry_forward))
    col3.metric("Box 46 without pro-rata (gestor mode)", _fmt_eur(result.c46_sin_prorrata))

    # AEAT boxes in form order; zero boxes are hidden except the totals.
    boxes = result.aeat_boxes()
    always = {"27", "45", "46", "64", "66", "71"}
    for title, rows in _M303_SECTIONS:
        shown = [(b, label, boxes[b]) for b, label in rows if boxes.get(b) or b in always]
        if not shown:
            continue
        st.markdown(f"##### {title}")
        st.dataframe(
            [{"Casilla": b, "Concepto": label,
              "Importe": f"{v:,.2f} %" if b == "65" else _fmt_eur(v)} for b, label, v in shown],
            width="stretch", hide_index=True,
        )

    prorrata = (f"Pro-rata: provisional {result.prorrata_provisional_pct:.0f}% "
                f"({result.prorrata_provisional_source})")
    if result.prorrata_definitive_pct is not None:
        prorrata += f" · definitive {result.prorrata_definitive_pct:.0f}%"
    if not result.prorrata_enabled:
        prorrata = "Pro-rata disabled."
    st.caption(f"{prorrata} · Box 110 source: {result.c110_source or '—'}")
    if result.oss_base > 0:
        st.info(
            f"OSS income (not in Modelo 303): base {_fmt_eur(result.oss_base)}, "
            f"VAT {_fmt_eur(result.oss_vat)} — declare separately via OSS portal."
        )
    if result.notes:
        st.warning(result.notes)

    st.divider()
    _save_filing_button("303", year, quarter, result_val)

    with st.expander("⚠️ Caveats"):
        st.markdown(
            "- `IVA_EU_B2B` transactions require a valid NIF-IVA verified in VIES — "
            "the system cannot verify this automatically.\n"
            "- Box 110 chains from the **filed** previous return when it was imported "
            "(`python -m src.filed_returns import …`); otherwise from the app's own previous quarter.\n"
            "- Manual override: enter corrected IVA soportado via **Manual Entries** below, with its VAT rate."
        )


# ---------------------------------------------------------------------------
# Sub-section C: Modelo 130
# ---------------------------------------------------------------------------

_M130_SECTIONS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("I. Actividades económicas en estimación directa (año acumulado)", (
        ("01", "Ingresos computables"), ("02", "Gastos fiscalmente deducibles"),
        ("03", "Rendimiento neto (01 − 02)"), ("04", "20% del importe positivo de 03"),
        ("05", "Pagos fraccionados de trimestres anteriores"),
        ("06", "Retenciones e ingresos a cuenta"), ("07", "Pago fraccionado previo (04 − 05 − 06)"),
    )),
    ("II. Actividades agrícolas, ganaderas, forestales y pesqueras", (
        ("08", "Volumen de ingresos"), ("09", "2% de 08"), ("10", "Retenciones e ingresos a cuenta"),
        ("11", "Pago fraccionado previo (09 − 10)"),
    )),
    ("III. Total liquidación", (
        ("12", "Suma de pagos fraccionados previos (07 + 11)"),
        ("13", "Minoración art. 110.3.c RIRPF"), ("14", "Diferencia (12 − 13)"),
        ("15", "Resultados negativos de trimestres anteriores"),
        ("16", "Deducción préstamo vivienda habitual"), ("17", "Total (14 − 15 − 16)"),
        ("18", "Resultado de la autoliquidación anterior (complementaria)"),
        ("19", "Resultado de la autoliquidación (17 − 18)"),
    )),
)


def _render_modelo_130(year: int, quarter: int,
                       bundle: dict[str, tuple[Any, str]]) -> None:
    st.subheader("C. Modelo 130 — IRPF Trimestral")
    pair = bundle.get("130")
    if not pair:
        _missing_snapshot_banner()
        return
    result, computed_at = pair
    assert isinstance(result, Modelo130Result)

    st.caption(f"Stored calculation: {computed_at}")
    st.markdown(f"**Period:** {_quarter_label(quarter)} {year} — boxes 01–07 are year-to-date")

    result_val = result.c19_resultado
    col1, col2, col3 = st.columns(3)
    col1.metric("Box 19 — Resultado", _fmt_eur(result_val),
                delta=("Negativa (carried to box 15)" if result_val < 0
                       else "To pay" if result_val > 0 else "Resultado cero"),
                delta_color="inverse" if result_val < 0 else "normal")
    col2.metric("Negative results pending for later quarters", _fmt_eur(result.negativos_pendientes_posteriores))
    col3.metric("Box 13 reduction", _fmt_eur(result.c13_minoracion))

    # AEAT boxes in form order; zero boxes are hidden except the key totals.
    boxes = result.aeat_boxes()
    always = {"01", "02", "03", "07", "12", "14", "17", "19"}
    for title, rows in _M130_SECTIONS:
        shown = [(b, label, boxes[b]) for b, label in rows if boxes.get(b) or b in always]
        if not shown:
            continue
        st.markdown(f"##### {title}")
        st.dataframe(
            [{"Casilla": b, "Concepto": label, "Importe": _fmt_eur(v)} for b, label, v in shown],
            width="stretch", hide_index=True,
        )

    st.caption(
        f"Box 02 = real expenses {_fmt_eur(result.gastos_reales)} + 5% gastos de difícil justificación "
        f"{_fmt_eur(result.gastos_dificil_justificacion)} = {_fmt_eur(result.c02_gastos)} · "
        f"Box 05 source: {result.c05_source or '—'} · Box 13 from previous-year net "
        + (f"{_fmt_eur(result.previous_year_net_yield)}" if result.previous_year_net_yield is not None else "—")
        + f" ({result.previous_year_net_source or '—'})"
    )
    if result.notes:
        st.warning(result.notes)

    st.divider()
    _save_filing_button("130", year, quarter, result_val)

    with st.expander("ℹ️ Notes"):
        st.markdown(
            "- Box 02 adds **Gastos Deducibles** manual entries; box 06 adds **Retenciones Soportadas** "
            "manual entries (Manual Entries below) to the invoice withholdings.\n"
            "- Boxes 05 and 15 chain from the **filed** 130s of the earlier quarters of the year when "
            "imported (`python -m src.filed_returns import …`); otherwise from the app's own quarters.\n"
            "- Box 13 uses the previous year's net yield: the filed Q4 130 box 03, else "
            "`tax.previous_year_net_yield` in config, else the app's own previous-year figure.\n"
            "- Stripe does not capture IRPF retentions — issued invoices carry them (`irpf_amount`)."
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
        df = pd.DataFrame(entries).reindex(
            columns=["id", "entry_type", "amount_eur", "vat_rate", "description", "notes", "created_at"])
        df.columns = ["ID", "Type", "Amount (€)", "VAT rate %", "Description", "Notes", "Created"]
        st.dataframe(df, width="stretch", hide_index=True)

        delete_id = st.number_input("Delete entry by ID", min_value=0, step=1, value=0,
                                    key=f"del_entry_{year}_{quarter}")
        if st.button("Delete Entry", key=f"del_btn_{year}_{quarter}"):
            if delete_id > 0:
                deleted = delete_tax_entry(int(delete_id))
                if deleted:
                    flash("tax", "success", f"Entry {delete_id} deleted.")
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
            key=f"add_entry_type_{year}_{quarter}",
        )
        amount = col2.number_input(
            "Amount (€)", min_value=0.0, step=0.01, format="%.2f",
            key=f"add_entry_amount_{year}_{quarter}",
        )
        vat_rate = col1.selectbox(
            "VAT rate (IVA_SOPORTADO only)", [21.0, 10.0, 4.0],
            format_func=lambda r: f"{r:.0f}%", key=f"add_entry_rate_{year}_{quarter}",
        )
        description = st.text_input("Description", key=f"add_entry_desc_{year}_{quarter}")
        notes = st.text_area("Notes", height=70, key=f"add_entry_notes_{year}_{quarter}")
        if st.form_submit_button("Add Entry", key=f"add_entry_submit_{year}_{quarter}"):
            if amount > 0:
                add_tax_entry(year, quarter, entry_type, amount, description, notes,
                              vat_rate=vat_rate if entry_type == "IVA_SOPORTADO" else None)
                flash("tax", "success", "Entry added.")
                st.rerun()
            else:
                st.warning("Amount must be greater than 0.")


# ---------------------------------------------------------------------------
# Sub-section E: OSS Return
# ---------------------------------------------------------------------------

def _load_eu_b2c_threshold(year: int, quarter: int, config: dict) -> EUB2CThresholdResult:
    conn = _get_conn()
    try:
        return compute_eu_b2c_threshold(year, quarter, conn, config)
    finally:
        conn.close()


def _show_threshold_alert(tracker: EUB2CThresholdResult) -> None:
    if tracker.status == "EXCEEDED":
        st.error(f"🔴 {tracker.message}")
    elif tracker.status == "WARNING":
        st.warning(f"⚠️ {tracker.message}")


def _render_eu_b2c_threshold(tracker: EUB2CThresholdResult) -> None:
    st.subheader("E. EU B2C distance-sales threshold (art. 73 LIVA)")
    st.caption(
        "Live from the transactions table (declared-report amounts where frozen). EU consumer "
        "sales stay taxed in Spain at 21% while the year's and the previous year's EU B2C sales "
        f"(ex-VAT) are at or below €{tracker.limit_eur:,.0f}."
    )
    col1, col2, col3 = st.columns(3)
    col1.metric(f"YTD {tracker.year} (to Q{tracker.quarter})", _fmt_eur(tracker.ytd_base_eur))
    col2.metric("Of threshold", f"{tracker.ratio:.0%}")
    col3.metric(f"Previous year {tracker.year - 1}", _fmt_eur(tracker.previous_year_base_eur))
    st.progress(min(tracker.ratio, 1.0))
    if tracker.status == "OK":
        st.success(f"Below {tracker.warn_ratio:.0%} of the threshold.")
    else:
        _show_threshold_alert(tracker)
    if tracker.by_country:
        with st.expander("By card country"):
            for country, base in tracker.by_country.items():
                st.markdown(f"- **{country}**: {_fmt_eur(base)}")
    st.divider()


def _render_oss_return(year: int, quarter: int, bundle: dict[str, tuple[Any, str]]) -> None:
    st.subheader("F. OSS Return (One Stop Shop)")
    pair = bundle.get("OSS")
    if not pair:
        _missing_snapshot_banner()
        return
    result, computed_at = pair
    assert isinstance(result, OSSReturnResult)

    st.caption(f"Stored calculation: {computed_at}")
    st.markdown(f"**Period:** {_quarter_label(quarter)} {year}")

    if not result.rows:
        st.info("No OSS transactions in this period.")
        _save_filing_button("OSS", year, quarter, result.total_vat)
        return

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
                       file_name=f"oss_return_{year}_Q{quarter}.csv", mime="text/csv",
                       key=f"oss_csv_download_{year}_{quarter}")

    _save_filing_button("OSS", year, quarter, result.total_vat)


# ---------------------------------------------------------------------------
# Sub-section F: Modelo 347
# ---------------------------------------------------------------------------

def _render_modelo_347(year: int, bundle: dict[str, tuple[Any, str]]) -> None:
    st.subheader("G. Modelo 347 — Operaciones con Terceros (Annual)")
    pair = bundle.get("347")
    if not pair:
        _missing_snapshot_banner()
        return
    result, computed_at = pair
    assert isinstance(result, Modelo347Result)

    st.caption(f"Stored calculation: {computed_at} (annual snapshot for {year})")
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
# Sub-section H: Modelo 349
# ---------------------------------------------------------------------------

def _render_modelo_349(year: int, quarter: int, bundle: dict[str, tuple[Any, str]]) -> None:
    st.subheader("H. Modelo 349 — Operaciones Intracomunitarias")
    pair = bundle.get("349")
    if not pair:
        _missing_snapshot_banner()
        return
    result, computed_at = pair
    assert isinstance(result, Modelo349Result)

    st.caption(f"Stored calculation: {computed_at}")
    boxes = result.aeat_boxes()
    c1, c2 = st.columns(2)
    c1.metric("01 · Número total de operadores", int(boxes["01"]))
    c2.metric("02 · Importe de las operaciones intracomunitarias", _fmt_eur(boxes["02"]))

    def _table(rows: list) -> None:
        st.dataframe(
            [{"Clave": r.key, "País": r.country, "NIF-IVA": r.vat_id[len(r.country):] or "—",
              "Operador": r.name, "Base imponible": _fmt_eur(r.base), "Registros": r.n_records}
             for r in rows],
            width="stretch", hide_index=True,
        )

    if result.rows:
        _table(result.rows)
    else:
        st.info("No intra-EU operations to declare in this period.")
    if result.excluded:
        st.markdown("##### Not declared — zero or negative total")
        _table(result.excluded)
    if result.unidentified:
        st.markdown("##### Not declared — missing VAT id")
        _table(result.unidentified)
    if result.notes:
        st.warning(result.notes)
    st.caption("Key I = services acquired from EU businesses (reverse charge, 303 boxes 10/11); "
               "key S = services supplied to EU businesses (303 box 59). Rectifications (03/04) "
               "are not modelled.")
    _save_filing_button("349", year, quarter, None)


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
        amt = f"€{amount:,.2f}" if amount is not None else "—"
        st.success(f"Modelo {model} {period_label} saved as COMPUTED ({amt}).")


# ---------------------------------------------------------------------------
# Main render
# ---------------------------------------------------------------------------

def render() -> None:
    show_flash("tax")
    st.title("Tax Obligations")
    st.markdown(_DISCLAIMER)
    st.divider()

    config = reload_config()

    if "tax" not in config:
        st.warning(
            "Tax configuration is incomplete. Go to **Configuration** tab and fill in the `tax` section "
            "(regime, IVA/OSS registration, EU VAT defaults, etc.)."
        )

    col1, col2, col3 = st.columns([2, 2, 2])
    current_year = date.today().year
    years = year_choices()[::-1]
    year = col1.selectbox("Year", years, index=years.index(current_year), key="tax_year")
    quarter = col2.selectbox("Quarter", [1, 2, 3, 4],
                             format_func=_quarter_label, index=0, key="tax_quarter")
    with col3:
        st.markdown("")  # align button with selectboxes
        if st.button("Calculate tax", key="tax_calc_all", type="primary"):
            conn = _get_conn()
            try:
                with st.spinner("Computing and saving tax obligations…"):
                    compute_and_persist_tax_snapshots(year, quarter, conn)
            finally:
                conn.close()
            flash("tax", "success", "Tax calculations saved to the database.")
            st.rerun()

    st.caption(
        "Obligation figures are read from **saved snapshots** in SQLite. They update only when you click "
        "**Calculate tax** (after you sync or change transactions and manual entries)."
    )

    conn = _get_conn()
    try:
        declared = get_declared_report(conn, year, quarter)
    finally:
        conn.close()
    if declared:
        st.caption(
            f"🔒 Stripe report for Q{quarter} {year} declared on {declared.created_at} "
            f"(v{declared.version}, {declared.n_transactions} transactions, sha256 "
            f"`{declared.sha256[:12]}…`): the engine uses its frozen EUR amounts."
        )

    eu_b2c_tracker = _load_eu_b2c_threshold(year, quarter, config)
    _show_threshold_alert(eu_b2c_tracker)

    st.divider()

    snapshot_bundle = _load_snapshot_bundle(year, quarter)

    (tab_calendar, tab_303, tab_130, tab_manual,
     tab_oss, tab_347, tab_349, tab_annual) = st.tabs([
        "Tax Calendar",
        "Modelo 303 — IVA",
        "Modelo 130 — IRPF",
        "Manual Entries",
        "EU B2C / OSS",
        "Modelo 347",
        "Modelo 349",
        "Annual Pack",
    ])

    with tab_calendar:
        _render_tax_calendar(year)

    with tab_303:
        _render_modelo_303(year, quarter, snapshot_bundle)

    with tab_130:
        _render_modelo_130(year, quarter, snapshot_bundle)

    with tab_manual:
        _render_manual_entries(year, quarter)

    with tab_oss:
        _render_eu_b2c_threshold(eu_b2c_tracker)
        _render_oss_return(year, quarter, snapshot_bundle)

    with tab_347:
        _render_modelo_347(year, snapshot_bundle)

    with tab_349:
        _render_modelo_349(year, quarter, snapshot_bundle)

    with tab_annual:
        render_annual_pack(year)
