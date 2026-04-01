"""Configuration page."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import json

import pandas as pd
import streamlit as st

from app.components.data_loader import invalidate_cache
from src.config import load_config, reload_config, save_config
from src.stripe_client import test_connection

st.set_page_config(page_title="Configuration", page_icon="⚙️", layout="wide")
st.title("⚙️ Configuration")

cfg = load_config()

tabs = st.tabs(["🔑 Stripe API", "🌍 Client Mappings", "⚙️ App Settings", "📋 Classification Patterns", "🗺️ Geographic Rules"])

with tabs[0]:
    st.subheader("Stripe API Setup")
    api_key_input = st.text_input("Stripe API Key", type="password", help="sk_live_... or rk_live_... (read-only restricted key recommended)")
    col1, col2 = st.columns(2)
    with col1:
        if st.button("💾 Save API Key"):
            env_path = ROOT / ".env"
            lines = []
            if env_path.exists():
                with open(env_path) as f:
                    lines = [l for l in f.readlines() if not l.startswith("STRIPE_API_KEY")]
            lines.append(f"STRIPE_API_KEY={api_key_input}\n")
            with open(env_path, "w") as f:
                f.writelines(lines)
            st.success("API key saved to .env")
    with col2:
        if st.button("🔌 Test Connection"):
            key = api_key_input or None
            success, msg = test_connection(key)
            if success:
                st.success(f"✓ {msg}")
            else:
                st.error(f"✗ {msg}")

    env_path = ROOT / ".env"
    if env_path.exists():
        import os
        from dotenv import load_dotenv
        load_dotenv(env_path)
        key_set = bool(os.getenv("STRIPE_API_KEY"))
        if key_set:
            st.info("✓ STRIPE_API_KEY is configured in .env")
        else:
            st.warning("STRIPE_API_KEY not found in .env")

with tabs[1]:
    st.subheader("Geographic Overrides")
    st.info("These rules override the default EUR→Spain / non-EUR→Outside-EU classification.")

    col_geo, col_email = st.columns(2)

    with col_geo:
        st.markdown("**Name/Description Overrides**")
        geo_ov = cfg.get("geographic_overrides", {})
        geo_rows = [{"Key (name/keyword)": k, "Region": v} for k, v in geo_ov.items()]
        edited_geo = st.data_editor(
            pd.DataFrame(geo_rows) if geo_rows else pd.DataFrame(columns=["Key (name/keyword)", "Region"]),
            num_rows="dynamic",
            width="stretch",
            key="geo_editor",
        )
        if st.button("💾 Save Name Overrides"):
            cfg["geographic_overrides"] = {
                row["Key (name/keyword)"].lower().strip(): row["Region"]
                for _, row in edited_geo.iterrows()
                if pd.notna(row["Key (name/keyword)"]) and str(row["Key (name/keyword)"]).strip()
            }
            save_config(cfg)
            invalidate_cache()
            st.success("Name overrides saved")

    with col_email:
        st.markdown("**Email Overrides**")
        email_ov = cfg.get("email_overrides", {})
        email_rows = [{"Email pattern": k, "Region": v} for k, v in email_ov.items()]
        edited_email = st.data_editor(
            pd.DataFrame(email_rows) if email_rows else pd.DataFrame(columns=["Email pattern", "Region"]),
            num_rows="dynamic",
            width="stretch",
            key="email_editor",
        )
        if st.button("💾 Save Email Overrides"):
            cfg["email_overrides"] = {
                row["Email pattern"].lower().strip(): row["Region"]
                for _, row in edited_email.iterrows()
                if pd.notna(row["Email pattern"]) and str(row["Email pattern"]).strip()
            }
            save_config(cfg)
            invalidate_cache()
            st.success("Email overrides saved")

    st.markdown("---")
    col_dl, col_ul = st.columns(2)
    with col_dl:
        config_json = json.dumps(
            {
                "geographic_overrides": cfg.get("geographic_overrides", {}),
                "email_overrides": cfg.get("email_overrides", {}),
            },
            indent=2,
            ensure_ascii=False,
        )
        st.download_button("📥 Export Mappings JSON", config_json, "client_mappings.json", "application/json")

    with col_ul:
        uploaded = st.file_uploader("📤 Import Mappings JSON", type="json")
        if uploaded:
            imported = json.load(uploaded)
            if "geographic_overrides" in imported:
                cfg["geographic_overrides"] = imported["geographic_overrides"]
            if "email_overrides" in imported:
                cfg["email_overrides"] = imported["email_overrides"]
            save_config(cfg)
            invalidate_cache()
            st.success("Mappings imported successfully")

with tabs[2]:
    st.subheader("App Settings")
    app_cfg = cfg.get("app", {})

    c1, c2 = st.columns(2)
    input_mode = c1.selectbox("Input mode", ["csv", "api"], index=0 if app_cfg.get("input_mode") == "csv" else 1)
    validate_on_load = c2.checkbox("Validate on load", value=app_cfg.get("validate_on_load", True))
    csv_path_old = st.text_input("CSV path (with Currency col)", app_cfg.get("csv_path", "tmp/unified_payments_all_old.csv"))
    csv_path_new = st.text_input("CSV path (without Currency col)", app_cfg.get("csv_path_new", "tmp/unified_payments_all.csv"))

    if st.button("💾 Save App Settings"):
        cfg["app"]["input_mode"] = input_mode
        cfg["app"]["validate_on_load"] = validate_on_load
        cfg["app"]["csv_path"] = csv_path_old
        cfg["app"]["csv_path_new"] = csv_path_new
        save_config(cfg)
        invalidate_cache()
        reload_config()
        st.success("Settings saved")

    st.markdown("---")
    st.subheader("Cache & Logs")
    col_cache, col_logs = st.columns(2)
    with col_cache:
        if st.button("🗑️ Clear Cache"):
            invalidate_cache()
            st.success("Cache cleared")
        logs_dir = ROOT / cfg.get("app", {}).get("logs_dir", "logs")
        st.caption(f"Data root: `{ROOT}`")

    with col_logs:
        log_path = ROOT / "logs" / "stripe_automation.log"
        if log_path.exists():
            with open(log_path, encoding="utf-8") as f:
                log_content = f.readlines()
            if st.button("📋 View Latest Logs"):
                st.code("".join(log_content[-50:]), language=None)

with tabs[3]:
    st.subheader("Activity Classification Patterns")
    patterns = cfg.get("classification_patterns", {})

    newsletter_kws = ", ".join(f"`{k}`" for k in patterns.get("newsletter", ["subscription"]))
    illustrations_kws = ", ".join(f"`{k}`" for k in patterns.get("illustrations", ["charge for"]))
    coaching_kws = ", ".join(f"`{k}`" for k in patterns.get("coaching", []))

    st.markdown(f"""
