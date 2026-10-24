# Invoice Ledger

The **Invoice Ledger** tab is where OCR output is reviewed and corrected. Pick a direction (expenses / income) and optionally a quarter (by invoice date), then:

- **Bulk edit** — an `st.data_editor` grid over the ledger columns (invoice date, tax treatment, VAT/IRPF business-use %, capital asset, exclusion, EUR received, payment date). Edits apply on **Save changes**.
- **Vendor** (expenses) — the [vendor-registry](vendor-registry.md) match for each row; **⚠ unknown** when no vendor matches (also counted in the *⚠ Unknown vendor* metric).
- **Edit one invoice** — a form covering the OCR fields (number, dates, parties, amounts, …) and the ledger columns. Saving stamps `reviewed_at` even when nothing changed.

**Locks.** Every field you change is added to the invoice's `locked_fields` (JSON list). Re-extracting the PDF in the Invoice OCR tab never overwrites a locked field — enforced in `src/database/invoices.py` (`upsert_invoice`), not just in the UI. Ledger-only columns (`excluded`, `eur_received`, …) also survive a plain re-extract. **Unlock all fields** releases the locks (values stay as they are) so the next re-extract may overwrite them.

**Ledger columns** (added by an idempotent migration on startup), and the **backfill of `tax_treatment`** from the legacy `vat_treatment`: [tax-engine-reference.md#ledger-columns](tax-engine-reference.md#ledger-columns) and [tax-engine-reference.md#backfill-of-tax_treatment](tax-engine-reference.md#backfill-of-tax_treatment).

The tax engine uses `excluded`, `invoice_date`, the split business-use percentages and, for the Modelo 303, `tax_treatment` (see [Modelo 303 box model](tax-engine-reference.md#modelo-303-box-model)). Rows with no `tax_treatment` are derived from the legacy `vat_treatment` on the fly.

## Exchange rate differences

A foreign-currency income invoice with no `eur_received` is booked at the ECB rate on the invoice date — final, not provisional (see [Foreign-currency invoices](invoice-ocr.md#foreign-currency-invoices-eur-resolution)). If that foreign-currency balance is **later converted** to EUR, the conversion realises a gain or loss against the EUR figure originally booked, which must be recorded as activity income (or a loss) in the quarter of conversion, not the invoice's own quarter.

The Invoice Ledger tab's **income** view has an "Exchange rate differences" form for this: pick the invoice (optional), the conversion date, the foreign-currency amount converted, and the EUR actually obtained. It computes `gain_loss_eur = eur_obtained − booked_eur` and stores it in the `fx_exchange_differences` table (`src.fx_rates.record_exchange_difference` / `get_exchange_differences`). Every recorded gain/loss dated within a quarter's year-to-date window is added to that quarter's Modelo 130 box 01 income (`src.modelo_130.compute_modelo_130`) — on top of, not instead of, the invoice's own booked income, which keeps counting in its own quarter as usual.

## Invoice Explorer

The **Invoice Explorer** tab provides a filterable, exportable view of all OCR-extracted invoices in a single table.

**Filters available:** direction (in/out), category, invoice type, vendor name (text search), client name (text search), invoice date range, subtotal range, and a "rectificativas only" toggle.

Live summary metrics (matching count, total expenses, total income) update as filters change. Results can be exported to CSV.
