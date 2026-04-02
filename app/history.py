"""History & Charts tab content."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.data_loader import get_classified_for_period, quarter_dates
from src.aggregator import calculate_grand_totals, calculate_regional_totals, get_transaction_count
from src.excel_exporter import create_excel_report, generate_report_filename


def render():
    """Render the History & Charts tab."""
    current_year = datetime.now().year

    all_quarters = []
    for y in range(2023, current_year + 1):
        start_q = 3 if y == 2023 else 1
        end_q = (datetime.now().month - 1) // 3 + 1 if y == current_year else 4
        for q in range(start_q, end_q + 1):
            all_quarters.append((y, q))

    def _load_history(*, mode: str) -> None:
        force_token = datetime.now().isoformat(timespec="seconds") if mode == "api" else None
        progress = st.progress(0, text="Loading quarters...")
        history = {}
        for i, (y, q) in enumerate(all_quarters):
            s, e = quarter_dates(y, q)
            label = f"Q{q} {y}"
            try:
                payments = get_classified_for_period(
                    y,
                    q,
                    s,
                    e,
                    input_mode=mode,
                    force_refresh_token=force_token,
                )
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
                    "data_source": mode,
                }
            except Exception as exc:
                history[label] = {"year": y, "quarter": q, "error": str(exc)}
            progress.progress((i + 1) / len(all_quarters), text=f"Loaded {label}")
        st.session_state["history_data"] = history
        progress.empty()

    # Default behaviour: on first visit, load from SQLite.
    if "history_data" not in st.session_state:
        with st.spinner("Loading from SQLite..."):
            _load_history(mode="db")

    refresh_api = st.button("Refresh history from API", type="primary", key="hist_refresh_api")
    if refresh_api:
        _load_history(mode="api")
        st.success("History refreshed from Stripe API.")

    history = st.session_state.get("history_data", {})
    if not history:
        return

    valid_quarters = {k: v for k, v in history.items() if "error" not in v}

    if not valid_quarters:
        st.warning("No valid quarter data found.")
        return

    # --- Summary Table ---
    st.subheader("Quarterly Summary")

    table_rows = []
    for label, d in sorted(valid_quarters.items(), key=lambda kv: kv[1].get("end", ""), reverse=True):
        table_rows.append({
            "Quarter": label,
            "Period": f"{d['start']} to {d['end']}",
            "Coaching EUR": d["coaching"],
            "Newsletter EUR": d["newsletter"],
            "Illustrations EUR": d["illustrations"],
            "Total Income EUR": d["total_income"],
            "Total Fees EUR": d["total_fee"],
            "Net EUR": round(d["total_income"] - d["total_fee"], 2),
            "Transactions": d["transactions"],
        })

    df_summary = pd.DataFrame(table_rows)
    st.dataframe(
        df_summary,
        width="stretch",
        hide_index=True,
        column_config={
            "Coaching EUR": st.column_config.NumberColumn(format="%.2f"),
            "Newsletter EUR": st.column_config.NumberColumn(format="%.2f"),
            "Illustrations EUR": st.column_config.NumberColumn(format="%.2f"),
            "Total Income EUR": st.column_config.NumberColumn(format="%.2f"),
            "Total Fees EUR": st.column_config.NumberColumn(format="%.2f"),
            "Net EUR": st.column_config.NumberColumn(format="%.2f"),
        },
    )

    st.markdown("---")

    # --- Stacked Area Chart: Income by Activity ---
    st.subheader("Income by Activity Type")

    sorted_labels = sorted(valid_quarters.keys())
    sorted_data = [valid_quarters[l] for l in sorted_labels]

    fig_activity = go.Figure()
    fig_activity.add_trace(go.Scatter(
        name="Coaching",
        x=sorted_labels,
        y=[d["coaching"] for d in sorted_data],
        mode="lines",
        stackgroup="one",
        line=dict(color="#2D4A7A"),
        hovertemplate="Coaching: %{y:,.2f} EUR<extra></extra>",
    ))
    fig_activity.add_trace(go.Scatter(
        name="Newsletter",
        x=sorted_labels,
        y=[d["newsletter"] for d in sorted_data],
        mode="lines",
        stackgroup="one",
        line=dict(color="#5B8DBE"),
        hovertemplate="Newsletter: %{y:,.2f} EUR<extra></extra>",
    ))
    fig_activity.add_trace(go.Scatter(
        name="Illustrations",
        x=sorted_labels,
        y=[d["illustrations"] for d in sorted_data],
        mode="lines",
        stackgroup="one",
        line=dict(color="#9CB8D8"),
        hovertemplate="Illustrations: %{y:,.2f} EUR<extra></extra>",
    ))
    fig_activity.update_layout(
        xaxis_title="Quarter",
        yaxis_title="Income (EUR)",
        legend_title="Activity",
        height=450,
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
    )
    st.plotly_chart(fig_activity, width="stretch")

    st.markdown("---")

    # --- Stacked Area Chart: Income by Geography ---
    st.subheader("Income by Geographic Region")

    fig_geo = go.Figure()
    for region_key, region_label, color in [
        ("SPAIN", "Spain", "#FF6B6B"),
        ("EU_NOT_SPAIN", "EU (not Spain)", "#4ECDC4"),
        ("OUTSIDE_EU", "Outside EU", "#45B7D1"),
    ]:
        incomes = [d["regional"].get(region_key, {}).get("total_income", 0) for d in sorted_data]
        fig_geo.add_trace(go.Scatter(
            name=region_label,
            x=sorted_labels,
            y=incomes,
            mode="lines",
            stackgroup="one",
            line=dict(color=color),
            hovertemplate=f"{region_label}: %{{y:,.2f}} EUR<extra></extra>",
        ))

    fig_geo.update_layout(
        xaxis_title="Quarter",
        yaxis_title="Income (EUR)",
        legend_title="Region",
        height=450,
        template="plotly_dark",
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        hovermode="x unified",
    )
    st.plotly_chart(fig_geo, width="stretch")

    st.markdown("---")

    # --- Per-Quarter Export ---
    st.subheader("Export Quarter")

    export_quarter = st.selectbox(
        "Select quarter to export",
        sorted(valid_quarters.keys(), reverse=True),
        key="hist_export_quarter",
    )
    if export_quarter and st.button("Generate Excel", type="primary", key="hist_export"):
        d = valid_quarters[export_quarter]
        payments = d.get("payments", [])
        if payments:
            with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
                tmp_path = tmp.name
            filename = generate_report_filename(d["year"], d["quarter"])
            create_excel_report(payments, tmp_path, d["year"], d["quarter"])
            with open(tmp_path, "rb") as f:
                excel_bytes = f.read()
            os.unlink(tmp_path)
            st.download_button(
                f"Download {filename}",
                excel_bytes,
                filename,
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key="hist_download",
            )
