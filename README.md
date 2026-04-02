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
   Persist classifications to SQLite
          │
          ▼
   Aggregate, display, export
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
│   ├── classifier.py              # Activity and geographic classification
│   ├── aggregator.py              # Monthly/quarterly aggregations and totals
│   ├── excel_exporter.py          # Multi-sheet Excel report generation
│   ├── stripe_client.py           # Stripe API wrapper (charges, fees, card country)
│   ├── fx_rates.py                # FX rate fetching (ECB/Frankfurter), storage, conversion
│   ├── database.py                # SQLite operations (transactions, FX rates, upload log)
│   ├── accounting_api_client.py   # IntegraLOOP/BILOOP Accounting API client
│   ├── logger.py                  # Rotating file logger
│   └── exceptions.py              # Custom exception classes
├── app/                           # Streamlit dashboard
│   ├── streamlit_app.py           # Entry point: welcome page and horizontal tabs
│   ├── data_loader.py             # Data loading, FX conversion, classification pipeline
│   ├── quarter_report.py          # Quarterly summary + Excel export
│   ├── transaction_browser.py     # Browse/filter transactions + geographic overrides
│   ├── history.py                 # Timeline charts across all quarters
│   ├── currency.py                # FX rate management, charts, and conversion tool
│   ├── configuration.py           # Rules editor, Stripe API key, cache management
│   └── invoice_upload.py          # Accounting partner (IntegraLOOP/BILOOP) integration
├── tests/                         # Pytest test suite
│   ├── conftest.py                # Shared fixtures
│   ├── test_classifier.py
│   ├── test_models.py
│   ├── test_database.py
│   ├── test_fx_rates.py
│   ├── test_rules_engine.py
│   └── test_aggregator.py
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

- **transactions** — Stripe payment records with classification and FX conversion data
- **fx_rates** — Daily ECB exchange rates (EUR/USD, EUR/GBP, EUR/CHF)
- **upload_log** — Invoice upload tracking to prevent duplicates

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
