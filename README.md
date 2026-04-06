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
   Classify VAT treatment
   (activity × geography → IVA_ES_21 / IVA_EU_B2B / OSS_EU / IVA_EXPORT)
          │
          ▼
   Persist classifications to SQLite
          │
          ▼
   Aggregate, display, export; save tax snapshots; validate vs. filed AEAT data
```

Transaction data is fetched from the Stripe API and stored in the local SQLite database (`data/accounting.db`). On subsequent loads the dashboard reads pre-classified data directly from the database — the classifier only runs when new data is fetched from the API. Non-EUR amounts (GBP, USD, CHF) are converted to EUR using ECB exchange rates. If a rate is missing for a transaction date, the system fetches it from the Frankfurter API or falls back to the most recent available rate.

---

## Project Structure

```
├── config.json                    # App settings (git-ignored, use config.json.example)
├── classification_rules.json      # Classification rules (git-ignored, copy from .example)
├── classification_rules.json.example
├── config.json.example
├── requirements.txt
├── launch_app.bat                 # Windows launch shortcut
├── src/                           # Core business logic
│   ├── models.py                  # Pydantic data models (Payment, ClassifiedPayment, ...)
│   ├── config.py                  # Load/save config.json
│   ├── rules_engine.py            # Load/save classification_rules.json
│   ├── classifier.py              # Activity, geographic and VAT classification
│   ├── aggregator.py              # Monthly/quarterly aggregations and totals
│   ├── excel_exporter.py          # Multi-sheet Excel report generation
│   ├── stripe_client.py           # Stripe API wrapper (charges, fees, card country)
│   ├── fx_rates.py                # FX rate fetching (ECB/Frankfurter), storage, conversion
│   ├── database.py                # SQLite operations (transactions, FX rates, upload log, invoices, SS, tax, audit)
│   ├── social_security.py         # SS cuota import from bank exports + DB query helpers
│   ├── tax_models.py              # Dataclasses for Modelo303, Modelo130, OSS, 347, 349 results + AuditEntry
│   ├── tax_engine.py              # Spanish tax computation: Modelo 303/130/349/347, OSS, calendar
│   ├── tax_snapshot_codec.py      # Serialize/deserialize tax engine results for SQLite snapshot storage
│   ├── tax_validator.py           # Validation: compare gestor-filed AEAT figures vs DB-computed values
│   ├── accounting_api_client.py   # IntegraLOOP/BILOOP Accounting API client
│   ├── invoice_ocr.py             # Gemini-powered PDF extraction for Spanish accounting
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
│   ├── invoice_ocr_tab.py         # AI invoice extraction tab (Gemini OCR)
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
│   └── test_tax_engine.py         # VAT classification, Modelo 303/130, OSS, Modelo 349
├── data/
│   ├── accounting.db              # SQLite database (git-ignored)
│   ├── processed/                 # Generated Excel reports
│   ├── cache/                     # Temporary cache files
│   └── invoices/
│       ├── in/                    # Invoices received (PDFs)
│       └── out/                   # Invoices produced (PDFs)
├── tmp/
│   ├── social_security_bank_export.xlsx  # Bank export for SS cuotas (git-ignored, configurable)
│   └── validation/
│       └── validation.yaml        # Gestor-filed AEAT reference data (git-ignored)
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

| Priority | Condition | Default region |
|----------|-----------|----------------|
| 1 | Currency is **not EUR** | OUTSIDE_EU |
| 2 | EUR + explicit name/email override | Per override |
| 3 | EUR + activity is **NEWSLETTER** | EU_NOT_SPAIN |
| 4 | EUR + any other activity | SPAIN |

The default region for each condition is configurable in the Geographic Rules section of the Configuration tab.

### Card issuing country

The card issuing country (`charge.payment_method_details.card.country`) is extracted from the Stripe API automatically. This provides ISO country codes (ES, DE, US, etc.) that can improve geographic classification accuracy beyond currency-based heuristics.

---

## Currency Conversion

Non-EUR transactions (USD, GBP, CHF) are automatically converted to EUR using daily exchange rates from the European Central Bank (ECB).

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

---

## Database

Transaction data is stored in a SQLite database (`data/accounting.db`):

