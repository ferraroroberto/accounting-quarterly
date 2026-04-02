# Feature Proposals — Accounting Sync & Management System

> **Document purpose:** Analysis and ready-to-implement feature proposals for an LLM coding assistant.
> Each feature section is self-contained and can be handed directly to an LLM as a prompt.
> **Current date:** 2026-04-02

---

## System Overview (Context for LLM)

This is a **Python/Streamlit accounting automation system** for a freelancer (Spain-based) that:
- Fetches payments from the **Stripe API** and stores them in **SQLite**
- Classifies payments by **activity type** (`COACHING`, `NEWSLETTER`, `ILLUSTRATIONS`) and **geography** (`SPAIN`, `EU_NOT_SPAIN`, `OUTSIDE_EU`) using a JSON rules engine
- Converts foreign currencies (USD, GBP, CHF) to EUR via **ECB rates (Frankfurter API)**
- Generates **quarterly Excel reports** (multi-sheet, formatted)
- Provides a **7-tab Streamlit dashboard** for browsing, reporting, and configuration
- Integrates with an accounting partner (IntegraLOOP/BILOOP) for invoice uploads

**Tech stack:** Python 3, SQLite, Pydantic 2, Streamlit, Plotly, Pandas, Openpyxl, Stripe SDK, Pytest

---

## Feature Proposals

---

### Feature 1: Expense Tracking Module

**Rationale:** The system currently tracks only income. For meaningful profit/loss analysis, expenses must be recorded alongside income. This is the single highest-value practical gap.

**What to build:**

Add an `expenses` SQLite table and a full CRUD UI tab in the Streamlit dashboard.

```
expenses table:
  id (TEXT PRIMARY KEY, auto-generated UUID)
  date (TEXT, ISO format)
  amount_eur (REAL)
  currency (TEXT, default 'eur')
  amount_original (REAL, nullable)
  fx_rate (REAL, nullable)
  description (TEXT)
  vendor (TEXT)
  category (TEXT)  -- TOOLS, SUBSCRIPTIONS, MARKETING, PROFESSIONAL_SERVICES, OTHER
  activity_type (TEXT)  -- same enum as income: COACHING, NEWSLETTER, ILLUSTRATIONS, SHARED
  receipt_filename (TEXT, nullable)
  notes (TEXT, nullable)
  created_at (TEXT)
  updated_at (TEXT)
```

**UI Tab: "Expenses"**
- Add expense form (date, amount, currency, vendor, description, category, activity mapping)
- Expenses table with filter by quarter/year/category/activity
- Receipt file upload (store in `data/receipts/`)
- FX conversion using existing `src/fx_rates.py` module

**Impact on existing features:**
- Update `src/aggregator.py` to include expense rows in monthly aggregations
- Update `src/excel_exporter.py` to add an Expenses sheet and a P&L summary sheet
- Update the Quarter Report tab to show net income (income minus expenses) per activity

**Educational value:** Introduces full CRUD operations, file handling, and extending an existing data model without breaking existing functionality.

---

### Feature 2: AI-Assisted Classification with LLM Fallback

**Rationale:** The current rules engine assigns `UNKNOWN` to transactions it cannot classify. Manual intervention is required. An LLM can infer the correct activity and geography from the payment description, email, and metadata — and explain its reasoning.

**What to build:**

Add a module `src/ai_classifier.py` that calls the **Anthropic Claude API** as a fallback classifier.

**Trigger:** Only invoked when the rules engine returns `UNKNOWN` for activity or geography.

**Inputs sent to Claude:**
```
description, payment_type_meta, email_meta, card_country, currency, amount_eur
```

**Expected Claude JSON output:**
```json
{
  "activity_type": "COACHING",
  "geo_region": "EU_NOT_SPAIN",
  "confidence": "high",
  "reasoning": "Description 'session booking via website' matches coaching pattern; email domain .fr suggests France (EU, not Spain).",
  "suggested_rule": {
    "match_type": "description_contains",
    "keywords": ["session booking"],
    "activity_type": "COACHING"
  }
}
```

**UI additions:**
- In the Transaction Browser, `UNKNOWN` transactions show an "Ask AI" button
- AI response shown inline with confidence and reasoning
- "Accept & Save as Rule" button — appends the suggested_rule to `classification_rules.json` automatically
- New Configuration sub-tab: "AI Classifier" — toggle on/off, show API key status, usage stats (calls made, UNKNOWN transactions resolved)

