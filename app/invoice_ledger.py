"""Invoice Ledger tab — review and correct invoice tax fields.

Bulk edits go through ``st.data_editor``; single invoices through a form. Every
edited field is locked in ``src.database.update_invoice_fields`` so a later
re-extraction (OCR) never overwrites it.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
import streamlit as st

from src.database import (
    EXCLUDED_REASONS,
    TAX_TREATMENTS_IN,
    TAX_TREATMENTS_OUT,
    get_invoices,
    parse_locked_fields,
    unlock_invoice_fields,
    update_invoice_fields,
)
from src.logger import get_logger
from src.vendor_registry import load_registry

log = get_logger(__name__)

_ALL_PERIODS = "All periods"
_ISO_DATE_RE = r"^\d{4}-\d{2}-\d{2}$"

# Bulk-grid columns in display order, and the read-only subset of them.
_BULK_COLUMNS = [
    "filename", "counterparty", "invoice_date", "invoice_number",
    "subtotal_eur", "iva_amount", "total_eur", "currency",
    "tax_treatment", "deductible_pct_vat", "deductible_pct_irpf",
    "is_capital_asset", "asset_class", "excluded", "excluded_reason",
    "eur_received", "payment_date", "vat_treatment", "locked", "reviewed_at",
]
_BULK_READONLY = [
    "filename", "counterparty", "vendor", "invoice_number", "subtotal_eur", "iva_amount",
    "total_eur", "currency", "vat_treatment", "locked", "reviewed_at",
]
UNKNOWN_VENDOR = "⚠ unknown"


def _treatments(direction: str) -> tuple[str, ...]:
    return TAX_TREATMENTS_IN if direction == "in" else TAX_TREATMENTS_OUT


def _quarter_label(invoice_date: Optional[str]) -> Optional[str]:
    """``'2025-04-04'`` → ``'2025-Q2'`` (accounting date = invoice_date)."""
    if not invoice_date or len(invoice_date) < 7:
        return None
    try:
        month = int(invoice_date[5:7])
    except ValueError:
        return None
    return f"{invoice_date[:4]}-Q{(month - 1) // 3 + 1}"


def _to_date(value: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None


def _editor_version() -> int:
    return st.session_state.setdefault("ledger_editor_version", 0)


def _bump_editor_version() -> None:
    """Remount the data_editor (fresh key) so applied edits don't linger as pending."""
    st.session_state["ledger_editor_version"] = _editor_version() + 1


def _flash(kind: str, message: str) -> None:
    st.session_state.setdefault("ledger_flash", []).append((kind, message))


def _show_flash() -> None:
    for kind, message in st.session_state.pop("ledger_flash", []):
        getattr(st, kind)(message)


def _build_frame(records: list[dict], direction: str) -> pd.DataFrame:
    df = pd.DataFrame(records)
    name_col, nif_col = ("vendor_name", "vendor_nif") if direction == "in" else ("client_name", "client_nif")
    df["counterparty"] = df[name_col].fillna(df[nif_col]).fillna("")
    df["locked"] = df["locked_fields"].map(lambda raw: ", ".join(parse_locked_fields(raw)))
    for col in ("excluded", "is_capital_asset"):
        df[col] = df[col].fillna(0).astype(bool)
    df["quarter"] = df["invoice_date"].map(_quarter_label)
    if direction == "in":
        registry = load_registry()
        df["vendor"] = [
            m.vendor.key if (m := registry.match_invoice(rec)) else UNKNOWN_VENDOR for rec in records
        ]
    return df.set_index("id")


def _bulk_columns(direction: str) -> list[str]:
    """Bulk-grid columns; expenses also show the vendor-registry match (⚠ when unknown)."""
    if direction != "in":
        return _BULK_COLUMNS
    cols = list(_BULK_COLUMNS)
    cols.insert(cols.index("counterparty") + 1, "vendor")
    return cols


