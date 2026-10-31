# Dashboard Tabs

The three tabs that make up the main reporting flow — the view a gestor or asesor fiscal is walked through first. Each reads classified payments via `src/payments_loader.get_classified_for_period()`, which classifies on load from `db` (SQLite, the default) or `api` (live Stripe refresh) and caches the result in `st.session_state` until the period or mode changes.

## Quarter Report

Module: `app/views/quarter_report.py`.

Income summary for one period — a quarter, a full year, "Since inception", or a custom date range. Shows:

- Headline metrics: total income, total fees, net income, transaction count.
- Income-type breakdown (Coaching, Newsletter, Illustrations) with per-type fees and transaction counts.
- Geographic breakdown (Spain, EU not Spain, Outside EU) with per-region fees and transaction counts.
- A monthly breakdown table per geographic region, with a totals row.
- Classification status (activity errors / unclassified count), with a **Show details** JSON dump.
- A warning when any EUR charge fell to the `eur_default` (Spain) rule for a customer that looks foreign — points at **Transaction Browser** to review.
- **Generate Excel Report** — exports the period's payments via `app/excel_download.render_excel_download()`.

## Transaction Browser

Module: `app/views/transaction_browser.py`.

Per-transaction search and filtering, plus the tool for fixing geographic misclassifications:

- Filters: year, quarter (or full year), description search, activity type, geography.
- **Load (from SQLite)** reads cached classified data; **Refresh from API** re-fetches from Stripe and reclassifies.
- The filtered transaction table (date, ID, description, activity type, geography, amounts, classification rule, a **Review** flag for the foreign-customer warning).
- **Raw database (SQLite)** — a separate query straight against the `transactions` table (own search/filter inputs, a row limit, the SQL used, and a payload inspector that shows the stored `raw_source_json` for a selected row).
- **Add Geographic Override** — a form that adds a client name/email/keyword override to `classification_rules.json`'s `geographic_rules`, applied immediately (invalidates the data-loader cache).

## History & Charts

Module: `app/views/history.py`.

Every quarter since the first stored transaction, loaded and classified at once, for trend views:

- On first visit, loads every quarter from SQLite; **Refresh history from API** reclassifies from a live Stripe fetch instead.
- A quarterly summary table (newest first): per-quarter income by activity type, total income, total fees, net, transaction count.
- Two stacked-area charts (Plotly): income by activity type (Coaching / Newsletter / Illustrations) and income by geographic region (Spain / EU not Spain / Outside EU), both across all loaded quarters.
- **Export Quarter** — pick any loaded quarter and generate its Excel report via the same `render_excel_download()` helper as Quarter Report.
