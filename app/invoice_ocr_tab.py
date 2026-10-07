"""Invoice OCR tab — extract accounting data from PDFs via local-llm-hub."""
from __future__ import annotations

import json

import pandas as pd
import streamlit as st

from app.flash import flash, show_flash
from src.config import load_config
from src.database import (
    clear_invoices,
    delete_invoice,
    delete_invoices_by_ids,
    get_invoice_by_filename,
    get_invoices,
    parse_locked_fields,
)
from src.fx_rates import STALE_TOLERANCE_DAYS
from src.invoice_ingest import extract_and_save, list_invoice_files, needs_extraction
from src.logger import get_logger
from src.vendor_registry import load_registry

log = get_logger(__name__)


def _render_invoice_panel(direction: str, invoice_dir: str) -> None:
    label = "Expenses (In — factures recibidas)" if direction == "in" else "Income (Out — factures emitidas)"
    st.subheader(label)
    st.caption(f"Directory: `{invoice_dir}`")

    all_files = list_invoice_files(direction)
    if not all_files:
        st.info(f"No PDF files found in `{invoice_dir}`.")
        return

    # Batch extract all
    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button(f"Extract new/changed ({direction})", key=f"extract_all_{direction}"):
            to_process = [f for f in all_files if needs_extraction(f, direction)]
            if not to_process:
                st.success("All files are already up to date (no changes detected).")
            else:
                progress = st.progress(0, text="Extracting…")
                errors: list[str] = []
                for i, fname in enumerate(to_process):
                    try:
                        extract_and_save(fname, direction)
                    except Exception as exc:
                        log.error("Extraction failed for %s: %s", fname, exc)
                        errors.append(f"{fname}: {exc}")
                    progress.progress((i + 1) / len(to_process), text=f"Extracted {fname}")
                progress.empty()
                if errors:
                    for e in errors:
                        flash("ocr", "error", e)
                else:
                    flash("ocr", "success", f"Extracted {len(to_process)} file(s).")
                st.rerun()

    with col_b:
        pending = sum(1 for f in all_files if needs_extraction(f, direction))
        st.caption(f"{len(all_files)} PDF(s) found · {pending} pending extraction")

    st.markdown("---")

    # Per-file cards
    for fname in all_files:
        existing = get_invoice_by_filename(fname, direction)
        with st.expander(f"{'✅' if existing else '⬜'} {fname}", expanded=not existing):
            col1, col2, col3 = st.columns([2, 1, 1])
            with col1:
                st.markdown(f"**{fname}**")
                if existing:
                    st.caption(f"Extracted: {existing.get('extracted_at', '')}")
            with col2:
                if st.button("Extract / Re-extract", key=f"extract_{direction}_{fname}"):
                    with st.spinner(f"Extracting {fname}…"):
                        try:
                            record = extract_and_save(fname, direction)
                            flash("ocr", "success", f"Extracted {fname}.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
            with col3:
                if existing and st.button("Delete record", key=f"delete_{direction}_{fname}"):
                    delete_invoice(fname, direction)
                    flash("ocr", "warning", "Record deleted.")
                    st.rerun()

            if existing:
                _render_invoice_fields(existing)


def _render_invoice_fields(rec: dict) -> None:
    """Render extracted fields in a tidy grid."""
    col_l, col_r = st.columns(2)
    with col_l:
        st.markdown("**Document info**")
        st.text(f"Number:      {rec.get('invoice_number') or '—'}")
        st.text(f"Type:        {rec.get('invoice_type') or '—'}")
        st.text(f"Date:        {rec.get('invoice_date') or '—'}")
        st.text(f"Supply date: {rec.get('supply_date') or '—'}")
        st.text(f"Due date:    {rec.get('due_date') or '—'}")
        st.text(f"Category:    {rec.get('category') or '—'}")
        st.text(f"Payment:     {rec.get('payment_method') or '—'}")
        if rec.get("billing_period_start") or rec.get("billing_period_end"):
            st.text(f"Period:      {rec.get('billing_period_start') or '?'} → {rec.get('billing_period_end') or '?'}")
        if rec.get("is_rectificativa"):
            st.warning(f"Factura rectificativa — ref: {rec.get('rectified_invoice_ref') or '—'}")

        st.markdown("**Vendor**")
        st.text(f"Name:    {rec.get('vendor_name') or '—'}")
        st.text(f"NIF:     {rec.get('vendor_nif') or '—'}")
        st.text(f"Address: {rec.get('vendor_address') or '—'}")

        st.markdown("**Client**")
        st.text(f"Name:    {rec.get('client_name') or '—'}")
        st.text(f"NIF:     {rec.get('client_nif') or '—'}")
        st.text(f"Address: {rec.get('client_address') or '—'}")

    with col_r:
        st.markdown("**Amounts (EUR)**")
        st.text(f"Subtotal:     {_fmt(rec.get('subtotal_eur'))} EUR")
        # Show IVA breakdown if available, otherwise single rate
        breakdown_raw = rec.get("iva_breakdown")
        if breakdown_raw:
            try:
                breakdown = json.loads(breakdown_raw) if isinstance(breakdown_raw, str) else breakdown_raw
                for line in breakdown:
                    b = line.get("base_imponible") or line.get("subtotal_eur")
                    r = line.get("iva_rate")
                    a = line.get("iva_amount")
                    st.text(f"  IVA {_fmt_pct(r)}: base {_fmt(b)} → {_fmt(a)} EUR")
            except Exception:
                pass
        else:
            iva_r = rec.get("iva_rate")
            iva_a = rec.get("iva_amount")
            st.text(f"IVA ({_fmt_pct(iva_r)}):  {_fmt(iva_a)} EUR")
        irpf_r = rec.get("irpf_rate")
        irpf_a = rec.get("irpf_amount")
        if irpf_r or irpf_a:
            st.text(f"IRPF ({_fmt_pct(irpf_r)}): -{_fmt(irpf_a)} EUR")
        st.text(f"Total:        {_fmt(rec.get('total_eur'))} EUR")
        if rec.get("original_currency") and rec.get("original_currency") != "EUR":
            st.text(f"Original:     {_fmt(rec.get('original_amount'))} {rec.get('original_currency')}")
            fx_source = rec.get("fx_source")
            if fx_source == "NO_RATE":
                st.error("⚠️ No ECB rate available for this currency/date — amount NOT converted. "
                          "Load rates for this period in the Currency tab.")
            elif fx_source == "MISSING_FX_INPUT":
                st.error("⚠️ Original amount or invoice date missing — EUR figures are the LLM's unverified "
                         "estimate, not converted. Correct them in the Invoice Ledger tab.")
            elif fx_source:
                rate_txt = (f" · rate 1 EUR = {rec['fx_rate_used']:.4f} on {rec.get('fx_rate_date') or '?'}"
                            if rec.get("fx_rate_used") else "")
                st.caption(f"FX source: {fx_source}{rate_txt}")
                if rec.get("fx_stale"):
                    st.warning(f"⚠️ Stale FX rate (fallback more than {STALE_TOLERANCE_DAYS} days from the invoice date).")
                diff_pct = rec.get("fx_cross_check_diff_pct")
                if diff_pct is not None and diff_pct > 1.0:
                    st.warning(f"⚠️ LLM's own EUR estimate differs from the {fx_source} conversion by {diff_pct:.1f}%.")
        st.markdown("**Tax treatment**")
        if rec.get("direction") == "in":
            match = load_registry().match_invoice(rec)
            if match:
                st.text(f"Vendor:       {match.vendor.key} (registry, by {match.signal})")
            else:
                st.warning("⚠ Unknown vendor — add it in the Vendors tab, then apply the registry.")
        st.text(f"Treatment:    {rec.get('tax_treatment') or '— (unset)'}")
        st.text(f"Business use: VAT {_fmt_pct(rec.get('deductible_pct_vat'))} · "
                f"IRPF {_fmt_pct(rec.get('deductible_pct_irpf'))}")
        if rec.get("excluded"):
            st.warning(f"Excluded from tax computations — {rec.get('excluded_reason') or 'no reason given'}")
        locked = parse_locked_fields(rec.get("locked_fields"))
        if locked:
            st.caption(f"🔒 Locked (kept on re-extract): {', '.join(locked)}")
        if rec.get("vat_exempt_reason"):
            st.text(f"VAT exempt:  {rec['vat_exempt_reason']}")

        st.markdown("**Description**")
        st.text(rec.get("description") or "—")

        if rec.get("notes"):
            st.markdown("**Notes**")
            st.info(rec["notes"])

    with st.expander("Raw extraction JSON", expanded=False):
        raw = rec.get("raw_json") or "{}"
        try:
            st.json(json.loads(raw))
        except Exception:
            st.code(raw)


def _fmt(val) -> str:
    if val is None:
        return "—"
    return f"{val:,.2f}"


def _fmt_pct(val) -> str:
    if val is None:
        return "?"
    return f"{val:g}%"


def render() -> None:
    """Render the Invoice OCR tab."""
    show_flash("ocr")
    cfg = load_config()
    app_cfg = cfg.get("app", {})
    invoice_in_dir = app_cfg.get("invoice_in_dir", "data/invoices/in")
    invoice_out_dir = app_cfg.get("invoice_out_dir", "data/invoices/out")

    st.subheader("Invoice OCR — AI Extraction for Spanish Accounting")

    st.caption("Extraction provider: local-llm-hub (`gemini_pro`).")

    st.info(
        "Upload invoices (PDFs) to the `data/invoices/in` or `data/invoices/out` directories, "
        "then click **Extract** to parse them via the configured OCR backend and store the accounting data in the "
        "`invoices` table. Correct fields in the **Invoice Ledger** tab: corrected fields are locked and "
        "survive re-extraction.\n\n"
        "- **In (expenses):** invoices you received — IVA soportado, deductible costs.\n"
        "- **Out (income):** invoices you issued — IVA repercutido, income."
    )

    st.markdown("---")

    # Summary metrics
    all_invoices = get_invoices()
    in_recs = [r for r in all_invoices if r["direction"] == "in"]
    out_recs = [r for r in all_invoices if r["direction"] == "out"]

    total_in = sum(r["total_eur"] or 0 for r in in_recs)
    total_out = sum(r["total_eur"] or 0 for r in out_recs)
    total_iva_in = sum(r["iva_amount"] or 0 for r in in_recs)
    total_iva_out = sum(r["iva_amount"] or 0 for r in out_recs)

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Expense invoices", len(in_recs))
    m2.metric("Total expenses", f"€{total_in:,.2f}")
    m3.metric("IVA soportado", f"€{total_iva_in:,.2f}")
    m4.metric("Income invoices", len(out_recs))
    m5.metric("Total income", f"€{total_out:,.2f}")

    st.markdown("---")

    # Sub-tabs: In / Out / All records
    tab_in, tab_out, tab_all = st.tabs([
        "Expenses — In (recibidas)",
        "Income — Out (emitidas)",
        "All Records",
    ])

    with tab_in:
        _render_invoice_panel("in", invoice_in_dir)

    with tab_out:
        _render_invoice_panel("out", invoice_out_dir)

    with tab_all:
        st.subheader("All extracted invoices")

        # ── Clear table button (with confirmation) ───────────────────────────
        with st.expander("Danger zone", expanded=False):
            if not st.session_state.get("confirm_clear_invoices"):
                if st.button("Clear invoice table", type="secondary", key="inv_ocr_clear_start"):
                    st.session_state["confirm_clear_invoices"] = True
                    st.rerun()
            else:
                st.warning(
                    "This will permanently delete **all** invoice records from the database. "
                    "The PDF files themselves are not touched."
                )
                col_yes, col_no = st.columns(2)
                if col_yes.button("Yes, delete everything", type="primary", key="inv_ocr_clear_confirm"):
                    n = clear_invoices()
                    st.session_state["confirm_clear_invoices"] = False
                    flash("ocr", "success", f"Deleted {n} record(s).")
                    st.rerun()
                if col_no.button("Cancel", key="inv_ocr_clear_cancel"):
                    st.session_state["confirm_clear_invoices"] = False
                    st.rerun()

        if not all_invoices:
            st.info("No invoices extracted yet.")
        else:
            display_cols = [
                "id", "direction", "filename", "extracted_at", "invoice_date",
                "invoice_type", "vendor_name", "vendor_nif",
                "client_name", "client_nif", "description",
                "subtotal_eur", "iva_rate", "iva_amount",
                "irpf_rate", "irpf_amount", "total_eur",
                "currency", "category", "payment_method",
                "supply_date", "due_date", "tax_treatment",
                "deductible_pct_vat", "deductible_pct_irpf", "excluded", "excluded_reason",
                "is_rectificativa", "vat_exempt_reason", "notes",
            ]
            df = pd.DataFrame(all_invoices)
            visible = [c for c in display_cols if c in df.columns]
            df_display = df[visible].rename(columns={"extracted_at": "date_scanned"})

            # Row-selection dataframe (Streamlit ≥ 1.35)
            event = st.dataframe(
                df_display.drop(columns=["id"], errors="ignore"),
                width="stretch",
                hide_index=True,
                selection_mode="multi-row",
                on_select="rerun",
                key="all_invoices_table",
            )

            selected_indices = event.selection.rows if event and event.selection else []

            col_del, col_csv = st.columns([1, 3])
            with col_del:
                if selected_indices:
                    if st.button(
                        f"Delete {len(selected_indices)} selected record(s)",
                        type="primary",
                        key="inv_ocr_delete_selected",
                    ):
                        ids_to_delete = [
                            df_display.iloc[i]["id"]
                            for i in selected_indices
                            if "id" in df_display.columns
                        ]
                        n = delete_invoices_by_ids(ids_to_delete)
                        flash("ocr", "success", f"Deleted {n} record(s).")
                        st.rerun()
                else:
                    st.caption("Select rows to enable deletion.")

            with col_csv:
                csv = df_display.drop(columns=["id"], errors="ignore").to_csv(index=False).encode()
                st.download_button(
                    "Download CSV",
                    data=csv,
                    file_name="invoices_extracted.csv",
                    mime="text/csv",
                    key="inv_ocr_download_csv",
                )