| Priority | Pattern | Activity |
|----------|---------|----------|
| 1 | Empty/null description | **COACHING** |
| 2 | Luma `registration` payment type | **COACHING** |
| 3 | Contains: {illustrations_kws} | **ILLUSTRATIONS** |
| 4 | Contains: {newsletter_kws} | **NEWSLETTER** |
| 5 | Contains: {coaching_kws} | **COACHING** |
| 6 | No pattern matched | **UNKNOWN** |
""")

    st.markdown("---")
    st.subheader("Customise Coaching Patterns")
    coaching_patterns = patterns.get("coaching", [])
    new_patterns_str = st.text_area("Coaching keywords (one per line)", "\n".join(coaching_patterns))
    if st.button("💾 Save Patterns"):
        cfg["classification_patterns"]["coaching"] = [
            p.strip().lower() for p in new_patterns_str.split("\n") if p.strip()
        ]
        save_config(cfg)
        invalidate_cache()
        st.success("Patterns saved")

with tabs[4]:
    st.subheader("Geographic Classification Rules")

    geo_rules = cfg.get("geographic_rules", {
        "eur_default": "SPAIN",
        "eur_newsletter_default": "EU_NOT_SPAIN",
        "non_eur_default": "OUTSIDE_EU",
    })

    REGION_OPTIONS = ["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"]

    st.markdown("""
Classification is applied **after** activity detection, using this priority order:

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | Currency is **not EUR** | → `non_eur_default` |
| 2 | EUR + explicit **name/email override** | → override value |
| 3 | EUR + activity is **NEWSLETTER** | → `eur_newsletter_default` |
| 4 | EUR + any other activity | → `eur_default` |
""")

    st.markdown("---")
    st.subheader("Default Region Values")

    col1, col2, col3 = st.columns(3)

    with col1:
        st.markdown("**EUR — General (non-newsletter)**")
        eur_default = st.selectbox(
            "eur_default",
            REGION_OPTIONS,
            index=REGION_OPTIONS.index(geo_rules.get("eur_default", "SPAIN")),
            key="eur_default_select",
            help="Default region for EUR payments that are not NEWSLETTER and have no override.",
        )

    with col2:
        st.markdown("**EUR — Newsletter**")
        eur_newsletter_default = st.selectbox(
            "eur_newsletter_default",
            REGION_OPTIONS,
            index=REGION_OPTIONS.index(geo_rules.get("eur_newsletter_default", "EU_NOT_SPAIN")),
            key="eur_newsletter_select",
            help="Default region for EUR NEWSLETTER payments with no override. Newsletter subscribers paying in EUR are typically outside Spain.",
        )

    with col3:
        st.markdown("**Non-EUR currency**")
        non_eur_default = st.selectbox(
            "non_eur_default",
            REGION_OPTIONS,
            index=REGION_OPTIONS.index(geo_rules.get("non_eur_default", "OUTSIDE_EU")),
            key="non_eur_select",
            help="Default region for any payment made in a currency other than EUR.",
        )

    if st.button("💾 Save Geographic Rules"):
        cfg["geographic_rules"] = {
            "eur_default": eur_default,
            "eur_newsletter_default": eur_newsletter_default,
            "non_eur_default": non_eur_default,
        }
        save_config(cfg)
        invalidate_cache()
        st.success("Geographic rules saved")

    st.markdown("---")
    st.caption(
        "Individual client exceptions are managed in the **Client Mappings** tab. "
        "Overrides always take priority over the defaults above."
    )
