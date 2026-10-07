# Stripe Accounting Quarterly Automation

Automated Stripe payment classification and quarterly reporting system. Classifies payments by activity type (Coaching, Newsletter, Illustrations) and geographic region (Spain, EU-not-Spain, Outside-EU), then produces Excel reports, Spanish tax obligation snapshots, a box-by-box **Reconciliation** of filed AEAT returns against the app's figures, and a Streamlit dashboard.

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
cp divergences.json.example divergences.json  # divergence catalogue (optional; see "Reconciliation")
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

### Restart matrix

| You changed | Restart |
|-------------|---------|
| Code under `src/` or `app/` | The Streamlit app (stop it, then relaunch with the command above or `launch_app.bat`) |
| `config.json`, `classification_rules.json` or `vendors.json` **by hand** | The Streamlit app — they are cached in the app process. Edits made in the app's own Configuration / Vendors tabs apply at once |
| `divergences.json`, `gestor_notes.md`, `data/accounting.db` | Nothing — read on use (click **↺ Refresh** where a tab caches for 5 minutes, see [Performance & Caching](#performance--caching)) |
| Anything, for the CLI | Nothing — each `close_quarter.py` run is a new process that re-reads code and config |

---

## Documentation

- [`docs/tax-conventions.md`](docs/tax-conventions.md) — every tax rule the engine applies, with its legal basis (LIVA / LIRPF / RIRPF articles, AEAT orders), the config key that controls it, and where an external accountant may do it differently.
- [`docs/quarter-close-runbook.md`](docs/quarter-close-runbook.md) — the quarterly close step by step: pre-flight checklist, the `close_quarter.py` pipeline, filing the 303 / 130 / 349 on the AEAT Sede, payment, **Mark filed**, receipt import, archive, and the annual calendar.
- [`docs/architecture.mmd`](docs/architecture.mmd) — the repo's internal structure (Mermaid).

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
├── divergences.json               # Divergence catalogue (git-ignored, copy from .example — see "Reconciliation")
├── divergences.json.example
├── gestor_notes.md                # Free-text notes for the accountant's pack (git-ignored, optional)
├── config.json.example
├── requirements.txt
├── launch_app.bat                 # Windows launch shortcut
├── scripts/
│   └── close_quarter.py           # Quarter-close CLI over src/close_pipeline.py (see "Closing a Quarter")
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
│   ├── periods.py                 # Quarter start/end bounds (date, ISO string, datetime) shared by the engine, dedupe, close and UI
│   ├── tax_models.py              # Dataclasses for Modelo303, Modelo130, OSS, 347, 349 results + AuditEntry
│   ├── vat_rules.py               # Single source of truth: activity×geo VAT matrix, OSS rates, base extraction
│   ├── tax_engine.py              # Spanish tax computation: Modelo 303/130/349/347, OSS, EU B2C threshold, calendar
│   ├── tax_snapshot_codec.py      # Serialize/deserialize tax engine results for SQLite snapshot storage
│   ├── tax_validator.py           # Filed-return loader: imported AEAT receipts first, validation.yaml fallback
│   ├── reconciliation.py          # Box-by-box filed-vs-app reconciliation, divergence catalogue, markdown export
│   ├── modelo_390.py              # Modelo 390 engine from the four 303 results (aeat_boxes, pro-rata, volume)
│   ├── modelo_347.py              # Modelo 347 purchases side (Spanish vendors > €3,005.06, exclusions)
│   ├── pl_by_activity.py          # P&L per IAE activity (826/861/751) tied to the Q4 Modelo 130
│   ├── annual_pack.py             # Annual pack: 390 + 347 + P&L, markdown/CSV export + CLI
│   ├── filed_returns.py           # Import filed AEAT receipt PDFs (303/130/349/390) as reference data + CLI
│   ├── fixed_assets.py            # Fixed assets: simplified-table depreciation, VAT capital goods (303 30/31), regularisation
│   ├── accounting_api_client.py   # IntegraLOOP/BILOOP Accounting API client
│   ├── invoice_ocr.py             # PDF extraction for Spanish accounting (local-llm-hub default, direct Gemini fallback)
│   ├── invoice_ingest.py          # OCR → FX → vendor registry → DB save path (OCR tab + close_quarter.py ocr)
│   ├── close_pipeline.py          # Quarter-close pipeline steps (sweep … gestor pack), each idempotent
│   ├── relink.py                  # Re-point invoice records after PDFs move/rename (move record, then content hash)
│   ├── filing_sheet.py            # Filing sheet (AEAT form order, credit chain, deadlines) + immutable "mark filed"
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
│   ├── tax_obligations.py         # Tax obligations tab (Modelo 303/130/349/347, OSS, Annual Pack)
│   ├── annual_pack_tab.py         # Annual Pack sub-tab: 390, 347 sales + purchases, P&L per activity
│   ├── filing_sheet_tab.py        # Filing Sheet tab (copyable box values, deadlines, Mark filed)
│   ├── tax_validation.py          # Reconciliation tab (filed vs app per box, drill-down, catalogue editor)
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
│   ├── test_modelo_303.py         # Modelo 303 box model: golden quarter, pro-rata, credit chain
│   ├── test_modelo_349.py         # Modelo 349 keys I/S: grouping, excluded/unidentified lines, snapshots
│   ├── test_modelo_130.py         # Modelo 130 box model: golden two quarters, box 13 scale, 05/15 chain
│   ├── test_annual_pack.py        # 390 from four synthetic quarters, 347 purchases threshold, P&L = 130 Q4
│   ├── test_invoice_ledger.py     # Ledger migration/backfill, edit locks, excluded rows, invoice-date keying
│   ├── test_invoice_ledger_tab.py # Invoice Ledger tab (AppTest)
│   ├── test_stripe_eu_b2c_reclassify.py  # EU B2C at 21%, reclassify, frozen reports, threshold
│   ├── test_reconciliation.py     # Reconciliation matching, catalogue, 349 operators, export, adapter
│   ├── test_tax_validation_tab.py # Reconciliation tab (AppTest, empty state)
│   ├── test_filed_returns.py      # AEAT receipt parser/import (synthetic PDFs only)
│   ├── test_invoice_dedupe.py     # Dedupe detectors, keeper rule, locked rows, engine picks up exclusions
│   ├── test_invoice_dedupe_tab.py # Duplicate Review tab (AppTest)
│   ├── test_vendor_registry.py    # Vendor matching, defaults vs locks, unknown vendors, xlsx seed
│   ├── test_vendor_registry_tab.py # Vendors tab + ledger unknown-vendor flag (AppTest)
│   ├── test_fixed_assets.py       # Depreciation, threshold, posting modes, capital-good VAT, 130 hook, tab
│   ├── test_close_pipeline.py     # Every close step on a temp DB (OCR/ECB/Stripe mocked), idempotence, skill ↔ CLI
│   ├── test_relink.py             # Relink matching (move record, hash, unmatched, ambiguous, swap) + archive step
│   ├── test_filing_sheet.py       # Filing sheet boxes, deadlines, filed-version freeze, triggers, migration
│   ├── test_filing_sheet_tab.py   # Filing Sheet tab (AppTest)
│   └── test_invoice_ocr_tab.py    # Invoice OCR tab extract button (AppTest, OCR mocked)
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
│       └── <year>_Q<quarter>/     # Swept invoices, Stripe report, reconciliation/filing sheet .md, accountant's notes + draft email
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

### Customer VAT ID overrides (EU B2B vs. B2C)

For an EU (non-Spain) sale, whether it is booked as B2B (reverse charge) or B2C (Spanish 21% / OSS) depends on whether the customer's VAT id is known — not on the activity (see "VAT treatment classification" below). The VAT id is looked up the same way as a geographic override, in `classification_rules.json`'s `customer_vat_ids`:

```json
"customer_vat_ids": {
  "email_vat_ids": {
    "client@example.com": "DE123456789"
  },
  "name_vat_ids": {
    "client name as it appears in description": "DE123456789"
  }
}
```

Editable in the same **Configuration → Geographic Rules** tab as the geographic overrides, under **Customer VAT IDs (B2B)**. An email match wins, then a name/description match. As a read-only fallback, a Stripe `customer.tax_ids` entry already present in the stored raw charge is used if the override lookup finds nothing (currently a no-op — the Stripe fetch does not request `tax_ids`). VIES validity is **not** checked; verify the id yourself before relying on the reverse charge.

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

- **transactions** — Stripe payment records with classification and FX conversion data. `fee` is the whole balance-transaction fee; `fee_stripe` / `fee_application` split it (EUR, from `fee_details`; NULL = split unknown, fetched before the split was stored — fill with `stripe-fetch --backfill-fee-split`). The VAT columns (`vat_treatment`, `vat_base_eur`, `vat_amount_eur`) are reserved for manual overrides; they are normally NULL — VAT treatment is derived on-the-fly by the tax engine at computation time, not stored per transaction
- **fx_rates** — Daily ECB exchange rates (EUR/USD, EUR/GBP, EUR/CHF, EUR/AUD, and any other currency seen in stored invoices/transactions)
- **upload_log** — Invoice upload tracking to prevent duplicates
- **invoices** — AI-extracted invoice records (vendor, client, IVA/IRPF breakdown, totals, Spanish AEAT fields). Includes `geo_region`, `vat_treatment`, `activity_type`, and `supply_country` columns auto-derived from the vendor/client NIF at insert time — mirroring the `transactions` table so both sources feed the tax engine uniformly — plus the ledger columns described in [Invoice Ledger](#invoice-ledger) (`tax_treatment`, split business-use %, `excluded`, `locked_fields`, …) and the FX resolution columns described in [Foreign-currency invoices: EUR resolution](#foreign-currency-invoices-eur-resolution) (`charged_eur`, `fx_rate_used`, `fx_source`, `fx_stale`, …)
- **fx_exchange_differences** — Gains/losses realised when a foreign-currency income balance booked at the ECB rate is later converted to EUR; see [Exchange rate differences](#exchange-rate-differences)
- **social_security_payments** — Seguridad Social cuota payments imported from bank account exports (or entered manually). Deduplication key: `(payment_date, amount_eur, description)`. Refunds are stored as negative amounts. Automatically included as deductible expenses in Modelo 130 box 02 (YTD)
- **quarterly_tax_entries** — Manual tax inputs (IVA soportado, gastos deducibles, retenciones)
- **tax_filing_status** — Filing status and computed amounts per model/quarter
- **tax_computation_snapshots** — Versioned JSON snapshots of tax engine outputs (Modelo 303/130/OSS/349/347) written when you click **Calculate tax** in Tax Obligations. Keyed by `(year, quarter, model, snapshot_version)` with `status` `COMPUTED` or `FILED`: a recompute rewrites the latest `COMPUTED` draft; **Mark filed** adds a new `FILED` version (with `justificante`, `presented_on`) that SQLite triggers make immutable — see [Filing Sheet](#filing-sheet)
- **filed_returns** — One row per non-empty box (casilla) of each filed AEAT return imported from its receipt PDF (model, year, period, box, value, justificante, CSV, presentation timestamp, source file). Reference data for the Reconciliation tab — see "Importing filed AEAT receipts"
- **filed_349_operators** — Operator rows (country, VAT id, name, clave, base) of each imported Modelo 349 receipt
- **tax_audit_log** — Per-cell calculation audit entries: every box in every model records the formula applied, named inputs, and computed value. Written alongside snapshots; queryable by year/quarter/model/run timestamp
- **declared_reports** / **declared_report_lines** — The Stripe report actually sent to the gestor, frozen by `close_quarter.py report --freeze` (or, for a report sent before freezing existed, `freeze-sent`): quarter, version, file name, SHA-256 of the `.xlsx`, and per-transaction EUR amounts. Insert-only (SQLite triggers reject UPDATE/DELETE); a corrected re-send is a new version. The tax engine uses the latest version's EUR amounts for every transaction it contains, so later FX re-conversions cannot move a declared quarter

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

`scripts/close_quarter.py` is the deterministic backbone for the recurring quarterly-close chore — a thin CLI over `src/close_pipeline.py`, invoked interactively via the `/close-quarter` Claude Code skill (`.claude/skills/close-quarter/`), which guides you through it step by step and pauses for review after extraction (`ocr`/`vendors`/`dedupe`) and after `compute`. It can also be run by hand. The full procedure, including filing and paying on the AEAT Sede and archiving, is [`docs/quarter-close-runbook.md`](docs/quarter-close-runbook.md).

### Pipeline

Every step takes `--year Y --quarter Q` (default: the last completed quarter), is idempotent, and prints `[step] N change(s)` with one `+` line per write, `⚠` review items and `❌` errors — a re-run with nothing new prints `[step] no changes`. Exit code 1 only on `❌`.

```bash
.venv/Scripts/python.exe scripts/close_quarter.py sweep                       # 1. copy new invoice PDFs
.venv/Scripts/python.exe scripts/close_quarter.py ocr [--direction in|out|both] [--dry-run] [--model ID]  # 2. extract new/changed PDFs
.venv/Scripts/python.exe scripts/close_quarter.py vendors [--all-periods]     # 3. vendor registry + unknown vendors ⚠
.venv/Scripts/python.exe scripts/close_quarter.py dedupe [--apply [--group N]]  # 4. duplicates / out-of-period
.venv/Scripts/python.exe scripts/close_quarter.py fx [--apply]                # 5. ECB backfill + invoice FX recompute
.venv/Scripts/python.exe scripts/close_quarter.py stripe                      # 6. fetch + backfill-emails + reclassify + warnings
.venv/Scripts/python.exe scripts/close_quarter.py reta --file <bank export>   # 7. RETA (TGSS) debits
.venv/Scripts/python.exe scripts/close_quarter.py compute                     # 8. 303/130/349 (+ OSS, 347) snapshots
.venv/Scripts/python.exe scripts/close_quarter.py reconcile                   # 9. filed vs app, markdown table
.venv/Scripts/python.exe scripts/close_quarter.py sheet                       # 10. filing sheet (form order, deadlines)
.venv/Scripts/python.exe scripts/close_quarter.py gestor-pack [--freeze]      # 11. accountant's pack
.venv/Scripts/python.exe scripts/close_quarter.py all [--apply] [--freeze] [--reta-file F] [--model ID]  # 1-11 in order
```

- **`ocr`** extracts every PDF under `invoice_in_dir` / `invoice_out_dir` that is new or changed since its last extraction (MD5 against the stored `file_hash`) through `src/invoice_ingest.extract_and_save` — the same save path as the Invoice OCR tab: OCR, FX resolution, vendor-registry defaults. A failing file is reported (`❌`) and the rest continue; it stays pending and is retried next run. `--dry-run` lists the pending files. `--model` overrides the hub model (else the `LLM_HUB_MODEL` env var, else `gemini_pro`) — use it when the hub no longer serves the default alias.
- **`vendors`** applies `vendors.json` to the expense invoices dated in the quarter, and lists the quarter's invoices with an unknown vendor. A quarter with a FILED snapshot is not written (⚠). Invoices of other periods are never touched, so a later registry edit cannot re-treat a filed year. `--all-periods` is the explicit opt-in to re-apply the registry to every stored expense invoice, filed periods included; it prints the rows written per year/quarter, marking filed ones `(FILED)`.
- **`dedupe`** runs the five detectors of [Duplicate review](#duplicate-review) (out-of-period over the files swept into the quarter folder) and numbers the proposed groups `#1`, `#2`…; `--apply` writes the exclusions, never over a locked row, and `--apply --group N` (repeatable) writes only group `#N`, so one correct proposal can go in without the others (numbers shift after an apply, so review again first). A shared invoice number with a different total or date is listed as a numbering ⚠ and never excluded. A group whose kept row is excluded by the time it is applied is skipped with a ⚠, so no apply leaves a group with every row excluded.
- **`fx`** backfills ECB rates up to today (warns when the stored rates stop short of the quarter end, e.g. network failure) and re-resolves the quarter's stored non-EUR invoices; the recompute writes only with `--apply`.
- **`stripe`** fetches the quarter from Stripe, fills billing email/country from saved raw charges, reclassifies the quarter, and reports new/changed transactions plus the review warnings of `stripe-fetch` (foreign-looking `eur_default`, EU B2C threshold). Use `stripe-fetch` for the full per-transaction table.
- **`reta`** imports a bank export of the Social Security debits (column names from `config.json → social_security`, see [Seguridad Social](#seguridad-social-social-security-cuotas)); rows already stored are skipped.
- **`compute`** computes the quarter and saves the snapshots only when a figure changed (so a re-run doesn't touch `computed_at`).
- **`reconcile`** reconciles 303/130/349 against the quarter's filed returns when they are imported, otherwise against the previous quarter's (the chain the carry-forwards start from), and writes `reconciliation_<Y>_Q<Q>.md`.
- **`sheet`** writes `filing_sheet_<Y>_Q<Q>.md` from the stored snapshots — see [Filing Sheet](#filing-sheet) (rendered by `src/filing_sheet.py` through the pluggable `src.close_pipeline.filing_sheet_renderer` hook).
- **`gestor-pack`** (while an external accountant is engaged) copies the quarter's invoices from the ledger into `tmp/close_quarter/<Y>_Q<Q>/invoices/` (every non-excluded `in`/`out` invoice dated in the quarter, including ones OCR'd before the close; pack files no longer in that set are removed, a PDF missing on disk is a ⚠), writes the reclassified Stripe report, `gestor_notes_<Y>_Q<Q>.md` — your free text from the git-ignored `gestor_notes.md` at the repo root (a template when absent) plus the special treatments detected in the ledger (partial business use, exclusions, non-default VAT treatment, fixed assets, foreign currency) — and a draft email `gestor_email_<Y>_Q<Q>.txt` whose invoice counts are the pack's. `--freeze` stores the report as the declared report (below); once declared the pack never regenerates it. Nothing is ever sent.
- **`all`** stops at the first failing step; completed steps are no-ops on the re-run. `--apply` lets `dedupe`/`fx` write, `--freeze` lets `gestor-pack` freeze.

### Other subcommands

```bash
.venv/Scripts/python.exe scripts/close_quarter.py stripe-check [--days N]              # read-only API smoke test
.venv/Scripts/python.exe scripts/close_quarter.py stripe-fetch --year Y --quarter Q    # fetch + classify + persist
.venv/Scripts/python.exe scripts/close_quarter.py stripe-fetch --backfill-fee-split [--from D --to D] [--dry-run]
.venv/Scripts/python.exe scripts/close_quarter.py add-override "<key>" REGION [--type email|name]
.venv/Scripts/python.exe scripts/close_quarter.py reclassify --from YYYY-MM-DD [--to YYYY-MM-DD] [--dry-run]
.venv/Scripts/python.exe scripts/close_quarter.py backfill-emails [--dry-run]           # fill email/country from saved raw charges
.venv/Scripts/python.exe scripts/close_quarter.py report --year Y --quarter Q          # regenerate the Excel report
.venv/Scripts/python.exe scripts/close_quarter.py report --year Y --quarter Q --freeze # ...and freeze it as declared
.venv/Scripts/python.exe scripts/close_quarter.py freeze-sent --year Y --quarter Q --file <sent.xlsx> [--supersede]  # freeze a report sent earlier
.venv/Scripts/python.exe scripts/close_quarter.py fx-backfill                          # backfill ECB FX rates to today
.venv/Scripts/python.exe scripts/close_quarter.py fx-recompute [--dry-run] [--since D] # re-resolve stored invoices' EUR at the ECB rate
.venv/Scripts/python.exe scripts/close_quarter.py archive --year Y --quarter Q         # after filing: quarter folder + DB snapshot -> app.archive_dir
.venv/Scripts/python.exe scripts/close_quarter.py relink [--manifest moves.csv] [--old-in-dir D] [--old-out-dir D] [--apply] # after moving/renaming invoice PDFs
```

- **`sweep`** (pipeline step 1) diffs `invoice_in_dir` / `invoice_out_dir` (recursively) against both the `invoices` DB table and a cumulative manifest (`tmp/close_quarter/invoice_copy_log.json`), copies only the files not seen before into `tmp/close_quarter/<year>_Q<quarter>/`, and updates the manifest — safe to rerun after adding more invoices.
- **`stripe-fetch`** flags transactions classified by a *default* geo rule (no client-specific override matched) so they can be double-checked before the report goes out, lists EUR charges that fell to `eur_default` for a foreign-looking customer (⚠), and prints the EU B2C year-to-date total against the €10,000 art. 73 LIVA threshold. With **`--backfill-fee-split`** it instead re-fetches `--from`/`--to` (default: the `--year`/`--quarter` bounds) from the Stripe API and writes only the `fee_stripe` / `fee_application` split of rows already stored — no insert, no reclassification, no other column — so closed quarters cannot move; `--dry-run` reports counts without writing. Run it once after upgrading for every year whose Modelo 130 is still computed by the app.
- **`reclassify`** re-runs the classifier over the transactions *already stored* from `--from` (optionally up to `--to`) with the current rules and overrides, prints every change (`old activity/geo (rule) -> new`) plus a per-quarter count, and writes only the changed rows. `--dry-run` reports without writing. Idempotent: a second run changes nothing. Run it after any rule change that should apply retroactively.
- **`backfill-emails`** fills empty stored `email_meta` / `billing_country` from each row's already-saved raw Stripe charge JSON (`raw_source_json`) — no Stripe API call, and a non-empty stored value is never overwritten. Use it once after upgrading to pick up `billing_details.email` / `billing_details.address.country` for rows fetched before that fallback existed. `--dry-run` reports counts without writing.
- **`report`** first reclassifies the quarter's stored rows (so a stale row can never be exported), then writes the Excel report. The exporter also refuses — `StaleClassificationError` — to write a non-EUR charge whose geography came from a EUR rule. With **`--freeze`** the written file becomes the quarter's immutable declared report (`declared_reports`); freezing an already-declared quarter needs `--supersede` (a new version, for a corrected re-send). Once a quarter is declared, a plain `report` writes `Stripe_Report_Q<Q>_<Y>_live.xlsx` instead of overwriting the sent file, and prints how the live rows differ from the declared ones.
- **`freeze-sent`** freezes a Stripe report file that was sent to the accountant before freezing existed. The declared lines come from the file's `import` sheet (columns matched by header name, so files written by older versions of the app still read): id, created date, currency, `Converted Amount`, `Converted Amount Refunded`, fee and net, not from today's live rows. The file's SHA-256 is stored as with `report --freeze`. The sheet has no original-currency amount, so that column stays empty. Everything is checked before anything is stored: a row dated outside the quarter, a duplicate id, an unreadable cell or a `Net Amount` that is not amount minus refunds aborts with the row numbers and stores nothing. File ids with no live `transactions` row are frozen anyway and listed as a warning, as are live charges of the quarter missing from the file (those keep their live amounts). As with `report --freeze`, only the EUR amounts are used; activity and region stay live, so a region mistake in the sent file is not re-imported. An already-declared quarter needs `--supersede`. Keep the sent file in `tmp/close_quarter/<Y>_Q<Q>/` under its original name, or `gestor-pack` warns that the frozen file is missing.
- **`add-override`** appends to `classification_rules.json`'s `geographic_overrides` / `email_overrides` — the same mechanism as the Transaction Browser tab's "Add Geographic Override" form.
- **`fx-backfill`** fetches and stores ECB rates from the last stored date up to today for every currency seen in stored invoices/transactions (`src.fx_rates.backfill_to_today`) — idempotent, safe to rerun every close.
- **`fx-recompute`** re-runs `resolve_invoice_amounts` over every *already-stored* non-EUR invoice (`src.fx_rates.recompute_stored_invoice_fx`) — corrects invoices extracted before the FX resolver existed, or before a later fix to it, in place. Writes by default; pass `--dry-run` to preview (scanned/changed/stale/cross-check/locked-skipped counts plus a per-row old→new EUR list) without touching the DB. `--since YYYY-MM-DD` restricts the scan to invoices dated on/after that date. Never overwrites a row with `subtotal_eur`/`iva_amount`/`total_eur` in its `locked_fields` — those are reported as skipped, not silently kept or dropped — and never touches `eur_received`, which already wins over `subtotal_eur` in the tax engine regardless. Idempotent: a second run reports zero changes. The same recompute is available as a preview-then-apply button in the Currency tab.
- **`archive`** (runbook step 19) copies the quarter folder and a dated database snapshot into `app.archive_dir/<year>T<quarter>/`. It only adds or updates copies, never deletes, and reports no changes on a same-day re-run. It fails with a clear error when `app.archive_dir` is not set.
- **`relink`**: see "Moving or renaming the invoice archive" below.
- All output lives under `tmp/close_quarter/` (git-ignored), apart from `archive`'s copies. Nothing is uploaded or sent anywhere by this script.

### Moving or renaming the invoice archive

An invoice record is keyed on its PDF's path **relative to** `invoice_in_dir` / `invoice_out_dir`. Moving a whole root therefore only needs the config change. **Renaming** files, or moving them between sub-folders, needs a relink, or every renamed PDF looks new to `ocr` and the old record, with its OCR result, locks, exclusions and fixed-asset link, is orphaned.

1. Back up the database (the runbook's pre-flight command).
2. Move or rename the files. Keep a move record: a CSV with `src` and `dst` absolute paths (other columns are ignored), such as the log a re-sorting tool writes.
3. Point `invoice_in_dir` / `invoice_out_dir` at the new roots.
4. Dry run:

   ```bash
   .venv/Scripts/python.exe scripts/close_quarter.py relink --manifest moves.csv --old-in-dir <old in root> --old-out-dir <old out root>
   ```

   Every record is reported as one of:
   - **moved:** matched by the move record, or by content hash when there is no move row;
   - **unchanged:** still at its path with the same content;
   - **⚠ unmatched:** no file found;
   - **⚠ ambiguous:** several files share its content, or two records would land on one file.

   The exit code is 1 while anything is unmatched or ambiguous.
5. Resolve the ⚠ rows, then repeat with `--apply`. It backs up the database to `data/backups/` and rewrites the paths in one transaction; row ids and every other field are kept.

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
- The audit trail (Tax Audit tab) records `ss_gastos` and the full list of individual payments as named inputs to the `c02_gastos_reales` cell (box 02 split).

### Quarterly breakdown

The tab shows per-year totals and, when a year is selected, a quarterly breakdown (Q1–Q4) so you can reconcile against the TGSS monthly receipts.

---

## Tax Obligations (Spanish Autónomo)

The **Tax Obligations** tab turns the classified transaction data into pre-filled Spanish tax filings. It covers the standard obligations for an autónomo in *régimen de estimación directa simplificada*. The legal basis of each rule, and where an external accountant may differ, is in [`docs/tax-conventions.md`](docs/tax-conventions.md).

### Stored calculations

Computed figures are **not** recalculated on every page load. Click **Calculate tax** to run the engines and persist results to the `tax_computation_snapshots` table in SQLite (per selected year and quarter; Modelo **347** is annual and stored with quarter `0`). After you sync Stripe data, change manual tax entries, or adjust classifications, run **Calculate tax** again to refresh.

### Supported models

| Model | Name | Frequency | What it computes |
|-------|------|-----------|-----------------|
| **Modelo 303** | Declaración IVA Trimestral | Quarterly | Every AEAT box: accrued (01–13, 27), deductible (28–46, incl. reverse charge, capital goods and pro-rata), informational (59/60/120) and the result with the credit chain (64–73, 110/78/87) — see [Modelo 303 box model](docs/tax-engine-reference.md#modelo-303-box-model) |
| **Modelo 130** | Pago Fraccionado IRPF | Quarterly | Every AEAT box 01–19: YTD income/expenses (02 includes the 5% allowance), 20% advance, withholdings, the art. 110.3.c reduction (13) and the negative-result carry (15) — see [Modelo 130 box model](docs/tax-engine-reference.md#modelo-130-box-model) |
| **Modelo 349** | Operaciones Intracomunitarias | Quarterly | Key `I` (services acquired from EU businesses) and key `S` (services supplied to EU businesses), one line per VAT id and key — see [Modelo 349 operators](docs/tax-engine-reference.md#modelo-349-operators) |
| **OSS Return** | One Stop Shop | Quarterly | B2C digital services to EU non-Spain customers, grouped by country — only when `oss_registered` is true |
| **EU B2C threshold** | Art. 73 LIVA | Live | Year-to-date EU B2C sales (ex-VAT) vs €10,000; warns at 80%, flags the previous year too |
| **Modelo 347** | Operaciones con Terceros | Annual | Spain counterparties with total operations > €3,005.06 (**importe IVA incluido**): sales here, purchases in the [Annual Pack](#annual-pack-modelo-390-modelo-347-pl-per-activity) |
| **Modelo 390** | Resumen Anual IVA | Annual | Built from the four 303s: rows by rate, deductions, pro-rata, result and compensation, volume of operations — see [Annual Pack](#annual-pack-modelo-390-modelo-347-pl-per-activity) |

### VAT treatment classification

VAT treatment is derived on-the-fly by the tax engine using each transaction's activity × geography (and, for EU sales, the customer's VAT id) — it is not stored per transaction. The mapping is:

| Activity | Geography | Customer VAT id | Treatment | IVA |
|----------|-----------|-----------------|-----------|-----|
| Any | OUTSIDE_EU | — | `IVA_EXPORT` | 0% |
| Any | SPAIN | — | `IVA_ES_21` | 21% |
| Any | EU_NOT_SPAIN | Known | `IVA_EU_B2B` | 0% (reverse charge) |
| Any | EU_NOT_SPAIN | Unknown | `EU_B2C_ES21` (default, not OSS-registered) | 21% Spanish IVA, Modelo 303 boxes 07/09 |
| Any | EU_NOT_SPAIN | Unknown | `OSS_EU` (only when `oss_registered: true`) | Buyer country rate, OSS return |

**B2B vs. B2C follows the customer's status, not the activity** (accounting-quarterly#113 — art. 69/70 LIVA). A sale to an EU business with a valid VAT id is reverse-charged (`IVA_EU_B2B`, Modelo 349 key S); a sale to an EU consumer is taxed in Spain below the €10,000 threshold. The VAT id comes from **Configuration → Geographic Rules → Customer VAT IDs** (an email or name/description override, mirroring the geographic overrides — see "Customer VAT ID overrides" below) or, as a read-only fallback, a Stripe `customer.tax_ids` entry already present in the stored raw charge (not currently requested by the Stripe fetch, so this fallback is a no-op on today's data). **VIES validation of the id is out of scope** — the app trusts whatever VAT id is on file; verify it yourself before relying on the reverse charge.

> **Behaviour change (accounting-quarterly#113):** before this, COACHING and ILLUSTRATIONS EU sales defaulted to `IVA_EU_B2B` regardless of whether the customer had a VAT id on file. They now default to `EU_B2C_ES21` (21% Spanish IVA) unless a VAT id is on record — run `close_quarter.py reclassify --dry-run` after upgrading to see which stored rows this moves.

**EU consumers below the threshold.** Under art. 73 LIVA, electronically supplied services to consumers in other EU countries stay located in Spain — Spanish 21% IVA — while the year's and the previous year's EU B2C sales are at or below €10,000 (ex-VAT) and you have not opted into OSS. That is `EU_B2C_ES21`. If `default_vat_treatment_eu_<activity>` says `OSS_EU` but `oss_registered` is not true, the engine uses `EU_B2C_ES21` (there is no OSS return to declare it on). The **Tax Obligations → EU B2C / OSS** tab tracks the threshold and a warning banner appears at 80%.

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
    "activity_start_date": "2025-01-01",
    "prorrata": {"enabled": true, "definitive_pct_by_year": {"2025": 100}},
    "modelo303_q4_negative_result": "compensate",
    "platform_fee_vat_treatment": "NON_EU_RC",
    "previous_year_net_yield": {"2025": 5000.00},
    "vat_proration_percentage": 100,
    "default_vat_treatment_eu_coaching": "EU_B2C_ES21",
    "default_vat_treatment_eu_newsletter": "EU_B2C_ES21",
    "default_vat_treatment_eu_illustrations": "EU_B2C_ES21"
  }
}
```

Every key above drives a computation:

| Setting | Effect on computation |
|---------|----------------------|
| `regime` | Gates the 5% *gastos de difícil justificación* in Modelo 130 — only `estimacion_directa_simplificada` is eligible (Art. 30.2.4ª LIRPF). |
| `vat_registered` | When `false`, Spanish sales are treated as `IVA_EXEMPT` (no IVA devengado) and no input IVA is deducted in Modelo 303. |
| `oss_registered` | Default `false` (OSS is opt-in, Modelo 035). Unless `true`, no OSS return is generated (an audit note records why) and EU B2C sales are `EU_B2C_ES21`. |
| `activity_start_date` | ISO date (`YYYY-MM-DD`) the business activity began. Absent → no lower bound (unchanged behaviour). Set, it floors every quarter/YTD range: a Stripe charge or invoice dated before it is left out of 303, 130, 349, 390 and 347 (income on the date itself is still counted), and the 303 box 110 / 130 box 05-15 chains never reach back before it. An audit note records what was excluded (accounting-quarterly#133). |
| `prorrata.enabled` | VAT pro-rata (arts. 102–106 LIVA), default `true`. See [Modelo 303 box model](docs/tax-engine-reference.md#modelo-303-box-model). |
| `prorrata.definitive_pct_by_year` | The definitive pro-rata % of each year once filed (Q4 303 / 390); it is the next year's provisional %. Years not listed fall back to the % the app computes from that year's data, then 100. |
| `previous_year_net_yield` | Previous year's net yield of economic activities for Modelo 130 box 13 — a number, or `{"<year>": amount}`. Only used when the previous year's Q4 130 receipt is not imported; without either, the app's own previous-year figure is used. |
| `modelo303_q4_negative_result` | `compensate` (default, box 72) or `refund` (box 73) for a negative Q4 result. Q1–Q3 always carry forward. |
| `platform_fee_vat_treatment` | `NON_EU_RC` (default): the quarter's Stripe platform (application) fees are self-assessed as a non-EU reverse charge — base in 303 box 12, 21% in 13, deducted in 28/29 (accounting-quarterly#147). `NONE` leaves them out of the 303 (e.g. to mirror an accountant during a shadow run); the 130 expenses them either way. |
| `pl_allocation` | P&L per activity: where RETA (`reta`), depreciation (`depreciation`) and lines without an activity (`unallocated`) go — an activity (`COACHING`, `NEWSLETTER`, `ILLUSTRATIONS`) or `BY_INCOME` (split by directly attributed income). Default `COACHING` (IAE 826) for all three, as the external accountant does. |
| `vat_proration_percentage` | Legacy flat pro-rata %. Only used, as the provisional %, when it is not `100` and the previous year has no `prorrata.definitive_pct_by_year` entry. |
| `default_vat_treatment_eu_coaching` / `default_vat_treatment_eu_newsletter` / `default_vat_treatment_eu_illustrations` | Pick the EU B2C sub-treatment (`EU_B2C_ES21` or `OSS_EU`) per activity for a sale **without** a known customer VAT id. Default `EU_B2C_ES21` for every activity. No longer selects B2B: since #113, `IVA_EU_B2B` only applies when the customer has a VAT id on file (see "VAT treatment classification" above) — a legacy `IVA_EU_B2B` value here is accepted but ignored. |

### Modelo 303, 130 and 349 box models

Field-by-field box tables (AEAT box → source, field names, audit cell names) are the tax-engine reference, not a tab overview: [docs/tax-engine-reference.md](docs/tax-engine-reference.md#modelo-303-box-model) covers the Modelo 303 box model, the [Modelo 130 box model](docs/tax-engine-reference.md#modelo-130-box-model) and [Modelo 349 operators](docs/tax-engine-reference.md#modelo-349-operators). The legal basis behind each box is in [tax-conventions.md](docs/tax-conventions.md).

### Invoice data in tax calculations

OCR-extracted invoices (from the Invoice OCR tab) feed directly into all tax models alongside Stripe transactions:

| Model | Source | Contribution |
|-------|--------|-------------|
| **Modelo 303** | Expense invoices (`direction='in'`) | By `tax_treatment`: `DOMESTIC` → 28/29, `DOMESTIC_CAPITAL` / registered capital goods → 30/31, `INTRA_EU_RC` → 10/11 + 36/37, `NON_EU_RC` → 12/13 + 28/29, each weighted by `deductible_pct_vat` |
| **Modelo 303** | Income invoices (`direction='out'`) | `ES_21` → 01/03, 04/06 or 07/09 by rate, `EU_B2C_ES21` → 07/09, `EU_B2B` → 59, `NON_EU_NOT_SUBJECT` → 120, `EXEMPT_TEACHING` → pro-rata denominator only |
| **Modelo 130** box 01 | Non-Stripe income invoices (`direction='out'`) | Subtotal ingresos YTD (gross of the withholding) — `eur_received` when set, otherwise the stored (ECB-resolved) `subtotal_eur`, plus `fx_exchange_differences.gain_loss_eur` recorded in the period |
| **Modelo 130** box 02 | Expense invoices (`direction='in'`, not `is_capital_asset`) | Subtotal gastos (weighted by `deductible_pct_irpf`) YTD |
| **Modelo 130** box 02 | `fixed_assets` table | Depreciation YTD (see [Fixed Assets](#fixed-assets)) |
| **Modelo 130** box 02 | `social_security_payments` table | SS cuotas YTD, net of refunds (fully deductible) |
| **Modelo 130** box 06 | Outgoing invoices | IRPF withheld (`irpf_amount`) YTD |
| **Modelo 347** | Income invoices | Spanish-client invoice operations alongside Stripe. Both sources accumulate on one VAT-inclusive basis so the single threshold compares like with like: Stripe uses `converted_amount − converted_amount_refunded`, invoices use `subtotal_eur + iva_amount`. Not `total_eur` — that is net of the IRPF retención, which is a withholding on payment rather than a smaller operation. |
| **Modelo 349** | Expense invoices (`direction='in'`) | `INTRA_EU_RC` → key `I`, per vendor VAT id |
| **Modelo 349** | Income invoices | `EU_B2B` → key `S` alongside the Stripe B2B charges |

Geographic classification is auto-derived from the vendor NIF (expenses) or client NIF (income) at OCR extraction time. Existing rows are backfilled automatically on database init.

Invoices are assigned to a quarter by **`invoice_date`** (the accounting date); `supply_date` is informational only, so an invoice dated in April for a service supplied in March counts in Q2. Rows marked `excluded = 1` (duplicates, receipts, personal, other period, superseded) are ignored by every model.

### Manual entries

Items that cannot be derived from Stripe or invoices (additional overrides, one-off corrections) are entered via the **Manual Entries** sub-tab and stored in `quarterly_tax_entries`.

> **Disclaimer:** This tool pre-fills tax data for review purposes only. It does not constitute tax advice. Always review outputs with a qualified gestor or asesor fiscal before filing.

---

## Annual Pack (Modelo 390, Modelo 347, P&L per activity)

**Tax Obligations → Annual Pack** (or the CLI) computes the three annual outputs live from the database — nothing is persisted — with markdown and CSV downloads:

```bash
.venv/Scripts/python.exe -m src.annual_pack --year 2026                      # markdown to stdout
.venv/Scripts/python.exe -m src.annual_pack --year 2026 --out tmp/annual_2026  # .md + 4 CSVs
.venv/Scripts/python.exe -m src.annual_pack --year 2025 --db path/to/copy.db
```

### Modelo 390 (`src/modelo_390.py`)

`compute_modelo_390(year, conn, config)` is built from the year's four `compute_modelo_303` results (their boxes and audit records), so it can never drift from the quarterly returns; `aeat_boxes()` returns every box keyed as printed, and the reconciliation and the validator use it. Full box layout: [docs/tax-engine-reference.md#modelo-390-box-model](docs/tax-engine-reference.md#modelo-390-box-model).

### Modelo 347 (`src/modelo_347.py` purchases + `compute_modelo_347` sales)

Purchases: Spanish vendors (by NIF — the invoice's, else the vendor registry's) whose **VAT-inclusive** (`subtotal_eur + iva_amount`) purchases of the year **exceed** €3,005.06 (art. 33.1 RD 1065/2007: "hayan superado"), with the quarterly split. Excluded (art. 33.2 RD 1065/2007): intra-EU acquisitions already in the 349 and purchases with IRPF withheld (both 33.2.i), non-Spanish vendors (33.2.g) and `excluded` invoices. A vendor above the threshold without a NIF is listed as unidentified. Sales (key B) come from `compute_modelo_347` as before.

### P&L per IAE activity (`src/pl_by_activity.py`)

Income and expenses are the Q4 Modelo 130 year-to-date inputs split by activity — **826** coaching/teaching (`COACHING`), **861** illustration (`ILLUSTRATIONS`), **751** newsletter/publicity (`NEWSLETTER`): Stripe rows by `activity_type`; issued invoices by `activity_type` (an `EXEMPT_TEACHING` invoice without one is teaching); expense invoices by `activity_type`, else the vendor registry's `activity`; Stripe platform fees by the charge's `activity_type`. RETA, depreciation and lines without an activity follow `tax.pl_allocation` (below). Totals equal 130 Q4 box 01 and the real expenses inside box 02; the 5 % *gastos de difícil justificación* is shown separately (it is one allowance on the whole net yield). The tab and the markdown flag a P&L that does not tie.

## Reconciliation

The **Reconciliation** tab (formerly Tax Validation) lines up every box of a filed AEAT return against the value the app computes, so each difference is either fixed or explained once and then recognised automatically.

### How it works

1. Pick a model (303, 130, 349, 390) and a period; the picker defaults to the latest filed period and lists the filed periods on record.
2. Filed values come from the **AEAT receipts imported into the database** (tables `filed_returns` / `filed_349_operators`, see below). `tmp/validation/validation.yaml` (gitignored — never committed) is a fallback, used only for periods whose receipt has not been imported. On an imported return a blank box counts as 0; a YAML filing only knows the boxes it lists.
3. App values come from `src/reconciliation.app_boxes(model, year, quarter, conn, config)`: it calls the engine result's `aeat_boxes()` (keyed by the box number printed on the form). The Modelo 390 comes from its own engine (`src/modelo_390.py`, see [Annual Pack](#annual-pack-modelo-390-modelo-347-pl-per-activity)), built from the year's four 303 results; its drill-down shows the live 390 audit cells.
4. One row per box in the union of both sides: filed / app / diff (**app − filed**) / status / tag / explanation. The Modelo 349 also gets one row per operator, keyed `op:<VATID>:<KEY>` (country prefix + number, operation key) and summed per operator on each side.

| Status | Meaning |
|--------|---------|
| ✅ `exact` | Filed and app agree within €0.01 |
| 🟡 `catalogued` | They differ and a divergence-catalogue entry explains the difference |
| 🔴 `uncatalogued` | They differ and nothing explains it |
| ⚪ `missing` | One side is unavailable: no filed return for the period, or the app does not compute that box |

With no filed return for the period the tab shows guidance and the app's own figures (all ⚪) instead of failing.

- **Drill-down:** pick an app box to see the audit cells behind it — formula, inputs and records — from the latest `tax_audit_log` run for the period (written by **Calculate tax**), or from the live computation when no run is stored.
- **Export:** **⬇️ Download table as markdown** writes the table (with status counts) for a private reconciliation note; `src/reconciliation.to_markdown` is the same function.

### Divergence catalogue (`divergences.json`)

A git-ignored JSON file at the repo root (`divergences.json.example` ships fake entries). Edit it in the tab's **📒 Divergence catalogue** editor — **💾 Save catalogue** validates every row and writes nothing if one is invalid — or by hand:

```json
{"divergences": [
  {"model": "303", "year": 2026, "quarter": 1, "box": "09",
   "expected_delta": 12.34, "rule": null, "tolerance": 0.01,
   "category": "gestor_error", "explanation": "Why the filed value differs"}
]}
```

| Field | Rule |
|-------|------|
| `model` | `303`, `130`, `349` or `390` |
| `year` / `quarter` | The period; `null` = any year / any quarter (390 is annual: quarter must be `null`) |
| `box` | Box as printed (`"07"`, `"110"`; `"7"` is normalised to `"07"`), or a 349 operator key `op:<VATID>:<KEY>` |
| `expected_delta` | Expected app − filed, matched within `tolerance` (default 0.01) |
| `rule` | Instead of a delta: `app_gte_filed`, `app_lte_filed` or `any` — give exactly one of the two |
| `category` | `gestor_error`, `convention` or `app_choice` (shown as the row's tag) |
| `explanation` | Required free text |

A differing box that matches an entry for its model, period and box shows 🟡 with the entry's tag and explanation; the first matching entry wins. A ⚪ row is never catalogued.

`src/tax_validator.py` is now only the loader of the filed returns (`load_filings` / `find_filing`: imported receipts first, `validation.yaml` as the fallback); the comparison itself lives in `src/reconciliation.py`.

### Importing filed AEAT receipts

Download the official receipt PDF of each presentation (Modelo 303, 130, 349 and the annual 390) from the AEAT Sede or BILOOP — the gestor does not email them — and import them:

```bash
# Windows — folders are scanned recursively; non-receipt PDFs and unsupported models are skipped
.\.venv\Scripts\python.exe -m src.filed_returns import <folder-or-pdf> [...]
```

It prints one line per file (`imported`, `replaced`, `unchanged`, `superseded` or `skipped`). The same can be done from the **📥 Import filed AEAT receipts** expander at the top of the Reconciliation tab.

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

## Filing Sheet

The **Filing Sheet** tab (and `scripts/close_quarter.py sheet`, which writes `filing_sheet_<Y>_Q<Q>.md`) lists what to type into each AEAT form for a quarter, built by `src/filing_sheet.py` from the **stored** snapshots — run **Calculate tax** (or `close_quarter.py compute`) first:

- **Modelo 303** boxes in form order (page 1 devengado / deducible, page 3 información adicional / resultado), the **credit chain** (110 → 78 → 87, 71, 72/73, and what carries to next quarter's 110), and the result (to pay / compensate / refund).
- **Modelo 130** boxes 01–19 and the result (box 19).
- **Modelo 349** summary boxes and the operator list (country, VAT id, name, key, base).
- Only non-zero boxes by default (**Show zero boxes** lists every modelled box). In the tab each value sits in a copyable block, formatted as the Sede form expects (`1234,56`); the markdown has the same value in its last column.
- **Deadlines** per model: filing until the 20th of the month after the quarter (Q4: 30 January), moved to the next business day when it falls on a weekend or holiday; **direct debit** (303/130 only) until the latest day leaving at least three business days or five calendar days before that — the 15th for the 20th, 27 January for 30 January 2026 (Orden HAC/241/2025, BOE-A-2025-5048, amending Orden EHA/1658/2009; AEAT *calendario del contribuyente*, "Plazos de presentación de autoliquidaciones con domiciliación bancaria"). Only national holidays plus Maundy Thursday and Good Friday are built in — check the AEAT calendar each period.

### Mark filed (immutable filed snapshots)

After presenting a return, **Mark filed** (per model, in the tab) asks for the **justificante** and the **presentation date** and stores a **new** snapshot version with `status = 'FILED'`, a copy of the computed figures. It also flags the period FILED in `tax_filing_status` (amount = 303 box 71 / 130 box 19). Then import the official receipt PDF (**Reconciliation** tab → **📥 Import filed AEAT receipts**) so the filed return is reconciled box by box.

- FILED rows are never overwritten or deleted: SQLite triggers reject any UPDATE or DELETE of a FILED row, and a draft cannot be flipped to FILED in place.
- A later **Calculate tax** never touches a FILED version: the snapshot writer (`src/database.upsert_tax_snapshot_conn`) adds a new `COMPUTED` version (or rewrites that newer draft), and skips the write when the figures equal the filed ones. The sheet then shows the new draft with a **Recomputed after filing** table of the boxes that differ from the filed version.
- A period whose latest version is FILED cannot be marked filed again; recompute first (a corrected return is filed from the new draft).
- The **Mark Filed** button of the Tax Obligations calendar only flips `tax_filing_status`; use the Filing Sheet tab to freeze the figures.

---

## Tax Audit Trail

The **Tax Audit** tab makes every calculated cell in every tax model fully inspectable. After running **Calculate Tax**, open this tab to see exactly how each figure was derived.

### How it works

Each time **Calculate Tax** runs, the engine writes one `AuditEntry` per cell to the `tax_audit_log` SQLite table alongside the usual snapshot. Entries are keyed by `(year, quarter, model, computed_at)` — re-running always replaces the previous entries for the same period.

### What is audited

| Model | Cells audited |
|-------|--------------|
| **Modelo 303** | one entry per AEAT box (`c01_base` … `c73_a_devolver`, with the contributing records on the base boxes), plus `oss_base` and `exempt_base` |
| **Modelo 130** | one entry per AEAT box (`c01_ingresos` … `c19_resultado`, with the records behind 01, 02 and 06 and the quarter chain behind 05/15), plus the box 02 split: `c02_gastos_reales`, `c02_gastos_dificil_justificacion` (with cap flag), `c02_amortizaciones` (per-asset breakdown), `c02_capital_assets_excluded` |
| **Modelo 349** | one entry per operator line (`op_<KEY>_<VATID>`, with the invoices / charges summed; `excluded_…` / `unidentified_…` for lines not declared) + `c01_operadores`, `c02_importe` |
| **OSS** | base + cuota per country + totals |
| **Modelo 347** | one entry per counterparty above threshold + summary |

### Per-cell detail

Each entry records:
- **Formula** — the rule applied (e.g. `"min(5% × max(0, 01 − gastos_reales), 2000) [art. 30.2.4ª LIRPF]"`)
- **Inputs** — named JSON dict of all values that fed the calculation (e.g. `{"base": 18400.00, "rate": 0.05, "cap_eur": 2000.0, "cap_applied": false}`)
- **Records** — the individual transactions and invoices included in the figure (date, counterparty, description, amounts), shown as a full DataFrame in the drill-down
- **Value** — the resulting EUR figure

The UI shows a summary table plus an expandable drill-down per cell. Each expander header shows how many records contribute to that figure. Results can be downloaded as JSON.

### Known approximations (documented in audit)

| Cell | Approximation | Impact |
|------|--------------|--------|
| `c28_base` (M303) | Manual `IVA_SOPORTADO` entries without a VAT rate (entered before #97) add their cuota to 29 but nothing to 28 | Flagged in the 303 notes — re-enter them with the rate |
| `c01_ingresos` (M130) | Ex-VAT base extracted from VAT-inclusive Stripe amounts | Correct for estimación directa — IVA is a pass-through, not income |

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
| `_cached_reconciliation()` · `_cached_filed_periods()` · `_cached_logged_audit()` | `app/tax_validation.py` | Engine computation + filed-return reads on every Reconciliation tab interaction (the catalogue is applied after the cache, so edits show at once) |
| `_load_invoices_df()` | `app/invoice_explorer.py` | Full `invoices` table scan + type conversions on every filter interaction |
| `_sidebar_stats()` | `app/streamlit_app.py` | 5 DB queries on every widget interaction across all tabs |

**Cache TTL:** 5 minutes. Results auto-refresh after 5 minutes, or immediately via the **↺ Refresh** button present in the Reconciliation and Invoice Explorer tabs.

**Invalidation rules:**
- Reconciliation: click **↺ Refresh** after running **Calculate tax** or loading new data to see updated figures
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

The LLM's own `subtotal_eur`/`iva_amount`/`total_eur` guess for a foreign-currency document is used only as a **cross-check** — the authoritative EUR figure comes from `src.fx_rates.resolve_invoice_amounts`, called right after extraction (`src/invoice_ingest.extract_and_save`, shared by the Invoice OCR tab and `close_quarter.py ocr`), in this order:

1. **`charged_eur`** — when the document itself states the EUR actually charged to the card (e.g. *"Charged 42.50 EUR using 1 USD = 0.8500 EUR"*), that wins. The OCR prompt extracts it into a new `charged_eur` field, left `null` when the document doesn't state it.
2. Otherwise, **the ECB rate on `invoice_date`** — `original_amount` divided by the daily rate from `fx_rates`, with the fallback/staleness behaviour above.

The resolved rate, its date and its source are stored per invoice (`fx_rate_used`, `fx_rate_date`, `fx_source` ∈ `NATIVE_EUR` / `CHARGED_EUR` / `ECB` / `NO_RATE` / `INVALID_DATE` / `MISSING_FX_INPUT`, `fx_stale`), and the LLM's own estimate is compared against the resolved figure: a difference over 1% is stored as `fx_cross_check_diff_pct` and surfaced as a ⚠️ warning in the Invoice OCR and Invoice Ledger tabs.

**Income invoices (`direction='out'`):** the same ECB resolution applies at extraction time, and it is **final**, not provisional — per art. 79.Once LIVA, income kept in a foreign-currency account (never converted) is booked at the ECB rate on the invoice date. If the money **was** actually converted on receipt, set `eur_received` in the Invoice Ledger tab once it's known; it then wins over the stored ECB figure in every tax computation that reads invoice income (Modelo 130 box 01, Modelo 303 box 120). See [Exchange rate differences](#exchange-rate-differences) for what happens when a foreign-currency balance booked at the ECB rate is converted later.

**Invoices stored before this resolver existed** (or before a later fix to it) keep whatever EUR figure the LLM originally guessed until corrected — resolution only runs at extraction time, not retroactively. Run `close_quarter.py fx-recompute` (or the Currency tab's **Recompute FX for stored invoices** button) to re-resolve every stored non-EUR invoice in place; see [Closing a Quarter](#closing-a-quarter) for the command and its guarantees (locked fields skipped, `eur_received` untouched, idempotent).

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

**Ledger columns** (added by an idempotent migration on startup), and the **backfill of `tax_treatment`** from the legacy `vat_treatment`: [docs/tax-engine-reference.md#ledger-columns](docs/tax-engine-reference.md#ledger-columns) and [docs/tax-engine-reference.md#backfill-of-tax_treatment](docs/tax-engine-reference.md#backfill-of-tax_treatment).

The tax engine uses `excluded`, `invoice_date`, the split business-use percentages and, for the Modelo 303, `tax_treatment` (see [Modelo 303 box model](docs/tax-engine-reference.md#modelo-303-box-model)). Rows with no `tax_treatment` are derived from the legacy `vat_treatment` on the fly.

### Exchange rate differences

A foreign-currency income invoice with no `eur_received` is booked at the ECB rate on the invoice date — final, not provisional (see above). If that foreign-currency balance is **later converted** to EUR, the conversion realises a gain or loss against the EUR figure originally booked, which must be recorded as activity income (or a loss) in the quarter of conversion, not the invoice's own quarter.

The Invoice Ledger tab's **income** view has an "Exchange rate differences" form for this: pick the invoice (optional), the conversion date, the foreign-currency amount converted, and the EUR actually obtained. It computes `gain_loss_eur = eur_obtained − booked_eur` and stores it in the `fx_exchange_differences` table (`src.fx_rates.record_exchange_difference` / `get_exchange_differences`). Every recorded gain/loss dated within a quarter's year-to-date window is added to that quarter's Modelo 130 box 01 income (`src.tax_engine.compute_modelo_130`) — on top of, not instead of, the invoice's own booked income, which keeps counting in its own quarter as usual.

---

## Duplicate Review

The **Duplicate Review** tab (and its `src/invoice_dedupe.py` module) finds and excludes duplicate, receipt and out-of-period invoices — the same expense counted twice inflates deductible VAT and IRPF expenses.

### Detectors

Five detectors run in priority order over the invoice ledger; a row already proposed by an earlier detector is never proposed again by a later one:

1. **Same `file_hash`** — byte-identical PDFs ingested under different filenames.
2. **Same vendor + `invoice_number`** — the same invoice re-ingested: same direction, vendor key (`vendor_vat_id_norm`, falling back to the vendor name), number, total and date. Rows that share the number but not the total or date (e.g. two issued invoices given the same number by mistake) are a **numbering problem**, not a duplicate: `find_numbering_conflicts` lists them as warnings (CLI ⚠, a warning in the tab) and nothing is excluded.
3. **Invoice/receipt pair** — same vendor, same total, dates within +/-3 days, and one side is a receipt (`invoice_type = "recibo"`, or `recibo`/`receipt` in the filename or description). **The invoice always wins.**
4. **Email-folder copy** — a file under an `email` subfolder duplicating a main-folder file (matched on vendor + invoice number, or vendor + total + date when no number is known).
5. **Out-of-period** — a row whose `invoice_date` falls outside the quarter being swept. Scoped to the invoices matching files copied into `tmp/close_quarter/<year>_Q<quarter>/`; a full-table scan is not meaningful here (every past quarter's rows would be "out of period").

**Keeper rule:** a row that is not excluded always beats an excluded one, so the current exclusion state is respected and a group keeps a live row; an invoice/receipt pair whose invoice is already excluded is not proposed. Then an invoice beats a receipt; among the rest, a non-`email`-path file wins, then the earliest-ingested (`extracted_at`) row. A row whose `excluded` field is already locked (a user edited it in the Invoice Ledger tab) is never touched by any detector or by `apply_groups` — a manual decision always wins over an automated one.

### Applying exclusions

Detection is a pure function (`find_duplicate_groups`) — nothing is written until you apply a group. Applying writes `excluded=1` / `excluded_reason` via `set_invoice_exclusion`, the same ledger-only write path `upsert_invoice` uses, **without** locking the field — so re-extraction or a later manual edit can still change it. This differs from editing a row directly in the Invoice Ledger tab, which always locks the fields you touch. `apply_groups` re-reads each group's keeper first and skips a group whose keeper is excluded by then (reported as a skipped group), so applying can never leave a duplicate group with every row excluded.

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

**Applying.** Every OCR extraction applies the registry to the new row, and `close_quarter.py vendors` applies it to the quarter being closed. The **Vendors** tab's **Apply registry** button, the CLI below and `close_quarter.py vendors --all-periods` re-apply it to all stored expense invoices, filed periods included, and report the rows written per year/quarter (filed ones marked `(FILED)`). All of them are idempotent:

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

**VAT capital goods.** An asset whose unit base is above €3,005.06 is a *bien de inversión* (art. 108 LIVA; auto-detected, overridable). `capital_goods_vat_for_period` gives its Modelo 303 boxes **30/31** in the quarter of acquisition: base × VAT business-use %, and VAT × VAT business-use % (or the `vat_deducted_eur` override). The **regularisation register** covers the year of acquisition + 4: record the VAT business-use % actually applied each year, and a year whose % differs from the acquisition year's by more than 10 points gets an adjustment of VAT borne ÷ 5 × (% of the year − initial %) (arts. 107–109 LIVA), for 303 box 43 (*regularización bienes de inversión*) at Q4. The 303 takes boxes 30/31 from this register and leaves the capital-good share of the linked invoice out of 28/29. The one-off disposal adjustment of art. 110 LIVA is not computed.

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
