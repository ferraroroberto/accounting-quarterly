"""Transaction Browser tab content."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from app.data_loader import get_classified_for_period, invalidate_cache, quarter_dates
from src.models import ClassifiedPayment
from src.rules_engine import load_rules, save_rules


def render():
    """Render the Transaction Browser tab."""
    col1, col2, col3 = st.columns([1, 1, 2])
    current_year = datetime.now().year
    with col1:
        year = st.selectbox(
            "Year",
            list(range(2023, current_year + 2)),
            index=list(range(2023, current_year + 2)).index(current_year),
            key="tb_year",
        )
    with col2:
        quarter_opt = st.radio("Quarter", ["Q1", "Q2", "Q3", "Q4", "Full Year"], horizontal=True, key="tb_quarter")
        quarter = None if quarter_opt == "Full Year" else int(quarter_opt[1])
    with col3:
        search_desc = st.text_input("Search description", "", key="tb_search")
        fc1, fc2 = st.columns(2)
        activity_filter = fc1.selectbox("Activity Type", ["All", "COACHING", "NEWSLETTER", "ILLUSTRATIONS", "UNKNOWN"], key="tb_activity")
        geo_filter = fc2.selectbox("Geography", ["All", "SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"], key="tb_geo")

    if quarter:
        start_dt, end_dt = quarter_dates(year, quarter)
    else:
        start_dt, end_dt = datetime(year, 1, 1), datetime(year, 12, 31, 23, 59, 59)

    if st.button("Load", type="primary", key="tb_load") or "browser_data" not in st.session_state:
        with st.spinner("Loading..."):
            payments = get_classified_for_period(year, quarter, start_dt, end_dt)
            st.session_state["browser_data"] = payments

    payments: list[ClassifiedPayment] = st.session_state.get("browser_data", [])

    if not payments:
        st.warning("No payments found for the selected period.")
        return

    filtered = payments
    if search_desc:
        filtered = [p for p in filtered if search_desc.lower() in p.description.lower()]
    if activity_filter != "All":
        filtered = [p for p in filtered if p.activity_type == activity_filter]
    if geo_filter != "All":
        filtered = [p for p in filtered if p.geo_region == geo_filter]

    st.markdown(f"**{len(filtered)} transactions** (of {len(payments)} total)")

    rows = []
    for p in filtered:
        rows.append({
            "Date": p.created_date.strftime("%Y-%m-%d"),
            "ID": p.id,
            "Description": p.description[:80] if p.description else "(empty)",
            "Activity Type": p.activity_type,
            "Geography": p.geo_region,
            "Amount EUR": p.converted_amount,
            "Refunded EUR": p.converted_amount_refunded,
            "Fee EUR": p.fee,
            "Currency": p.currency.upper(),
            "Rule": p.classification_rule,
            "Geo Rule": p.geo_rule,
        })

    df = pd.DataFrame(rows)

    st.dataframe(
        df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Amount EUR": st.column_config.NumberColumn(format="%.2f"),
            "Refunded EUR": st.column_config.NumberColumn(format="%.2f"),
            "Fee EUR": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    st.markdown("---")
    st.subheader("Add Geographic Override")

    with st.form("add_override"):
        oc1, oc2, oc3 = st.columns(3)
        override_key = oc1.text_input("Client name / email / keyword", help="Substring match applied to description or email")
        override_region = oc2.selectbox("Region", ["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"])
        override_type = oc3.selectbox("Match on", ["Name/Description", "Email"])
        submitted = st.form_submit_button("Add Override")

        if submitted and override_key.strip():
            rules = load_rules()
            geo = rules.setdefault("geographic_rules", {})
            key = override_key.strip().lower()
            if override_type == "Email":
                geo.setdefault("email_overrides", {})[key] = override_region
            else:
                geo.setdefault("geographic_overrides", {})[key] = override_region
            save_rules(rules)
            invalidate_cache()
            st.success(f"Override added: {key!r} -> {override_region}")
