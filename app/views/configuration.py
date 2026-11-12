"""Configuration tab content."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent.parent

from app.data_loader import invalidate_cache
from app.flash import flash, show_flash
from src.config import load_config, reload_config, save_config, save_stripe_api_key
from src.exceptions import ConfigError
from src.rules_engine import load_rules, save_rules
from src.stripe_client import check_permissions, test_connection


def _render_classification_rules(rules: dict) -> None:
    """Classification Rules tab: per-rule editor plus the raw JSON editor."""
    st.subheader("Activity Classification Rules")
    st.caption("Edit the classification rules below. Changes are saved to `classification_rules.json`.")

    # `rules` is the cached object the classifier and every other save button use:
    # the widgets edit a private copy, which is written back only by the Save button.
    activity_rules = copy.deepcopy(rules.get("activity_rules", []))

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
            stored_rules = copy.deepcopy(rules)
            stored_list = stored_rules.setdefault("activity_rules", [])
            new_priority = max((r.get("priority", 0) for r in stored_list), default=0) + 1
            stored_list.append({
                "priority": new_priority,
                "name": f"new_rule_{new_priority}",
                "activity_type": "COACHING",
                "match_type": "description_contains",
                "keywords": [],
                "description": "",
            })
            save_rules(stored_rules)
            st.rerun()

    with col_save:
        if st.button("Save classification rules", type="primary", key="save_rules"):
            updated = copy.deepcopy(rules)
            updated["activity_rules"] = activity_rules
            save_rules(updated)
            invalidate_cache()
            st.success("Classification rules saved")

    st.markdown("---")
    st.subheader("Raw JSON Editor")
    st.caption("Advanced: edit the full classification_rules.json directly.")

    # A keyed text_area only takes its value on the first render of a key, so the key
    # follows the stored rules: a save from any form (or tab) reseeds the editor
    # instead of leaving a stale copy that "Save raw JSON" would write back.
    stored_json = json.dumps(rules, indent=2, ensure_ascii=False)
    rules_version = hashlib.sha256(stored_json.encode()).hexdigest()[:12]
    raw_json = st.text_area(
        "classification_rules.json",
        stored_json,
        height=400,
        key=f"raw_rules_json_{rules_version}",
    )
    if st.button("Save raw JSON", key="save_raw_json"):
        try:
            parsed = json.loads(raw_json)
            save_rules(parsed)
            invalidate_cache()
            st.success("Raw JSON saved and rules reloaded")
        except json.JSONDecodeError as e:
            st.error(f"Invalid JSON: {e}")


def _render_geo_defaults(rules: dict, geo_rules: dict) -> None:
    """Geographic Rules tab: the decision tables and the three region defaults."""
    st.subheader("Geographic Classification")
    defaults = geo_rules.get("defaults", {})

    st.markdown("""
**EUR charges** — currency alone decides the branch:

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | EUR + explicit name/email override | override value |
| 2 | EUR + activity is **NEWSLETTER** | eur_newsletter_default |
| 3 | EUR + any other activity | eur_default |

**Non-EUR charges** — classified by the *charge country* first (card issuing country → Stripe billing address → Stripe customer address, first one set wins); currency is only a fallback when no country is known at all:

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | Charge country known, `ES` | SPAIN |
| 2 | Charge country known, other EU member state | EU_NOT_SPAIN |
| 3 | Charge country known, non-EU | OUTSIDE_EU |
| 4 | No charge country, currency is an EU non-euro currency (DKK/SEK/PLN/CZK/HUF/RON/BGN) | EU_NOT_SPAIN, flagged for review |
| 5 | No charge country, any other non-EUR currency | non_eur_default |

The three defaults below configure the EUR rows 2-3 and non-EUR row 5. Name/email overrides apply only to EUR charges.
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
        st.markdown("**Non-EUR, no charge country**")
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


def _mapping_editor(
    mapping: dict, key_header: str, value_header: str, editor_key: str, button_label: str, button_key: str,
) -> Optional[dict]:
    """Edit a ``{key: value}`` dict in a table; on the save button return the cleaned dict, else ``None``.

    Keys are lower-cased and stripped; a row with a blank key or value is dropped.
    """
    rows = [{key_header: k, value_header: v} for k, v in mapping.items()]
    edited = st.data_editor(
        pd.DataFrame(rows) if rows else pd.DataFrame(columns=[key_header, value_header]),
        num_rows="dynamic",
        width="stretch",
        key=editor_key,
    )
    if not st.button(button_label, key=button_key):
        return None
    return {
        row[key_header].lower().strip(): str(row[value_header]).strip()
        for _, row in edited.iterrows()
        if pd.notna(row[key_header]) and str(row[key_header]).strip()
        and pd.notna(row[value_header]) and str(row[value_header]).strip()
    }


