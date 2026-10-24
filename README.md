# Stripe Accounting Quarterly Automation

Automated Stripe payment classification and quarterly reporting system. Classifies payments by activity type (Coaching, Newsletter, Illustrations) and geographic region (Spain, EU-not-Spain, Outside-EU), then produces Excel reports, Spanish tax obligation snapshots, a box-by-box **Reconciliation** of filed AEAT returns against the app's figures, and a Streamlit dashboard.

> **Disclaimer:** This tool pre-fills tax data for review purposes only. It does not constitute tax advice. Always review outputs with a qualified gestor or asesor fiscal before filing.

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
cp vendors.json.example vendors.json  # vendor registry (optional; see docs/vendor-registry.md)
cp divergences.json.example divergences.json  # divergence catalogue (optional; see docs/reconciliation.md)
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
| `divergences.json`, `gestor_notes.md`, `data/accounting.db` | Nothing — read on use (click **↺ Refresh** where a tab caches for 5 minutes, see [Performance & Caching](docs/performance-caching.md)) |
| Anything, for the CLI | Nothing — each `close_quarter.py` run is a new process that re-reads code and config |

### Verification gate

```powershell
# Windows: byte-compile, docs ratchet, structure ratchet, then the full pytest suite
.\scripts\verify-before-ship.ps1
```

```bash
# macOS / Linux (tests only)
.venv/bin/pytest -v
```

The docs ratchet keeps `README.md` under 40 KiB and fails on a new history marker (an issue reference, a dated heading or a changelog heading) in `README.md` or `docs/**/*.md`; existing ones are baselined in `.docs-ratchet-baseline.json`. State the present-tense why in words instead. A check that could not run (exit 3) fails as not confirmed.

The structure ratchet fails when a new file passes 1,000 lines, a new function 100 lines or a directory 40 modules, when `src/` imports `app/`, when a baselined offender grows past its size in `.structure-ratchet-baseline.json`, or when a root folder falls outside the declared shape. Split the offender rather than raising its baseline; exit 3 also fails as not confirmed.

The instruction ratchet lints the instruction files (`CLAUDE.md`, `AGENTS.md`, any rules files, every `SKILL.md` and the docs a skill links to) with the fleet's prompt-audit rules and fails when a rule's hit count rises above `.instruction-ratchet-baseline.json` or a file passes its size cap. Reword the offending line rather than raising the baseline; exit 3 also fails as not confirmed.

The doc-refs ratchet fails when `README.md`, `CLAUDE.md` or a file under `docs/` names a repo path, `python -m` module, relative link or loopback port that does not exist, beyond `.doc-refs-ratchet-baseline.json`. Fix the reference rather than baselining it; a path named on purpose, such as a folder created at run time, stays in the baseline, and exit 3 fails as not confirmed.

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

Transaction data is fetched from the Stripe API and stored in the local SQLite database (`data/accounting.db`). On subsequent loads the dashboard reads pre-classified data directly from the database — the classifier only runs when new data is fetched from the API, or when you run `scripts/close_quarter.py reclassify` after a rule change. Non-EUR amounts are converted to EUR using ECB exchange rates, fetched from the Frankfurter API when a date's rate is missing.

Layout: `src/` holds the business logic (no Streamlit), `app/` the Streamlit tabs, `scripts/close_quarter.py` the quarter-close CLI, `tests/` the pytest suite, and `data/`, `tmp/` and `logs/` the git-ignored runtime output. The module-by-module tree is [`docs/project-structure.md`](docs/project-structure.md); the structure diagram is [`docs/architecture.mmd`](docs/architecture.mmd). Where new code goes is in [`docs/layout.md`](docs/layout.md).

---

## Documentation

Each feature has one reference file under `docs/`.

**Setup and data**
- [`docs/project-structure.md`](docs/project-structure.md) — every file and module, what it does and which git-ignored files hold local data.
- [`docs/architecture.mmd`](docs/architecture.mmd) — the repo's internal structure (Mermaid).
- [`docs/database.md`](docs/database.md) — the SQLite tables and what writes each.
- [`docs/classification.md`](docs/classification.md) — activity and geographic rules, customer VAT-id overrides, the Stripe API key and permissions.
- [`docs/currency-conversion.md`](docs/currency-conversion.md) — ECB rates, fallback and staleness, auto-backfill.

**Quarter close**
- [`docs/quarter-close-runbook.md`](docs/quarter-close-runbook.md) — the quarterly close step by step: pre-flight checklist, the `close_quarter.py` pipeline, filing the 303 / 130 / 349 on the AEAT Sede, payment, **Mark filed**, receipt import, archive, and the annual calendar.
- [`docs/close-quarter-cli.md`](docs/close-quarter-cli.md) — every `close_quarter.py` subcommand and flag, and relinking after the invoice archive is moved.

**Tax engine**
- [`docs/tax-conventions.md`](docs/tax-conventions.md) — every tax rule the engine applies, with its legal basis (LIVA / LIRPF / RIRPF articles, AEAT orders), the config key that controls it, and where an external accountant may do it differently.
- [`docs/tax-obligations.md`](docs/tax-obligations.md) — the Tax Obligations tab: supported models, VAT treatment classification, VAT-inclusive pricing, tax configuration, how invoices feed each model.
- [`docs/tax-engine-reference.md`](docs/tax-engine-reference.md) — Modelo 303 / 130 / 349 / 390 box models, audit cell names and ledger columns.
- [`docs/annual-pack.md`](docs/annual-pack.md) — Modelo 390, Modelo 347 purchases, P&L per IAE activity.
- [`docs/reconciliation.md`](docs/reconciliation.md) — filed-vs-app comparison per box, divergence catalogue, importing AEAT receipts.
- [`docs/filing-sheet.md`](docs/filing-sheet.md) — values to type into each AEAT form, deadlines, immutable **Mark filed** snapshots.
- [`docs/tax-audit-trail.md`](docs/tax-audit-trail.md) — the per-cell formula, inputs and records behind every figure.
- [`docs/social-security.md`](docs/social-security.md) — importing RETA cuotas from a bank export into Modelo 130.

**Invoices**
- [`docs/invoice-ocr.md`](docs/invoice-ocr.md) — AI extraction through local-llm-hub, extracted fields, EUR resolution for foreign-currency invoices.
- [`docs/invoice-ledger.md`](docs/invoice-ledger.md) — review and correction with field locks, exchange-rate differences, the Invoice Explorer.
- [`docs/duplicate-review.md`](docs/duplicate-review.md) — the five duplicate / receipt / out-of-period detectors and how exclusions apply.
- [`docs/vendor-registry.md`](docs/vendor-registry.md) — `vendors.json`: matching, defaults, applying, spreadsheet import.
- [`docs/fixed-assets.md`](docs/fixed-assets.md) — depreciation, capital-goods VAT and the regularisation register.
- [`docs/invoice-upload.md`](docs/invoice-upload.md) — uploading invoice PDFs to the accounting partner API (IntegraLOOP/BILOOP).

**Runtime**
- [`docs/performance-caching.md`](docs/performance-caching.md) — which Streamlit loaders are cached and when to refresh.
