# Stripe Accounting Quarterly Automation

Automated Stripe payment classification and quarterly reporting system. Classifies payments by activity type (Coaching, Newsletter, Illustrations) and geographic region (Spain, EU-not-Spain, Outside-EU), then produces Excel reports, Spanish tax obligation snapshots, gestor-vs-database **Tax Validation**, and a Streamlit dashboard.

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv
# Windows
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
# macOS / Linux
.venv/bin/pip install -r requirements.txt
```

### 2. Configure

Copy the example files and edit them:

```bash
cp config.json.example config.json
cp classification_rules.json.example classification_rules.json
cp vendors.json.example vendors.json  # vendor registry (optional; see "Vendor Registry")
cp .env.example .env  # add your Stripe API key (and optional Accounting API settings)
```

### 3. Launch the dashboard

```bash
# Windows
.\.venv\Scripts\python.exe -m streamlit run app/streamlit_app.py
# macOS / Linux
.venv/bin/streamlit run app/streamlit_app.py
```

A `launch_app.bat` shortcut is provided for Windows.

---

## Data Flow

```
Stripe API (live charges)
          │
          ▼
   Fetch & deduplicate
   (upserted into SQLite)
          │
          ▼
   FX conversion (non-EUR → EUR)
   using ECB daily rates from SQLite
          │
          ▼
   Classify activity + geography
   (rules from classification_rules.json)
          │
          ▼
   Persist classifications to SQLite
          │
          ▼
   Aggregate, display, export; save tax snapshots; validate vs. filed AEAT data
   (VAT treatment derived on-the-fly by the tax engine — not stored per transaction)
