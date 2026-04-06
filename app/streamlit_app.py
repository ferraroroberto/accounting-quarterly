"""Main Streamlit entry point - tab-based layout with welcome page."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from src.database import get_invoice_stats, get_latest_stripe_sync_at, get_transaction_count_db, init_db
from src.fx_rates import get_latest_fx_sync_at, get_rate_count, init_fx_table
from src.social_security import get_ss_count

# Initialise database and FX table on startup
init_db()
init_fx_table()


# ── Cached sidebar stats (5-minute TTL) ─────────────────────────────────────
# These functions run on every Streamlit re-render (every widget interaction).
# Caching them avoids 5 DB round-trips per render.

@st.cache_data(ttl=300, show_spinner=False)
def _sidebar_stats() -> dict:
    return {
        "tx_count": get_transaction_count_db(),
        "stripe_last_sync": get_latest_stripe_sync_at(),
        "fx_count": get_rate_count(),
        "fx_last_sync": get_latest_fx_sync_at(),
        "inv_stats": get_invoice_stats(),
    }

st.set_page_config(
    page_title="Stripe Accounting Dashboard",
    page_icon="S",
    layout="wide",
    initial_sidebar_state="expanded",
)

# --- Sidebar: project description only ---
with st.sidebar:
    st.title("Stripe Accounting")
    st.markdown(
        "Automated payment classification and quarterly reporting. "
        "Classifies Stripe payments by activity type and geographic region, "
        "generates Excel reports, and tracks invoice uploads."
    )
    st.markdown("---")
    _stats = _sidebar_stats()

    st.markdown("**Stripe data:** `API`")
    st.caption("Stripe transactions are loaded from the Stripe API.")
    st.caption(f"Transactions stored: {_stats['tx_count']}")
    _stripe_sync = _stats["stripe_last_sync"]
    st.caption(f"Stripe last update: {_stripe_sync.strftime('%Y-%m-%d %H:%M') if _stripe_sync else 'n/a'}")

    st.markdown("**FX data:** `API`")
    st.caption("FX rates loaded from the ECB (Frankfurter API).")
    st.caption(f"FX rates stored: {_stats['fx_count']}")
    _fx_sync = _stats["fx_last_sync"]
    st.caption(f"FX last update: {_fx_sync.strftime('%Y-%m-%d %H:%M:%S') if _fx_sync else 'n/a'}")

    st.markdown("**Invoices data** `OCR`")
    st.caption("Invoices are extracted from PDF files using OCR.")
    _inv_stats = _stats["inv_stats"]
    _in_last = _inv_stats["in"]["last_extracted_at"]
    _out_last = _inv_stats["out"]["last_extracted_at"]
    st.caption(f"Expenses (in):  {_inv_stats['in']['count']}")
    st.caption(f"  Expenses last extracted: {_in_last[:10] if _in_last else 'n/a'}")
    st.caption(f"Income (out):   {_inv_stats['out']['count']}")
    st.caption(f"  Income last extracted: {_out_last[:10] if _out_last else 'n/a'}")

    st.markdown("**Seguridad Social** `Bank export`")
    st.caption("SS cuota payments imported from bank account exports.")
    _ss_count = get_ss_count()
    st.caption(f"SS payments stored: {_ss_count}")

# --- Main content: horizontal tabs ---
(tab_welcome, tab_report, tab_browser, tab_history,
 tab_currency, tab_config, tab_invoices, tab_invoice_ocr,
 tab_invoice_explorer, tab_ss, tab_tax, tab_validation, tab_audit) = st.tabs([
    "Welcome",
    "Quarter Report",
    "Transaction Browser",
    "History & Charts",
    "Currency",
    "Configuration",
    "Invoice Upload",
    "Invoice OCR",
    "Invoice Explorer",
    "Seguridad Social",
    "Tax Obligations",
    "Tax Validation",
    "Tax Audit",
])

# --- Welcome tab ---
with tab_welcome:
    st.title("Stripe Accounting Dashboard")

    st.markdown(
        "This dashboard automates the classification and reporting of Stripe "
        "payments for quarterly accounting. Transactions are fetched from the "
        "**Stripe API**, classified by activity type and geographic region, "
        "converted to EUR using ECB exchange rates, and aggregated into reports. "
        "Use **Tax Obligations** to save computed Spanish filing figures to SQLite, "
        "and **Tax Validation** to compare those engine outputs with gestor-filed "
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

    tabs_info = [
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
         "(invoice, receipt, ticket). Uses Google Gemini to parse vendor, "
         "client, IVA, IRPF, and totals, and stores results in the `invoices` "
         "table. Supports in (expenses) and out (income) documents."),
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
        ("Tax Validation",
         "Cross-check gestor-filed AEAT figures (from `tmp/validation/validation.yaml`) "
         "against database-computed values for Modelo 130, 303, 349, and 390 — "
         "line-by-line casilla comparison with OK / high / low status."),
        ("Tax Audit",
         "Full calculation audit trail. For every cell in Modelo 303, 130, 349, OSS, and 347 "
         "you can inspect the exact formula applied, all named input values, and the "
         "computed result. Entries are written to `tax_audit_log` each time you hit "
         "Calculate Tax. Download any run as JSON."),
    ]

    for name, description in tabs_info:
        st.markdown(f"**{name}** - {description}")

    st.markdown("---")
    st.caption(
        "All data is stored locally in SQLite (data/accounting.db). "
        "Classification rules are in classification_rules.json. "
        "App settings are in config.json."
    )


# --- Tab imports and rendering ---
from app.quarter_report import render as render_quarter_report
from app.social_security_tab import render as render_social_security
from app.tax_obligations import render as render_tax_obligations
from app.transaction_browser import render as render_transaction_browser
from app.history import render as render_history
from app.currency import render as render_currency
from app.configuration import render as render_configuration
from app.invoice_upload import render as render_invoice_upload
from app.invoice_ocr_tab import render as render_invoice_ocr
from app.invoice_explorer import render as render_invoice_explorer
from app.tax_validation import render as render_tax_validation
from app.tax_audit import render as render_tax_audit

with tab_report:
    render_quarter_report()

with tab_browser:
    render_transaction_browser()

with tab_history:
    render_history()

with tab_currency:
    render_currency()

with tab_config:
    render_configuration()

with tab_invoices:
    render_invoice_upload()

with tab_invoice_ocr:
    render_invoice_ocr()

with tab_invoice_explorer:
    render_invoice_explorer()

with tab_ss:
    render_social_security()

with tab_tax:
    render_tax_obligations()

with tab_validation:
    render_tax_validation()

with tab_audit:
    render_tax_audit()