def _render_bulk_editor(view: pd.DataFrame, direction: str, filter_sig: str) -> None:
    st.markdown("**Bulk edit**")
    st.caption(
        "Edit cells, then **Save changes**. Each edited cell is locked against re-extraction. "
        "Excluded rows are ignored by every tax computation."
    )
    editor_key = f"ledger_editor_{direction}_{filter_sig}_{_editor_version()}"
    st.data_editor(
        view[_bulk_columns(direction)],
        key=editor_key,
        width="stretch",
        hide_index=True,
        num_rows="fixed",
        disabled=_BULK_READONLY,
        column_config={
            "invoice_date": st.column_config.TextColumn(
                "invoice_date", help="Accounting date (quarter keying). YYYY-MM-DD.",
                validate=_ISO_DATE_RE,
            ),
            "tax_treatment": st.column_config.SelectboxColumn(
                "tax_treatment", options=list(_treatments(direction)),
            ),
            "deductible_pct_vat": st.column_config.NumberColumn(
                "VAT %", min_value=0, max_value=100, step=0.01, help="Business-use share for the VAT deduction",
            ),
            "deductible_pct_irpf": st.column_config.NumberColumn(
                "IRPF %", min_value=0, max_value=100, step=0.01, help="Business-use share for the IRPF expense",
            ),
            "is_capital_asset": st.column_config.CheckboxColumn("capital asset"),
            "excluded": st.column_config.CheckboxColumn("excluded"),
            "excluded_reason": st.column_config.SelectboxColumn(
                "excluded_reason", options=list(EXCLUDED_REASONS),
            ),
            "eur_received": st.column_config.NumberColumn("EUR received", step=0.01, format="%.2f"),
            "payment_date": st.column_config.TextColumn("payment_date", validate=_ISO_DATE_RE),
            "subtotal_eur": st.column_config.NumberColumn(format="%.2f"),
            "iva_amount": st.column_config.NumberColumn(format="%.2f"),
            "total_eur": st.column_config.NumberColumn(format="%.2f"),
            "vat_treatment": st.column_config.TextColumn("legacy vat_treatment"),
            "vendor": st.column_config.TextColumn(
                "vendor", help="Vendor-registry match; ⚠ unknown → add it in the Vendors tab",
            ),
        },
    )
    edited_rows: dict = st.session_state.get(editor_key, {}).get("edited_rows", {})
    col_save, col_discard, col_info = st.columns([1, 1, 3])
    with col_info:
        st.caption(f"{len(edited_rows)} row(s) with pending edits")
    with col_discard:
        if st.button("Discard", key=f"ledger_discard_{direction}", disabled=not edited_rows):
            _bump_editor_version()
            st.rerun()
    with col_save:
        if st.button("Save changes", type="primary", key=f"ledger_save_{direction}",
                     disabled=not edited_rows):
            saved, errors = 0, []
            for pos, changes in edited_rows.items():
                invoice_id = view.index[int(pos)]
                try:
                    if update_invoice_fields(invoice_id, changes):
                        saved += 1
                except (ValueError, KeyError) as exc:
                    errors.append(f"{view.at[invoice_id, 'filename']}: {exc}")
            if saved:
                _flash("success", f"Saved {saved} invoice(s); edited fields are now locked.")
            for err in errors:
                log.warning("⚠️ Ledger bulk edit rejected — %s", err)
                _flash("error", err)
            _bump_editor_version()
            st.rerun()