- **transactions** — Stripe payment records with classification, FX conversion, and VAT treatment data
- **fx_rates** — Daily ECB exchange rates (EUR/USD, EUR/GBP, EUR/CHF)
- **upload_log** — Invoice upload tracking to prevent duplicates
- **invoices** — AI-extracted invoice records (vendor, client, IVA/IRPF breakdown, totals, Spanish AEAT fields). Includes `geo_region`, `vat_treatment`, `activity_type`, and `supply_country` columns auto-derived from the vendor/client NIF at insert time — mirroring the `transactions` table so both sources feed the tax engine uniformly
- **social_security_payments** — Seguridad Social cuota payments imported from bank account exports. Deduplication key: `(payment_date, amount_eur)`. Automatically included as deductible expenses in Modelo 130 box 02 (YTD)
- **quarterly_tax_entries** — Manual tax inputs (IVA soportado, gastos deducibles, retenciones)
- **tax_filing_status** — Filing status and computed amounts per model/quarter
- **tax_computation_snapshots** — JSON snapshots of tax engine outputs (Modelo 303/130/OSS/349/347) written when you click **Calculate tax** in Tax Obligations
- **tax_audit_log** — Per-cell calculation audit entries: every box in every model records the formula applied, named inputs, and computed value. Written alongside snapshots; queryable by year/quarter/model/run timestamp

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

## Seguridad Social (Social Security Cuotas)

The **Seguridad Social** tab imports monthly autónomo quota payments debited from your bank account and feeds them automatically into **Modelo 130** as deductible expenses.

### Data source

Cuota payments are not issued as invoices — they appear as bank debits. Export your bank statement (the rows corresponding to Seguridad Social payments) to Excel or CSV and import via this tab.

### Setup

1. Export the relevant rows from your bank's online portal to `.xlsx` or `.csv`.
2. Place the file anywhere accessible (default: `tmp/social_security_bank_export.xlsx`).
3. Configure the column names in `config.json`:

```json
{
  "social_security": {
    "bank_export_file": "tmp/social_security_bank_export.xlsx",
    "date_column": "Fecha",
    "amount_column": "Importe",
    "description_column": "Concepto",
    "sheet_name": 0,
    "skiprows": 0
  }
}
```

4. Open the **Seguridad Social** tab, verify the column mapping with **Preview file columns**, then click **Import from file**.

### How it works

- Amounts are stored as **positive values** in the `social_security_payments` table (debits from the bank export are negative; the importer takes the absolute value).
- Deduplication is by `(payment_date, amount_eur)` — re-importing the same file is safe.
- The **Modelo 130** engine sums all SS payments from January 1 through the end of the selected quarter (YTD) and includes them in **box 02 — gastos deducibles**, alongside OCR-extracted expense invoices and manual entries. Legal basis: cuotas de autónomo are fully deductible under Art. 30 LIRPF (*régimen de estimación directa*).
- The audit trail (Tax Audit tab) records `ss_gastos` and the full list of individual payments as named inputs to the `box_02_gastos` cell.

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
| **OSS Return** | One Stop Shop | Quarterly | B2C digital services to EU non-Spain customers, grouped by country |
| **Modelo 347** | Operaciones con Terceros | Annual | Spain counterparties with total operations > €3,005.06 |

### VAT treatment classification

Each transaction is automatically assigned a VAT treatment based on activity × geography:

| Activity | Geography | Treatment | IVA |
|----------|-----------|-----------|-----|
| Any | OUTSIDE_EU | `IVA_EXPORT` | 0% |
| Any | SPAIN | `IVA_ES_21` | 21% |
| COACHING / ILLUSTRATIONS | EU_NOT_SPAIN | `IVA_EU_B2B` | 0% (reverse charge) |
| NEWSLETTER | EU_NOT_SPAIN | `OSS_EU` | Buyer country rate |

### Tax configuration

Add a `tax` section to `config.json` (see `config.json.example`), or use the **Configuration → Tax Settings** tab:

```json
{
  "tax": {
    "regime": "estimacion_directa_simplificada",
    "nif": "YOUR_NIF",
    "irpf_retention_rate": 0.15,
    "vat_registered": true,
    "oss_registered": true,
    "oss_registration_country": "ES",
    "activity_start_date": "YYYY-MM-DD",
    "default_vat_treatment_eu_coaching": "IVA_EU_B2B",
    "default_vat_treatment_eu_newsletter": "OSS_EU"
  }
}
```

