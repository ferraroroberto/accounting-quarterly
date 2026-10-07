"""Vendors tab — the vendor registry (#91): edit, import, apply, review unknown vendors.

The registry file (``vendors.json``, git-ignored) is the source of truth; all logic
lives in ``src.vendor_registry``.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

from app.flash import flash, show_flash
from src.database import TAX_TREATMENTS_IN
from src.logger import get_logger
from src.vendor_registry import (
    ACTIVITIES,
    ACTIVITY_IAE,
    VendorRegistry,
    apply_vendor_registry,
    find_unmatched_invoices,
    load_registry,
    registry_path,
    save_registry,
    seed_from_xlsx,
)

log = get_logger(__name__)

_COLUMNS = [
    "key", "aliases", "legal_entity", "country", "vat_id", "alt_vat_ids",
    "default_tax_treatment", "default_deductible_pct_vat", "default_deductible_pct_irpf",
    "activity", "asset_class", "recurrence", "notes",
]
_LIST_COLUMNS = ("aliases", "alt_vat_ids")


def _editor_version() -> int:
    return st.session_state.setdefault("vendors_editor_version", 0)


def _to_frame(registry: VendorRegistry) -> pd.DataFrame:
    rows = []
    for vendor in registry.vendors:
        row = vendor.to_dict()
        for col in _LIST_COLUMNS:
            row[col] = ", ".join(row[col])
        rows.append(row)
    return pd.DataFrame(rows, columns=_COLUMNS)


def _from_frame(df: pd.DataFrame) -> VendorRegistry:
    """Editor grid → validated registry (raises ValueError on invalid rows)."""
    clean = df.astype(object).where(df.notna(), None)
    records = [r for r in clean.to_dict("records") if any(v not in (None, "") for v in r.values())]
    return VendorRegistry.from_dict({"vendors": records})


def _render_editor(registry: VendorRegistry) -> None:
    st.markdown("**Registry**")
    st.caption(
        "One row per vendor. `key` is the normalised vendor name and should equal the vendor's "
        "sub-folder under the invoices-in directory (the first matching signal); `aliases` and "
        "`alt_vat_ids` are comma-separated. Empty defaults leave the invoice's own value alone."
    )
    edited = st.data_editor(
        _to_frame(registry),
        key=f"vendors_editor_{_editor_version()}",
        width="stretch",
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "country": st.column_config.TextColumn("country", validate=r"^[A-Z]{2}$", help="ISO-2"),
            "default_tax_treatment": st.column_config.SelectboxColumn(
                "tax treatment", options=list(TAX_TREATMENTS_IN),
            ),
            "default_deductible_pct_vat": st.column_config.NumberColumn(
                "VAT %", min_value=0, max_value=100, step=0.01, help="Business-use share, VAT deduction",
            ),
            "default_deductible_pct_irpf": st.column_config.NumberColumn(
                "IRPF %", min_value=0, max_value=100, step=0.01, help="Business-use share, IRPF expense",
            ),
            "activity": st.column_config.SelectboxColumn(
                "activity", options=list(ACTIVITIES),
                help=" · ".join(f"{a} = IAE {iae}" for a, iae in ACTIVITY_IAE.items()),
            ),
        },
    )
    if st.button("Save registry", type="primary", key="vendors_save"):
        try:
            new_registry = _from_frame(edited)
        except ValueError as exc:
            st.error(f"Registry not saved: {exc}")
            return
        save_registry(new_registry)
        flash("vendors", "success", f"Saved {len(new_registry.vendors)} vendor(s) to {registry_path().name}. "
                          "Apply the registry to update stored invoices.")
        st.session_state["vendors_editor_version"] = _editor_version() + 1
        st.rerun()


def _render_apply(registry: VendorRegistry) -> None:
    st.markdown("**Apply to stored expense invoices**")
    st.caption(
        "Writes the registry defaults (treatment, business-use %, activity, asset class) onto matched "
        "expense invoices of every period, filed ones included, and fills a missing VAT id, region and country. 🔒 Locked fields are never "
        "touched, and nothing gets locked. New OCR extractions apply the registry automatically."
    )
    if st.button("Apply registry", key="vendors_apply", disabled=not registry.vendors):
        result = apply_vendor_registry(registry)
        flash("vendors", "success",
               f"Matched {result.matched} of {result.scanned} expense invoice(s) "
               f"({', '.join(f'{k}: {v}' for k, v in result.by_signal.items()) or 'none'}); "
               f"updated {result.rows_updated}; {result.unmatched} unknown vendor(s)."
               + (f" Updated per period: {result.period_summary()}." if result.rows_updated else ""))
        st.rerun()


def _render_unmatched(registry: VendorRegistry) -> None:
    st.markdown("**⚠ Unknown vendors**")
    unmatched = find_unmatched_invoices(registry)
    if not unmatched:
        st.success("Every (non-excluded) expense invoice matches a registry vendor.")
        return
    df = pd.DataFrame(unmatched)
    st.warning(f"{len(df)} expense invoice(s) match no registry vendor — their tax treatment is a guess.")
    by_key = (df.groupby(df["suggested_key"].fillna("?")).size()
              .rename("invoices").reset_index().sort_values("invoices", ascending=False))
    st.dataframe(by_key, width="stretch", hide_index=True, key="vendors_unmatched_summary")
    cols = ["suggested_key", "filename", "vendor_name", "vendor_nif", "invoice_date", "total_eur", "tax_treatment"]
    st.dataframe(df[[c for c in cols if c in df.columns]], width="stretch", hide_index=True,
                 key="vendors_unmatched_rows")


def _render_import(registry: VendorRegistry) -> None:
    st.markdown("**Import from a spreadsheet**")
    st.caption(
        "An .xlsx whose first sheet has a vendor column (`vendor` / `item` / `name`) and optionally "
        "`activity` (or `business`) and `recurrence` (or `recurrency`). New vendors are added; existing "
        "vendors only get an empty activity / recurrence filled."
    )
    upload = st.file_uploader("Vendor spreadsheet", type=["xlsx"], key="vendors_import_file")
    if upload is not None and st.button("Merge into registry", key="vendors_import"):
        try:
            merged, res = seed_from_xlsx(upload, registry)
        except ValueError as exc:
            st.error(str(exc))
            return
        save_registry(merged)
        flash("vendors", "success", f"Imported: {len(res.added)} added, {len(res.updated)} updated, "
                          f"{len(res.unchanged)} unchanged.")
        st.session_state["vendors_editor_version"] = _editor_version() + 1
        st.rerun()


def render() -> None:
    """Render the Vendors tab."""
    st.subheader("Vendor registry — per-vendor tax defaults")
    show_flash("vendors")
    try:
        registry = load_registry()
    except (ValueError, TypeError) as exc:
        st.error(f"{registry_path().name} is invalid: {exc}")
        return
    if not registry_path().exists():
        st.info(f"No `{registry_path().name}` yet — start from `vendors.json.example`, import a spreadsheet "
                "below, or add rows and save.")
    st.caption(f"Source of truth: `{registry_path().name}` (git-ignored) · {len(registry.vendors)} vendor(s). "
               "Matching order: invoice sub-folder → VAT id → vendor name.")

    _render_editor(registry)
    st.markdown("---")
    _render_apply(registry)
    st.markdown("---")
    _render_unmatched(registry)
    st.markdown("---")
    _render_import(registry)
