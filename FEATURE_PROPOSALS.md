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

### Feature 11: Spanish Autónomo — Full Tax Obligations Suite

**Rationale:** A Spain-based freelancer (autónomo) has a fixed calendar of mandatory tax filings with the Agencia Tributaria. All the raw data needed to compute these obligations is already in the system (income by geography, activity, VAT treatment, FX-converted amounts). This feature turns the system into a tax preparation assistant — it does not replace an accountant but eliminates the data-gathering step and pre-fills every box that can be computed automatically.

---

#### 11.1 Spanish Tax Obligations Overview

The following filings apply to an autónomo in **régimen de estimación directa simplificada** (the standard regime for freelancers with < €600,000 annual revenue):

| Model | Name | Frequency | Deadline | What it covers |
|---|---|---|---|---|
| **Modelo 303** | Declaración IVA trimestral | Quarterly | Apr 20 / Jul 20 / Oct 20 / Jan 30 | VAT collected vs. VAT paid — net to pay or refund |
| **Modelo 390** | Resumen anual IVA | Annual | 30 Jan (following year) | Annual summary of all four Modelo 303s |
| **Modelo 130** | Pago fraccionado IRPF (ED) | Quarterly | Same as 303 | 20% advance income tax on quarterly net profit |
| **Modelo 100** | Declaración de la Renta (IRPF) | Annual | May–Jun (following year) | Full annual income tax return |
| **Modelo 347** | Operaciones con terceros | Annual | Feb (following year) | Operations > €3,005.06 with same party |
| **Modelo 349** | Operaciones intracomunitarias | Quarterly/Monthly | 20th of month after quarter | Intra-EU B2B supply of services |
| **OSS** | One Stop Shop (IVA digital services) | Quarterly | 31st of month after quarter | B2C digital services to EU non-Spain customers |

> **Not in scope (no data in system):** Modelo 190 (retention summary for employees), Modelo 111 (monthly retentions). Social Security cuota is a payment, not a filing — not modelled.

---

#### 11.2 VAT (IVA) Rules by Activity and Geography

Each income transaction must be assigned a **VAT treatment** before any model can be computed:

| Activity | Geography | VAT Treatment | IVA Rate | Notes |
|---|---|---|---|---|
| COACHING | SPAIN | `IVA_ES_21` | 21% | Standard professional services |
| COACHING | EU_NOT_SPAIN | `IVA_EU_B2B` | 0% (reverse charge) | Requires buyer NIF-IVA; seller states "inversión del sujeto pasivo" |
| COACHING | EU_NOT_SPAIN (B2C) | `IVA_EU_B2C` | Buyer country rate | Rare for coaching; typically B2B |
| COACHING | OUTSIDE_EU | `IVA_EXPORT` | 0% | Export of services — exempt |
| NEWSLETTER | SPAIN | `IVA_ES_21` | 21% | Digital service, Spain B2C |
| NEWSLETTER | EU_NOT_SPAIN | `OSS_EU` | Buyer country rate (see table below) | Digital service B2C — OSS registration required |
| NEWSLETTER | OUTSIDE_EU | `IVA_EXPORT` | 0% | Export |
| ILLUSTRATIONS | SPAIN | `IVA_ES_21` | 21% | Artistic/professional services |
| ILLUSTRATIONS | EU_NOT_SPAIN | `IVA_EU_B2B` | 0% | Typically B2B commissions |
| ILLUSTRATIONS | OUTSIDE_EU | `IVA_EXPORT` | 0% | Export |

**EU VAT rates for OSS digital services (major countries):**
```python
OSS_RATES = {
    "DE": 0.19, "FR": 0.20, "IT": 0.22, "NL": 0.21,
    "BE": 0.21, "PT": 0.23, "AT": 0.20, "PL": 0.23,
    "SE": 0.25, "DK": 0.25, "FI": 0.24, "IE": 0.23,
    "DEFAULT_EU": 0.21  # fallback if country unknown
}
```

> The system already stores `card_country` on each transaction — this is the basis for OSS country assignment.

---

#### 11.3 IRPF Retention Rules

When issuing invoices to **Spanish clients (empresas or autónomos)**, the payer deducts **15% IRPF** from the invoice (7% in the first 3 years of activity). This retention is tracked on invoices but does NOT affect income recognition in this system — it affects Modelo 130 calculation.

