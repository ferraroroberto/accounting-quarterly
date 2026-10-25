"""Invoice Explorer — filterable table of all extracted invoices."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pandas as pd
import streamlit as st

from src.database import get_invoices


@st.cache_data(ttl=300, show_spinner=False)
def _load_invoices_df() -> pd.DataFrame:
    """Load all invoices from DB and normalise types — cached for 5 minutes.

    The heavy work (SQL query + type conversions) happens once; every filter
    interaction in the UI reuses this DataFrame without touching the database.
    """
    all_invoices = get_invoices()
    if not all_invoices:
        return pd.DataFrame()

    df = pd.DataFrame(all_invoices)

    if "invoice_date" in df.columns:
        df["invoice_date"] = pd.to_datetime(df["invoice_date"], errors="coerce")
    if "extracted_at" in df.columns:
        df["extracted_at"] = pd.to_datetime(df["extracted_at"], errors="coerce")
    for num_col in ("subtotal_eur", "iva_amount", "irpf_amount", "total_eur"):
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors="coerce")

    return df


@dataclass
class _Filters:
    """The explorer's filter selections, as read from the filter widgets."""

    direction: str
    categories: list[str]
    invoice_types: list[str]
    vendor_query: str
    client_query: str
    date_from: date | None
    date_to: date | None
    subtotal_range: tuple[float, float] | None
    rectificativas_only: bool


def render() -> None:
    """Render the Invoice Explorer tab."""
    st.subheader("Invoice Explorer")
    st.caption("Browse, filter and export all OCR-extracted invoices.")

    col_cap, col_btn = st.columns([5, 1])
    with col_btn:
        if st.button("↺ Refresh", key="inv_refresh", help="Reload invoices from the database"):
            _load_invoices_df.clear()

    df = _load_invoices_df()
    if df.empty:
        st.info("No invoices extracted yet. Use the **Invoice OCR** tab to extract invoices first.")
        return

    filtered = _apply_filters(df, _render_filters(df))
    _render_metrics(filtered)

    st.markdown("---")

    if filtered.empty:
        st.warning("No records match the current filters.")
        return

    _render_table_and_export(filtered)


def _render_filters(df: pd.DataFrame) -> _Filters:
    """Filter widgets in an expander; returns the selections."""
    with st.expander("Filters", expanded=True):
        f1, f2, f3 = st.columns(3)

        # Direction
        directions = ["All"] + sorted(df["direction"].dropna().unique().tolist())
        direction_sel = f1.selectbox("Direction", directions, key="inv_explorer_direction")

        # Category
        cats = sorted(df["category"].dropna().unique().tolist()) if "category" in df.columns else []
        cat_sel = f2.multiselect("Category", cats, key="inv_explorer_cat")

        # Invoice type
        if "invoice_type" in df.columns:
            types = sorted(df["invoice_type"].dropna().unique().tolist())
            type_sel = f3.multiselect("Invoice type", types, key="inv_explorer_type")
        else:
            type_sel = []

        f4, f5 = st.columns(2)

        # Vendor name (free text)
        vendor_q = f4.text_input("Vendor name contains", key="inv_explorer_vendor")
        # Client name (free text)
        client_q = f5.text_input("Client name contains", key="inv_explorer_client")

        f6, f7 = st.columns(2)

        # Date range
        min_date = df["invoice_date"].min() if "invoice_date" in df.columns and not df["invoice_date"].isna().all() else None
        max_date = df["invoice_date"].max() if "invoice_date" in df.columns and not df["invoice_date"].isna().all() else None

        if min_date is not None and not pd.isna(min_date):
            date_from = f6.date_input("Invoice date from", value=min_date.date(), min_value=min_date.date(), max_value=max_date.date(), key="inv_explorer_date_from")
            date_to = f7.date_input("Invoice date to", value=max_date.date(), min_value=min_date.date(), max_value=max_date.date(), key="inv_explorer_date_to")
        else:
            date_from = None
            date_to = None

        f8, f9 = st.columns(2)
        # Subtotal range
        if "subtotal_eur" in df.columns and df["subtotal_eur"].notna().any():
            min_sub = float(df["subtotal_eur"].min(skipna=True) or 0)
            max_sub = float(df["subtotal_eur"].max(skipna=True) or 0)
            if min_sub < max_sub:
                sub_range = f8.slider(
                    "Subtotal EUR range",
                    min_value=min_sub,
                    max_value=max_sub,
                    value=(min_sub, max_sub),
                    step=1.0,
                    key="inv_explorer_subtotal",
                )
            else:
                sub_range = None
        else:
            sub_range = None

        # Rectificativas only
        show_rect_only = f9.checkbox("Rectificativas only", key="inv_explorer_rectificativas")

    return _Filters(
        direction=direction_sel,
        categories=cat_sel,
        invoice_types=type_sel,
        vendor_query=vendor_q,
        client_query=client_q,
        date_from=date_from,
        date_to=date_to,
        subtotal_range=sub_range,
        rectificativas_only=show_rect_only,
    )