**Implementation notes:**
- Use `anthropic` Python SDK (already in requirements or add it)
- Cache AI responses in SQLite to avoid re-querying for the same transaction description
- System prompt must enforce JSON output and reference the existing activity/geography enums
- Log all AI calls with input/output to `logs/` for auditability

**Educational value:** Demonstrates LLM tool use, structured JSON outputs, human-in-the-loop approval flows, and the "AI as fallback" pattern which is more robust than pure AI classification.

---

### Feature 3: Scheduled Sync & Background Jobs

**Rationale:** Currently, syncing with Stripe is manual — a user must open the dashboard and click "Sync". Automating this removes friction and ensures the database is always fresh.

**What to build:**

Add a `src/scheduler.py` module using Python's **APScheduler** library (or a simple cron wrapper).

**Scheduled jobs:**
1. **Daily Stripe sync** — fetch yesterday's charges at 02:00 local time, classify, store
2. **Weekly FX rate backfill** — ensure no rate gaps in the past 30 days
3. **Monthly report pre-generation** — generate Excel report for the closing month on the 1st of each new month, store in `data/processed/`

**Implementation options (offer both):**

Option A — **Standalone scheduler script** (`scheduler.py` at project root):
```bash
python scheduler.py  # runs as a daemon; add to crontab or systemd
```

Option B — **APScheduler embedded in Streamlit** (background thread):
- Starts when the app starts
- Shows "Last auto-sync: X minutes ago" in the sidebar
- User can configure sync frequency in the Configuration tab

**New config.json fields:**
```json
{
  "scheduler": {
    "enabled": true,
    "stripe_sync_hour": 2,
    "stripe_sync_lookback_days": 2,
    "fx_rate_backfill_days": 30,
    "auto_report_on_month_close": true
  }
}
```

**Educational value:** Introduces background job scheduling, daemon processes, and the difference between event-driven vs. polling architectures.

---

### Feature 4: Email & Alert Notifications

**Rationale:** For a freelancer working with an accounting partner, proactive notifications are more useful than a dashboard that requires manual checking.

**What to build:**

Add a `src/notifier.py` module with email sending capability (SMTP or SendGrid API).

**Notification triggers:**

| Trigger | Audience | Content |
|---|---|---|
| New quarterly report ready | User | PDF/Excel attachment link, income summary |
| UNKNOWN transactions detected | User | Count, links to Transaction Browser |
| Large transaction (configurable threshold) | User | Amount, description, classification |
| Invoice upload success/failure | User | File name, API response |
| Monthly income below threshold | User | Alert with YoY comparison |

**Implementation:**
- `src/notifier.py` — `send_email(subject, body, attachments=None)` using `smtplib` + `email` stdlib
- Optional SendGrid integration for reliability
- Notification preferences in Configuration tab (toggle per event type, set thresholds)
- Notification log in SQLite (`notifications` table: event_type, sent_at, subject, success)

**Educational value:** Teaches email protocols (SMTP), attachment handling, event-driven side effects, and configurable alerting systems.

---

### Feature 5: VAT / Tax Compliance Reporter

**Rationale:** Spanish freelancers (autónomos) must file quarterly VAT (IVA) returns (Modelo 303) and annual income summaries. This system already has the data needed — it just needs to compute the right figures.

**What to build:**

Add a `src/tax_calculator.py` module and a "Tax Reports" tab.

**VAT rules to implement:**
- **Spain sales:** 21% IVA collected (or 0% if B2B with valid NIF/CIF)
- **EU sales (B2C digital services):** OSS (One Stop Shop) — VAT rate of buyer's country
- **Outside-EU sales:** 0% VAT (export)
- **Spain purchases (expenses):** 21% IVA deductible (from Feature 1)

**Modelo 303 output (quarterly):**
```
Box 01: Taxable base (Spain sales)
Box 03: IVA collected (Spain, 21%)
Box 28: IVA deductible (Spain purchases)
Box 46: Net IVA to pay/refund
```

**UI Tab: "Tax Reports"**
- Quarter selector
- Breakdown table: transactions by VAT treatment, base amount, VAT amount
- Modelo 303 summary box with copy-to-clipboard values
- Warning panel for transactions with ambiguous VAT treatment (require manual tagging)
- Export to CSV for accountant review

**New data fields needed:**
- Add `vat_treatment` to transactions: `SPAIN_21`, `OSS`, `EXPORT`, `EXEMPT`, `UNKNOWN`
- Add `vat_amount_eur` computed field
- Add `buyer_vat_id` to expenses for deductibility

**Educational value:** Teaches domain-specific business logic, regulatory compliance implementation, and the importance of separating computation from presentation.