> **IRPF retention rate:** Use `0.15` (15%) after the first 3 years of activity, or `0.07` (7%) during the first 3 years. Configurable per the slider in Tax Settings.

### Invoice data in tax calculations

OCR-extracted invoices (from the Invoice OCR tab) feed directly into all tax models alongside Stripe transactions:

| Model | Source | Contribution |
|-------|--------|-------------|
| **Modelo 303** box_28 | Expense invoices (`direction='in'`) | IVA soportado deducible |
| **Modelo 303** box_01 | Income invoices (`IVA_ES_21`) | Base imponible devengado |
| **Modelo 130** box_01 | Non-Stripe income invoices (`direction='out'`) | Subtotal ingresos YTD |
| **Modelo 130** box_02 | Expense invoices (`direction='in'`) | Subtotal gastos (weighted by `deductible_pct`) YTD |
| **Modelo 130** box_02 | `social_security_payments` table | SS cuotas YTD (fully deductible) |
| **Modelo 130** box_07 | Outgoing invoices | IRPF withheld (`irpf_amount`) YTD |
| **Modelo 347** | Income invoices | Spanish-client invoice income alongside Stripe |
| **Modelo 349** | Income invoices | EU B2B invoice income alongside Stripe |

Geographic classification is auto-derived from the vendor NIF (expenses) or client NIF (income) at OCR extraction time. Existing rows are backfilled automatically on database init.

### Manual entries

Items that cannot be derived from Stripe or invoices (additional overrides, one-off corrections) are entered via the **Manual Entries** sub-tab and stored in `quarterly_tax_entries`.

> **Disclaimer:** This tool pre-fills tax data for review purposes only. It does not constitute tax advice. Always review outputs with a qualified gestor or asesor fiscal before filing.

---

## Tax Validation

The **Tax Validation** tab cross-checks the figures your gestor filed with AEAT against the values computed from your local database, making it easy to spot missing invoices, unclassified transactions, or expenses not yet entered.

### How it works

1. Filed reference data is stored in `tmp/validation/validation.yaml` (gitignored — never committed).
2. The tab loads that file, runs the same tax-engine computations as in Tax Obligations (against your current SQLite data), and builds a line-by-line comparison for each casilla (PDF box).
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

### Adding a new filing period

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
| **Modelo 130** | box_01_ingresos, box_02_gastos, box_03_rendimiento, gastos_dificil_justificacion (with cap flag), rendimiento_neto, box_05_base, box_07_retenciones, box_14_pagos_anteriores, box_16_resultado |
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
| `box_29_base_soportado` (M303) | `box_28_iva_soportado / 0.21` assumes all deductible expenses at 21% | Display only — does not affect `box_46` or `box_48` |
| `box_48_resultado` (M303) | 100% proration assumed | User must enter only the deductible portion of IVA in manual entries |

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

The **Invoice OCR** tab uses Google Gemini to extract Spanish accounting data from any PDF — invoices, receipts, tickets, foreign bills — and stores the results in the `invoices` SQLite table.

### Invoice directories

Configured via `config.json` (`invoice_in_dir` / `invoice_out_dir`). Both accept absolute paths. PDFs are scanned **recursively**, so subdirectories (e.g. year/quarter folders) are included automatically.

| Direction | Default path | Accounting role |
|-----------|-------------|-----------------|
| **In** (expenses) | `E:/.../invoices in` | Facturas recibidas — IVA soportado |
| **Out** (income) | `E:/.../invoices out/archive` | Facturas emitidas — IVA repercutido |

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

### All Records tab features

- **Date scanned** column shows when each invoice was extracted.
- **Row-selection checkboxes** — select one or more records and click **Delete selected**.
- **Clear invoice table** — wipes all records (with confirmation); PDF files are never touched.

### Google API key (AI Studio — recommended)

The simplest option: get a free key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey) and add it to `.env`:

```
GOOGLE_API_KEY=AIzaSy...
```

Model used: `gemini-3.1-flash-lite-preview`.

---

## Invoice Explorer

The **Invoice Explorer** tab provides a filterable, exportable view of all OCR-extracted invoices in a single table.

**Filters available:** direction (in/out), category, invoice type, vendor name (text search), client name (text search), invoice date range, subtotal range, and a "rectificativas only" toggle.

Live summary metrics (matching count, total expenses, total income) update as filters change. Results can be exported to CSV.

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
