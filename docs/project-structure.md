# Project Structure

```
├── config.json                    # App settings (git-ignored, use config.json.example)
├── classification_rules.json      # Classification rules (git-ignored, copy from .example)
├── classification_rules.json.example
├── vendors.json                   # Vendor registry (git-ignored, copy from .example — see [vendor-registry.md](vendor-registry.md))
├── vendors.json.example
├── divergences.json               # Divergence catalogue (git-ignored, copy from .example — see [reconciliation.md](reconciliation.md))
├── divergences.json.example
├── gestor_notes.md                # Free-text notes for the accountant's pack (git-ignored, optional)
├── config.json.example
├── requirements.txt
├── launch_app.bat                 # Windows launch shortcut
├── scripts/
│   ├── close_quarter.py           # Quarter-close CLI over src/close_pipeline.py (see [close-quarter-cli.md](close-quarter-cli.md))
│   └── clean_invoice_activity_type.py  # One-off: NULL the non-activity values old rows hold in invoices.activity_type (dry run by default, --apply writes)
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
│   ├── invoice_dedupe.py          # Duplicate/receipt/out-of-period detection + exclusion
│   ├── periods.py                 # Quarter start/end bounds (date, ISO string, datetime) shared by the engine, dedupe, close and UI
│   ├── tax_models.py              # Dataclasses for Modelo303, Modelo130, OSS, 347, 349 results + AuditEntry
│   ├── tax_codes.py               # EU country / VAT prefixes, Spanish NIF → geography, tax_treatment values and legacy mapping
│   ├── tax_data.py                # Shared by every tax model: config plumbing, record loaders, activity-start floor, per-row VAT figures
│   ├── vat_rules.py               # Single source of truth: activity×geo VAT matrix, OSS rates, base extraction
│   ├── modelo_303.py              # Modelo 303 (AEAT boxes, pro-rata, credit chain)
│   ├── modelo_130.py              # Modelo 130 (AEAT boxes, box 13 reduction, negative carry)
│   ├── modelo_349.py              # Modelo 349 (intra-EU operations, per operator and key)
│   ├── oss_return.py              # OSS quarterly return and the EU B2C distance-selling threshold watch
│   ├── tax_engine.py              # Tax calendar and snapshot persistence; re-exports each model's compute_* function
│   ├── tax_snapshot_codec.py      # Serialize/deserialize tax engine results for SQLite snapshot storage
│   ├── tax_validator.py           # Filed-return loader: imported AEAT receipts first, validation.yaml fallback
│   ├── reconciliation.py          # Box-by-box filed-vs-app reconciliation, divergence catalogue, markdown export
│   ├── modelo_390.py              # Modelo 390 engine from the four 303 results (aeat_boxes, pro-rata, volume)
│   ├── modelo_347.py              # Modelo 347: sales side (Stripe + issued invoices) and purchases side (Spanish vendors > €3,005.06, exclusions)
│   ├── pl_by_activity.py          # P&L per IAE activity (826/861/751) tied to the Q4 Modelo 130
│   ├── annual_pack.py             # Annual pack: 390 + 347 + P&L, markdown/CSV export + CLI
│   ├── filed_returns.py           # Import filed AEAT receipt PDFs (303/130/349/390) as reference data + CLI
│   ├── fixed_assets.py            # Fixed assets: simplified-table depreciation, VAT capital goods (303 30/31), regularisation
│   ├── accounting_api_client.py   # IntegraLOOP/BILOOP Accounting API client
│   ├── invoice_ocr.py             # PDF extraction for Spanish accounting (local-llm-hub only)
│   ├── invoice_ingest.py          # OCR → FX → vendor registry → DB save path (OCR tab + close_quarter.py ocr)
│   ├── close_pipeline.py          # Quarter-close pipeline steps (sweep … gestor pack), each idempotent
│   ├── relink.py                  # Re-point invoice records after PDFs move/rename (move record, then content hash)
│   ├── filing_sheet.py            # Filing sheet (AEAT form order, credit chain, deadlines) + immutable "mark filed"
│   ├── tax_deadlines.py           # Filing / direct-debit deadlines + business-day rules, shared by the filing sheet and the Obligations calendar
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
│   ├── invoice_ocr_tab.py         # AI invoice extraction tab (OCR via local-llm-hub)
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
│   ├── test_tax_deadlines.py      # Deadline rules, Easter, extra holidays, calendar vs filing sheet agreement
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
│   ├── test_filing_sheet.py       # Filing sheet boxes, filed-version freeze, triggers, migration
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