---

### Feature 6: REST API Layer

**Rationale:** The system currently only has a Streamlit UI. Exposing a REST API allows integration with external tools (mobile apps, other systems, automation scripts), and is a critical architectural skill.

**What to build:**

Add a `api/` directory with a **FastAPI** application (`api/main.py`).

**Endpoints:**

```
GET  /api/health                          -- service health check
GET  /api/transactions?year=&quarter=&activity=&geo=  -- list transactions
GET  /api/transactions/{id}               -- single transaction detail
POST /api/transactions/sync               -- trigger Stripe sync
GET  /api/reports/quarterly?year=&quarter= -- quarterly aggregation JSON
GET  /api/reports/quarterly/export        -- download Excel file
GET  /api/fx-rates?currency=&from=&to=   -- FX rates for date range
POST /api/fx-rates/refresh                -- fetch latest ECB rates
GET  /api/rules                           -- current classification rules
PUT  /api/rules                           -- update classification rules
POST /api/rules/test                      -- test a rule against sample data
GET  /api/stats/summary                   -- dashboard summary stats
```

**Authentication:** API key header (`X-API-Key`) — key stored in `.env`

**Running both simultaneously:**
```bash
# Run Streamlit on port 8501, FastAPI on port 8000
streamlit run app/streamlit_app.py &
uvicorn api.main:app --port 8000
```

**Both apps share the same `src/` business logic** — no code duplication.

**Educational value:** Teaches API design (REST principles, HTTP verbs, status codes), the separation of UI from business logic, authentication patterns, and how the same core logic can serve multiple clients.

---

### Feature 7: Customer Analytics & Revenue Insights

**Rationale:** Payments include customer email and Stripe customer IDs. Mining this data reveals customer lifetime value, churn, and which activity drives the most recurring revenue — valuable for business decisions.

**What to build:**

Add a `src/customer_analytics.py` module and a "Customers" tab in the dashboard.

**Data model:**
```
customer_view (SQLite view, not a table):
  email_meta
  stripe_customer_id
  first_seen_date
  last_seen_date
  total_payments
  total_revenue_eur
  primary_activity  -- activity generating most revenue for this customer
  geo_region
  is_recurring      -- has > 1 payment
  months_active     -- count of distinct months with payments
```

**Analytics to display:**

1. **Customer List** — sortable by total revenue, last payment, payment count; searchable by email
2. **Retention Chart** — cohort analysis: customers acquired per quarter, % still paying 1/2/3 quarters later
3. **Revenue Concentration** — pie/bar: top 10 customers as % of total revenue (business risk indicator)
4. **Activity Cross-sell** — how many customers pay for multiple activity types
5. **New vs. Returning Revenue** — monthly bar chart splitting new customer revenue from returning

**Export:** CSV export of customer list for CRM import

**Educational value:** Introduces SQL views, cohort analysis, business intelligence concepts, and the difference between transactional data and analytical data.

---

### Feature 8: Data Audit Trail & Change History

**Rationale:** Classification overrides and rule changes are currently not tracked — there is no record of who changed what and why. For accounting purposes, an audit trail is both good practice and (in Spain) potentially legally required.

**What to build:**

Add an `audit_log` table and a `src/audit.py` module.

```
audit_log table:
  id (INTEGER PRIMARY KEY AUTOINCREMENT)
  timestamp (TEXT, ISO format)
  entity_type (TEXT)  -- 'transaction', 'rule', 'config', 'expense'
  entity_id (TEXT)
  action (TEXT)       -- 'classify', 'reclassify', 'rule_added', 'rule_edited', 'rule_deleted', 'sync'
  field_changed (TEXT, nullable)
  old_value (TEXT, nullable)
  new_value (TEXT, nullable)
  source (TEXT)       -- 'user', 'ai_classifier', 'rules_engine', 'stripe_sync'
  notes (TEXT, nullable)
```

**Integration points:**
- Every call to `upsert_classified()` that changes an existing classification writes an audit entry
- Every save to `classification_rules.json` writes audit entries for added/changed/deleted rules
- Every AI classification acceptance writes an audit entry

**UI additions:**
- Audit Log viewer in the Configuration tab (filterable by entity, action, date range)
- Transaction detail modal showing the full classification history for that transaction

**Educational value:** Teaches append-only logging patterns, the importance of immutability in financial systems, and how to retrofit audit capability onto an existing system without breaking it.

---

### Feature 9: Multi-Source Income Aggregation

