"""Transaction Browser tab content."""
from __future__ import annotations

from datetime import datetime

import pandas as pd
import streamlit as st

from app.data_loader import get_classified_for_period, invalidate_cache, quarter_dates
from src.database import get_transaction_count_db, search_transactions_raw
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

    btn_col1, btn_col2 = st.columns([1, 1])
    load_db = btn_col1.button("Load (from SQLite)", type="primary", key="tb_load_db")
    refresh_api = btn_col2.button("Refresh from API", type="secondary", key="tb_refresh_api")

    if load_db or refresh_api or "browser_data" not in st.session_state:
        with st.spinner("Loading..."):
            if refresh_api:
                payments = get_classified_for_period(
                    year,
                    quarter,
                    start_dt,
                    end_dt,
                    input_mode="api",
                    force_refresh_token=datetime.now().isoformat(timespec="seconds"),
                )
            else:
                payments = get_classified_for_period(year, quarter, start_dt, end_dt, input_mode="db")
            st.session_state["browser_data"] = payments

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

                # Build display dataframe: checkbox FIRST, then the rest.
                # Only the currently selected row gets True — so the table always
                # shows exactly one tick (radio-button behaviour).
                current_inspect = st.session_state.get("tb_inspect_id")
                df_view = raw_df[display_cols].copy()
                ids = df_view["id"].astype(str)
                df_view.insert(0, "📋", ids == str(current_inspect) if current_inspect else False)

                edited = st.data_editor(
                    df_view,
                    width="stretch",
                    hide_index=True,
                    disabled=[c for c in df_view.columns if c != "📋"],
                    column_config={
                        "📋": st.column_config.CheckboxColumn(
                            "📋",
                            help="Tick a row to inspect its raw Stripe payload",
                            default=False,
                            width="small",
                        ),
                    },
                    key="tb_raw_editor",
                )

                # Detect which row the user interacted with:
                # - any row now True that wasn't True before → new selection
                # - currently selected row now False → deselect
                try:
                    prev_true = set(ids[df_view["📋"]].tolist())
                    now_true  = set(edited.loc[edited["📋"] == True, "id"].astype(str).tolist())  # noqa: E712
                    newly_checked = now_true - prev_true
                    if newly_checked:
                        st.session_state["tb_inspect_id"] = next(iter(newly_checked))
                        st.rerun()
                    elif current_inspect and current_inspect not in now_true:
                        st.session_state["tb_inspect_id"] = None
                        st.rerun()
                except Exception:
                    pass

                # Payload inspector
                st.markdown("**Inspect source payload**")
                if "raw_source_json" not in raw_df.columns:
                    st.info("Raw source JSON not in result — click **Run DB search** again.")
                else:
                    inspect_id = st.session_state.get("tb_inspect_id")
                    if not inspect_id:
                        st.caption("Tick a row above to inspect its payload.")
                    else:
                        row_match = raw_df[raw_df["id"].astype(str) == inspect_id]
                        if row_match.empty:
                            st.caption("Tick a row above to inspect its payload.")
                        else:
                            raw_json_val = row_match.iloc[0].get("raw_source_json")
                            raw_type_val = row_match.iloc[0].get("raw_source_type")
                            st.caption(f"`{inspect_id}` · source: `{raw_type_val}`")
                            if raw_json_val:
                                try:
                                    import json as _json
                                    st.json(_json.loads(raw_json_val))
                                except Exception:
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