```

Transaction data is fetched from the Stripe API and stored in the local SQLite database (`data/accounting.db`). On subsequent loads the dashboard reads pre-classified data directly from the database — the classifier only runs when new data is fetched from the API, or when you run `scripts/close_quarter.py reclassify` after a rule change (see "Closing a Quarter"). Non-EUR amounts (GBP, USD, CHF) are converted to EUR using ECB exchange rates. If a rate is missing for a transaction date, the system fetches it from the Frankfurter API or falls back to the most recent available rate.

---

## Project Structure

```
├── config.json                    # App settings (git-ignored, use config.json.example)
├── classification_rules.json      # Classification rules (git-ignored, copy from .example)
├── classification_rules.json.example
├── vendors.json                   # Vendor registry (git-ignored, copy from .example — see "Vendor Registry")
├── vendors.json.example
├── config.json.example
├── requirements.txt
├── launch_app.bat                 # Windows launch shortcut
├── scripts/
│   └── close_quarter.py           # Deterministic quarterly-close helper (see "Closing a Quarter")
├── src/                           # Core business logic
│   ├── models.py                  # Pydantic data models (Payment, ClassifiedPayment, ...)
│   ├── _json_store.py             # Shared cached-JSON store backing config.py and rules_engine.py
│   ├── config.py                  # Load/save config.json
│   ├── rules_engine.py            # Load/save classification_rules.json
│   ├── classifier.py              # Activity, geographic and VAT classification (+ eur_default foreign-customer warning)
│   ├── reclassify.py              # Re-run the classifier over stored transactions, logging every change
│   ├── declared_reports.py        # Frozen (declared) Stripe reports: immutable per-transaction EUR amounts
│   ├── aggregator.py              # Monthly/quarterly aggregations and totals
│   ├── excel_exporter.py          # Multi-sheet Excel report generation
│   ├── stripe_client.py           # Stripe API wrapper (charges, fees, card country)
│   ├── fx_rates.py                # FX rate fetching (ECB/Frankfurter), storage, conversion
│   ├── database.py                # SQLite operations (transactions, FX rates, upload log, invoices, SS, tax, audit)
│   ├── social_security.py         # SS cuota import from bank exports + DB query helpers
│   ├── invoice_dedupe.py          # Duplicate/receipt/out-of-period detection + exclusion (issue #92)
│   ├── tax_models.py              # Dataclasses for Modelo303, Modelo130, OSS, 347, 349 results + AuditEntry
│   ├── vat_rules.py               # Single source of truth: activity×geo VAT matrix, OSS rates, base extraction
│   ├── tax_engine.py              # Spanish tax computation: Modelo 303/130/349/347, OSS, EU B2C threshold, calendar
│   ├── tax_snapshot_codec.py      # Serialize/deserialize tax engine results for SQLite snapshot storage
│   ├── tax_validator.py           # Validation: compare gestor-filed AEAT figures vs DB-computed values
│   ├── filed_returns.py           # Import filed AEAT receipt PDFs (303/130/349/390) as reference data + CLI
│   ├── fixed_assets.py            # Fixed assets: simplified-table depreciation, VAT capital goods (303 30/31), regularisation
│   ├── accounting_api_client.py   # IntegraLOOP/BILOOP Accounting API client
│   ├── invoice_ocr.py             # PDF extraction for Spanish accounting (local-llm-hub default, direct Gemini fallback)
│   ├── vendor_registry.py         # Vendor registry: match invoices to vendors, apply tax defaults, xlsx seed/import (CLI)
│   ├── logger.py                  # Rotating file logger
│   └── exceptions.py              # Custom exception classes
├── app/                           # Streamlit dashboard
│   ├── streamlit_app.py           # Entry point: welcome page and horizontal tabs
│   ├── data_loader.py             # Data loading, FX conversion, classification pipeline
│   ├── quarter_report.py          # Quarterly summary + Excel export
│   ├── transaction_browser.py     # Browse/filter transactions + geographic overrides
│   ├── history.py                 # Timeline charts across all quarters
│   ├── currency.py                # FX rate management, charts, and conversion tool
│   ├── configuration.py           # Rules editor, Stripe API key, tax settings, cache
│   ├── invoice_upload.py          # Accounting partner (IntegraLOOP/BILOOP) integration
│   ├── invoice_ocr_tab.py         # AI invoice extraction tab (OCR via local-llm-hub / Gemini)
│   ├── invoice_ledger.py          # Invoice Ledger tab: tax treatment, exclusions, corrections (locked vs re-OCR)
│   ├── invoice_dedupe_tab.py      # Duplicate Review tab: scan/confirm/apply invoice_dedupe.py groups
│   ├── vendor_registry_tab.py     # Vendors tab: registry editor, apply, unknown vendors, spreadsheet import
│   ├── fixed_assets_tab.py        # Fixed Assets tab: assets grid, manual add, schedule, VAT register (+ ledger hook)
│   ├── invoice_explorer.py        # Filterable table of all extracted invoices
│   ├── social_security_tab.py     # Seguridad Social tab: import bank export + view cuotas
│   ├── tax_obligations.py         # Tax obligations tab (Modelo 303/130/349/347, OSS)
│   ├── tax_validation.py          # Tax validation tab (gestor-filed vs DB-computed comparison)
│   └── tax_audit.py               # Tax audit trail tab (per-cell formula + inputs drill-down)
├── tests/                         # Pytest test suite
│   ├── conftest.py                # Shared fixtures
│   ├── test_classifier.py
│   ├── test_models.py
│   ├── test_database.py
│   ├── test_fx_rates.py
│   ├── test_rules_engine.py
│   ├── test_aggregator.py
│   ├── test_tax_engine.py         # VAT classification, Modelo 303/130, OSS, Modelo 349
│   ├── test_invoice_ledger.py     # Ledger migration/backfill, edit locks, excluded rows, invoice-date keying
│   ├── test_invoice_ledger_tab.py # Invoice Ledger tab (AppTest)
│   ├── test_stripe_eu_b2c_reclassify.py  # EU B2C at 21%, reclassify, frozen reports, threshold
│   ├── test_tax_validator.py      # Gestor-filed vs DB-computed validation
│   ├── test_filed_returns.py      # AEAT receipt parser/import (synthetic PDFs only)
│   ├── test_invoice_dedupe.py     # Dedupe detectors, keeper rule, locked rows, engine picks up exclusions
│   ├── test_invoice_dedupe_tab.py # Duplicate Review tab (AppTest)
│   ├── test_vendor_registry.py    # Vendor matching, defaults vs locks, unknown vendors, xlsx seed
│   ├── test_vendor_registry_tab.py # Vendors tab + ledger unknown-vendor flag (AppTest)
│   └── test_fixed_assets.py       # Depreciation, threshold, posting modes, capital-good VAT, 130 hook, tab
├── data/
│   ├── accounting.db              # SQLite database (git-ignored)
│   ├── processed/                 # Generated Excel reports
│   ├── cache/                     # Temporary cache files
│   └── invoices/
│       ├── in/                    # Invoices received (PDFs)
│       └── out/                   # Invoices produced (PDFs)
├── tmp/
│   ├── social_security_bank_export.xlsx  # Bank export for SS cuotas (git-ignored, configurable)
│   ├── validation/
│   │   └── validation.yaml        # Fallback gestor-filed reference data (git-ignored)
│   └── close_quarter/             # Output of scripts/close_quarter.py (git-ignored)
│       ├── invoice_copy_log.json  # Cumulative "already swept" invoice manifest
│       └── <year>_Q<quarter>/     # Swept invoices + Stripe_Report_Q<quarter>_<year>.xlsx
└── logs/                          # Rotating daily log files
```

---

## Classification Logic

Classification rules are defined in `classification_rules.json` and can be edited directly or through the Configuration tab in the dashboard.

### Activity type

Rules are evaluated in priority order; the first match wins.

| Priority | Match Type | Activity |
|----------|-----------|----------|
| 1 | Empty / null description | COACHING |
| 2 | Luma `registration` payment type | COACHING |
| 3 | Description contains illustration keywords | ILLUSTRATIONS |
| 4 | Description contains newsletter keywords | NEWSLETTER |
| 5 | Description contains coaching keywords | COACHING |
| - | No pattern matched | UNKNOWN |

### Geographic region

**EUR charges** — unchanged, currency alone decides the branch:

| Priority | Condition | Default region |
|----------|-----------|----------------|
| 1 | EUR + explicit name/email override | Per override |
| 2 | EUR + activity is **NEWSLETTER** | EU_NOT_SPAIN |
| 3 | EUR + any other activity | SPAIN |

**Non-EUR charges** — classified by the *charge country* first (card issuing country → Stripe billing address → Stripe customer address, first one set wins), currency only as a fallback when no country is known at all (#111 — a non-EUR currency alone is not evidence of being outside the EU: DKK, SEK, PLN, CZK, HUF, RON and BGN are EU member-state currencies):

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | Charge country known, `ES` | SPAIN |
| 2 | Charge country known, other EU member state | EU_NOT_SPAIN |
| 3 | Charge country known, non-EU | OUTSIDE_EU |
| 4 | No charge country, currency is an EU non-euro currency (DKK/SEK/PLN/CZK/HUF/RON/BGN) | EU_NOT_SPAIN, flagged for review (`non_eur_currency_eu_review` rule — weaker signal than a known country) |
| 5 | No charge country, any other non-EUR currency | OUTSIDE_EU |

The default region for each EUR condition is configurable in the Geographic Rules section of the Configuration tab. Name/email overrides apply only to EUR charges; the charge-country rule cannot be overridden by name/email today.

**Foreign-customer warning.** A EUR charge that falls through to the `eur_default` rule is flagged with ⚠ when the customer looks non-Spanish: a card or billing-address country other than `ES`, or an email (Stripe customer email, billing email, or one written in the description) on a country-code domain other than `.es` (generic-use ccTLDs such as `.io`, `.co`, `.me` are ignored). The flag shows in `close_quarter.py stripe-fetch`, the Transaction Browser (**Review** column) and the Quarter Report. Fix it with a geographic override.

### Card issuing country

The card issuing country (`charge.payment_method_details.card.country`) is extracted from the Stripe API automatically. It is the first signal used to classify a non-EUR charge's geographic region (see above), and also improves the foreign-customer warning on EUR charges.

---

## Currency Conversion

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

**Stale rates are flagged, not silent.** When no rate exists for the exact date, the most recent earlier rate is used as before, but if that fallback date is more than 5 days older than the date requested, the lookup is flagged `is_stale` (`src.fx_rates.get_rate_with_fallback_info`) — surfaced as a ⚠️ warning in the Invoice OCR / Invoice Ledger tabs, in the audit trail, and in the logs. A missing rate is never silently left unconverted either — it comes back flagged (`fx_source="NO_RATE"`) with an explanation, instead of a quiet pass-through of an unconverted amount.

---

## Database

Transaction data is stored in a SQLite database (`data/accounting.db`):

- **transactions** — Stripe payment records with classification and FX conversion data. The VAT columns (`vat_treatment`, `vat_base_eur`, `vat_amount_eur`) are reserved for manual overrides; they are normally NULL — VAT treatment is derived on-the-fly by the tax engine at computation time, not stored per transaction
- **fx_rates** — Daily ECB exchange rates (EUR/USD, EUR/GBP, EUR/CHF, EUR/AUD, and any other currency seen in stored invoices/transactions)
- **upload_log** — Invoice upload tracking to prevent duplicates
- **invoices** — AI-extracted invoice records (vendor, client, IVA/IRPF breakdown, totals, Spanish AEAT fields). Includes `geo_region`, `vat_treatment`, `activity_type`, and `supply_country` columns auto-derived from the vendor/client NIF at insert time — mirroring the `transactions` table so both sources feed the tax engine uniformly — plus the ledger columns described in [Invoice Ledger](#invoice-ledger) (`tax_treatment`, split business-use %, `excluded`, `locked_fields`, …) and the FX resolution columns described in [Foreign-currency invoices: EUR resolution](#foreign-currency-invoices-eur-resolution) (`charged_eur`, `fx_rate_used`, `fx_source`, `fx_stale`, …)
- **fx_exchange_differences** — Gains/losses realised when a foreign-currency income balance booked at the ECB rate is later converted to EUR; see [Exchange rate differences](#exchange-rate-differences)
- **social_security_payments** — Seguridad Social cuota payments imported from bank account exports (or entered manually). Deduplication key: `(payment_date, amount_eur, description)`. Refunds are stored as negative amounts. Automatically included as deductible expenses in Modelo 130 box 02 (YTD)
- **quarterly_tax_entries** — Manual tax inputs (IVA soportado, gastos deducibles, retenciones)
- **tax_filing_status** — Filing status and computed amounts per model/quarter
- **tax_computation_snapshots** — JSON snapshots of tax engine outputs (Modelo 303/130/OSS/349/347) written when you click **Calculate tax** in Tax Obligations
- **filed_returns** — One row per non-empty box (casilla) of each filed AEAT return imported from its receipt PDF (model, year, period, box, value, justificante, CSV, presentation timestamp, source file). Reference data for Tax Validation — see "Importing filed AEAT receipts"
- **filed_349_operators** — Operator rows (country, VAT id, name, clave, base) of each imported Modelo 349 receipt
- **tax_audit_log** — Per-cell calculation audit entries: every box in every model records the formula applied, named inputs, and computed value. Written alongside snapshots; queryable by year/quarter/model/run timestamp
- **declared_reports** / **declared_report_lines** — The Stripe report actually sent to the gestor, frozen by `close_quarter.py report --freeze`: quarter, version, file name, SHA-256 of the `.xlsx`, and per-transaction EUR amounts. Insert-only (SQLite triggers reject UPDATE/DELETE); a corrected re-send is a new version. The tax engine uses the latest version's EUR amounts for every transaction it contains, so later FX re-conversions cannot move a declared quarter

Classifications are persisted in the database so the classifier only runs when fresh data is fetched from Stripe, not on every page load. Tax obligation figures shown in the Tax Obligations tab are read from stored snapshots until you run **Calculate tax** again.

---

## Stripe API

Set `STRIPE_API_KEY` in a `.env` file at the project root:

```
STRIPE_API_KEY=sk_live_...
```

Required permissions for restricted keys (`rk_live_...`):
- **Read charges** — transaction data, amounts, descriptions, card country
- **Read balance transactions** — fee details

The dashboard includes a connection tester and permission checker under **Configuration → Stripe API**.

---

## Closing a Quarter

`scripts/close_quarter.py` is the deterministic backbone for the recurring quarterly-close chore — invoked interactively via the `/close-quarter` Claude Code skill (`.claude/skills/close-quarter/`), which guides you through it step by step and pauses for confirmation/overrides at the points that need judgement. It can also be run by hand:

```bash
.venv/Scripts/python.exe scripts/close_quarter.py sweep [--year Y --quarter Q]         # copy new invoice PDFs
.venv/Scripts/python.exe scripts/close_quarter.py stripe-check [--days N]              # read-only API smoke test
.venv/Scripts/python.exe scripts/close_quarter.py stripe-fetch --year Y --quarter Q    # fetch + classify + persist
.venv/Scripts/python.exe scripts/close_quarter.py add-override "<key>" REGION [--type email|name]
.venv/Scripts/python.exe scripts/close_quarter.py reclassify --from YYYY-MM-DD [--to YYYY-MM-DD] [--dry-run]
.venv/Scripts/python.exe scripts/close_quarter.py backfill-emails [--dry-run]           # fill email/country from saved raw charges
.venv/Scripts/python.exe scripts/close_quarter.py report --year Y --quarter Q          # regenerate the Excel report
.venv/Scripts/python.exe scripts/close_quarter.py report --year Y --quarter Q --freeze # ...and freeze it as declared
.venv/Scripts/python.exe scripts/close_quarter.py fx-backfill                          # backfill ECB FX rates to today
.venv/Scripts/python.exe scripts/close_quarter.py fx-recompute [--dry-run] [--since D] # re-resolve stored invoices' EUR at the ECB rate
```

- **`sweep`** diffs `invoice_in_dir` / `invoice_out_dir` (recursively) against both the `invoices` DB table and a cumulative manifest (`tmp/close_quarter/invoice_copy_log.json`), copies only the files not seen before into `tmp/close_quarter/<year>_Q<quarter>/`, and updates the manifest — safe to rerun after adding more invoices.
- **`stripe-fetch`** flags transactions classified by a *default* geo rule (no client-specific override matched) so they can be double-checked before the report goes out, lists EUR charges that fell to `eur_default` for a foreign-looking customer (⚠), and prints the EU B2C year-to-date total against the €10,000 art. 73 LIVA threshold.
- **`reclassify`** re-runs the classifier over the transactions *already stored* from `--from` (optionally up to `--to`) with the current rules and overrides, prints every change (`old activity/geo (rule) -> new`) plus a per-quarter count, and writes only the changed rows. `--dry-run` reports without writing. Idempotent: a second run changes nothing. Run it after any rule change that should apply retroactively.
- **`backfill-emails`** fills empty stored `email_meta` / `billing_country` from each row's already-saved raw Stripe charge JSON (`raw_source_json`) — no Stripe API call, and a non-empty stored value is never overwritten. Use it once after upgrading to pick up `billing_details.email` / `billing_details.address.country` for rows fetched before that fallback existed. `--dry-run` reports counts without writing.
- **`report`** first reclassifies the quarter's stored rows (so a stale row can never be exported), then writes the Excel report. The exporter also refuses — `StaleClassificationError` — to write a non-EUR charge whose geography came from a EUR rule. With **`--freeze`** the written file becomes the quarter's immutable declared report (`declared_reports`); freezing an already-declared quarter needs `--supersede` (a new version, for a corrected re-send). Once a quarter is declared, a plain `report` writes `Stripe_Report_Q<Q>_<Y>_live.xlsx` instead of overwriting the sent file, and prints how the live rows differ from the declared ones.
- **`add-override`** appends to `classification_rules.json`'s `geographic_overrides` / `email_overrides` — the same mechanism as the Transaction Browser tab's "Add Geographic Override" form.
- **`fx-backfill`** fetches and stores ECB rates from the last stored date up to today for every currency seen in stored invoices/transactions (`src.fx_rates.backfill_to_today`) — idempotent, safe to rerun every close.
- **`fx-recompute`** re-runs `resolve_invoice_amounts` over every *already-stored* non-EUR invoice (`src.fx_rates.recompute_stored_invoice_fx`) — corrects invoices extracted before the FX resolver existed, or before a later fix to it, in place. Writes by default; pass `--dry-run` to preview (scanned/changed/stale/cross-check/locked-skipped counts plus a per-row old→new EUR list) without touching the DB. `--since YYYY-MM-DD` restricts the scan to invoices dated on/after that date. Never overwrites a row with `subtotal_eur`/`iva_amount`/`total_eur` in its `locked_fields` — those are reported as skipped, not silently kept or dropped — and never touches `eur_received`, which already wins over `subtotal_eur` in the tax engine regardless. Idempotent: a second run reports zero changes. The same recompute is available as a preview-then-apply button in the Currency tab.
- All output lives under `tmp/close_quarter/` (git-ignored) — nothing is uploaded or sent anywhere by this script.

---

## Seguridad Social (Social Security Cuotas)

The **Seguridad Social** tab imports monthly autónomo quota payments debited from your bank account and feeds them automatically into **Modelo 130** as deductible expenses.

### Data source

Cuota payments are not issued as invoices — they appear as bank debits. Export your bank statement (the rows corresponding to Seguridad Social payments, or the full statement filtered by concept) to Excel (`.xlsx` or legacy `.xls`) or CSV and import via this tab.

### Setup

1. Export the relevant rows from your bank's online portal to `.xlsx`, `.xls`, or `.csv`.
2. Place the file anywhere accessible (default: `tmp/social_security_bank_export.xlsx`).
3. Configure the column names in `config.json`:

```json
{
  "social_security": {
    "bank_export_file": "tmp/social_security_bank_export.xlsx",
    "date_column": "Fecha",
    "amount_column": "Importe",
    "description_column": "Más datos",
    "concept_column": "Movimiento",
    "concept_patterns": ["TGSS", "SEG.SOCIAL", "SEGURIDAD SOCIAL", "AUTONOMOS"],
    "sheet_name": 0,
    "skiprows": null
  }
}
```

- `skiprows`: leave `null` (or omit) to auto-detect the header row — the importer scans the first rows for the one containing both `date_column` and `amount_column`, so title rows above the header (e.g. "Movimientos de la cuenta ...") are skipped automatically. Set an explicit row index to override.
- `concept_column` / `concept_patterns`: optional. When `concept_column` is set, only rows whose value in that column matches one of `concept_patterns` (case/accent-insensitive substring match) are imported — useful when the export contains all bank movements rather than pre-filtered Social Security rows. Leave `concept_column` unset to import every row in the file (e.g. when the bank export is already filtered to Social Security movements only).

4. Open the **Seguridad Social** tab, verify the column mapping and detected header row with **Preview file columns**, then click **Import from file**.

### How it works

- Legacy `.xls` (BIFF) files are supported via `xlrd`, in addition to `.xlsx`/`.xlsm` (`openpyxl`) and `.csv`.
- Amounts are **sign-flipped and stored net of refunds**: a bank debit (negative in the export — a cuota payment) is stored as a positive contribution; a bank credit (positive in the export — e.g. the automatic *pluriactividad* excess-contribution refund) is stored as a **negative** contribution entry in the period it was received, so it nets off the total automatically.
- Deduplication is by `(payment_date, amount_eur, description)` — re-importing the same file is safe, and a same-day/same-amount contribution and refund (or two distinct concepts) don't collide.
- A **manual entry** fallback (expander below the import controls) lets you record a month missing from the bank export, or a refund, directly — subject to the same dedupe key.
- The **Modelo 130** engine sums all SS payments from January 1 through the end of the selected quarter (YTD) and includes them in **box 02 — gastos deducibles**, alongside OCR-extracted expense invoices and manual entries. Legal basis: cuotas de autónomo are fully deductible under Art. 30 LIRPF (*régimen de estimación directa*).
- The audit trail (Tax Audit tab) records `ss_gastos` and the full list of individual payments as named inputs to the `box_02_gastos` cell.
- `src/social_security.py`'s `get_ss_period_totals()` returns quarterly + yearly totals net of refunds for a given year, for reporting or future use by the Modelo 130 engine.

### Quarterly breakdown

The tab shows per-year totals and, when a year is selected, a quarterly breakdown (Q1–Q4) so you can reconcile against the TGSS monthly receipts.

---

## Tax Obligations (Spanish Autónomo)

The **Tax Obligations** tab turns the classified transaction data into pre-filled Spanish tax filings. It covers the standard obligations for an autónomo in *régimen de estimación directa simplificada*.

### Stored calculations

Computed figures are **not** recalculated on every page load. Click **Calculate tax** to run the engines and persist results to the `tax_computation_snapshots` table in SQLite (per selected year and quarter; Modelo **347** is annual and stored with quarter `0`). After you sync Stripe data, change manual tax entries, or adjust classifications, run **Calculate tax** again to refresh.

### Supported models

| Model | Name | Frequency | What it computes |
|-------|------|-----------|-----------------|
| **Modelo 303** | Declaración IVA Trimestral | Quarterly | IVA collected (devengado) vs. IVA paid (soportado); net to pay or refund |
| **Modelo 130** | Pago Fraccionado IRPF | Quarterly | 20% advance on YTD net profit, minus retenciones and prior payments |
| **Modelo 349** | Operaciones Intracomunitarias | Quarterly | Intra-EU B2B operations grouped by buyer VAT ID |
| **OSS Return** | One Stop Shop | Quarterly | B2C digital services to EU non-Spain customers, grouped by country — only when `oss_registered` is true |
| **EU B2C threshold** | Art. 73 LIVA | Live | Year-to-date EU B2C sales (ex-VAT) vs €10,000; warns at 80%, flags the previous year too |
| **Modelo 347** | Operaciones con Terceros | Annual | Spain counterparties with total operations > €3,005.06 (**importe IVA incluido**) |

### VAT treatment classification

VAT treatment is derived on-the-fly by the tax engine using each transaction's activity × geography — it is not stored per transaction. The mapping is:

| Activity | Geography | Treatment | IVA |
|----------|-----------|-----------|-----|
| Any | OUTSIDE_EU | `IVA_EXPORT` | 0% |
| Any | SPAIN | `IVA_ES_21` | 21% |
| COACHING / ILLUSTRATIONS | EU_NOT_SPAIN | `IVA_EU_B2B` | 0% (reverse charge) |
| NEWSLETTER | EU_NOT_SPAIN | `EU_B2C_ES21` (default, not OSS-registered) | 21% Spanish IVA, Modelo 303 box 01/03 |
| NEWSLETTER | EU_NOT_SPAIN | `OSS_EU` (only when `oss_registered: true`) | Buyer country rate, OSS return |

**EU consumers below the threshold.** Under art. 73 LIVA, electronically supplied services to consumers in other EU countries stay located in Spain — Spanish 21% IVA — while the year's and the previous year's EU B2C sales are at or below €10,000 (ex-VAT) and you have not opted into OSS. That is `EU_B2C_ES21`. If `default_vat_treatment_eu_newsletter` says `OSS_EU` but `oss_registered` is not true, the engine uses `EU_B2C_ES21` (there is no OSS return to declare it on). The **Tax Obligations → EU B2C / OSS** tab tracks the threshold and a warning banner appears at 80%.

### VAT-inclusive pricing (Stripe amounts)

Stripe records the gross amount charged to the customer, which for Spain and EU sales **includes VAT**. The engine extracts the taxable base by dividing the net received amount by `(1 + rate)`:

| Treatment | Base formula | Example |
|-----------|-------------|---------|
| `IVA_ES_21` | `net ÷ 1.21` | €121 gross → €100.00 base + €21.00 IVA |
| `EU_B2C_ES21` | `net ÷ 1.21` | €121 gross (AT consumer) → €100.00 base + €21.00 IVA |
| `OSS_EU` | `net ÷ (1 + country_rate)` | €120 (AT, 20%) → €100.00 base + €20.00 IVA |
| `IVA_EXPORT` | `net` (no VAT) | €100 gross = €100.00 base |
| `IVA_EU_B2B` | `net` (reverse charge) | Full amount is income base |

If `vat_base_eur` is already set on a transaction (manual override), that value is used directly and no extraction is performed.

For **Modelo 130** (IRPF), income reported in Box 01 is the ex-VAT base — IVA collected is a pass-through to AEAT and is not part of your rendimiento.

### Aggregated audit records

The audit trail in the Tax Audit tab mirrors the **Quarter Report** view that the gestor receives: one aggregated line per (geo_region × activity × VAT treatment) bucket, rather than one line per individual Stripe transaction. The totals are identical — only the presentation changes.

### Tax configuration

Add a `tax` section to `config.json` (see `config.json.example`), or use the **Configuration → Tax Settings** tab:

```json
{
  "tax": {
    "regime": "estimacion_directa_simplificada",
    "vat_registered": true,
    "oss_registered": false,
    "vat_proration_percentage": 100,
    "default_vat_treatment_eu_coaching": "IVA_EU_B2B",
    "default_vat_treatment_eu_newsletter": "EU_B2C_ES21"
  }
}
```

Every key above drives a computation:

| Setting | Effect on computation |
|---------|----------------------|
| `regime` | Gates the 5% *gastos de difícil justificación* in Modelo 130 — only `estimacion_directa_simplificada` is eligible (Art. 30.2.4ª LIRPF). |
| `vat_registered` | When `false`, Spanish sales are treated as `IVA_EXEMPT` (no IVA devengado) and no input IVA is deducted in Modelo 303. |
| `oss_registered` | Default `false` (OSS is opt-in, Modelo 035). Unless `true`, no OSS return is generated (an audit note records why) and EU B2C sales are `EU_B2C_ES21`. |
| `vat_proration_percentage` | Prorrata general applied to deducible IVA (Modelo 303 casilla 28/29). `100` = fully deductible. |
| `default_vat_treatment_eu_coaching` / `default_vat_treatment_eu_newsletter` | Override the EU (`EU_NOT_SPAIN`) VAT treatment per activity. Defaults: `IVA_EU_B2B` for coaching; `EU_B2C_ES21` for newsletter (`OSS_EU` when OSS-registered). |

### Invoice data in tax calculations

OCR-extracted invoices (from the Invoice OCR tab) feed directly into all tax models alongside Stripe transactions:

| Model | Source | Contribution |
|-------|--------|-------------|
| **Modelo 303** box_29 | Expense invoices (`direction='in'`) | IVA soportado deducible (cuota), weighted by `deductible_pct_vat` |
| **Modelo 303** box_01 | Income invoices (`IVA_ES_21`) | Base imponible devengado |
| **Modelo 130** box_01 | Non-Stripe income invoices (`direction='out'`) | Subtotal ingresos YTD — `eur_received` when set, otherwise the stored (ECB-resolved) `subtotal_eur`, plus `fx_exchange_differences.gain_loss_eur` recorded in the period |
| **Modelo 130** box_02 | Expense invoices (`direction='in'`, not `is_capital_asset`) | Subtotal gastos (weighted by `deductible_pct_irpf`) YTD |
| **Modelo 130** box_02 | `fixed_assets` table | Depreciation YTD (see [Fixed Assets](#fixed-assets)) |
| **Modelo 130** box_02 | `social_security_payments` table | SS cuotas YTD (fully deductible) |
| **Modelo 130** box_07 | Outgoing invoices | IRPF withheld (`irpf_amount`) YTD |
| **Modelo 347** | Income invoices | Spanish-client invoice operations alongside Stripe. Both sources accumulate on one VAT-inclusive basis so the single threshold compares like with like: Stripe uses `converted_amount − converted_amount_refunded`, invoices use `subtotal_eur + iva_amount`. Not `total_eur` — that is net of the IRPF retención, which is a withholding on payment rather than a smaller operation. |
| **Modelo 349** | Income invoices | EU B2B invoice income alongside Stripe |

Geographic classification is auto-derived from the vendor NIF (expenses) or client NIF (income) at OCR extraction time. Existing rows are backfilled automatically on database init.

Invoices are assigned to a quarter by **`invoice_date`** (the accounting date); `supply_date` is informational only, so an invoice dated in April for a service supplied in March counts in Q2. Rows marked `excluded = 1` (duplicates, receipts, personal, other period, superseded) are ignored by every model.

### Manual entries

Items that cannot be derived from Stripe or invoices (additional overrides, one-off corrections) are entered via the **Manual Entries** sub-tab and stored in `quarterly_tax_entries`.

> **Disclaimer:** This tool pre-fills tax data for review purposes only. It does not constitute tax advice. Always review outputs with a qualified gestor or asesor fiscal before filing.

---

## Tax Validation

The **Tax Validation** tab cross-checks the figures your gestor filed with AEAT against the values computed from your local database, making it easy to spot missing invoices, unclassified transactions, or expenses not yet entered.

### How it works

1. Filed reference data comes from the **AEAT receipts imported into the database** (tables `filed_returns` / `filed_349_operators`, see below). `tmp/validation/validation.yaml` (gitignored — never committed) is a fallback, used only for periods whose receipt has not been imported. On an imported return a blank box counts as 0.
2. The tab loads the filings, runs the same tax-engine computations as in Tax Obligations (against your current SQLite data), and builds a line-by-line comparison for each casilla (PDF box).
3. Each line gets a status:

| Status | Icon | Meaning |
|--------|------|---------|
| `OK` | ✅ | DB value matches filed value (within €0.02 tolerance) |
| `DB_HIGH` | ⬆️ | DB computes a higher value than the gestor filed |
| `DB_LOW` | ⬇️ | DB computes a lower value than the gestor filed |
| `N/A` | ➖ | One side has no data (yet) |

**Diff sign convention:** `DB − filed`. Positive = our system computes more; negative = our system computes less.

### Supported models

| Model | Scope |
|-------|-------|
| Modelo 130 | Quarterly IRPF advance (YTD boxes) |
| Modelo 303 | Quarterly IVA — devengado, deducible, result |
| Modelo 349 | Intracomunitarias — operator count and total amount |
| Modelo 390 | Annual IVA summary — all major casillas |

### Importing filed AEAT receipts

Download the official receipt PDF of each presentation (Modelo 303, 130, 349 and the annual 390) from the AEAT Sede or BILOOP — the gestor does not email them — and import them:

```bash
# Windows — folders are scanned recursively; non-receipt PDFs and unsupported models are skipped
.\.venv\Scripts\python.exe -m src.filed_returns import <folder-or-pdf> [...]
```

It prints one line per file (`imported`, `replaced`, `unchanged`, `superseded` or `skipped`). The same can be done from the **📥 Import filed AEAT receipts** expander at the top of the Tax Validation tab.

- **What is read:** header (model, fiscal year, period, justificante, CSV, presentation timestamp, presenter) and every non-empty box — 303 pages 2–4 (including 60, 64–72 and 110/78/87), 130 boxes 01–19, 349 summary boxes plus every operator row, and the 390 boxes. The 303 "Tipo %" boxes are pre-printed rates and are not stored.
- **How:** `pdfplumber` word coordinates; each amount is paired with the nearest box number to its left on the same row (±7 pt). Text printed in the form-template font (e.g. the 130's "máximo 660,14 euros" note) is ignored. `pdftotext -layout` is deliberately not used — it misaligns the 303 rows.
- **Idempotent:** re-importing a receipt already stored (same justificante) is a no-op. A different receipt for the same model/year/period replaces the stored one if it was presented later (rectificativa) and is ignored if older.
- **Privacy:** receipts contain the taxpayer's and the presenter's NIF. They stay local (the database is git-ignored); never commit a receipt or anything derived from one — tests use synthetic PDFs only.

### Adding a new filing period by hand (fallback)

Uncomment and fill in the appropriate template block in `tmp/validation/validation.yaml`. No code changes are required — the tab reads all entries dynamically.

```yaml
- model: "130"
  year: 2026
  quarter: 1
  filed_date: "2026-04-20"
  result: 0.00
  values:
    "01_ingresos_ytd": 0.00
    # ... (copy from gestor PDF)
