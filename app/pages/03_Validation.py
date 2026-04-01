"""Validation page – compares computed totals against historical known totals."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime

import pandas as pd
import streamlit as st

from src.config import load_config
from src.validator import format_validation_report, run_validation

st.set_page_config(page_title="Validation", page_icon="✅", layout="wide")
st.title("✅ Historical Validation")

cfg = load_config()
known = cfg.get("known_totals", {})

st.info(
    f"**Validation period:** {known.get('period', 'Jul 2023 – Dec 2025')}  \n"
    f"**Known total income:** €{known.get('total_income', 0):,.2f}  \n"
    f"Validates that the automation reproduces the exact same totals as the original Excel file."
)

if st.button("▶️ Run Validation", type="primary"):
    with st.spinner("Loading data and running validation..."):
        try:
            result, classified = run_validation(cfg=cfg)
            st.session_state["val_result"] = result
            st.session_state["val_classified"] = classified
        except Exception as exc:
            st.error(f"Validation failed: {exc}")
            st.stop()

result = st.session_state.get("val_result")
classified = st.session_state.get("val_classified")

if not result:
    st.markdown("Click **Run Validation** to start.")
    st.stop()

if result.passed:
    st.success(f"✅ VALIDATION PASSED — All {result.total_transactions} transactions match expected totals")
else:
    st.error(f"❌ VALIDATION FAILED — {len(result.discrepancies)} discrepancies found")

st.markdown("---")
st.subheader("📊 Grand Totals Comparison")

comparison_rows = [
    {"Metric": "Coaching Income", "Expected": result.coaching_expected, "Actual": result.coaching_actual,
     "Diff": round(result.coaching_actual - result.coaching_expected, 2)},
    {"Metric": "Newsletter Income", "Expected": result.newsletter_expected, "Actual": result.newsletter_actual,
     "Diff": round(result.newsletter_actual - result.newsletter_expected, 2)},
    {"Metric": "Illustrations Income", "Expected": result.illustrations_expected, "Actual": result.illustrations_actual,
     "Diff": round(result.illustrations_actual - result.illustrations_expected, 2)},
    {"Metric": "Total Income", "Expected": result.total_income_expected, "Actual": result.total_income_actual,
     "Diff": round(result.total_income_actual - result.total_income_expected, 2)},
]

def status_icon(diff: float) -> str:
    if abs(diff) <= 0.02:
        return "✓"
    elif abs(diff) < 1.0:
        return "⚠"
    return "✗"

for row in comparison_rows:
    row["Status"] = status_icon(row["Diff"])

df_comp = pd.DataFrame(comparison_rows)
st.dataframe(
    df_comp,
    width="stretch",
    hide_index=True,
    column_config={
        "Expected": st.column_config.NumberColumn(format="€ %.2f"),
        "Actual": st.column_config.NumberColumn(format="€ %.2f"),
        "Diff": st.column_config.NumberColumn(format="%.2f"),
    },
)

st.markdown("---")
st.subheader("🌍 Regional Breakdown")

regional_expected = result.regional_expected
regional_actual = result.regional_actual

region_labels = {"spain": "Spain", "eu_not_spain": "EU (not Spain)", "outside_eu": "Outside EU"}
activity_labels = ["coaching", "newsletter", "illustrations"]

regional_rows = []
for region_key, region_label in region_labels.items():
    exp = regional_expected.get(region_key, {})
    act_region_key = region_key.upper()
    act = regional_actual.get(act_region_key, {})
    for act_key in activity_labels:
        exp_val = exp.get(act_key, 0)
        act_val = round(act.get(act_key, 0), 2)
        diff = round(act_val - exp_val, 2)
        regional_rows.append({
            "Region": region_label,
            "Activity": act_key.capitalize(),
            "Expected €": exp_val,
            "Actual €": act_val,
            "Diff €": diff,
            "Status": status_icon(diff),
        })

df_regional = pd.DataFrame(regional_rows)
st.dataframe(
    df_regional,
    width="stretch",
    hide_index=True,
    column_config={
        "Expected €": st.column_config.NumberColumn(format="€ %.2f"),
        "Actual €": st.column_config.NumberColumn(format="€ %.2f"),
        "Diff €": st.column_config.NumberColumn(format="%.2f"),
    },
)

if result.discrepancies:
    st.markdown("---")
    st.subheader("⚠️ Discrepancies Detail")
    df_disc = pd.DataFrame(result.discrepancies)
    st.dataframe(df_disc, width="stretch", hide_index=True)

if result.unclassified_ids:
    st.markdown("---")
    st.subheader("❓ Unclassified Transactions")
    st.warning(f"{len(result.unclassified_ids)} transactions could not be auto-classified:")
    if classified:
        unclassified = [p for p in classified if p.id in set(result.unclassified_ids)]
        rows = []
        for p in unclassified:
            rows.append({
                "ID": p.id,
                "Date": p.created_date.strftime("%Y-%m-%d"),
                "Description": p.description or "(empty)",
                "Amount €": p.converted_amount,
                "Currency": p.currency.upper(),
            })
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

st.markdown("---")
st.subheader("📝 Full Validation Report")
report_md = format_validation_report(result)
with st.expander("View Markdown Report"):
    st.markdown(report_md)

if st.download_button("📥 Download Report (.md)", report_md, "validation_report.md", "text/markdown"):
    pass

st.markdown("---")
st.subheader("📈 Summary Statistics")
s1, s2, s3, s4 = st.columns(4)
s1.metric("Total Transactions", result.total_transactions)
s2.metric("Classification Errors", result.classification_errors)
s3.metric("Geo Errors", result.geo_errors)
s4.metric("Unclassified", len(result.unclassified_ids))
