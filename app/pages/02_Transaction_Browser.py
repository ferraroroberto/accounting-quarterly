"""Transaction Browser page."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime

import pandas as pd
import streamlit as st

from app.components.data_loader import get_classified_for_period, quarter_dates
from src.config import load_config, save_config
from src.models import ClassifiedPayment

st.set_page_config(page_title="Transaction Browser", page_icon="🔍", layout="wide")
st.title("🔍 Transaction Browser")

cfg = load_config()

with st.sidebar:
    st.subheader("📅 Date Range")
    current_year = datetime.now().year
    year = st.selectbox("Year", list(range(2023, current_year + 2)), index=list(range(2023, current_year + 2)).index(current_year))
    quarter_opt = st.radio("Quarter", ["Q1", "Q2", "Q3", "Q4", "Full Year"], horizontal=False)
    quarter = None if quarter_opt == "Full Year" else int(quarter_opt[1])
    if quarter:
        start_dt, end_dt = quarter_dates(year, quarter)
    else:
        start_dt, end_dt = datetime(year, 1, 1), datetime(year, 12, 31, 23, 59, 59)

    if st.button("🔄 Load", type="primary"):
        with st.spinner("Loading..."):
            payments = get_classified_for_period(year, quarter, start_dt, end_dt)
            st.session_state["browser_data"] = payments

payments: list[ClassifiedPayment] = st.session_state.get("browser_data", [])
if not payments:
    with st.spinner("Loading..."):
        payments = get_classified_for_period(year, quarter, start_dt, end_dt)
        st.session_state["browser_data"] = payments

if not payments:
    st.warning("No payments found for the selected period.")
    st.stop()

with st.sidebar:
    st.markdown("---")
    st.subheader("🔎 Filters")
    search_desc = st.text_input("Search description", "")
    activity_filter = st.selectbox("Activity Type", ["All", "COACHING", "NEWSLETTER", "ILLUSTRATIONS", "UNKNOWN"])
    geo_filter = st.selectbox("Geography", ["All", "SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"])

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
        "Amount €": p.converted_amount,
        "Refunded €": p.converted_amount_refunded,
        "Fee €": p.fee,
        "Currency": p.currency.upper(),
        "Rule": p.classification_rule,
        "Geo Rule": p.geo_rule,
    })

df = pd.DataFrame(rows)

st.dataframe(
    df,
    width="stretch",
    hide_index=True,
    column_config={
        "Amount €": st.column_config.NumberColumn(format="%.2f"),
        "Refunded €": st.column_config.NumberColumn(format="%.2f"),
        "Fee €": st.column_config.NumberColumn(format="%.2f"),
    },
)

st.markdown("---")
st.subheader("✏️ Add Geographic Override")

with st.form("add_override"):
    oc1, oc2, oc3 = st.columns(3)
    override_key = oc1.text_input("Client name / email / keyword", help="Substring match applied to description or email")
    override_region = oc2.selectbox("Region", ["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"])
    override_type = oc3.selectbox("Match on", ["Name/Description", "Email"])
    submitted = st.form_submit_button("➕ Add Override")

    if submitted and override_key.strip():
        cfg = load_config()
        key = override_key.strip().lower()
        if override_type == "Email":
            cfg.setdefault("email_overrides", {})[key] = override_region
        else:
            cfg.setdefault("geographic_overrides", {})[key] = override_region
        save_config(cfg)
        st.success(f"Override added: {key!r} → {override_region}")
        st.cache_data.clear()

st.markdown("---")
st.subheader("📋 Classification Rules Reference")

c1, c2 = st.columns(2)
with c1:
    st.markdown("""
**Activity Detection:**
| Pattern | Activity |
|---------|----------|
| `calendly`, `coach`, `discovery session`, `consulting` | COACHING |
| Luma `registration` payment type | COACHING |
| `master virtual meetings` | COACHING |
| `subscription` | NEWSLETTER |
| `charge for` | ILLUSTRATIONS |
| *(empty description)* | COACHING |
""")
with c2:
    cfg = load_config()
    geo_ov = cfg.get("geographic_overrides", {})
    st.markdown("**Known Geographic Overrides:**")
    ov_rows = [{"Key": k, "Region": v} for k, v in list(geo_ov.items())[:20]]
    if ov_rows:
        st.dataframe(pd.DataFrame(ov_rows), width="stretch", hide_index=True)