def _render_edit_form(view: pd.DataFrame, records: dict[str, dict], direction: str) -> None:
    st.markdown("**Edit one invoice**")

    def _label(invoice_id: str) -> str:
        row = view.loc[invoice_id]
        total = row["total_eur"]
        total_txt = f"{total:,.2f}" if pd.notna(total) else "—"
        inv_date = row["invoice_date"] if pd.notna(row["invoice_date"]) else "no date"
        return f"{inv_date} · {row['counterparty'] or '—'} · {total_txt} · {row['filename']}"

    invoice_id = st.selectbox(
        "Invoice", list(view.index), format_func=_label, key=f"ledger_select_{direction}",
    )
    if invoice_id is None:
        return
    rec = records[invoice_id]
    locked = parse_locked_fields(rec.get("locked_fields"))
    st.caption(
        f"🔒 Locked: {', '.join(locked) if locked else 'none'} · "
        f"Reviewed: {rec.get('reviewed_at') or 'never'} · Legacy vat_treatment: {rec.get('vat_treatment') or '—'}"
    )
    k = f"ledger_{invoice_id}"
    treatments = [None, *_treatments(direction)]
    reasons = [None, *EXCLUDED_REASONS]

    with st.form(key=f"{k}_form"):
        c_doc, c_amt, c_tax = st.columns(3)
        with c_doc:
            st.markdown("*Document*")
            inv_date = st.date_input("Invoice date (accounting date)", value=_to_date(rec.get("invoice_date")),
                                     min_value=date(2000, 1, 1), format="YYYY-MM-DD", key=f"{k}_invoice_date")
            sup_date = st.date_input("Supply date (informational)", value=_to_date(rec.get("supply_date")),
                                     min_value=date(2000, 1, 1), format="YYYY-MM-DD", key=f"{k}_supply_date")
            changes: dict = {
                "invoice_number": st.text_input("Number", value=rec.get("invoice_number") or "",
                                                key=f"{k}_invoice_number"),
                "vendor_name": st.text_input("Vendor", value=rec.get("vendor_name") or "", key=f"{k}_vendor_name"),
                "vendor_nif": st.text_input("Vendor NIF / VAT id", value=rec.get("vendor_nif") or "",
                                            key=f"{k}_vendor_nif"),
                "client_name": st.text_input("Client", value=rec.get("client_name") or "", key=f"{k}_client_name"),
                "client_nif": st.text_input("Client NIF / VAT id", value=rec.get("client_nif") or "",
                                            key=f"{k}_client_nif"),
                "category": st.text_input("Category", value=rec.get("category") or "", key=f"{k}_category"),
                "description": st.text_area("Description", value=rec.get("description") or "",
                                            key=f"{k}_description"),
                "notes": st.text_area("Notes", value=rec.get("notes") or "", key=f"{k}_notes"),
            }
            changes["invoice_date"] = inv_date.isoformat() if inv_date else None
            changes["supply_date"] = sup_date.isoformat() if sup_date else None
        with c_amt:
            st.markdown("*Amounts (EUR unless noted)*")
            for field, label in (
                ("subtotal_eur", "Subtotal"), ("iva_rate", "IVA rate %"), ("iva_amount", "IVA amount"),
                ("irpf_rate", "IRPF rate %"), ("irpf_amount", "IRPF amount"), ("total_eur", "Total"),
                ("original_amount", "Original amount"), ("fx_rate", "FX rate"),
                ("eur_received", "EUR actually received"),
            ):
                changes[field] = st.number_input(label, value=rec.get(field), format="%.4f" if field == "fx_rate"
                                                 else "%.2f", key=f"{k}_{field}")
            changes["original_currency"] = st.text_input("Original currency", value=rec.get("original_currency") or "",
                                                         key=f"{k}_original_currency")
            pay_date = st.date_input("Payment date", value=_to_date(rec.get("payment_date")),
                                     min_value=date(2000, 1, 1), format="YYYY-MM-DD", key=f"{k}_payment_date")
            changes["payment_date"] = pay_date.isoformat() if pay_date else None
        with c_tax:
            st.markdown("*Tax treatment*")
            current_tt = rec.get("tax_treatment") if rec.get("tax_treatment") in treatments else None
            changes["tax_treatment"] = st.selectbox(
                "Tax treatment", treatments, index=treatments.index(current_tt),
                format_func=lambda v: v or "— (unset)", key=f"{k}_tax_treatment",
            )
            changes["deductible_pct_vat"] = st.number_input(
                "Business use — VAT %", min_value=0.0, max_value=100.0,
                value=float(rec.get("deductible_pct_vat") if rec.get("deductible_pct_vat") is not None else 100.0),
                key=f"{k}_deductible_pct_vat",
            )
            changes["deductible_pct_irpf"] = st.number_input(
                "Business use — IRPF %", min_value=0.0, max_value=100.0,
                value=float(rec.get("deductible_pct_irpf") if rec.get("deductible_pct_irpf") is not None else 100.0),
                key=f"{k}_deductible_pct_irpf",
            )
            changes["is_capital_asset"] = st.checkbox("Capital asset", value=bool(rec.get("is_capital_asset")),
                                                      key=f"{k}_is_capital_asset")
            changes["asset_class"] = st.text_input("Asset class", value=rec.get("asset_class") or "",
                                                   key=f"{k}_asset_class")
            changes["excluded"] = st.checkbox("Excluded from tax computations", value=bool(rec.get("excluded")),
                                              key=f"{k}_excluded")
            current_reason = rec.get("excluded_reason") if rec.get("excluded_reason") in reasons else None
            changes["excluded_reason"] = st.selectbox(
                "Exclusion reason", reasons, index=reasons.index(current_reason),
                format_func=lambda v: v or "—", key=f"{k}_excluded_reason",
            )
        submitted = st.form_submit_button("Save & mark reviewed", type="primary")

    if submitted:
        try:
            changed = update_invoice_fields(invoice_id, changes)
        except (ValueError, KeyError) as exc:
            log.warning("⚠️ Ledger edit rejected for %s — %s", rec.get("filename"), exc)
            st.error(str(exc))
        else:
            _flash("success", f"Saved; locked: {', '.join(changed)}." if changed
                   else "No field changed; invoice marked reviewed.")
            _bump_editor_version()
            st.rerun()

    if locked and st.button("Unlock all fields (next re-extract may overwrite them)", key=f"{k}_unlock"):
        unlock_invoice_fields(invoice_id)
        _flash("info", "All locks released; stored values are unchanged.")
        _bump_editor_version()
        st.rerun()