```

---

## Tax Audit Trail

The **Tax Audit** tab makes every calculated cell in every tax model fully inspectable. After running **Calculate Tax**, open this tab to see exactly how each figure was derived.

### How it works

Each time **Calculate Tax** runs, the engine writes one `AuditEntry` per cell to the `tax_audit_log` SQLite table alongside the usual snapshot. Entries are keyed by `(year, quarter, model, computed_at)` — re-running always replaces the previous entries for the same period.

### What is audited

| Model | Cells audited |
|-------|--------------|
| **Modelo 303** | box_01_base, box_03_cuota, box_59_intracom, box_28, box_29, box_46, box_48, oss_base, oss_vat, export_base |
| **Modelo 130** | box_01_ingresos, box_02_gastos, amortizaciones (per-asset breakdown), capital_assets_excluded, box_03_rendimiento, gastos_dificil_justificacion (with cap flag), rendimiento_neto, box_05_base, box_07_retenciones, box_14_pagos_anteriores, box_16_resultado |
| **Modelo 349** | one entry per operator (VAT ID) + total |
| **OSS** | base + cuota per country + totals |
| **Modelo 347** | one entry per counterparty above threshold + summary |

### Per-cell detail

Each entry records:
- **Formula** — the rule applied (e.g. `"min(box_03_rendimiento × 5%, 2000) [Art. 30.2.4ª LIRPF]"`)
- **Inputs** — named JSON dict of all values that fed the calculation (e.g. `{"box_03_rendimiento": 18400.00, "rate": 0.05, "cap_eur": 2000.0, "cap_applied": false}`)
- **Records** — the individual transactions and invoices included in the figure (date, counterparty, description, amounts), shown as a full DataFrame in the drill-down
- **Value** — the resulting EUR figure

The UI shows a summary table plus an expandable drill-down per cell. Each expander header shows how many records contribute to that figure. Results can be downloaded as JSON.

### Known approximations (documented in audit)

| Cell | Approximation | Impact |
|------|--------------|--------|
| `box_28_base_soportado` (M303) | `box_29_cuota_soportado / 0.21` assumes all deductible expenses at 21% | Display only — does not affect `box_46` or `box_48` |
| `box_48_resultado` (M303) | Prorrata from `tax.vat_proration_percentage` (default 100%) applied to casilla 29 | Set the prorrata in Tax Settings; manual IVA entries should still reflect only genuinely deductible cuota |
| `box_01_ingresos` (M130) | Ex-VAT base extracted from VAT-inclusive Stripe amounts | Correct for estimación directa — IVA is a pass-through, not income |

---

## Running Tests

```bash
# Windows
.\.venv\Scripts\python.exe -m pytest -v
# macOS / Linux
.venv/bin/pytest -v
```

---

## Historical Validation

The classification system was validated against historical known totals covering the period from July 2023 to December 2025. All computed totals matched the original manual Excel files, confirming the accuracy of the automated classification rules.

---

## Performance & Caching

Streamlit re-runs the entire app script on every user interaction (widget change, tab switch). To avoid re-querying SQLite on every render, key data-loading paths are wrapped with `@st.cache_data(ttl=300)`:

| Cached function | Where | What it avoids |
|----------------|-------|----------------|
| `_cached_validations()` | `app/tax_validation.py` | 39–45 DB queries per Tax Validation tab render (4 quarters × multiple model computations) |
| `_load_invoices_df()` | `app/invoice_explorer.py` | Full `invoices` table scan + type conversions on every filter interaction |
| `_sidebar_stats()` | `app/streamlit_app.py` | 5 DB queries on every widget interaction across all tabs |

**Cache TTL:** 5 minutes. Results auto-refresh after 5 minutes, or immediately via the **↺ Refresh** button present in the Tax Validation and Invoice Explorer tabs.

**Invalidation rules:**
- Tax Validation: click **↺ Refresh** after running **Calculate tax** or loading new data to see updated figures
- Invoice Explorer: click **↺ Refresh** after extracting new invoices via OCR to see them in the table
- Sidebar stats: auto-refresh every 5 minutes (no manual control needed)

---

## Invoice Upload

The Invoice Upload tab supports uploading invoice PDFs to the accounting partner API (IntegraLOOP/BILOOP).

- **Invoices In** (`data/invoices/in/`): received invoices
- **Invoices Out** (`data/invoices/out/`): produced invoices
- Tracks which files have been uploaded to avoid duplicates

Enable it in `config.json`:

```json
{
  "accounting_api": {
    "company_id": "YOUR_COMPANY_ID",
    "enabled": true
  }
}
```

Then set the Accounting API credentials in `.env`:

```
ACCOUNTING_BASE_URL=https://api.example.com
ACCOUNTING_SUBSCRIPTION_KEY=your_subscription_key_here
ACCOUNTING_TOKEN=your_token_here
# OR (optional) user/pass to fetch a 2h token via /api-global/v1/token
ACCOUNTING_USER=your_user_here
ACCOUNTING_PASSWORD=your_password_here
```

---

## Invoice OCR (AI Extraction)

The **Invoice OCR** tab extracts Spanish accounting data from any PDF — invoices, receipts, tickets, foreign bills — and stores the results in the `invoices` SQLite table.

### Extraction provider

Extraction runs through one of two interchangeable backends, selected by `invoice_ocr.provider` in `config.json` (or the `INVOICE_OCR_PROVIDER` env var, or the `provider=` argument to `extract_invoice()`). Both backends share the same prompt and post-parsing, so the stored fields are identical regardless of provider.

| Provider | How it calls | Credentials |
|----------|-------------|-------------|
| `hub` (default) | local-llm-hub at `http://127.0.0.1:8000` via the Anthropic SDK and a `document` content block, model alias `gemini_pro` | none (the hub holds the Google session) |
| `gemini` | Direct `google-genai` SDK to Gemini / Vertex AI | `GOOGLE_API_KEY` or Vertex ADC (`GOOGLE_APPLICATION_CREDENTIALS`) |

