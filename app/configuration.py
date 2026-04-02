"""Configuration tab content."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent

from app.data_loader import invalidate_cache
from src.config import load_config, reload_config, save_config
from src.rules_engine import load_rules, reload_rules, save_rules
from src.stripe_client import check_permissions, test_connection


def render():
    """Render the Configuration tab."""
    cfg = load_config()
    rules = load_rules()

    config_tabs = st.tabs([
        "Classification Rules",
        "Geographic Rules",
        "Stripe API",
        "App Settings",
    ])

    # --- Classification Rules (editable JSON) ---
    with config_tabs[0]:
        st.subheader("Activity Classification Rules")
        st.caption("Edit the classification rules below. Changes are saved to `classification_rules.json`.")

        activity_rules = rules.get("activity_rules", [])

        for i, rule in enumerate(activity_rules):
            with st.expander(f"Priority {rule.get('priority', i+1)}: {rule.get('name', 'rule')} -> {rule.get('activity_type', '?')}"):
                c1, c2, c3 = st.columns(3)
                rule["name"] = c1.text_input("Rule name", rule.get("name", ""), key=f"rule_name_{i}")
                rule["activity_type"] = c2.selectbox(
                    "Activity type",
                    ["COACHING", "NEWSLETTER", "ILLUSTRATIONS"],
                    index=["COACHING", "NEWSLETTER", "ILLUSTRATIONS"].index(rule.get("activity_type", "COACHING")),
                    key=f"rule_activity_{i}",
                )
                rule["match_type"] = c3.selectbox(
                    "Match type",
                    ["empty_description", "payment_type", "description_contains"],
                    index=["empty_description", "payment_type", "description_contains"].index(rule.get("match_type", "description_contains")),
                    key=f"rule_match_{i}",
                )

                if rule["match_type"] == "payment_type":
                    rule["match_value"] = st.text_input("Match value", rule.get("match_value", ""), key=f"rule_val_{i}")
                elif rule["match_type"] == "description_contains":
                    keywords = rule.get("keywords", [])
                    kw_str = st.text_area("Keywords (one per line)", "\n".join(keywords), key=f"rule_kw_{i}")
                    rule["keywords"] = [k.strip().lower() for k in kw_str.split("\n") if k.strip()]

                rule["description"] = st.text_input("Description", rule.get("description", ""), key=f"rule_desc_{i}")

        col_add, col_save = st.columns(2)
        with col_add:
            if st.button("Add new rule", key="add_rule"):
                new_priority = max((r.get("priority", 0) for r in activity_rules), default=0) + 1
                activity_rules.append({
                    "priority": new_priority,
                    "name": f"new_rule_{new_priority}",
                    "activity_type": "COACHING",
                    "match_type": "description_contains",
                    "keywords": [],
                    "description": "",
                })
                rules["activity_rules"] = activity_rules
                save_rules(rules)
                st.rerun()

        with col_save:
            if st.button("Save classification rules", type="primary", key="save_rules"):
                rules["activity_rules"] = activity_rules
                save_rules(rules)
                invalidate_cache()
                st.success("Classification rules saved")

        st.markdown("---")
        st.subheader("Raw JSON Editor")
        st.caption("Advanced: edit the full classification_rules.json directly.")

        raw_json = st.text_area(
            "classification_rules.json",
            json.dumps(rules, indent=2, ensure_ascii=False),
            height=400,
            key="raw_rules_json",
        )
        if st.button("Save raw JSON", key="save_raw_json"):
            try:
                parsed = json.loads(raw_json)
                save_rules(parsed)
                invalidate_cache()
                st.success("Raw JSON saved and rules reloaded")
            except json.JSONDecodeError as e:
                st.error(f"Invalid JSON: {e}")

    # --- Geographic Rules ---
    with config_tabs[1]:
        st.subheader("Geographic Classification")
        geo_rules = rules.get("geographic_rules", {})
        defaults = geo_rules.get("defaults", {})

        st.markdown("""