def render() -> None:
    """Render the Invoice Ledger tab."""
    st.subheader("Invoice Ledger — tax treatment and corrections")
    st.caption(
        "The accounting date is the **invoice date**: it decides the quarter. Supply date is informational. "
        "Any field you edit here is 🔒 locked and survives re-extraction in the Invoice OCR tab."
    )
    _show_flash()

    all_invoices = get_invoices()
    if not all_invoices:
        st.info("No invoices extracted yet — use the Invoice OCR tab first.")
        return

    f_dir, f_period, f_excl, f_rev = st.columns([2, 2, 1, 1])
    with f_dir:
        direction = st.radio(
            "Direction", ["in", "out"], horizontal=True, key="ledger_direction",
            format_func=lambda d: "Expenses (in)" if d == "in" else "Income (out)",
        )
    records = [r for r in all_invoices if r["direction"] == direction]
    if not records:
        st.info(f"No {'expense' if direction == 'in' else 'income'} invoices extracted yet.")
        return
    df = _build_frame(records, direction)
    periods = sorted({q for q in df["quarter"] if q}, reverse=True)
    with f_period:
        period = st.selectbox("Quarter (by invoice date)", [_ALL_PERIODS, *periods], key="ledger_period")
    with f_excl:
        show_excluded = st.checkbox("Show excluded", value=True, key="ledger_show_excluded")
    with f_rev:
        only_unreviewed = st.checkbox("Only unreviewed", value=False, key="ledger_only_unreviewed")

    view = df
    if period != _ALL_PERIODS:
        view = view[view["quarter"] == period]
    if not show_excluded:
        view = view[~view["excluded"]]
    if only_unreviewed:
        view = view[view["reviewed_at"].isna()]

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Invoices", len(view))
    m2.metric("Excluded", int(view["excluded"].sum()))
    m3.metric("No tax treatment", int(view["tax_treatment"].isna().sum()))
    m4.metric("Unreviewed", int(view["reviewed_at"].isna().sum()))
    n_unknown = int((view["vendor"] == UNKNOWN_VENDOR).sum()) if "vendor" in view else 0
    m5.metric("⚠ Unknown vendor", n_unknown if direction == "in" else "—")

    if view.empty:
        st.warning("No invoices match the current filters.")
        return

    filter_sig = f"{period}_{int(show_excluded)}_{int(only_unreviewed)}"
    _render_bulk_editor(view, direction, filter_sig)
    st.markdown("---")
    _render_edit_form(view, {r["id"]: r for r in records}, direction)