def _save_rules_section(rules: dict, section: str, value: dict, message: str) -> None:
    """Store ``value`` under ``rules[section]``, persist the rules and confirm."""
    rules[section] = value
    save_rules(rules)
    invalidate_cache()
    st.success(message)


def _render_client_overrides(rules: dict, geo_rules: dict) -> None:
    """Geographic Rules tab: name and email region overrides."""
    st.markdown("---")
    st.subheader("Client Overrides")

    col_geo, col_email = st.columns(2)
    with col_geo:
        st.markdown("**Name/Description Overrides**")
        cleaned = _mapping_editor(geo_rules.get("geographic_overrides", {}), "Key", "Region",
                                  "geo_overrides_editor", "Save name overrides", "save_name_ov")
        if cleaned is not None:
            geo_rules["geographic_overrides"] = cleaned
            _save_rules_section(rules, "geographic_rules", geo_rules, "Name overrides saved")

    with col_email:
        st.markdown("**Email Overrides**")
        cleaned = _mapping_editor(geo_rules.get("email_overrides", {}), "Email", "Region",
                                  "email_overrides_editor", "Save email overrides", "save_email_ov")
        if cleaned is not None:
            geo_rules["email_overrides"] = cleaned
            _save_rules_section(rules, "geographic_rules", geo_rules, "Email overrides saved")


def _render_customer_vat_ids(rules: dict) -> None:
    """Geographic Rules tab: customer VAT ids that mark an EU sale as B2B."""
    st.markdown("---")
    st.subheader("Customer VAT IDs (B2B)")
    st.caption(
        "An EU (non-Spain) sale is business-to-business (reverse charge, `IVA_EU_B2B`, "
        "Modelo 349 key S) only when the customer's VAT id is known here — otherwise it is "
        "treated as a consumer sale (`EU_B2C_ES21` / OSS). VIES validity is not checked."
    )
    vat_id_rules = rules.get("customer_vat_ids", {})
    col_vat_email, col_vat_name = st.columns(2)
    with col_vat_email:
        st.markdown("**By Email**")
        cleaned = _mapping_editor(vat_id_rules.get("email_vat_ids", {}), "Email", "VAT ID",
                                  "vat_id_email_editor", "Save email VAT IDs", "save_vat_id_email")
        if cleaned is not None:
            vat_id_rules["email_vat_ids"] = cleaned
            _save_rules_section(rules, "customer_vat_ids", vat_id_rules, "Email VAT IDs saved")

    with col_vat_name:
        st.markdown("**By Name / Description**")
        cleaned = _mapping_editor(vat_id_rules.get("name_vat_ids", {}), "Name", "VAT ID",
                                  "vat_id_name_editor", "Save name VAT IDs", "save_vat_id_name")
        if cleaned is not None:
            vat_id_rules["name_vat_ids"] = cleaned
            _save_rules_section(rules, "customer_vat_ids", vat_id_rules, "Name VAT IDs saved")


def _render_geographic_rules(rules: dict) -> None:
    """Geographic Rules tab."""
    geo_rules = rules.get("geographic_rules", {})
    _render_geo_defaults(rules, geo_rules)
    _render_client_overrides(rules, geo_rules)
    _render_customer_vat_ids(rules)


def _render_stripe_api() -> None:
    """Stripe API tab: key entry, connection test and permission check."""
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
            try:
                save_stripe_api_key(api_key_input)
            except ConfigError as exc:
                st.error(str(exc))
            else:
                st.success("API key saved to .env and applied to the running app")

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


def _render_app_settings() -> None:
    """App Settings tab: cache and config reload."""
    st.subheader("Cache")
    if st.button("Clear Cache", key="clear_cache"):
        invalidate_cache()
        st.success("Cache cleared")
    if st.button("Reload Config", key="reload_config"):
        reload_config()
        flash("config", "success", "Config reloaded from disk")
        st.rerun()
    st.caption(f"Data root: `{ROOT}`")


