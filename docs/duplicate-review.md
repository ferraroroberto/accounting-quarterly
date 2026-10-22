# Duplicate Review

The **Duplicate Review** tab (and its `src/invoice_dedupe.py` module) finds and excludes duplicate, receipt and out-of-period invoices — the same expense counted twice inflates deductible VAT and IRPF expenses.

## Detectors

Five detectors run in priority order over the invoice ledger; a row already proposed by an earlier detector is never proposed again by a later one:

1. **Same `file_hash`** — byte-identical PDFs ingested under different filenames.
2. **Same vendor + `invoice_number`** — the same invoice re-ingested: same direction, vendor key (`vendor_vat_id_norm`, falling back to the vendor name), number, total and date. Rows that share the number but not the total or date (e.g. two issued invoices given the same number by mistake) are a **numbering problem**, not a duplicate: `find_numbering_conflicts` lists them as warnings (CLI ⚠, a warning in the tab) and nothing is excluded.
3. **Invoice/receipt pair** — same vendor, same total, dates within +/-3 days, and one side is a receipt (`invoice_type = "recibo"`, or `recibo`/`receipt` in the filename or description). **The invoice always wins.**
4. **Email-folder copy** — a file under an `email` subfolder duplicating a main-folder file (matched on vendor + invoice number, or vendor + total + date when no number is known).
5. **Out-of-period** — a row whose `invoice_date` falls outside the quarter being swept. Scoped to the invoices matching files copied into `tmp/close_quarter/<year>_Q<quarter>/`; a full-table scan is not meaningful here (every past quarter's rows would be "out of period").

**Keeper rule:** a row that is not excluded always beats an excluded one, so the current exclusion state is respected and a group keeps a live row; an invoice/receipt pair whose invoice is already excluded is not proposed. Then an invoice beats a receipt; among the rest, a non-`email`-path file wins, then the earliest-ingested (`extracted_at`) row. A row whose `excluded` field is already locked (a user edited it in the Invoice Ledger tab) is never touched by any detector or by `apply_groups` — a manual decision always wins over an automated one.

## Applying exclusions

Detection is a pure function (`find_duplicate_groups`) — nothing is written until you apply a group. Applying writes `excluded=1` / `excluded_reason` via `set_invoice_exclusion`, the same ledger-only write path `upsert_invoice` uses, **without** locking the field — so re-extraction or a later manual edit can still change it. This differs from editing a row directly in the Invoice Ledger tab, which always locks the fields you touch. `apply_groups` re-reads each group's keeper first and skips a group whose keeper is excluded by then (reported as a skipped group), so applying can never leave a duplicate group with every row excluded.

- **UI:** scan, review each proposed group (reassign the keeper if needed), then **Exclude the rest** per group or **Apply all proposed exclusions**. A row auto-excluded this way shows up under "Auto-excluded rows" with an **Undo** button.
- **CLI:**

```bash
.venv/Scripts/python.exe -m src.invoice_dedupe scan [--direction in|out] [--apply]
.venv/Scripts/python.exe -m src.invoice_dedupe scan --year 2026 --quarter 2 --apply  # also runs the out-of-period detector
.venv/Scripts/python.exe -m src.invoice_dedupe scan --db path/to/a/copy.db          # dry run against a DB copy
```

Excluded rows are ignored by every tax computation (Modelo 303/130/347), same as a manual exclusion.