def _apply_filters(df: pd.DataFrame, f: _Filters) -> pd.DataFrame:
    """Rows of `df` matching every active filter."""
    mask = pd.Series([True] * len(df), index=df.index)

    if f.direction != "All":
        mask &= df["direction"] == f.direction

    if f.categories:
        mask &= df["category"].isin(f.categories)

    if f.invoice_types and "invoice_type" in df.columns:
        mask &= df["invoice_type"].isin(f.invoice_types)

    if f.vendor_query.strip() and "vendor_name" in df.columns:
        mask &= df["vendor_name"].str.contains(f.vendor_query.strip(), case=False, na=False)

    if f.client_query.strip() and "client_name" in df.columns:
        mask &= df["client_name"].str.contains(f.client_query.strip(), case=False, na=False)

    if f.date_from and f.date_to and "invoice_date" in df.columns:
        mask &= (df["invoice_date"].dt.date >= f.date_from) & (df["invoice_date"].dt.date <= f.date_to)

    if f.subtotal_range is not None and "subtotal_eur" in df.columns:
        mask &= (df["subtotal_eur"] >= f.subtotal_range[0]) & (df["subtotal_eur"] <= f.subtotal_range[1])

    if f.rectificativas_only and "is_rectificativa" in df.columns:
        mask &= df["is_rectificativa"].astype(bool)

    return df[mask].copy()


def _render_metrics(filtered: pd.DataFrame) -> None:
    """Summary metrics for the filtered rows."""
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Matching records", len(filtered))
    in_f = filtered[filtered["direction"] == "in"] if "direction" in filtered.columns else pd.DataFrame()
    out_f = filtered[filtered["direction"] == "out"] if "direction" in filtered.columns else pd.DataFrame()
    m2.metric("Expenses (in)", len(in_f))
    m3.metric("Total expenses", f"€{in_f['total_eur'].sum(skipna=True):,.2f}" if "total_eur" in in_f.columns and not in_f.empty else "€0.00")
    m4.metric("Income (out)", len(out_f))
    m5.metric("Total income", f"€{out_f['total_eur'].sum(skipna=True):,.2f}" if "total_eur" in out_f.columns and not out_f.empty else "€0.00")


def _render_table_and_export(filtered: pd.DataFrame) -> None:
    """Result table plus the filtered-CSV download."""
    display_cols = [
        "direction", "filename", "extracted_at", "invoice_date",
        "invoice_type", "vendor_name", "vendor_nif",
        "client_name", "client_nif",
        "description", "subtotal_eur", "iva_rate", "iva_amount",
        "irpf_rate", "irpf_amount", "total_eur", "currency",
        "category", "payment_method",
        "supply_date", "due_date", "tax_treatment",
        "deductible_pct_vat", "deductible_pct_irpf", "excluded", "excluded_reason",
        "is_rectificativa", "vat_exempt_reason", "notes",
    ]
    visible = [c for c in display_cols if c in filtered.columns]
    display_df = filtered[visible].rename(columns={"extracted_at": "date_scanned"})

    st.dataframe(display_df, width="stretch", hide_index=True)

    # ── Export ────────────────────────────────────────────────────────────────
    csv = display_df.to_csv(index=False).encode()
    st.download_button(
        "Download filtered CSV",
        data=csv,
        file_name="invoices_filtered.csv",
        mime="text/csv",
        key="inv_explorer_download",
    )
