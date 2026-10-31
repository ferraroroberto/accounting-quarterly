# Annual Pack (Modelo 390, Modelo 347, P&L per activity)

**Tax Obligations → Annual Pack** (or the CLI) computes the three annual outputs live from the database — nothing is persisted — with markdown and CSV downloads:

```bash
.venv/Scripts/python.exe -m src.annual_pack --year 2026                      # markdown to stdout
.venv/Scripts/python.exe -m src.annual_pack --year 2026 --out tmp/annual_2026  # .md + 4 CSVs
.venv/Scripts/python.exe -m src.annual_pack --year 2025 --db path/to/copy.db
```

## Modelo 390 (`src/returns/modelo_390.py`)

`compute_modelo_390(year, conn, config)` is built from the year's four `compute_modelo_303` results (their boxes and audit records), so it can never drift from the quarterly returns; `aeat_boxes()` returns every box keyed as printed, and the reconciliation uses it. Full box layout: [docs/tax-engine-reference.md#modelo-390-box-model](tax-engine-reference.md#modelo-390-box-model).

## Modelo 347 (`src/returns/modelo_347.py` purchases + `compute_modelo_347` sales)

Purchases: Spanish vendors (by NIF — the invoice's, else the vendor registry's) whose **VAT-inclusive** (`subtotal_eur + iva_amount`) purchases of the year **exceed** €3,005.06 (art. 33.1 RD 1065/2007: "hayan superado"), with the quarterly split. Excluded (art. 33.2 RD 1065/2007): intra-EU acquisitions already in the 349 and purchases with IRPF withheld (both 33.2.i), non-Spanish vendors (33.2.g) and `excluded` invoices. A vendor above the threshold without a NIF is listed as unidentified. Sales (key B) come from `compute_modelo_347` as before.

## P&L per IAE activity (`src/pl_by_activity.py`)

Income and expenses are the Q4 Modelo 130 year-to-date inputs split by activity — **826** coaching/teaching (`COACHING`), **861** illustration (`ILLUSTRATIONS`), **751** newsletter/publicity (`NEWSLETTER`): Stripe rows by `activity_type`; issued invoices by `activity_type` (an `EXEMPT_TEACHING` invoice without one is teaching); expense invoices by `activity_type`, else the vendor registry's `activity`; Stripe platform fees by the charge's `activity_type`. RETA, depreciation and lines without an activity follow `tax.pl_allocation` ([tax configuration](tax-obligations.md#tax-configuration)). Totals equal 130 Q4 box 01 and the real expenses inside box 02; the 5 % *gastos de difícil justificación* is shown separately (it is one allowance on the whole net yield). The tab and the markdown flag a P&L that does not tie.