- If the autónomo has retenciones soportadas (Spanish clients deducted IRPF), Modelo 130 box 16 is reduced.
- The system should allow the user to input total retenciones received per quarter (manual entry, since Stripe doesn't handle IRPF).

---

#### 11.4 Data Model Additions

```python
# New field on ClassifiedPayment / transactions table
vat_treatment: str  
# Values: IVA_ES_21 | IVA_EU_B2B | IVA_EU_B2C | OSS_EU | IVA_EXPORT | EXEMPT | UNKNOWN

vat_base_eur: float     # taxable base (= net_amount for income, already EUR)
vat_amount_eur: float   # IVA collected (vat_base_eur × applicable rate)
oss_country: str        # ISO-2 country code, populated if vat_treatment == OSS_EU
buyer_vat_id: str       # NIF-IVA / VAT ID of buyer (manual entry for B2B EU)

# New table: quarterly_tax_entries (manual inputs that can't be computed from Stripe data)
CREATE TABLE quarterly_tax_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    year        INTEGER,
    quarter     INTEGER,
    entry_type  TEXT,   -- RETENCIONES_SOPORTADAS | GASTOS_DEDUCIBLES | IVA_SOPORTADO | OTHER
    amount_eur  REAL,
    description TEXT,
    notes       TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

# New table: tax_filing_status
CREATE TABLE tax_filing_status (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    year         INTEGER,
    quarter      INTEGER,  -- NULL for annual filings
    model        TEXT,     -- '303' | '390' | '130' | '100' | '347' | '349' | 'OSS'
    status       TEXT,     -- PENDING | COMPUTED | REVIEWED | FILED
    filed_at     TEXT,
    amount_eur   REAL,     -- final amount paid or refunded (negative = refund)
    notes        TEXT,
    created_at   TEXT
);
```

---

#### 11.5 New Module: `src/tax_engine.py`

```python
# Public interface — all functions return a dataclass or dict ready for UI display

def compute_vat_treatment(payment: ClassifiedPayment, config: dict) -> VATTreatment:
    """Assign vat_treatment, vat_base_eur, vat_amount_eur, oss_country to a payment."""

def compute_modelo_303(year: int, quarter: int, db_conn) -> Modelo303Result:
    """
    Returns all boxes for Modelo 303 quarterly VAT return.
    
    Key boxes computed:
      Box 01: Base imponible operaciones interiores (IVA_ES_21 income base)
      Box 03: Cuota devengada (IVA_ES_21 income × 21%)
      Box 10: Base intracomunitarias (IVA_EU_B2B base — informative)
      Box 28: Cuota IVA soportado deducible (from quarterly_tax_entries)
      Box 46: Resultado (Box 03 - Box 28)
      Box 47: % atribuible a actividad (100% default)
      Box 48: Net result to pay or compensate
    """

def compute_modelo_130(year: int, quarter: int, db_conn) -> Modelo130Result:
    """
    Returns all boxes for Modelo 130 quarterly IRPF fractional payment.
    
    Key boxes computed:
      Box 01: Ingresos computables (total net income for the year-to-date)
      Box 02: Gastos deducibles YTD (from quarterly_tax_entries, cumulative)
      Box 03: Rendimiento neto (Box 01 - Box 02)
      Box 05: 20% × Box 03
      Box 07: Retenciones soportadas YTD (manual entry, cumulative)
      Box 14: Previous quarters' Modelo 130 payments (cumulative)
      Box 16: Result to pay (max(0, Box 05 - Box 07 - Box 14))
    """

def compute_modelo_349(year: int, quarter: int, db_conn) -> Modelo349Result:
    """
    Returns rows for Modelo 349 (intra-EU operations).
    One row per EU client (identified by buyer_vat_id) with total operations amount.
    Only applies to IVA_EU_B2B transactions.
    """

def compute_oss_return(year: int, quarter: int, db_conn) -> OSSReturnResult:
    """
    Returns OSS quarterly return: one row per EU member state.
    Groups OSS_EU transactions by oss_country, sums base and VAT at each country's rate.
    """

def compute_modelo_347(year: int, db_conn) -> Modelo347Result:
    """
    Annual: group all operations by counterparty (email/name).
    Flag any counterparty where total operations > €3,005.06.
    Returns list of reportable counterparties with amounts.
    """

def get_tax_calendar(year: int) -> list[TaxDeadline]:
    """Return all filing deadlines for the year with status (PENDING/DUE/OVERDUE/FILED)."""
```

---

#### 11.6 UI Tab: "Tax Obligations"

The tab has four sub-sections:

**A. Tax Calendar**

A visual timeline showing all deadlines for the selected year. Each item shows:
- Model number and name
- Deadline date
- Status badge: `PENDING` / `DUE` (within 15 days) / `OVERDUE` / `FILED`
- Computed amount (or `—` if not yet computed)
- "Compute" / "Mark as Filed" action buttons

Color coding: green = filed, amber = due soon, red = overdue, grey = pending.

---

**B. Modelo 303 — IVA Trimestral**

Quarter selector → triggers `compute_modelo_303()`.

Display as a form mirroring the official Agencia Tributaria layout:

```
DEVENGADO (IVA collected)
  Box 01  Base imponible al 21%          €  [auto-computed]
  Box 03  Cuota (21% × Box 01)           €  [auto-computed]
  Box 10  Entregas intracom. exentas     €  [auto-computed, informative]

DEDUCIBLE (IVA paid on expenses)  
  Box 28  Cuota IVA soportado            €  [from quarterly_tax_entries, editable]
  Box 29  Base correspondiente           €  [editable]

RESULTADO
  Box 46  Diferencia (Box 03 - Box 28)   €  [auto-computed]
  Box 48  Resultado a ingresar/devolver  €  [auto-computed, highlighted]
```

Manual override: any auto-computed box can be manually overridden with an explanation note (stored in `tax_filing_status`).

"Export Modelo 303 Summary" → generates a PDF or structured TXT file with all boxes for accountant review.

---

**C. Modelo 130 — IRPF Trimestral**

Quarter selector → triggers `compute_modelo_130()`.

Key input required from user (cannot be computed from Stripe data):
- Total deductible expenses YTD (or import from Expense Tracking if Feature 1 is implemented)
- Total retenciones soportadas YTD (IRPF deducted by Spanish clients on invoices issued)
- Previous quarters' Modelo 130 amounts paid

```
INGRESOS Y GASTOS (year-to-date, cumulative)
  Box 01  Ingresos del periodo            €  [auto-computed from system]
  Box 02  Gastos deducibles               €  [from expenses / manual entry]
  Box 03  Rendimiento neto (01 - 02)      €  [auto-computed]

CÁLCULO
  Box 05  20% de Box 03                   €  [auto-computed]
  Box 07  Retenciones soportadas YTD      €  [manual entry]
  Box 14  Pagos fraccionados anteriores   €  [auto-filled from previous quarters]
  Box 16  Resultado a ingresar            €  [auto-computed, min 0]
```

---

**D. Manual Entries & Adjustments**

A form to add entries to `quarterly_tax_entries` for items that cannot be derived from Stripe:

| Entry type | Description | Example |
|---|---|---|
| `IVA_SOPORTADO` | VAT paid on deductible purchases | Laptop purchase, software subscriptions |
| `GASTOS_DEDUCIBLES` | IRPF-deductible expenses (no IVA, or IVA non-deductible) | Home office % of rent, phone bill |
| `RETENCIONES_SOPORTADAS` | IRPF withheld by Spanish clients from invoices | Client X withheld €150 in Q1 |

Table showing all manual entries for the selected quarter, with edit/delete capability.

---

**E. OSS Return (if applicable)**

Only shown if there are `OSS_EU` transactions in the selected quarter.

```
OSS QUARTERLY RETURN — Q[n] [year]

Country   Transactions  Base (€)   VAT Rate  VAT Amount (€)
-------   ------------  --------   --------  --------------
DE        12            €1,240     19%       €235.60
FR         8            €820       20%       €164.00
IT         3            €310       22%       €68.20
...
TOTAL      23            €2,370               €467.80

Filing due: [date]
```

"Export OSS Return" → CSV in the format accepted by the AEAT OSS portal.

---

**F. Modelo 347 (Annual)**

Year selector → shows table of counterparties with operations > €3,005.06.

```
COUNTERPARTY            VAT ID     TOTAL OPERATIONS   QUARTER BREAKDOWN
Client A SL             B12345678  €5,200             Q1: €1,200 Q2: €2,000 Q3: €2,000
Supplier B SL           A87654321  €3,500             Q1: €1,500 Q4: €2,000
...
```

Note: only applies to Spain-based counterparties with a NIF/CIF. EU and non-EU clients appear on Modelo 349 instead.

---

#### 11.7 Filing Deadline Reference (hard-coded constants)

```python
TAX_DEADLINES = {
    "303": {
        1: "April 20",
        2: "July 20",
        3: "October 20",
        4: "January 30 (next year)"
    },
    "130": {  # same as 303
        1: "April 20",
        2: "July 20",
        3: "October 20",
        4: "January 30 (next year)"
    },
    "390": "January 30 (following year)",  # annual
    "347": "February 28 (following year)",  # annual
    "349": {  # quarterly if > €50k/quarter, otherwise monthly option
        1: "April 20",
        2: "July 20",
        3: "October 20",
        4: "January 20 (next year)"
    },
    "OSS": {  # EU deadline — 31st of month after quarter end
        1: "April 30",
        2: "July 31",
        3: "October 31",
        4: "January 31 (next year)"
    }
}
```

---

#### 11.8 Important Caveats to Surface in the UI

The UI must display these prominently, not hide them in fine print:

> **This tool pre-fills tax data for review purposes only. It does not constitute tax advice. Always review outputs with a qualified gestor or asesor fiscal before filing. Regulatory changes (IVA rates, IRPF thresholds, OSS rules) are not automatically tracked — verify current rules with the Agencia Tributaria each filing period.**

Specific known limitations to flag:
- `IVA_EU_B2B` treatment requires a valid NIF-IVA in the VIES database — the system cannot verify this automatically; flag all EU B2B transactions as "requires NIF-IVA confirmation"
- OSS country assignment uses `card_country` as a proxy — this may differ from the buyer's country of residence (the legally relevant field for B2C digital services)
- Modelo 347 requires the seller's NIF/CIF, which is not stored in Stripe — must be entered manually
- The system does not model the **Recargo de Equivalencia** regime (applies to retail; not relevant for these activities)
- If the autónomo is in their **first 3 years of activity**, the IRPF retention rate on invoices is 7% instead of 15% — this is a user-configurable setting

---

#### 11.9 New Configuration Fields

```json
{
  "tax": {
    "regime": "estimacion_directa_simplificada",
    "vat_registered": true,
    "oss_registered": true,
    "oss_registration_country": "ES",
    "irpf_retention_rate": 0.15,
    "activity_start_date": "2022-01-01",
    "nif": "XXXXXXXX",
    "fiscal_year_start_month": 1,
    "vat_proration_percentage": 100,
    "default_vat_treatment_eu_coaching": "IVA_EU_B2B",
    "default_vat_treatment_eu_newsletter": "OSS_EU"
  }
}
```

---

#### 11.10 Implementation Notes for LLM

**Files to create:**
- `src/tax_engine.py` — all computation logic
- `src/tax_models.py` — Pydantic models for Modelo303Result, Modelo130Result, etc.
- `app/tax_obligations.py` — Streamlit tab
- `tests/test_tax_engine.py` — unit tests for each computation function

**Files to modify:**
- `src/database.py` — add `quarterly_tax_entries` and `tax_filing_status` tables; add `vat_treatment`, `vat_base_eur`, `vat_amount_eur`, `oss_country`, `buyer_vat_id` columns to `transactions`
- `src/models.py` — extend `ClassifiedPayment` with VAT fields
- `src/classifier.py` — add `classify_vat()` function called after `classify_geography()`
- `app/streamlit_app.py` — add "Tax Obligations" tab (8th tab)
- `config.json.example` — add `tax` section

**Key test cases for `test_tax_engine.py`:**
```python
# Modelo 303
- Zero IVA quarter (all OUTSIDE_EU) → Box 48 = 0
- Spain-only income → Box 03 = 21% of total net
- Mixed Spain + EU + Outside → correct allocation per treatment
- IVA soportado > devengado → negative Box 48 (refund scenario)

# Modelo 130
- First quarter of year → no prior payments in Box 14
- Q2+ → prior payments correctly accumulated in Box 14
- Retenciones > 20% net → Box 16 = 0 (floor at zero)
- High expenses → rendimiento neto negative → Box 16 = 0

# VAT treatment classification
- NEWSLETTER + EU_NOT_SPAIN → OSS_EU
- COACHING + EU_NOT_SPAIN → IVA_EU_B2B
- Any + OUTSIDE_EU → IVA_EXPORT
- Any + SPAIN → IVA_ES_21
```

---

*Generated by Claude Code on 2026-04-02*
