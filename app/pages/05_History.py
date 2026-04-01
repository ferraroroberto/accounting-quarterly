"""History page – timeline of all quarters."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime
from io import BytesIO

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.components.data_loader import get_classified_for_period, quarter_dates
from src.aggregator import calculate_grand_totals, calculate_regional_totals, get_transaction_count
from src.config import load_config
from src.excel_exporter import create_excel_report, generate_report_filename

st.set_page_config(page_title="History", page_icon="📈", layout="wide")
st.title("📈 Income History")

cfg = load_config()

current_year = datetime.now().year

all_quarters = []
for y in range(2023, current_year + 1):
    start_q = 3 if y == 2023 else 1
    end_q = datetime.now().month // 3 if y == current_year else 4
    for q in range(start_q, end_q + 1):
        all_quarters.append((y, q))

all_quarters.reverse()

if "history_data" not in st.session_state:
    st.info("Click **Load History** to compute all quarterly summaries.")
    load_history = st.button("📊 Load History", type="primary")
else:
    load_history = st.button("🔄 Refresh History", type="secondary")

if load_history:
    progress = st.progress(0, text="Loading quarters...")
    history = {}
    for i, (y, q) in enumerate(all_quarters):
        s, e = quarter_dates(y, q)
        label = f"Q{q} {y}"
        try:
            payments = get_classified_for_period(y, q, s, e)
            grand = calculate_grand_totals(payments)
            regional = calculate_regional_totals(payments)
            counts = get_transaction_count(payments)
            history[label] = {
                "year": y, "quarter": q,
                "start": s.date().isoformat(),
                "end": e.date().isoformat(),
                "total_income": round(grand.get("total_income", 0), 2),
                "total_fee": round(grand.get("total_fee", 0), 2),
                "coaching": round(grand.get("coaching", 0), 2),
                "newsletter": round(grand.get("newsletter", 0), 2),
                "illustrations": round(grand.get("illustrations", 0), 2),
                "transactions": counts.get("total", 0),
                "regional": regional,
                "payments": payments,
            }
        except Exception as exc:
            history[label] = {"year": y, "quarter": q, "error": str(exc)}
        progress.progress((i + 1) / len(all_quarters), text=f"Loaded {label}")
    st.session_state["history_data"] = history
    progress.empty()
    st.success(f"Loaded {len(all_quarters)} quarters")

history = st.session_state.get("history_data", {})
if not history:
    st.stop()

valid_quarters = {k: v for k, v in history.items() if "error" not in v}

if valid_quarters:
    st.subheader("📊 Quarterly Summary Table")

    table_rows = []
    for label, d in sorted(valid_quarters.items(), reverse=True):
        table_rows.append({
            "Quarter": label,
            "Period": f"{d['start']} → {d['end']}",
            "Coaching €": d["coaching"],
            "Newsletter €": d["newsletter"],
            "Illustrations €": d["illustrations"],
            "Total Income €": d["total_income"],
            "Total Fees €": d["total_fee"],
            "Transactions": d["transactions"],
        })

    df_summary = pd.DataFrame(table_rows)
    st.dataframe(
        df_summary,
        width="stretch",
        hide_index=True,
        column_config={
            "Coaching €": st.column_config.NumberColumn(format="€ %.2f"),
            "Newsletter €": st.column_config.NumberColumn(format="€ %.2f"),
            "Illustrations €": st.column_config.NumberColumn(format="€ %.2f"),
            "Total Income €": st.column_config.NumberColumn(format="€ %.2f"),
            "Total Fees €": st.column_config.NumberColumn(format="€ %.2f"),
        },
    )

    st.markdown("---")
    st.subheader("📈 Income Trends")

    sorted_labels = sorted(valid_quarters.keys())
    sorted_data = [valid_quarters[l] for l in sorted_labels]

    fig = go.Figure()
    fig.add_trace(go.Bar(name="Coaching", x=sorted_labels, y=[d["coaching"] for d in sorted_data], marker_color="#2D4A7A"))
    fig.add_trace(go.Bar(name="Newsletter", x=sorted_labels, y=[d["newsletter"] for d in sorted_data], marker_color="#5B8DBE"))
    fig.add_trace(go.Bar(name="Illustrations", x=sorted_labels, y=[d["illustrations"] for d in sorted_data], marker_color="#9CB8D8"))
    fig.update_layout(
        barmode="stack",
        xaxis_title="Quarter",
        yaxis_title="Income (€)",
        legend_title="Activity",
        height=400,
        plot_bgcolor="white",
    )
    st.plotly_chart(fig, width="stretch")

    st.subheader("🌍 Geographic Distribution")
    fig2 = go.Figure()
    for region_key, region_label, color in [
        ("SPAIN", "Spain", "#FF6B6B"),
        ("EU_NOT_SPAIN", "EU (not Spain)", "#4ECDC4"),
        ("OUTSIDE_EU", "Outside EU", "#45B7D1"),
    ]:
        incomes = [d["regional"].get(region_key, {}).get("total_income", 0) for d in sorted_data]
        fig2.add_trace(go.Bar(name=region_label, x=sorted_labels, y=incomes, marker_color=color))

    fig2.update_layout(
        barmode="stack",
        xaxis_title="Quarter",
        yaxis_title="Income (€)",
        legend_title="Region",
        height=400,
        plot_bgcolor="white",
    )
    st.plotly_chart(fig2, width="stretch")

    st.markdown("---")
    st.subheader("📤 Per-Quarter Export")

    export_quarter = st.selectbox("Select quarter to export", sorted(valid_quarters.keys(), reverse=True))
    if export_quarter and st.button("📥 Generate Excel", type="primary"):
        d = valid_quarters[export_quarter]
        payments = d.get("payments", [])
        if payments:
            import tempfile, os
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp_path = tmp.name
            filename = generate_report_filename(d["year"], d["quarter"])
            create_excel_report(payments, tmp_path, d["year"], d["quarter"])
            with open(tmp_path, "rb") as f:
                excel_bytes = f.read()
            os.unlink(tmp_path)
            st.download_button(f"⬇️ Download {filename}", excel_bytes, filename,
                               "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
