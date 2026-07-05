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

# --- Tab imports and rendering ---
from app.welcome import render as render_welcome
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

with tab_welcome:
    render_welcome()

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