The `hub` path is the default: it keeps all LLM access flowing through the local-llm-hub (central LAN access and observability) and needs no Google key on this machine. It became the default once the hub's PDF-attachment reliability bug ([local-llm-hub#63](https://github.com/ferraroroberto/local-llm-hub/issues/63)) was fixed — the hub now passes attachment dirs via `agy --add-dir`, so document/PDF blocks ingest deterministically. Override the hub endpoint/alias with `LLM_HUB_BASE_URL` / `LLM_HUB_MODEL` if needed. To fall back to the legacy direct Gemini/Vertex path, set `invoice_ocr.provider` to `gemini` (or `INVOICE_OCR_PROVIDER=gemini`).

### Invoice directories

Configured via `config.json` (`invoice_in_dir` / `invoice_out_dir`). Both accept absolute paths. PDFs are scanned **recursively**, so subdirectories (e.g. year/quarter folders) are included automatically.

| Direction | Default path | Accounting role |
|-----------|-------------|-----------------|
| **In** (expenses) | `data/invoices/in` | Facturas recibidas — IVA soportado |
| **Out** (income) | `data/invoices/out` | Facturas emitidas — IVA repercutido |

Re-extraction is skipped automatically when the PDF has not changed (MD5 hash comparison).

### Extracted fields

All fields required for AEAT compliance (Libro de IVA, SII, Modelo 303/347/349):