Classification priority order:

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | Currency is **not EUR** | non_eur_default |
| 2 | EUR + explicit **name/email override** | override value |
| 3 | EUR + activity is **NEWSLETTER** | eur_newsletter_default |
| 4 | EUR + any other activity | eur_default |
""")

        REGION_OPTIONS = ["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"]

        col1, col2, col3 = st.columns(3)
        with col1:
            st.markdown("**EUR General**")
            eur_default = st.selectbox(
                "eur_default",
                REGION_OPTIONS,
                index=REGION_OPTIONS.index(defaults.get("eur_default", "SPAIN")),
                key="geo_eur_default",
            )
        with col2:
            st.markdown("**EUR Newsletter**")
            eur_newsletter_default = st.selectbox(
                "eur_newsletter_default",
                REGION_OPTIONS,
                index=REGION_OPTIONS.index(defaults.get("eur_newsletter_default", "EU_NOT_SPAIN")),
                key="geo_eur_newsletter",
            )
        with col3:
            st.markdown("**Non-EUR currency**")
            non_eur_default = st.selectbox(
                "non_eur_default",
                REGION_OPTIONS,
                index=REGION_OPTIONS.index(defaults.get("non_eur_default", "OUTSIDE_EU")),
                key="geo_non_eur",
            )

        if st.button("Save geographic defaults", type="primary", key="save_geo_defaults"):
            geo_rules["defaults"] = {
                "eur_default": eur_default,
                "eur_newsletter_default": eur_newsletter_default,
                "non_eur_default": non_eur_default,
            }
            rules["geographic_rules"] = geo_rules
            save_rules(rules)
            invalidate_cache()
            st.success("Geographic defaults saved")

        st.markdown("---")
        st.subheader("Client Overrides")

        col_geo, col_email = st.columns(2)
        with col_geo:
            st.markdown("**Name/Description Overrides**")
            geo_ov = geo_rules.get("geographic_overrides", {})
            geo_rows = [{"Key": k, "Region": v} for k, v in geo_ov.items()]
            edited_geo = st.data_editor(
                pd.DataFrame(geo_rows) if geo_rows else pd.DataFrame(columns=["Key", "Region"]),
                num_rows="dynamic",
                use_container_width=True,
                key="geo_overrides_editor",
            )
            if st.button("Save name overrides", key="save_name_ov"):
                geo_rules["geographic_overrides"] = {
                    row["Key"].lower().strip(): row["Region"]
                    for _, row in edited_geo.iterrows()
                    if pd.notna(row["Key"]) and str(row["Key"]).strip()
                }
                rules["geographic_rules"] = geo_rules
                save_rules(rules)
                invalidate_cache()
                st.success("Name overrides saved")

        with col_email:
            st.markdown("**Email Overrides**")
            email_ov = geo_rules.get("email_overrides", {})
            email_rows = [{"Email": k, "Region": v} for k, v in email_ov.items()]
            edited_email = st.data_editor(
                pd.DataFrame(email_rows) if email_rows else pd.DataFrame(columns=["Email", "Region"]),
                num_rows="dynamic",
                use_container_width=True,
                key="email_overrides_editor",
            )
            if st.button("Save email overrides", key="save_email_ov"):
                geo_rules["email_overrides"] = {
                    row["Email"].lower().strip(): row["Region"]
                    for _, row in edited_email.iterrows()
                    if pd.notna(row["Email"]) and str(row["Email"]).strip()
                }
                rules["geographic_rules"] = geo_rules
                save_rules(rules)
                invalidate_cache()
                st.success("Email overrides saved")

    # --- Stripe API ---
    with config_tabs[2]:
        st.subheader("Stripe API Setup")
        api_key_input = st.text_input(
            "Stripe API Key",
            type="password",
            help="sk_live_... or rk_live_... (read-only restricted key recommended)",
            key="stripe_api_key",
        )
        col1, col2, col3 = st.columns(3)
        with col1:
            if st.button("Save API Key", key="save_api_key"):
                env_path = ROOT / ".env"
                lines = []
                if env_path.exists():
                    with open(env_path) as f:
                        lines = [line for line in f.readlines() if not line.startswith("STRIPE_API_KEY")]
                lines.append(f"STRIPE_API_KEY={api_key_input}\n")
                with open(env_path, "w") as f:
                    f.writelines(lines)
                st.success("API key saved to .env")

        with col2:
            if st.button("Test Connection", key="test_stripe"):
                key = api_key_input or None
                success, msg = test_connection(key)
                if success:
                    st.success(f"Connected: {msg}")
                else:
                    st.error(f"Failed: {msg}")

        with col3:
            if st.button("Check Permissions", key="check_perms"):
                try:
                    perms = check_permissions(api_key_input or None)
                    for resource, has_access in perms.items():
                        if has_access:
                            st.success(f"{resource}: accessible")
                        else:
                            st.warning(f"{resource}: no access")
                except Exception as e:
                    st.error(str(e))

        st.markdown("---")
        st.markdown("""
**Available data with read permissions:**

| Data | Permission Required | Use Case |
|------|-------------------|----------|
| Charge amount, currency, description | Read charges | Core transaction data |
| Balance transaction fees | Read charges | Fee calculations |
| Customer email | Read customers | Geographic overrides |
| Card issuing country | Read charges | Automatic geographic classification |
| Payment method details | Read charges | Card country extraction |

**Card issuing country** (`charge.payment_method_details.card.country`) provides
the ISO country code (e.g., ES, DE, US) which can be used for automatic
geographic classification instead of manual overrides.
""")

    # --- App Settings ---
    with config_tabs[3]:
        st.subheader("App Settings")
        app_cfg = cfg.get("app", {})

        c1, c2 = st.columns(2)
        input_mode = c1.selectbox(
            "Input mode",
            ["csv", "api"],
            index=0 if app_cfg.get("input_mode") == "csv" else 1,
            key="app_input_mode",
        )
        csv_path_old = st.text_input("CSV path (with Currency col)", app_cfg.get("csv_path", "tmp/unified_payments_all_old.csv"), key="app_csv_old")
        csv_path_new = st.text_input("CSV path (without Currency col)", app_cfg.get("csv_path_new", "tmp/unified_payments_all.csv"), key="app_csv_new")

        if st.button("Save App Settings", type="primary", key="save_app"):
            cfg["app"]["input_mode"] = input_mode
            cfg["app"]["csv_path"] = csv_path_old
            cfg["app"]["csv_path_new"] = csv_path_new
            save_config(cfg)
            invalidate_cache()
            reload_config()
            st.success("Settings saved")

        st.markdown("---")
        st.subheader("Cache")
        if st.button("Clear Cache", key="clear_cache"):
            invalidate_cache()
            st.success("Cache cleared")
        st.caption(f"Data root: `{ROOT}`")
