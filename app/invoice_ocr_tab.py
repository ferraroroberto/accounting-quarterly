"""Invoice OCR tab — extract accounting data from PDFs via Gemini."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent

from src.config import load_config
from src.database import (
    clear_invoices,
    delete_invoice,
    delete_invoices_by_ids,
    get_invoice_by_filename,
    get_invoice_hash,
    get_invoices,
    upsert_invoice,
)
from src.logger import get_logger

log = get_logger(__name__)


def _resolve_dir(directory: str) -> Path:
    p = Path(directory)
    return p if p.is_absolute() else ROOT / directory


def _scan_pdfs(directory: str) -> list[str]:
    dir_path = _resolve_dir(directory)
    if not dir_path.exists():
        return []
    # Scan recursively; return paths relative to the directory root
    return sorted(str(f.relative_to(dir_path)) for f in dir_path.rglob("*.pdf"))


def _direction_label(direction: str) -> str:
    return "Expense (In)" if direction == "in" else "Income (Out)"


def _compute_hash(filename: str, invoice_dir: str) -> str:
    import hashlib
    pdf_path = _resolve_dir(invoice_dir) / filename
    return hashlib.md5(pdf_path.read_bytes()).hexdigest()


def _needs_extraction(filename: str, direction: str, invoice_dir: str) -> bool:
    """Return True if the file has not been extracted yet or the PDF has changed."""
    stored_hash = get_invoice_hash(filename, direction)
    if stored_hash is None:
        return True
    current_hash = _compute_hash(filename, invoice_dir)
    return current_hash != stored_hash


def _extract_and_save(filename: str, direction: str, invoice_dir: str) -> dict:
    """Run Gemini extraction and persist to DB. Returns the extracted data dict."""
    from src.invoice_ocr import extract_invoice

    pdf_path = _resolve_dir(invoice_dir) / filename
    data = extract_invoice(pdf_path)

    record = {
        "filename": filename,
        "direction": direction,
        "file_hash": data.get("_file_hash"),
        "invoice_number": data.get("invoice_number"),
        "invoice_date": data.get("invoice_date"),
        "vendor_name": data.get("vendor_name"),
        "vendor_nif": data.get("vendor_nif"),
        "vendor_address": data.get("vendor_address"),
        "client_name": data.get("client_name"),
        "client_nif": data.get("client_nif"),
        "client_address": data.get("client_address"),
        "description": data.get("description"),
        "subtotal_eur": data.get("subtotal_eur"),
        "iva_rate": data.get("iva_rate"),
        "iva_amount": data.get("iva_amount"),
        "irpf_rate": data.get("irpf_rate"),
        "irpf_amount": data.get("irpf_amount"),
        "total_eur": data.get("total_eur"),
        "currency": data.get("currency", "EUR"),
        "original_currency": data.get("original_currency"),
        "original_amount": data.get("original_amount"),
        "fx_rate": data.get("fx_rate"),
        "payment_method": data.get("payment_method"),
        "category": data.get("category"),
        "notes": data.get("notes"),
        "raw_json": data.get("_raw_response"),
        # Enhanced Spanish accounting fields
        "invoice_type": data.get("invoice_type"),
        "supply_date": data.get("supply_date"),
        "due_date": data.get("due_date"),
        "is_rectificativa": 1 if data.get("is_rectificativa") else 0,
        "rectified_invoice_ref": data.get("rectified_invoice_ref"),
        "vat_exempt_reason": data.get("vat_exempt_reason"),
        "iva_breakdown": json.dumps(data.get("iva_breakdown")) if data.get("iva_breakdown") else None,
        "deductible_pct": data.get("deductible_pct"),
        "billing_period_start": data.get("billing_period_start"),
        "billing_period_end": data.get("billing_period_end"),
    }
    upsert_invoice(record)
    return record


def _render_invoice_panel(direction: str, invoice_dir: str) -> None:
    label = "Expenses (In — factures recibidas)" if direction == "in" else "Income (Out — factures emitidas)"
    st.subheader(label)
    st.caption(f"Directory: `{invoice_dir}`")

    all_files = _scan_pdfs(invoice_dir)
    if not all_files:
        st.info(f"No PDF files found in `{invoice_dir}`.")
        return

    # Batch extract all
    col_a, col_b = st.columns([1, 3])
    with col_a:
        if st.button(f"Extract new/changed ({direction})", key=f"extract_all_{direction}"):
            to_process = [f for f in all_files if _needs_extraction(f, direction, invoice_dir)]
            if not to_process:
                st.success("All files are already up to date (no changes detected).")
            else:
                progress = st.progress(0, text="Extracting…")
                errors: list[str] = []
                for i, fname in enumerate(to_process):
                    try:
                        _extract_and_save(fname, direction, invoice_dir)
                    except Exception as exc:
                        log.error("Extraction failed for %s: %s", fname, exc)
                        errors.append(f"{fname}: {exc}")
                    progress.progress((i + 1) / len(to_process), text=f"Extracted {fname}")
                progress.empty()
                if errors:
                    for e in errors:
                        st.error(e)
                else:
                    st.success(f"Extracted {len(to_process)} file(s).")
                st.rerun()

    with col_b:
        pending = sum(1 for f in all_files if _needs_extraction(f, direction, invoice_dir))
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
                            record = _extract_and_save(fname, direction, invoice_dir)
                            st.success("Extracted successfully.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
            with col3:
                if existing and st.button("Delete record", key=f"delete_{direction}_{fname}"):
                    delete_invoice(fname, direction)
                    st.warning("Record deleted.")
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
        ded = rec.get("deductible_pct")
        if ded is not None and ded != 100:
            st.text(f"Deductible:   {ded}%")
        if rec.get("vat_exempt_reason"):
            st.text(f"VAT exempt:  {rec['vat_exempt_reason']}")

        st.markdown("**Description**")
        st.text(rec.get("description") or "—")

        if rec.get("notes"):
            st.markdown("**Notes**")
            st.info(rec["notes"])

    with st.expander("Raw Gemini JSON", expanded=False):
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
    cfg = load_config()
    app_cfg = cfg.get("app", {})
    invoice_in_dir = app_cfg.get("invoice_in_dir", "data/invoices/in")
    invoice_out_dir = app_cfg.get("invoice_out_dir", "data/invoices/out")

    st.subheader("Invoice OCR — AI Extraction for Spanish Accounting")

    # API key check
    has_key = bool(os.getenv("GOOGLE_API_KEY"))
    if not has_key:
        st.error(
            "GOOGLE_API_KEY is not set. Add it to your `.env` file to use this feature."
        )
        return

    st.info(
        "Upload invoices (PDFs) to the `data/invoices/in` or `data/invoices/out` directories, "
        "then click **Extract** to parse them via Gemini and store the accounting data in the "
        "`invoices` table.\n\n"
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
                if st.button("Clear invoice table", type="secondary"):
                    st.session_state["confirm_clear_invoices"] = True
                    st.rerun()
            else:
                st.warning(
                    "This will permanently delete **all** invoice records from the database. "
                    "The PDF files themselves are not touched."
                )
                col_yes, col_no = st.columns(2)
                if col_yes.button("Yes, delete everything", type="primary"):
                    n = clear_invoices()
                    st.session_state["confirm_clear_invoices"] = False
                    st.success(f"Deleted {n} record(s).")
                    st.rerun()
                if col_no.button("Cancel"):
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
                "supply_date", "due_date", "deductible_pct",
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
                    ):
                        ids_to_delete = [
                            df_display.iloc[i]["id"]
                            for i in selected_indices
                            if "id" in df_display.columns
                        ]
                        n = delete_invoices_by_ids(ids_to_delete)
                        st.success(f"Deleted {n} record(s).")
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
                )
