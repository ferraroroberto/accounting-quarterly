# Stripe Accounting Quarterly Automation

Automated Stripe payment classification and quarterly reporting system. Classifies payments by activity type (Coaching, Newsletter, Illustrations) and geographic region (Spain, EU-not-Spain, Outside-EU), then produces Excel reports and a Streamlit dashboard.

---

## Quick Start

### 1. Install dependencies

```bash
python -m venv .venv
.venv\Scripts\activate      # Windows
# source .venv/bin/activate  # macOS/Linux

pip install -r requirements.txt
```

### 2. Place CSV exports

Copy your Stripe CSV exports to:
```
data/raw/unified_payments_all_old.csv   # older export (with Currency column)
data/raw/unified_payments_all.csv       # newer export (without Currency column)
```

Or update the paths in `config.json → csv_paths`.

### 3. Launch the dashboard

```bash
cd E:\automation\accounting-quarterly
.venv\Scripts\streamlit run app/streamlit_app.py
```

---

## Project Structure

```
├── config.json                # All settings, classification rules, client overrides
├── requirements.txt
├── src/
│   ├── models.py              # Pydantic data models (Payment, ClassifiedPayment, …)
│   ├── config.py              # Load/save config.json
│   ├── csv_importer.py        # CSV parsing, amount normalisation, deduplication
│   ├── classifier.py          # Activity and geographic classification logic
│   ├── aggregator.py          # Monthly/quarterly aggregations and totals
│   ├── validator.py           # Historical validation against known_totals
│   ├── excel_exporter.py      # Multi-sheet Excel report generation
│   ├── stripe_client.py       # Stripe API wrapper (optional live data)
│   ├── logger.py              # Rotating file logger
│   └── exceptions.py          # Custom exception classes
├── app/
│   ├── streamlit_app.py       # Streamlit entry point
│   ├── components/
│   │   └── data_loader.py     # Cached data loading helpers
│   └── pages/
│       ├── 01_Quarter_Report.py      # Quarterly summary + Excel export
│       ├── 02_Transaction_Browser.py # Browse/filter transactions + overrides
│       ├── 03_Validation.py          # Historical validation runner
│       ├── 04_Configuration.py       # API keys, overrides, patterns
│       └── 05_History.py             # Trend charts across all quarters
├── data/
│   ├── raw/                   # CSV source files (git-ignored)
│   ├── processed/             # Generated reports and validation output
│   └── cache/                 # Temporary processing cache
└── logs/                      # Rotating daily log files
```

---

## Classification Logic

### Activity type

| Pattern | Activity |
|---------|----------|
| `Calendly` in description | COACHING |
| `Master Virtual Meetings` / Luma events | COACHING |
| `Subscription update/creation` | NEWSLETTER |
| `Charge for <email>` | ILLUSTRATIONS |

### Geographic region

| Signal | Region |
|--------|--------|
| Currency = USD / GBP | OUTSIDE_EU |
| Currency = EUR, no override | SPAIN (default) |
| email_meta or description email in `email_overrides` | Per override |
| Description substring in `geographic_overrides` | Per override |

Override keys in `config.json`:
- `email_overrides` — maps exact email addresses to `SPAIN / EU_NOT_SPAIN / OUTSIDE_EU`
- `geographic_overrides` — maps description substrings to regions

---

## Historical Validation


**Newsletter regional split (Spain vs EU-not-Spain):** EUR newsletter subscriptions (`Subscription update/creation`) carry no customer email or billing-country metadata in the Stripe CSV export. All EUR newsletter payments therefore default to Spain. Accurate regional split requires either the Stripe API (`charge.billing_details.address.country`) or a manual override file keyed by transaction ID.

---

## Stripe API (optional)

Set `STRIPE_SECRET_KEY` in a `.env` file at the project root:

```
STRIPE_SECRET_KEY=sk_live_...
```

The dashboard will offer to fetch live charges from the API instead of CSV when a key is configured.

---

## Generating Reports Manually

```bash
# Run full validation and save report + Excel
python tmp/generate_report.py

# Generate Q-specific Excel
python tmp/test_q1_2026.py
```
