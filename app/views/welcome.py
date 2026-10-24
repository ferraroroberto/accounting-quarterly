"""Welcome tab content."""
from __future__ import annotations

import streamlit as st


# The dashboard tabs after Welcome, in display order: the one place the tab names live
# (``streamlit_app.py`` builds its ``st.tabs`` from ``TAB_NAMES``), so this guide cannot drift from them.
TAB_GUIDE: list[tuple[str, str]] = [
    ("Quarter Report",
     "View income summaries for a selected quarter or year. "
     "Includes geographic breakdown, monthly tables by region, "
     "classification status, and Excel export."),
    ("Transaction Browser",
     "Search and filter individual transactions by date, activity type, "
     "geography, or description. Add geographic overrides for specific "
     "clients directly from this view."),
    ("History & Charts",
     "Load all quarters at once to see a summary table and stacked area "
     "charts showing income trends by activity type (Coaching, Newsletter, "
     "Illustrations) and by geographic region (Spain, EU, Outside EU)."),
    ("Currency",
     "Manage ECB exchange rates. Load historical rates for EUR/USD, "
     "EUR/GBP, and EUR/CHF, view interactive charts, and use the "
     "conversion calculator. Rates are stored locally for offline use."),
    ("Configuration",
     "Edit classification rules (activity keywords, geographic defaults, "
     "client overrides), manage the Stripe API key, and clear the cache."),
    ("Invoice Upload",
     "Upload invoice PDFs to your accounting partner. Scans the "
     "`invoices/in` and `invoices/out` directories, tracks which files "
     "have already been uploaded, and sends only new ones."),
    ("Invoice OCR",
     "AI-powered extraction of Spanish accounting data from any PDF "
     "(invoice, receipt, ticket). Routes through the configured OCR backend "
     "(local-llm-hub) to parse vendor, "
     "client, IVA, IRPF, and totals, and stores results in the `invoices` "
     "table. Supports in (expenses) and out (income) documents."),
    ("Invoice Ledger",
     "Review and correct the tax fields of every extracted invoice (treatment, "
     "deductible %, activity, geography, exclusion). An edited field is locked, so a "
     "later re-extraction never overwrites it. Also records FX exchange differences "
     "and registers an expense as a fixed asset."),
    ("Duplicate Review",
     "Proposes invoice duplicates found by five detectors. Confirm the invoice to "
     "keep in each group and the others are excluded from the tax computations."),
    ("Vendors",
     "Edit the vendor registry (`vendors.json`): tax treatment, activity and VAT id "
     "per vendor. Import vendors, apply the registry to stored invoices and review "
     "unknown vendors."),
    ("Fixed Assets",
     "Register and edit depreciable assets, see the depreciation schedule that feeds "
     "**Modelo 130**, and keep the VAT capital-goods regularisation register used by "
     "the **Modelo 303**."),
    ("Invoice Explorer",
     "Browse and filter all OCR-extracted invoices in a single table. "
     "Filter by vendor, client, direction, date range, subtotal, category, "
     "and invoice type. Export filtered results to CSV."),
    ("Seguridad Social",
     "Import Seguridad Social cuota payments from a bank account export (Excel or CSV). "
     "Stores payments in the `social_security_payments` table and automatically includes "
     "them as deductible expenses in **Modelo 130** (box 02 — gastos deducibles YTD). "
     "Supports quarterly breakdown and CSV export."),
    ("Tax Obligations",
     "Spanish autónomo tax filing assistant. Run **Calculate tax** to persist "
     "Modelo 303 (IVA), 130 (IRPF advance), OSS, 349 (intra-EU), and 347 "
     "(annual) snapshots in SQLite; the UI reads stored results until you "
     "recalculate. Includes a tax calendar with deadlines and filing status."),
    ("Filing Sheet",
     "What to type into each AEAT form for a quarter: the stored 303 / 130 / 349 boxes "
     "in form order with copyable values, the 303 credit chain, the 349 operators and "
     "the deadlines. **Mark filed** freezes the snapshot as the filed version."),
    ("Reconciliation",
     "Lines up each filed AEAT return (imported receipt) against the app's figures, "
     "box by box, for Modelo 303, 130, 349 and 390. Status: exact, catalogued "
     "divergence, unexplained, or one side missing. Known divergences are kept in "
     "`divergences.json`."),
    ("Tax Audit",
     "Full calculation audit trail. For every cell in Modelo 303, 130, 349, OSS, and 347 "
     "you can inspect the exact formula applied, all named input values, and the "
     "computed result. Entries are written to `tax_audit_log` each time you hit "
     "Calculate Tax. Download any run as JSON."),
]
TAB_NAMES: list[str] = ["Welcome", *(name for name, _ in TAB_GUIDE)]


def render() -> None:
    """Render the Welcome tab."""
    st.title("Stripe Accounting Dashboard")

    st.markdown(
        "This dashboard automates the classification and reporting of Stripe "
        "payments for quarterly accounting. Transactions are fetched from the "
        "**Stripe API**, classified by activity type and geographic region, "
        "converted to EUR using ECB exchange rates, and aggregated into reports. "
        "Use **Tax Obligations** to save computed Spanish filing figures to SQLite, "
        "and **Reconciliation** to compare those engine outputs with gestor-filed "
        "AEAT reference data."
    )

    st.markdown("---")
    st.subheader("Where is data loaded from?")

    data_col1, data_col2 = st.columns(2)
    with data_col1:
        st.markdown(
            "**Stripe transactions** are fetched live from the Stripe API "
            "(API key configured in the Configuration tab)."
        )
    with data_col2:
        st.markdown(
            "**Exchange rates** are fetched from the European Central Bank "
            "(ECB) via the Frankfurter API and stored locally in SQLite. "
            "Non-EUR amounts (USD, GBP, CHF) are automatically converted "
            "to EUR using the daily rate for each transaction date."
        )

    st.markdown("---")
    st.subheader("Tabs")

    for name, description in TAB_GUIDE:
        st.markdown(f"**{name}** - {description}")

    st.markdown("---")
    st.caption(
        "All data is stored locally in SQLite (data/accounting.db). "
        "Classification rules are in classification_rules.json. "
        "App settings are in config.json."
    )