def _render_tax_settings(cfg: dict) -> None:
    """Tax Settings tab: the regime and VAT form behind Modelo 303/130."""
    st.subheader("Tax Settings (Spanish Autónomo)")
    st.caption("These settings drive Modelo 303, 130, OSS, and other tax computations.")

    tax = cfg.get("tax", {})

    st.markdown("##### Fiscal Regime")
    REGIME_OPTIONS = ["estimacion_directa_simplificada", "estimacion_directa_normal", "modulos"]
    regime_val = tax.get("regime", "estimacion_directa_simplificada")
    if regime_val not in REGIME_OPTIONS:
        REGIME_OPTIONS.append(regime_val)
    regime = st.selectbox(
        "Regime", REGIME_OPTIONS,
        index=REGIME_OPTIONS.index(regime_val), key="tax_regime",
        help="Gates the 5% gastos de difícil justificación in Modelo 130 "
             "(only estimación directa simplificada is eligible).",
    )

    col1, col2, col3 = st.columns(3)
    vat_registered = col1.checkbox(
        "VAT registered (IVA)", value=tax.get("vat_registered", True),
        key="tax_vat_reg",
        help="If off, Spanish sales are treated as exempt (no IVA devengado) "
             "and no input IVA is deducted in Modelo 303.",
    )
    oss_registered = col2.checkbox(
        "OSS registered", value=tax.get("oss_registered", False),
        key="tax_oss_reg",
        help="Only if enrolled in the One Stop Shop (Modelo 035). If off (default), no OSS "
             "return is generated and EU B2C sales are taxed at Spanish 21% "
             "(EU_B2C_ES21, art. 73 LIVA — below the €10,000 EU threshold).",
    )
    vat_proration = col3.number_input(
        "VAT proration % (legacy provisional fallback)", min_value=0, max_value=100,
        value=tax.get("vat_proration_percentage", 100),
        key="tax_vat_prorrata",
        help="Legacy flat pro-rata %. Only used, as the provisional %, when it is not 100 "
             "and the previous year has no `prorrata.definitive_pct_by_year` entry in the "
             "config.",
    )

    st.markdown("##### EU VAT defaults")
    st.caption(
        "B2B vs. B2C is decided by the customer's VAT id: a sale "
        "to a customer with a known VAT id (Geographic Rules → Customer VAT IDs) is always "
        "`IVA_EU_B2B` (reverse charge), for every activity. The selector below only picks "
        "the treatment for a sale **without** a known VAT id — `EU_B2C_ES21` (Spanish 21%) "
        "or `OSS_EU` (only applied when OSS registered is on above; otherwise coerced to "
        "`EU_B2C_ES21`, since there is no OSS return to declare it on)."
    )
    # IVA_EU_B2C is intentionally excluded: compute_modelo_303's aggregation
    # (src/returns/modelo_303.py) has no devengado box or audit-record bucket for it,
    # so selecting it would silently drop the income from the quarterly VAT
    # return. Only offer treatments the engine actually accounts for.
    EU_B2C_OPTIONS = ["EU_B2C_ES21", "OSS_EU"]

    def _eu_b2c_selectbox(col, label: str, config_key: str, key: str):
        configured = tax.get(config_key, "EU_B2C_ES21")
        options = EU_B2C_OPTIONS
        if configured not in options:
            # Stale config (e.g. a legacy IVA_EU_B2B default from before B2B depended on the VAT id) —
            # keep it selectable so the page doesn't crash, but it no longer has
            # any effect: B2B now depends solely on the customer's VAT id.
            options = options + [configured]
        return col.selectbox(label, options, index=options.index(configured), key=key)

    col1, col2, col3 = st.columns(3)
    eu_coaching = _eu_b2c_selectbox(
        col1, "EU Coaching (no VAT id)", "default_vat_treatment_eu_coaching", "tax_eu_coaching")
    eu_newsletter = _eu_b2c_selectbox(
        col2, "EU Newsletter (no VAT id)", "default_vat_treatment_eu_newsletter", "tax_eu_newsletter")
    eu_illustrations = _eu_b2c_selectbox(
        col3, "EU Illustrations (no VAT id)", "default_vat_treatment_eu_illustrations", "tax_eu_illustrations")

    st.divider()
    if st.button("Save Tax Settings", type="primary", key="save_tax_settings"):
        # Update only the keys this form edits: the tax section also holds
        # settings it doesn't show (prorrata, activity_start_date, …).
        cfg.setdefault("tax", {}).update({
            "regime": regime,
            "vat_registered": vat_registered,
            "oss_registered": oss_registered,
            "vat_proration_percentage": int(vat_proration),
            "default_vat_treatment_eu_coaching": eu_coaching,
            "default_vat_treatment_eu_newsletter": eu_newsletter,
            "default_vat_treatment_eu_illustrations": eu_illustrations,
        })
        save_config(cfg)
        st.success("Tax settings saved to config.json")


def render() -> None:
    """Render the Configuration tab."""
    show_flash("config")
    cfg = load_config()
    rules = load_rules()

    config_tabs = st.tabs([
        "Classification Rules",
        "Geographic Rules",
        "Stripe API",
        "App Settings",
        "Tax Settings",
    ])

    with config_tabs[0]:
        _render_classification_rules(rules)
    with config_tabs[1]:
        _render_geographic_rules(rules)
    with config_tabs[2]:
        _render_stripe_api()
    with config_tabs[3]:
        _render_app_settings()
    with config_tabs[4]:
        _render_tax_settings(cfg)
