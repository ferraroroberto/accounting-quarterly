"""Main Streamlit entry point - tab-based layout with welcome page."""
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import streamlit as st

from src.database import init_db
from src.fx_rates import get_rate_count, init_fx_table

# Initialise database and FX table on startup
init_db()
init_fx_table()

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
    st.markdown("**Data source:** `API`")
    st.caption("Stripe transactions are loaded live from the Stripe API.")
    fx_count = get_rate_count()
    st.caption(f"FX rates stored: {fx_count}")
    st.caption("v2.1")

# --- Main content: horizontal tabs ---
(tab_welcome, tab_report, tab_browser, tab_history,
 tab_currency, tab_config, tab_invoices) = st.tabs([
    "Welcome",
    "Quarter Report",
    "Transaction Browser",
    "History & Charts",
    "Currency",
    "Configuration",
    "Invoice Upload",
])

# --- Welcome tab ---
with tab_welcome:
    st.title("Stripe Accounting Dashboard")

    st.markdown(
        "This dashboard automates the classification and reporting of Stripe "
        "payments for quarterly accounting. Transactions are fetched from the "
        "**Stripe API**, classified by activity type and geographic region, "
        "converted to EUR using ECB exchange rates, and aggregated into reports."
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
from app.transaction_browser import render as render_transaction_browser
from app.history import render as render_history
from app.currency import render as render_currency
from app.configuration import render as render_configuration
from app.invoice_upload import render as render_invoice_upload

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
