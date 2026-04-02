# Stripe Accounting Quarterly Automation

Automated Stripe payment classification and quarterly reporting system. Classifies payments by activity type (Coaching, Newsletter, Illustrations) and geographic region (Spain, EU-not-Spain, Outside-EU), then produces Excel reports and a Streamlit dashboard.

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate  # macOS/Linux
# .venv\Scripts\activate   # Windows

pip install -r requirements.txt
```

### 2. Configure

Copy the example files and edit them:

```bash
cp config.json.example config.json
cp classification_rules.json classification_rules.json  # already included
cp .env.example .env  # add your Stripe API key
```

### 3. Place CSV exports (or use Stripe API)

Copy your Stripe CSV exports to:
```
data/raw/unified_payments_all_old.csv   # older export (with Currency column)
data/raw/unified_payments_all.csv       # newer export (without Currency column)
```

Or configure the Stripe API key to fetch data directly.

### 4. Launch the dashboard

```bash
streamlit run app/streamlit_app.py
```

---

## Data Flow

```
Stripe CSV exports  ──or──  Stripe API (live charges)
        │                           │
        └───────────┬───────────────┘
                    ▼
          Parse & deduplicate
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
       Aggregate, display, export
```

**Where data is loaded from:** The app reads Stripe transaction data from CSV
files in `data/raw/` (configured in `config.json`). Non-EUR amounts (GBP, USD,
CHF) are automatically converted to EUR using ECB exchange rates stored in the
local SQLite database (`data/accounting.db`). If a rate is missing for a
transaction date, the system fetches it from the Frankfurter API or falls back
to the most recent available rate.

---

## Project Structure

```
├── config.json                    # App settings (git-ignored, use config.json.example)
├── classification_rules.json      # Activity and geographic classification rules (editable)
├── requirements.txt
├── src/                           # Core business logic
│   ├── models.py                  # Pydantic data models (Payment, ClassifiedPayment, ...)
│   ├── config.py                  # Load/save config.json
│   ├── rules_engine.py            # Load/save classification_rules.json
│   ├── classifier.py              # Activity and geographic classification (reads from rules JSON)
│   ├── csv_importer.py            # CSV parsing, amount normalisation, deduplication
│   ├── aggregator.py              # Monthly/quarterly aggregations and totals
│   ├── excel_exporter.py          # Multi-sheet Excel report generation
│   ├── stripe_client.py           # Stripe API wrapper (charges, fees, card country)
│   ├── fx_rates.py                # FX rate fetching (ECB/Frankfurter), storage, conversion
│   ├── database.py                # SQLite database for transactions, FX rates, upload log
│   ├── logger.py                  # Rotating file logger
│   └── exceptions.py              # Custom exception classes
├── app/                           # Streamlit dashboard (flat structure, no subfolders)
│   ├── streamlit_app.py           # Entry point with welcome page and horizontal tabs
│   ├── data_loader.py             # Cached data loading + FX conversion pipeline
│   ├── quarter_report.py          # Quarterly summary + Excel export
│   ├── transaction_browser.py     # Browse/filter transactions + overrides
│   ├── history.py                 # Timeline charts across all quarters
│   ├── currency.py                # FX rate management, charts, and conversion tool
│   ├── configuration.py           # Classification rules editor, API keys, settings
│   └── invoice_upload.py          # Invoice upload scaffold for accounting partner
├── tests/                         # Pytest test suite
│   ├── conftest.py                # Shared fixtures
│   ├── test_classifier.py         # Classification engine tests
│   ├── test_models.py             # Data model tests
│   ├── test_database.py           # SQLite database tests
│   ├── test_fx_rates.py           # FX rate fetch, store, convert, fallback tests
│   ├── test_rules_engine.py       # Rules JSON load/save tests
│   └── test_aggregator.py         # Aggregation logic tests
├── data/
│   ├── raw/                       # CSV source files (git-ignored)
│   ├── processed/                 # Generated reports
│   ├── accounting.db              # SQLite database (git-ignored)
│   ├── invoices/in/               # Invoices received (PDFs)
│   └── invoices/out/              # Invoices produced (PDFs)
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

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | Currency is **not EUR** | OUTSIDE_EU |
| 2 | EUR + explicit name/email override | Per override |
| 3 | EUR + activity is **NEWSLETTER** | EU_NOT_SPAIN |
| 4 | EUR + any other activity | SPAIN |

Newsletter payments in EUR lack customer email/country metadata in Stripe CSV exports. By convention they are classified as EU (not Spain). Individual exceptions can be added via overrides.

### Card issuing country

When using the Stripe API, the card issuing country (`charge.payment_method_details.card.country`) is extracted automatically. This provides ISO country codes (ES, DE, US, etc.) that can improve geographic classification accuracy beyond currency-based heuristics.

---

## Currency Conversion

Non-EUR transactions (USD, GBP, CHF) are automatically converted to EUR using daily exchange rates from the European Central Bank (ECB).

**Source:** [Frankfurter API](https://frankfurter.dev) - free, open-source, based on ECB reference rates. No API key required.

**How it works:**

1. Load historical FX rates via the **Currency** tab (or they are fetched on-demand)
2. Rates are stored in SQLite (`fx_rates` table) for offline access
3. When a non-EUR transaction is loaded, the rate for its date is looked up
4. If no rate exists for the exact date (weekends, holidays), the most recent previous rate is used
5. If no rate exists at all, the system attempts a live fetch from the Frankfurter API

**Supported currency pairs (all expressed as 1 EUR = X):**

| Pair | Description |
|------|-------------|
| EUR/USD | US Dollar |
| EUR/GBP | British Pound |
| EUR/CHF | Swiss Franc |

The Currency tab also provides interactive charts showing historical rates and a conversion calculator.

---

## Historical Validation

The classification system was validated against historical known totals covering the period from July 2023 to December 2025. All computed totals matched the original manual Excel files, confirming the accuracy of the automated classification rules.

---

## Database

Transaction data is stored in a SQLite database (`data/accounting.db`) for persistent storage and incremental loading:

- **transactions** — Stripe payment records with classification and FX conversion data
- **fx_rates** — Daily ECB exchange rates (EUR/USD, EUR/GBP, EUR/CHF)
- **upload_log** — Invoice upload tracking to prevent duplicates
- Date-range queries and incremental upserts for efficient operation

---

## Stripe API

Set `STRIPE_API_KEY` in a `.env` file at the project root:

```
STRIPE_API_KEY=sk_live_...
```

Required permissions for restricted keys (`rk_live_...`):
- **Read charges** — transaction data, amounts, descriptions, card country
- **Read balance transactions** — fee details

The dashboard includes a permission checker to verify which API resources are accessible.

---

## Running Tests

```bash
pytest tests/ -v
```

---

## Invoice Upload

The Invoice Upload tab provides a scaffold for uploading invoice PDFs to an accounting partner's system:

- **Invoices In** (`data/invoices/in/`): received invoices
- **Invoices Out** (`data/invoices/out/`): produced invoices
- Tracks which files have been uploaded to avoid duplicates
- API connection is a placeholder — configure `accounting_api` in `config.json` when ready
