# Stripe Accounting Quarterly Automation

Automated Stripe payment classification and quarterly reporting system. Classifies payments by activity type (Coaching, Newsletter, Illustrations) and geographic region (Spain, EU-not-Spain, Outside-EU), then produces Excel reports and a Streamlit dashboard.

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
   Aggregate, display, export, compute tax models
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
│   ├── database.py                # SQLite operations (transactions, FX rates, upload log, invoices, tax)
│   ├── tax_models.py              # Dataclasses for Modelo303, Modelo130, OSS, 347, 349 results
│   ├── tax_engine.py              # Spanish tax computation: Modelo 303/130/349/347, OSS, calendar
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
│   └── tax_obligations.py         # Tax obligations tab (Modelo 303/130/349/347, OSS)
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
- **invoices** — AI-extracted invoice records (vendor, client, IVA/IRPF breakdown, totals, Spanish AEAT fields)
- **quarterly_tax_entries** — Manual tax inputs (IVA soportado, gastos deducibles, retenciones)
- **tax_filing_status** — Filing status and computed amounts per model/quarter

Classifications are persisted in the database so the classifier only runs when fresh data is fetched from Stripe, not on every page load.

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

## Tax Obligations (Spanish Autónomo)

The **Tax Obligations** tab turns the classified transaction data into pre-filled Spanish tax filings. It covers the standard obligations for an autónomo in *régimen de estimación directa simplificada*.

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

### Manual entries

Items that cannot be derived from Stripe (IVA soportado on expenses, deductible costs, retenciones received from Spanish clients) are entered via the **Manual Entries** sub-tab and stored in `quarterly_tax_entries`.

> **Disclaimer:** This tool pre-fills tax data for review purposes only. It does not constitute tax advice. Always review outputs with a qualified gestor or asesor fiscal before filing.

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
