"""Transaction Browser tab content."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd
import streamlit as st

from app.data_loader import get_classified_for_period, invalidate_cache
from app.year_picker import year_choices
from src.periods import quarter_datetime_bounds
from src.classifier import eur_default_foreign_warning
from src.database import (
    get_transaction_count_db,
    search_transactions_raw,
)
from src.models import ClassifiedPayment
from src.rules_engine import load_rules, save_rules


def render() -> None:
    """Render the Transaction Browser tab."""
    col1, col2, col3 = st.columns([1, 1, 2])
    current_year = datetime.now().year
    year_options = year_choices()
    with col1:
        year = st.selectbox(
            "Year",
            year_options,
            index=year_options.index(current_year) if current_year in year_options else 0,
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
        start_dt, end_dt = quarter_datetime_bounds(year, quarter)
    else:
        start_dt, end_dt = datetime(year, 1, 1), datetime(year, 12, 31, 23, 59, 59)

    btn_col1, btn_col2 = st.columns([1, 1])
    load_db = btn_col1.button("Load (from SQLite)", type="primary", key="tb_load_db")
    refresh_api = btn_col2.button("Refresh from API", type="secondary", key="tb_refresh_api")

    period_key = (year, quarter)
    if (load_db or refresh_api or "browser_data" not in st.session_state
            or st.session_state.get("browser_key") != period_key):
        with st.spinner("Loading..."):
            if refresh_api:
                payments = get_classified_for_period(
                    year,
                    quarter,
                    start_dt,
                    end_dt,
                    input_mode="api",
                )
            else:
                payments = get_classified_for_period(year, quarter, start_dt, end_dt, input_mode="db")
            st.session_state["browser_data"] = payments
            st.session_state["browser_key"] = period_key

    payments: list[ClassifiedPayment] = st.session_state.get("browser_data", [])

    filtered = payments
    if search_desc:
        filtered = [p for p in filtered if search_desc.lower() in p.description.lower()]
    if activity_filter != "All":
        filtered = [p for p in filtered if p.activity_type == activity_filter]
    if geo_filter != "All":
        filtered = [p for p in filtered if p.geo_region == geo_filter]

    if not payments:
        st.warning("No payments found via the current loader for the selected period.")
        st.info("Raw database results are shown below (if your SQLite DB has data).")
    else:
        st.markdown(f"**{len(filtered)} transactions** (of {len(payments)} total)")
        foreign = [p for p in payments if eur_default_foreign_warning(p)]
        if foreign:
            st.warning(
                f"⚠️ {len(foreign)} EUR charge(s) fell to the `eur_default` (SPAIN) rule for a "
                f"customer that looks foreign — see the **Review** column and add a geographic "
                f"override below if they are not in Spain."
            )

    if payments:
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
                "Review": (f"⚠ {w}" if (w := eur_default_foreign_warning(p)) else ""),
            })

        df = pd.DataFrame(rows)

        st.dataframe(
            df,
            width="stretch",
            hide_index=True,
            column_config={
                "Amount EUR": st.column_config.NumberColumn(format="%.2f"),
                "Refunded EUR": st.column_config.NumberColumn(format="%.2f"),
                "Fee EUR": st.column_config.NumberColumn(format="%.2f"),
            },
        )

    st.markdown("---")
    st.subheader("Raw database (SQLite)")
    try:
        total_db = get_transaction_count_db()
        st.caption(f"SQLite `transactions` rows: {total_db}")
    except Exception as exc:
        st.error(f"Could not query SQLite database: {exc}")
        total_db = None

    raw_limit = st.number_input("Max rows", min_value=100, max_value=20000, value=2000, step=100, key="tb_db_limit")
    run_raw = st.button("Run DB search", key="tb_db_run")

    if run_raw or "tb_db_last" not in st.session_state:
        try:
            sql, params, raw_rows = search_transactions_raw(
                start_date=start_dt,
                end_date=end_dt,
                search_text=search_desc,
                activity_type=activity_filter,
                geo_region=geo_filter,
                limit=int(raw_limit),
            )
            st.session_state["tb_db_last"] = {"sql": sql, "params": params, "rows": raw_rows}
        except Exception as exc:
            st.error(f"DB query failed: {exc}")

    last = st.session_state.get("tb_db_last")
    if last and isinstance(last, dict):
        raw_rows = last.get("rows") or []
        st.markdown(f"**{len(raw_rows)} raw rows** (limited to {int(raw_limit)})")
        with st.expander("SQL used", expanded=False):
            st.code(last.get("sql", ""), language="sql")
            st.code(repr(last.get("params", [])))
        if raw_rows:
            raw_df = pd.DataFrame(raw_rows)
            if "id" in raw_df.columns:
                # Columns to hide from the table (large blobs, shown separately)
                _hidden = {"raw_source_json", "raw_source_type"}
                display_cols = [c for c in raw_df.columns if c not in _hidden]

                event = st.dataframe(
                    raw_df[display_cols],
                    width="stretch",
                    hide_index=True,
                    selection_mode="single-row",
                    on_select="rerun",
                    key="tb_raw_table",
                )
                selected = event.selection.rows if event and event.selection else []

                # Payload inspector
                st.markdown("**Inspect source payload**")
                if "raw_source_json" not in raw_df.columns:
                    st.info("Raw source JSON not in result — click **Run DB search** again.")
                elif not selected:
                    st.caption("Select a row above to inspect its payload.")
                else:
                    row = raw_df.iloc[selected[0]]
                    st.caption(f"`{row['id']}` · source: `{row.get('raw_source_type')}`")
                    raw_json_val = row.get("raw_source_json")
                    if raw_json_val:
                        try:
                            st.json(json.loads(raw_json_val))
                        except (TypeError, ValueError):
                            st.code(str(raw_json_val))
                    else:
                        st.info("No raw_source_json stored for this row.")
            else:
                st.dataframe(raw_df, width="stretch", hide_index=True)

    st.markdown("---")
    st.subheader("Add Geographic Override")

    with st.form("add_override"):
        oc1, oc2, oc3 = st.columns(3)
        override_key = oc1.text_input("Client name / email / keyword", help="Substring match applied to description or email")
        override_region = oc2.selectbox("Region", ["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"])
        override_type = oc3.selectbox("Match on", ["Name/Description", "Email"])
        submitted = st.form_submit_button("Add Override", key="add_override_submit")

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
