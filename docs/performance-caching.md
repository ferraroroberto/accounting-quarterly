# Performance & Caching

Streamlit re-runs the entire app script on every user interaction (widget change, tab switch). To avoid re-querying SQLite on every render, key data-loading paths are wrapped with `@st.cache_data(ttl=300)`:

| Cached function | Where | What it avoids |
|----------------|-------|----------------|
| `_cached_reconciliation()` · `_cached_filed_periods()` · `_cached_logged_audit()` | `app/tax_validation.py` | Engine computation + filed-return reads on every Reconciliation tab interaction (the catalogue is applied after the cache, so edits show at once) |
| `_load_invoices_df()` | `app/invoice_explorer.py` | Full `invoices` table scan + type conversions on every filter interaction |
| `_sidebar_stats()` | `app/streamlit_app.py` | 5 DB queries on every widget interaction across all tabs |

**Cache TTL:** 5 minutes. Results auto-refresh after 5 minutes, or immediately via the **↺ Refresh** button present in the Reconciliation and Invoice Explorer tabs.

**Invalidation rules:**
- Reconciliation: click **↺ Refresh** after running **Calculate tax** or loading new data to see updated figures
- Invoice Explorer: click **↺ Refresh** after extracting new invoices via OCR to see them in the table
- Sidebar stats: auto-refresh every 5 minutes (no manual control needed)