| Field | Description |
|-------|-------------|
| `invoice_number`, `invoice_date` | Document identification |
| `invoice_type` | `factura_completa`, `factura_simplificada`, `ticket`, `recibo`, `nota_gastos` |
| `supply_date`, `due_date` | Fecha prestación / fecha vencimiento |
| `vendor_name`, `vendor_nif`, `vendor_address` | Emisor |
| `client_name`, `client_nif`, `client_address` | Receptor |
| `subtotal_eur`, `iva_rate`, `iva_amount` | Base imponible and main IVA |
| `iva_breakdown` | JSON array — one entry per IVA rate line (supports mixed-rate invoices and recargo de equivalencia) |
| `irpf_rate`, `irpf_amount` | IRPF retention |
| `total_eur` | Total a pagar |
| `vat_exempt_reason` | Legal basis for 0% IVA (Art. 20 LIVA, intracomunitaria, exportación, etc.) |
| `deductible_pct` | Deductibility percentage (default 100; 50 for vehicles, home office, etc.) |
| `is_rectificativa`, `rectified_invoice_ref` | Factura rectificativa handling |
| `billing_period_start`, `billing_period_end` | Subscription billing period |
| `payment_method`, `category`, `notes` | Classification and flags |

### Foreign-currency invoices: EUR resolution