**Rationale:** Income may not come exclusively from Stripe. A freelancer might also receive bank transfers, PayPal payments, or Gumroad sales. The system should be the single source of truth for all income.

**What to build:**

Extend the `transactions` table with a `source_platform` field and add importers for other platforms.

**New importers (`src/importers/`):**

1. **`csv_importer.py`** — generic CSV import with column mapping UI
   - User maps CSV columns to internal fields: date, amount, currency, description, email
   - Saved column mappings per file format for reuse
   - Deduplication by (date + amount + description) hash

2. **`gumroad_importer.py`** — Gumroad Sales CSV (common for digital creators)
   - Parses Gumroad export format
   - Maps product names to activity types via configurable rules

3. **`paypal_importer.py`** — PayPal Activity CSV
   - Handles PayPal's multi-currency format
   - Filters for completed payments only (excludes refunds already counted)

**UI additions:**
- "Import" section in the Transaction Browser tab
- Drag-and-drop CSV upload
- Preview table before confirming import
- Import log showing history of imports and their transaction counts

**Educational value:** Teaches file parsing, data normalization across sources, deduplication strategies, and building extensible importer architectures with a common interface.

---

### Feature 10: Forecasting & Budget Planning

**Rationale:** With 2+ years of historical data already in the system, basic statistical forecasting is feasible and highly practical for a freelancer estimating annual income and tax liability.

**What to build:**

Add a `src/forecaster.py` module and a "Forecast" tab.

**Forecasting methods (implement all three for comparison):**

1. **Trailing 3-month average** — simple, easy to explain
2. **Same-quarter last year** — seasonal adjustment
3. **Linear trend** — numpy polyfit on monthly revenue, project forward

**Output:**

```
Forecast for Q2 2026:
  Method              Coaching    Newsletter  Illustrations  Total
  3-month average     €2,100      €850        €400           €3,350
  Same quarter 2025   €2,400      €780        €350           €3,530
  Linear trend        €2,250      €910        €420           €3,580
  
  Estimated annual income (Q2-Q4 remaining + Q1 actual): €14,200 - €15,800
  Estimated quarterly tax provision (20% IRPF): €710 - €790
```

**UI Tab: "Forecast"**
- Year selector (forecast remaining quarters of selected year)
- Method selector (or show all three side by side)
- Actual vs. forecast chart (Plotly) — historical actuals + projected future bars
- Tax provision calculator (apply user-configured IRPF % to forecast income)
- "What-if" slider: adjust forecast by ±% to model optimistic/pessimistic scenarios

**Educational value:** Introduces time-series concepts, statistical methods, uncertainty communication, and the practical combination of data visualization with actionable business outputs.

---

## Implementation Priority Matrix

| # | Feature | Practical Value | Educational Value | Complexity | Suggested Order |
|---|---|---|---|---|---|
| 1 | Expense Tracking | High | Medium | Low | **1st** |
| 8 | Audit Trail | High | High | Low | **2nd** |
| 3 | Scheduled Sync | High | Medium | Medium | **3rd** |
| 5 | VAT/Tax Reporter | High | High | Medium | **4th** |
| 2 | AI Classification | Medium | Very High | Medium | **5th** |
| 6 | REST API Layer | Medium | Very High | Medium | **6th** |
| 4 | Email Alerts | Medium | Medium | Low | **7th** |
| 7 | Customer Analytics | Medium | High | Medium | **8th** |
| 10 | Forecasting | Medium | High | Medium | **9th** |
| 9 | Multi-Source Import | Low | High | High | **10th** |

---

## How to Use This Document as an LLM Prompt

Each feature section above is designed to be self-contained. To implement a feature, provide an LLM with:

1. **The "System Overview (Context for LLM)" section** at the top of this document
2. **The specific feature section** you want to implement
3. **The relevant existing source files** (read and paste their contents)

### Suggested prompt template:

```
You are implementing a new feature for an existing Python/Streamlit accounting automation system.

## Existing System Context
[paste System Overview section]

## Feature to Implement
[paste the chosen Feature section]

## Relevant Existing Files
[paste contents of files the feature will modify or extend]

## Instructions
- Follow the existing code style and patterns
- Reuse existing modules (src/fx_rates.py, src/database.py, etc.) — do not duplicate logic
- Add tests in tests/ following the existing pytest pattern (see tests/conftest.py for fixtures)
- Keep changes minimal and focused — do not refactor unrelated code
- Update requirements.txt if new dependencies are added
- Ensure the Streamlit app still runs after changes (no breaking imports)
```

---

*Generated by Claude Code on 2026-04-02*
