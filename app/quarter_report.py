"""Quarter Report tab content."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime
from io import BytesIO

import pandas as pd
import streamlit as st

from app.data_loader import get_classified_for_period, quarter_dates
from src.aggregator import (
    build_monthly_table,
    calculate_grand_totals,
    calculate_regional_totals,
    get_transaction_count,
)
from src.classifier import validate_classifications
from src.excel_exporter import create_excel_report, generate_report_filename


def render():
    """Render the Quarter Report tab."""
    col1, col2, col3 = st.columns([1, 1, 2])
    current_year = datetime.now().year
    with col1:
        year = st.selectbox(
            "Year",
            list(range(2023, current_year + 2)),
            index=list(range(2023, current_year + 2)).index(current_year),
            key="qr_year",
        )
    with col2:
        quarter_opt = st.radio("Quarter", ["Q1", "Q2", "Q3", "Q4", "Full Year"], horizontal=True, key="qr_quarter")
    with col3:
        use_custom = st.checkbox("Custom date range", key="qr_custom")
        if use_custom:
            c1, c2 = st.columns(2)
            custom_start = c1.date_input("From", datetime(year, 1, 1), key="qr_from")
            custom_end = c2.date_input("To", datetime(year, 12, 31), key="qr_to")
            start_dt = datetime.combine(custom_start, datetime.min.time())
            end_dt = datetime.combine(custom_end, datetime.max.time().replace(microsecond=0))
            quarter = None
        else:
            quarter = None if quarter_opt == "Full Year" else int(quarter_opt[1])
            if quarter:
                start_dt, end_dt = quarter_dates(year, quarter)
            else:
                start_dt, end_dt = datetime(year, 1, 1), datetime(year, 12, 31, 23, 59, 59)

    load_btn = st.button("Load Data", type="primary", key="qr_load")
    if load_btn or "quarter_data" not in st.session_state or st.session_state.get("quarter_key") != (year, quarter_opt):
        with st.spinner("Loading and classifying payments..."):
            payments = get_classified_for_period(year, quarter, start_dt, end_dt)
            st.session_state["quarter_data"] = payments
            st.session_state["quarter_key"] = (year, quarter_opt)
            st.session_state["quarter_year"] = year
            st.session_state["quarter_q"] = quarter

    payments = st.session_state.get("quarter_data", [])

    if not payments:
        st.warning("No payments found for the selected period. Check your CSV files and date range.")
        return

    grand = calculate_grand_totals(payments)
    regional = calculate_regional_totals(payments)
    counts = get_transaction_count(payments)
    val_report = validate_classifications(payments)

    st.markdown("---")

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Total Income", f"{grand.get('total_income', 0):,.2f} EUR")
    m2.metric("Total Fees", f"{grand.get('total_fee', 0):,.2f} EUR")
    net = round(grand.get("total_income", 0) - grand.get("total_fee", 0), 2)
    m3.metric("Net Income", f"{net:,.2f} EUR")
    m4.metric("Transactions", counts.get("total", 0))

    st.markdown("---")
    st.subheader("Geographic Breakdown")

    rc1, rc2, rc3 = st.columns(3)
    for col_widget, region_key, label in [
        (rc1, "SPAIN", "Spain"),
        (rc2, "EU_NOT_SPAIN", "EU (not Spain)"),
        (rc3, "OUTSIDE_EU", "Outside EU"),
    ]:
        r = regional.get(region_key, {})
        income = r.get("total_income", 0)
        fees = r.get("total_fee", 0)
        count = counts.get(region_key.lower(), 0)
        col_widget.metric(label, f"{income:,.2f} EUR", f"Fees: {fees:,.2f} EUR | {count} tx")

    st.markdown("---")
    st.subheader("Monthly Breakdown")

    geo_tab_labels = ["Spain", "EU (not Spain)", "Outside EU"]
    geo_tab_keys = ["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"]

    tabs = st.tabs(geo_tab_labels)
    for tab, geo_key in zip(tabs, geo_tab_keys):
        with tab:
            table_rows = build_monthly_table(payments, geo_key, year, quarter)
            if table_rows:
                df = pd.DataFrame(table_rows)
                total_row = df[df["Month"] == "TOTAL"]
                data_rows = df[df["Month"] != "TOTAL"]

                st.dataframe(
                    data_rows,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "Coaching": st.column_config.NumberColumn("Coaching EUR", format="%.2f"),
                        "Newsletter": st.column_config.NumberColumn("Newsletter EUR", format="%.2f"),
                        "Illustrations": st.column_config.NumberColumn("Illustrations EUR", format="%.2f"),
                        "Total Income": st.column_config.NumberColumn("Total Income EUR", format="%.2f"),
                        "Coaching Fee": st.column_config.NumberColumn("Coaching Fee EUR", format="%.2f"),
                        "Newsletter Fee": st.column_config.NumberColumn("Newsletter Fee EUR", format="%.2f"),
                        "Illustrations Fee": st.column_config.NumberColumn("Illust. Fee EUR", format="%.2f"),
                        "Total Fee": st.column_config.NumberColumn("Total Fee EUR", format="%.2f"),
                    },
                )

                if not total_row.empty:
                    t = total_row.iloc[0]
                    st.markdown(
                        f"**Total:** Income {t['Total Income']:,.2f} EUR | "
                        f"Coaching {t['Coaching']:,.2f} | "
                        f"Newsletter {t['Newsletter']:,.2f} | "
                        f"Illustrations {t['Illustrations']:,.2f} | "
                        f"Fees {t['Total Fee']:,.2f}"
                    )
            else:
                st.info("No data for this region in the selected period.")

    st.markdown("---")

    val_col, export_col = st.columns([1, 1])

    with val_col:
        st.subheader("Classification Status")
        if val_report["activity_errors"] == 0 and val_report["unknown_activity"] == 0:
            st.success("All classifications valid")
        else:
            st.warning(
                f"{val_report['activity_errors']} activity errors | "
                f"{val_report['unknown_activity']} unclassified"
            )
            if st.button("Show details", key="qr_val_details"):
                st.json(val_report)

    with export_col:
        st.subheader("Export")
        label = f"Q{quarter}_{year}" if quarter else str(year)
        filename = generate_report_filename(year, quarter)

        if st.button("Generate Excel Report", type="primary", key="qr_export"):
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp_path = tmp.name
            create_excel_report(payments, tmp_path, year, quarter, label)
            with open(tmp_path, "rb") as f:
                excel_bytes = f.read()
            os.unlink(tmp_path)
            st.download_button(
                label=f"Download {filename}",
                data=excel_bytes,
                file_name=filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="qr_download",
            )
