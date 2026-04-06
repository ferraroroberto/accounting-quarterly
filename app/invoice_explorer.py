"""Invoice Explorer — filterable table of all extracted invoices."""
from __future__ import annotations

import json
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

    # ── Sidebar-style filters in an expander ─────────────────────────────────
    with st.expander("Filters", expanded=True):
        f1, f2, f3 = st.columns(3)

        # Direction
        directions = ["All"] + sorted(df["direction"].dropna().unique().tolist())
        direction_sel = f1.selectbox("Direction", directions)

        # Category
        cats = sorted(df["category"].dropna().unique().tolist()) if "category" in df.columns else []
        cat_sel = f2.multiselect("Category", cats)

        # Invoice type
        if "invoice_type" in df.columns:
            types = sorted(df["invoice_type"].dropna().unique().tolist())
            type_sel = f3.multiselect("Invoice type", types)
        else:
            type_sel = []

        f4, f5 = st.columns(2)

        # Vendor name (free text)
        vendor_q = f4.text_input("Vendor name contains")
        # Client name (free text)
        client_q = f5.text_input("Client name contains")

        f6, f7 = st.columns(2)

        # Date range
        min_date = df["invoice_date"].min() if "invoice_date" in df.columns and not df["invoice_date"].isna().all() else None
        max_date = df["invoice_date"].max() if "invoice_date" in df.columns and not df["invoice_date"].isna().all() else None

        if min_date is not None and not pd.isna(min_date):
            date_from = f6.date_input("Invoice date from", value=min_date.date(), min_value=min_date.date(), max_value=max_date.date())
            date_to = f7.date_input("Invoice date to", value=max_date.date(), min_value=min_date.date(), max_value=max_date.date())
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
                )
            else:
                sub_range = None
        else:
            sub_range = None

        # Rectificativas only
        show_rect_only = f9.checkbox("Rectificativas only")

    # ── Apply filters ─────────────────────────────────────────────────────────
    mask = pd.Series([True] * len(df), index=df.index)

    if direction_sel != "All":
        mask &= df["direction"] == direction_sel

    if cat_sel:
        mask &= df["category"].isin(cat_sel)

    if type_sel and "invoice_type" in df.columns:
        mask &= df["invoice_type"].isin(type_sel)

    if vendor_q.strip() and "vendor_name" in df.columns:
        mask &= df["vendor_name"].str.contains(vendor_q.strip(), case=False, na=False)

    if client_q.strip() and "client_name" in df.columns:
        mask &= df["client_name"].str.contains(client_q.strip(), case=False, na=False)

    if date_from and date_to and "invoice_date" in df.columns:
        mask &= (df["invoice_date"].dt.date >= date_from) & (df["invoice_date"].dt.date <= date_to)

    if sub_range is not None and "subtotal_eur" in df.columns:
        mask &= (df["subtotal_eur"] >= sub_range[0]) & (df["subtotal_eur"] <= sub_range[1])

    if show_rect_only and "is_rectificativa" in df.columns:
        mask &= df["is_rectificativa"].astype(bool)

    filtered = df[mask].copy()

    # ── Summary metrics ───────────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Matching records", len(filtered))
    in_f = filtered[filtered["direction"] == "in"] if "direction" in filtered.columns else pd.DataFrame()
    out_f = filtered[filtered["direction"] == "out"] if "direction" in filtered.columns else pd.DataFrame()
    m2.metric("Expenses (in)", len(in_f))
    m3.metric("Total expenses", f"€{in_f['total_eur'].sum(skipna=True):,.2f}" if "total_eur" in in_f.columns and not in_f.empty else "€0.00")
    m4.metric("Income (out)", len(out_f))
    m5.metric("Total income", f"€{out_f['total_eur'].sum(skipna=True):,.2f}" if "total_eur" in out_f.columns and not out_f.empty else "€0.00")

    st.markdown("---")

    if filtered.empty:
        st.warning("No records match the current filters.")
        return

    # ── Display columns ───────────────────────────────────────────────────────
    display_cols = [
        "direction", "filename", "extracted_at", "invoice_date",
        "invoice_type", "vendor_name", "vendor_nif",
        "client_name", "client_nif",
        "description", "subtotal_eur", "iva_rate", "iva_amount",
        "irpf_rate", "irpf_amount", "total_eur", "currency",
        "category", "payment_method",
        "supply_date", "due_date", "deductible_pct",
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
    )
