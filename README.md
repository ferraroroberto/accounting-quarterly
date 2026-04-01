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
│       ├── 04_Configuration.py       # API keys, overrides, patterns, geographic rules
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

Rules are evaluated in priority order; the first match wins.

| Priority | Pattern | Activity |
|----------|---------|----------|
| 1 | Empty / null description | COACHING |
| 2 | Luma `registration` payment type | COACHING |
| 3 | `charge for` in description | ILLUSTRATIONS |
| 4 | `subscription` in description | NEWSLETTER |
| 5 | `calendly`, `coach`, `discovery session`, `consulting`, `virtual meetings` | COACHING |
| 6 | No pattern matched | UNKNOWN |

Keywords for each category are configurable in `config.json → classification_patterns`.

### Geographic region

Rules are evaluated in priority order; the first match wins.

| Priority | Condition | Region |
|----------|-----------|--------|
| 1 | Currency is **not EUR** | OUTSIDE_EU |
| 2 | EUR + explicit name/email match in overrides | Per override |
| 3 | EUR + activity is **NEWSLETTER** | EU_NOT_SPAIN |
| 4 | EUR + any other activity | SPAIN |

Newsletter payments in EUR lack customer email/country metadata in Stripe CSV exports, so they cannot be attributed to a specific person. By convention they are classified as EU (not Spain) since the newsletter audience is international.

Override keys in `config.json`:
- `geographic_rules` — configures the three default regions (`eur_default`, `eur_newsletter_default`, `non_eur_default`)
- `email_overrides` — maps exact email addresses to `SPAIN / EU_NOT_SPAIN / OUTSIDE_EU`
- `geographic_overrides` — maps description substrings (or email patterns) to regions; always takes priority over defaults

---

## Historical Validation

**Newsletter regional split:** EUR newsletter subscriptions (`Subscription update/creation`) carry no customer email or billing-country metadata in the Stripe CSV export. They are classified as **EU (not Spain)** by default (`eur_newsletter_default`). Individual exceptions can be added via `geographic_overrides` or `email_overrides` in `config.json`, or through the **Geographic Rules** tab in the dashboard.

---

## Stripe API (optional)

Set `STRIPE_API_KEY` in a `.env` file at the project root (see `.env.example`):

```
STRIPE_API_KEY=sk_live_...
```

The dashboard will offer to fetch live charges from the API instead of CSV when a key is configured.

If you use a **restricted key** (`rk_live_...`), turn on **Read charges** for that key in the [Stripe API keys](https://dashboard.stripe.com/apikeys) editor. The app calls `Charge.list`; without charge read, Stripe returns an error mentioning `rak_charge_read`.

---

## Generating Reports Manually

```bash
# Run full validation and save report + Excel
python tmp/generate_report.py

# Generate Q-specific Excel
python tmp/test_q1_2026.py
```
