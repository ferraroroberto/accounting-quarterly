# Seguridad Social (Social Security Cuotas)

The **Seguridad Social** tab imports monthly autónomo quota payments debited from your bank account and feeds them automatically into **Modelo 130** as deductible expenses.

## Data source

Cuota payments are not issued as invoices — they appear as bank debits. Export your bank statement (the rows corresponding to Seguridad Social payments, or the full statement filtered by concept) to Excel (`.xlsx` or legacy `.xls`) or CSV and import via this tab.

## Setup

1. Export the relevant rows from your bank's online portal to `.xlsx`, `.xls`, or `.csv`.
2. Place the file anywhere accessible (default: `tmp/social_security_bank_export.xlsx`).
3. Configure the column names in `config.json`:

```json
{
  "social_security": {
    "bank_export_file": "tmp/social_security_bank_export.xlsx",
    "date_column": "Fecha",
    "amount_column": "Importe",
    "description_column": "Más datos",
    "concept_column": "Movimiento",
    "concept_patterns": ["TGSS", "SEG.SOCIAL", "SEGURIDAD SOCIAL", "AUTONOMOS"],
    "sheet_name": 0,
    "skiprows": null
  }
}
```

- `skiprows`: leave `null` (or omit) to auto-detect the header row — the importer scans the first rows for the one containing both `date_column` and `amount_column`, so title rows above the header (e.g. "Movimientos de la cuenta ...") are skipped automatically. Set an explicit row index to override.
- `concept_column` / `concept_patterns`: optional. When `concept_column` is set, only rows whose value in that column matches one of `concept_patterns` (case/accent-insensitive substring match) are imported — useful when the export contains all bank movements rather than pre-filtered Social Security rows. Leave `concept_column` unset to import every row in the file (e.g. when the bank export is already filtered to Social Security movements only).

4. Open the **Seguridad Social** tab, verify the column mapping and detected header row with **Preview file columns**, then click **Import from file**.

## How it works

- Legacy `.xls` (BIFF) files are supported via `xlrd`, in addition to `.xlsx`/`.xlsm` (`openpyxl`) and `.csv`.
- Amounts are **sign-flipped and stored net of refunds**: a bank debit (negative in the export — a cuota payment) is stored as a positive contribution; a bank credit (positive in the export — e.g. the automatic *pluriactividad* excess-contribution refund) is stored as a **negative** contribution entry in the period it was received, so it nets off the total automatically.
- Deduplication is by `(payment_date, amount_eur, description)` — re-importing the same file is safe, and a same-day/same-amount contribution and refund (or two distinct concepts) don't collide.
- A **manual entry** fallback (expander below the import controls) lets you record a month missing from the bank export, or a refund, directly — subject to the same dedupe key.
- The **Modelo 130** engine sums all SS payments from January 1 through the end of the selected quarter (YTD) and includes them in **box 02 — gastos deducibles**, alongside OCR-extracted expense invoices and manual entries. Legal basis: cuotas de autónomo are fully deductible under Art. 30 LIRPF (*régimen de estimación directa*).
- The audit trail (Tax Audit tab) records `ss_gastos` and the full list of individual payments as named inputs to the `c02_gastos_reales` cell (box 02 split).

## Quarterly breakdown

The tab shows per-year totals and, when a year is selected, a quarterly breakdown (Q1–Q4) so you can reconcile against the TGSS monthly receipts.