The LLM's own `subtotal_eur`/`iva_amount`/`total_eur` guess for a foreign-currency
document is used only as a **cross-check** — the authoritative EUR figure comes
from `src.fx_rates.resolve_invoice_amounts`, called right after extraction
(`app/invoice_ocr_tab._extract_and_save`), in this order:

1. **`charged_eur`** — when the document itself states the EUR actually charged
   to the card (e.g. *"Charged 42.50 EUR using 1 USD = 0.8500 EUR"*), that wins.
   The OCR prompt extracts it into a new `charged_eur` field, left `null` when
   the document doesn't state it.
2. Otherwise, **the ECB rate on `invoice_date`** — `original_amount` divided by
   the daily rate from `fx_rates`, with the fallback/staleness behaviour above.

The resolved rate, its date and its source are stored per invoice
(`fx_rate_used`, `fx_rate_date`, `fx_source` ∈ `NATIVE_EUR` / `CHARGED_EUR` /
`ECB` / `NO_RATE` / `INVALID_DATE`, `fx_stale`), and the LLM's own estimate is
compared against the resolved figure: a difference over 1% is stored as
`fx_cross_check_diff_pct` and surfaced as a ⚠️ warning in the Invoice OCR and
Invoice Ledger tabs.

**Income invoices (`direction='out'`):** the same ECB resolution applies at
extraction time, and it is **final**, not provisional — per art. 79.Once LIVA,
income kept in a foreign-currency account (never converted) is booked at the
ECB rate on the invoice date. If the money **was** actually converted on
receipt, set `eur_received` in the Invoice Ledger tab once it's known; it then
wins over the stored ECB figure in every tax computation that reads invoice
income (Modelo 130 box 01, Modelo 303's export base). See
[Exchange rate differences](#exchange-rate-differences) for what happens when
a foreign-currency balance booked at the ECB rate is converted later.

**Invoices stored before this resolver existed** (or before a later fix to
it) keep whatever EUR figure the LLM originally guessed until corrected —
resolution only runs at extraction time, not retroactively. Run
`close_quarter.py fx-recompute` (or the Currency tab's **Recompute FX for
stored invoices** button) to re-resolve every stored non-EUR invoice in
place; see [Closing a Quarter](#closing-a-quarter) for the command and its
guarantees (locked fields skipped, `eur_received` untouched, idempotent).

### All Records tab features

- **Date scanned** column shows when each invoice was extracted.
- **Row-selection checkboxes** — select one or more records and click **Delete selected**.
- **Clear invoice table** — wipes all records (with confirmation); PDF files are never touched.

### Hub provider (default — no Google key needed)

The default `hub` provider routes all PDF extraction through local-llm-hub and needs no Google API key. If `invoice_ocr.provider` is unset (or set to `hub` in `config.json`), no further credential setup is required — stop here.

### Google API key (legacy `gemini` provider)

Only needed if you explicitly set `invoice_ocr.provider` to `gemini` (or `INVOICE_OCR_PROVIDER=gemini`). Get a free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and add it to `.env`:

```
GOOGLE_API_KEY=AIzaSy...
```

Model used: `gemini-3.1-flash-lite-preview`.

### Vertex AI (GCP service account)

If you manage the API key through a GCP project (service account bound key), the Generative Language API must be enabled and unrestricted. Two pre-requisites in the GCP console:

1. **Enable the API** — visit `https://console.developers.google.com/apis/api/generativelanguage.googleapis.com/overview?project=YOUR_PROJECT` and click Enable.
2. **Remove API restrictions** on the key — Credentials → find the key → API restrictions → "Don't restrict key" (or add Generative Language API to the allowed list).

For ADC-based auth (service account JSON), download the key file and set:

```
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=us-central1   # or europe-west1, etc.
```

When `GOOGLE_APPLICATION_CREDENTIALS` is set the module switches to Vertex AI mode automatically (no API key needed).

---

## Invoice Ledger

The **Invoice Ledger** tab is where OCR output is reviewed and corrected. Pick a direction (expenses / income) and optionally a quarter (by invoice date), then:

- **Bulk edit** — an `st.data_editor` grid over the ledger columns (invoice date, tax treatment, VAT/IRPF business-use %, capital asset, exclusion, EUR received, payment date). Edits apply on **Save changes**.
- **Vendor** (expenses) — the [vendor-registry](#vendor-registry) match for each row; **⚠ unknown** when no vendor matches (also counted in the *⚠ Unknown vendor* metric).
- **Edit one invoice** — a form covering the OCR fields (number, dates, parties, amounts, …) and the ledger columns. Saving stamps `reviewed_at` even when nothing changed.

**Locks.** Every field you change is added to the invoice's `locked_fields` (JSON list). Re-extracting the PDF in the Invoice OCR tab never overwrites a locked field — enforced in `src/database.py` (`upsert_invoice`), not just in the UI. Ledger-only columns (`excluded`, `eur_received`, …) also survive a plain re-extract. **Unlock all fields** releases the locks (values stay as they are) so the next re-extract may overwrite them.

**Ledger columns** (added by an idempotent migration on startup):

| Column | Meaning |
|--------|---------|
| `tax_treatment` | Expenses: `DOMESTIC`, `DOMESTIC_CAPITAL`, `INTRA_EU_RC`, `NON_EU_RC`, `NO_VAT`, `NOT_DEDUCTIBLE`. Income: `ES_21`, `EU_B2C_ES21`, `EU_B2B`, `NON_EU_NOT_SUBJECT`, `EXEMPT_TEACHING`. |
| `deductible_pct_vat` / `deductible_pct_irpf` | Business-use share for the VAT deduction (303) and the IRPF expense (130), independently. Backfilled from the legacy `deductible_pct`. |
| `is_capital_asset`, `asset_class` | Capital-asset flag and class. A flagged invoice is not expensed in the Modelo 130; register it as a fixed asset so its cost enters through depreciation (see [Fixed Assets](#fixed-assets)). |
| `excluded`, `excluded_reason` | `1` removes the row from every tax computation; reason ∈ `duplicate`, `receipt`, `personal`, `other_period`, `superseded`. |
| `eur_received`, `payment_date` | EUR actually received for foreign-currency income, and when. |
| `vendor_vat_id_norm` | `vendor_nif` normalised for matching: upper-case, separators stripped, Spanish ids `ES`-prefixed. |
| `locked_fields`, `reviewed_at` | User-edited fields (never overwritten by re-OCR) and last review time. |
| `charged_eur` | EUR actually charged to the card, when the document states it (expenses) — wins over the ECB rate. The one FX field you may correct by hand. |
| `fx_rate_used`, `fx_rate_date`, `fx_source`, `fx_stale`, `fx_cross_check_diff_pct` | FX resolution metadata (#93) — see [Foreign-currency invoices: EUR resolution](#foreign-currency-invoices-eur-resolution). Derived; re-resolved on the next OCR extraction, not directly editable. |

**Backfill of `tax_treatment`** from the legacy `vat_treatment` (still stored and kept in sync when you edit the treatment). The mapping preserves what the engine did with the legacy value:

| Direction | Legacy `vat_treatment` (+ `geo_region`) | `tax_treatment` |
|-----------|------------------------------------------|-----------------|
| in | `IVA_ES_21` | `DOMESTIC` |
| in | `IVA_EU_B2B` | `INTRA_EU_RC` |
| in | `IVA_EXEMPT` + `OUTSIDE_EU` | `NON_EU_RC` |
| in | `IVA_EXEMPT` + any other region | `NO_VAT` |
| out | `IVA_ES_21` | `ES_21` |
| out | `OSS_EU` | `EU_B2C_ES21` |
| out | `IVA_EU_B2B` | `EU_B2B` |
| out | `IVA_EXPORT` | `NON_EU_NOT_SUBJECT` |
| out | `IVA_EXEMPT` + `SPAIN` | `EXEMPT_TEACHING` |
| out | `IVA_EXEMPT` + other / unknown region | left empty — review it in the Ledger tab |

The tax engine currently uses `excluded`, `invoice_date` and the split business-use percentages; the per-treatment Modelo 303 box model (reverse charge, capital goods, pro-rata) is a later step.

### Exchange rate differences

A foreign-currency income invoice with no `eur_received` is booked at the ECB
rate on the invoice date — final, not provisional (see above). If that
foreign-currency balance is **later converted** to EUR, the conversion
realises a gain or loss against the EUR figure originally booked, which must
be recorded as activity income (or a loss) in the quarter of conversion, not
the invoice's own quarter.

The Invoice Ledger tab's **income** view has an "Exchange rate differences"
form for this: pick the invoice (optional), the conversion date, the
foreign-currency amount converted, and the EUR actually obtained. It computes
`gain_loss_eur = eur_obtained − booked_eur` and stores it in the
`fx_exchange_differences` table (`src.fx_rates.record_exchange_difference` /
`get_exchange_differences`). Every recorded gain/loss dated within a quarter's
year-to-date window is added to that quarter's Modelo 130 box 01 income
(`src.tax_engine.compute_modelo_130`) — on top of, not instead of, the
invoice's own booked income, which keeps counting in its own quarter as usual.

---

## Duplicate Review

The **Duplicate Review** tab (and its `src/invoice_dedupe.py` module) finds and excludes duplicate, receipt and out-of-period invoices — the same expense counted twice inflates deductible VAT and IRPF expenses.

### Detectors

Five detectors run in priority order over the invoice ledger; a row already proposed by an earlier detector is never proposed again by a later one:

1. **Same `file_hash`** — byte-identical PDFs ingested under different filenames.
2. **Same vendor + `invoice_number`** — the same invoice re-ingested (vendor key: `vendor_vat_id_norm`, falling back to the vendor name).
3. **Invoice/receipt pair** — same vendor, same total, dates within +/-3 days, and one side is a receipt (`invoice_type = "recibo"`, or `recibo`/`receipt` in the filename or description). **The invoice always wins.**
4. **Email-folder copy** — a file under an `email` subfolder duplicating a main-folder file (matched on vendor + invoice number, or vendor + total + date when no number is known).
5. **Out-of-period** — a row whose `invoice_date` falls outside the quarter being swept. Scoped to the invoices matching files copied into `tmp/close_quarter/<year>_Q<quarter>/`; a full-table scan is not meaningful here (every past quarter's rows would be "out of period").

**Keeper rule:** an invoice always beats a receipt; among the rest, a non-`email`-path file wins, then the earliest-ingested (`extracted_at`) row. A row whose `excluded` field is already locked (a user edited it in the Invoice Ledger tab) is never touched by any detector or by `apply_groups` — a manual decision always wins over an automated one.

### Applying exclusions

Detection is a pure function (`find_duplicate_groups`) — nothing is written until you apply a group. Applying writes `excluded=1` / `excluded_reason` via `set_invoice_exclusion`, the same ledger-only write path `upsert_invoice` uses, **without** locking the field — so re-extraction or a later manual edit can still change it. This differs from editing a row directly in the Invoice Ledger tab, which always locks the fields you touch.

- **UI:** scan, review each proposed group (reassign the keeper if needed), then **Exclude the rest** per group or **Apply all proposed exclusions**. A row auto-excluded this way shows up under "Auto-excluded rows" with an **Undo** button.
- **CLI:**

```bash
.venv/Scripts/python.exe -m src.invoice_dedupe scan [--direction in|out] [--apply]
.venv/Scripts/python.exe -m src.invoice_dedupe scan --year 2026 --quarter 2 --apply  # also runs the out-of-period detector
.venv/Scripts/python.exe -m src.invoice_dedupe scan --db path/to/a/copy.db          # dry run against a DB copy
```

Excluded rows are ignored by every tax computation (Modelo 303/130/347), same as a manual exclusion.

---

## Vendor Registry

Vendors repeat every month, so a small registry makes expense classification deterministic instead of relying on the (often missing) vendor VAT id.

**Source of truth:** `vendors.json` at the repo root — git-ignored like `classification_rules.json`, because it holds real vendor tax ids. The repo ships `vendors.json.example` with fake vendors. There is no `vendors` table: the registry's defaults are written onto the `invoices` rows, so the tax engine only reads invoices. A missing file means an empty registry (every expense invoice is flagged).

| Field | Meaning |
|-------|---------|
| `key` | Normalised vendor name (lower-case, punctuation → spaces). Should equal the vendor's sub-folder under `invoice_in_dir`. |
| `aliases` | Other names / folder names for the same vendor. |
| `legal_entity`, `country` (ISO-2), `vat_id`, `alt_vat_ids` | Who bills you. `country` fills `geo_region` / `supply_country` when the invoice has none. |
| `default_tax_treatment` | One of the expense treatments (`DOMESTIC`, `DOMESTIC_CAPITAL`, `INTRA_EU_RC`, `NON_EU_RC`, `NO_VAT`, `NOT_DEDUCTIBLE`). Typical: EU SaaS with a reverse-charge note → `INTRA_EU_RC`; US SaaS without VAT → `NON_EU_RC`; foreign vendor charging Spanish VAT → `DOMESTIC`; bank / fintech fees → `NO_VAT`. |
| `default_deductible_pct_vat`, `default_deductible_pct_irpf` | Business-use % (e.g. home-office utilities, mixed-use devices). |
| `activity` | `COACHING` (IAE 826), `NEWSLETTER` (IAE 751) or `ILLUSTRATIONS` (IAE 861) — written to `invoices.activity_type` for the P&L per activity. |
| `asset_class`, `recurrence`, `notes` | Optional. |

Leave a default empty to keep the invoice's own (heuristic) value — e.g. for a vendor that bills from both an EU and a US entity.

**Matching** (first hit wins): (1) the invoice's **sub-folder** (first component of `filename`) against `key` / `aliases`; (2) the normalised **vendor VAT id** against `vat_id` / `alt_vat_ids`; (3) the **vendor name** against `key` / `aliases` / `legal_entity` (whole-word, longest alias first).

**Applying.** Every OCR extraction applies the registry to the new row, and the **Vendors** tab's **Apply registry** button (or the CLI below) re-applies it to all stored expense invoices — idempotent:

- `tax_treatment` (legacy `vat_treatment` kept in sync), `deductible_pct_vat`, `deductible_pct_irpf`, `activity_type` and `asset_class` take the registry default, **except 🔒 locked fields** — a Ledger edit always wins.
- `vendor_vat_id_norm`, `geo_region` (only when `UNKNOWN`) and `supply_country` are filled only when missing: an id read from the document wins.
- Registry writes never lock a field and never mark the invoice reviewed.

**Unknown vendors** are listed in the **Vendors** tab (grouped by suggested key = the invoice folder), flagged ⚠ in the Invoice Ledger grid, and warned about on the Invoice OCR card.

**Editing.** The Vendors tab has an editable grid (**Save registry** validates every row) and an **Import from a spreadsheet** panel: an `.xlsx` whose first sheet has a vendor column (`vendor` / `item` / `name`) plus optional `activity` (or `business`: coaching / newsletter / illustration) and `recurrence` (or `recurrency`). New vendors are added; existing vendors only get an empty activity / recurrence filled.

**CLI:**

```bash
# Build or extend vendors.json from a vendor spreadsheet (merge; hand edits are kept)
.\.venv\Scripts\python.exe -m src.vendor_registry seed-from-xlsx <path-to-vendor-list.xlsx>
# Apply the registry to every stored expense invoice and list the unknown vendors
.\.venv\Scripts\python.exe -m src.vendor_registry apply [--db data/accounting.db] [--registry vendors.json]
```

---

## Fixed Assets

Durable purchases are depreciated instead of expensed (`src/fixed_assets.py`, **Fixed Assets** tab).

**Creating assets.** In the **Invoice Ledger** tab, pick an expense invoice and open **Register as fixed asset**: the form is prefilled from the invoice (base, VAT, invoice date, IRPF/VAT business-use %, class) and saving flags the invoice `is_capital_asset`, so the Modelo 130 stops expensing it. One asset row is one unit — an invoice with several units gets one asset per unit. Assets can also be added by hand in the Fixed Assets tab, and edited in its grid (changing the class resets the coefficient to the class maximum). Deleting the last asset of an invoice unflags the invoice.

**Depreciation (IRPF).** *Tabla de amortizaciones simplificada* of *estimación directa simplificada* — Orden de 27 de marzo de 1998 (BOE 28/03/1998), art. 30 RIRPF, checked on 2026-09-28 against the AEAT IRPF 2025 practical manual:

| Class key | Group | Max coefficient | Max period |
|-----------|-------|-----------------|------------|
| `buildings` | Edificios y otras construcciones | 3% | 68 y |
| `installations` | Instalaciones, mobiliario y enseres | 10% | 20 y |
| `machinery` | Maquinaria | 12% | 18 y |
| `vehicles` | Elementos de transporte | 16% | 14 y |
| `it_equipment` | Equipos para tratamiento de la información y sistemas y programas informáticos | 26% | 10 y |
| `tools` | Útiles y herramientas | 30% | 8 y |
| `other` | Resto del inmovilizado material | 10% | 20 y |

- Charge = base × business-use % × coefficient × days in use ÷ days in the year (365; 366 in a leap year, so a full year is exactly the coefficient), from `start_of_use` (default: acquisition date) until the day before `disposal_date`. The coefficient defaults to the class maximum and can be lowered, never raised.
- Cumulative depreciation is capped at base × business-use %.
- Unit bases at or below `assets.threshold_eur` (default 300) are expensed in full in the quarter they are acquired.
- `assets.posting_mode`: `annual_q4` (default) books the full year's depreciation in Q4 (Q1–Q3 YTD carry none); `quarterly` books each quarter's days.
- The Modelo 130 adds `depreciation_for_period(year, quarter, conn, ytd=True, config=...)` to box 02; the Tax Audit tab shows it as `amortizaciones` with a per-asset breakdown, plus `capital_assets_excluded` (flagged invoices removed from expenses; any flagged invoice without a registered asset is listed there and logged as a warning).

**VAT capital goods.** An asset whose unit base is above €3,005.06 is a *bien de inversión* (art. 108 LIVA; auto-detected, overridable). `capital_goods_vat_for_period` gives its Modelo 303 boxes **30/31** in the quarter of acquisition: base × VAT business-use %, and VAT × VAT business-use % (or the `vat_deducted_eur` override). The **regularisation register** covers the year of acquisition + 4: record the VAT business-use % actually applied each year, and a year whose % differs from the acquisition year's by more than 10 points gets an adjustment of VAT borne ÷ 5 × (% of the year − initial %) (arts. 107–109 LIVA), for 303 box 44 at Q4. These figures are computed and shown in the tab; wiring them into the 303 box model is a later step (the 303 still counts the invoice's VAT in 28/29, at the invoice's `deductible_pct_vat`). The one-off disposal adjustment of art. 110 LIVA is not computed.

Configuration (`config.json`):

```json
{
  "assets": {
    "threshold_eur": 300,
    "posting_mode": "annual_q4"
  }
}
```

Storage: `fixed_assets` (one row per unit) and `fixed_asset_vat_usage` (asset, year, % used), created on first use.

---

## Invoice Explorer

The **Invoice Explorer** tab provides a filterable, exportable view of all OCR-extracted invoices in a single table.

**Filters available:** direction (in/out), category, invoice type, vendor name (text search), client name (text search), invoice date range, subtotal range, and a "rectificativas only" toggle.

Live summary metrics (matching count, total expenses, total income) update as filters change. Results can be exported to CSV.
