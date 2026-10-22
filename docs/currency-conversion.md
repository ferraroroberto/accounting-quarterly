# Currency Conversion

Non-EUR transactions and invoices are automatically converted to EUR using daily exchange rates from the European Central Bank (ECB).

**Source:** [Frankfurter API](https://www.frankfurter.app) — free, open-source, based on ECB reference rates. No API key required.

**How it works:**

1. Load historical FX rates via the **Currency** tab (or they are fetched on-demand)
2. Rates are stored in SQLite (`fx_rates` table) for offline access
3. When a non-EUR transaction is loaded, the rate for its date is looked up
4. If no rate exists for the exact date (weekends, holidays), the most recent previous rate is used
5. If no rate exists at all, the system attempts a live fetch from the Frankfurter API

**Supported currency pairs (expressed as 1 EUR = X):**

| Pair | Description |
|------|-------------|
| EUR/USD | US Dollar |
| EUR/GBP | British Pound |
| EUR/CHF | Swiss Franc |
| EUR/AUD | Australian Dollar |

`src.fx_rates.get_currencies_in_use()` also picks up any other currency actually seen in stored invoices or transactions, so a new one is backfilled automatically without a code change.

**Auto-backfill.** `src.fx_rates.backfill_to_today()` fetches and stores rates from the last stored date up to today. It runs once at app start (`app/streamlit_app.py`) and is exposed as `close_quarter.py fx-backfill` for the quarterly close — both are cheap and idempotent, and network failures are caught and logged rather than raised, so they can't break startup.

**Stale rates are flagged, not silent.** When no rate exists for the exact date, the most recent earlier rate is used, but if that fallback date is more than 5 days older than the date requested, the lookup is flagged `is_stale` (`src.fx_rates.get_rate_with_fallback_info`) — surfaced as a ⚠️ warning in the Invoice OCR / Invoice Ledger tabs, in the audit trail, and in the logs. A missing rate is never silently left unconverted either — it comes back flagged (`fx_source="NO_RATE"`) with an explanation, instead of a quiet pass-through of an unconverted amount.
