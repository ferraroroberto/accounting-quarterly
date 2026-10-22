# Vendor Registry

Vendors repeat every month, so a small registry makes expense classification deterministic instead of relying on the (often missing) vendor VAT id.

**Source of truth:** `vendors.json` at the repo root — git-ignored like `classification_rules.json`, because it holds real vendor tax ids. The repo ships `vendors.json.example` with fake vendors. There is no `vendors` table: the registry's defaults are written onto the `invoices` rows, so the tax engine only reads invoices. A missing file means an empty registry (every expense invoice is flagged).

| Field | Meaning |
|-------|---------|
| `key` | Normalised vendor name (lower-case, punctuation → spaces). Should equal the vendor's sub-folder under `invoice_in_dir`. |
| `aliases` | Other names / folder names for the same vendor. |
| `legal_entity`, `country` (ISO-2), `vat_id`, `alt_vat_ids` | Who bills you. `country` fills `geo_region` / `supply_country` when the invoice has none. |
| `default_tax_treatment` | One of the expense treatments (`DOMESTIC`, `DOMESTIC_CAPITAL`, `INTRA_EU_RC`, `NON_EU_RC`, `NO_VAT`, `NOT_DEDUCTIBLE`). Typical: EU SaaS with a reverse-charge note → `INTRA_EU_RC`; US SaaS without VAT → `NON_EU_RC`; foreign vendor charging Spanish VAT → `DOMESTIC`; bank / fintech fees → `NO_VAT`. |
| `default_deductible_pct_vat`, `default_deductible_pct_irpf` | Business-use % (e.g. home-office utilities, mixed-use devices). |
| `activity` | `COACHING` (IAE 826), `NEWSLETTER` (IAE 751) or `ILLUSTRATIONS` (IAE 861) — written to `invoices.activity_type` for the P&L per activity. |
| `asset_class`, `recurrence`, `notes` | Optional. |

Leave a default empty to keep the invoice's own (heuristic) value — e.g. for a vendor that bills from both an EU and a US entity.

**Matching** (first hit wins): (1) the invoice's **sub-folder** (first component of `filename`) against `key` / `aliases`; (2) the normalised **vendor VAT id** against `vat_id` / `alt_vat_ids`; (3) the **vendor name** against `key` / `aliases` / `legal_entity` (whole-word, longest alias first).

**Applying.** Every OCR extraction applies the registry to the new row, and `close_quarter.py vendors` applies it to the quarter being closed. The **Vendors** tab's **Apply registry** button, the CLI below and `close_quarter.py vendors --all-periods` re-apply it to all stored expense invoices, filed periods included, and report the rows written per year/quarter (filed ones marked `(FILED)`). All of them are idempotent:

- `tax_treatment` (legacy `vat_treatment` kept in sync), `deductible_pct_vat`, `deductible_pct_irpf`, `activity_type` and `asset_class` take the registry default, **except 🔒 locked fields** — a Ledger edit always wins.
- `vendor_vat_id_norm`, `geo_region` (only when `UNKNOWN`) and `supply_country` are filled only when missing: an id read from the document wins.
- Registry writes never lock a field and never mark the invoice reviewed.

**Unknown vendors** are listed in the **Vendors** tab (grouped by suggested key = the invoice folder), flagged ⚠ in the Invoice Ledger grid, and warned about on the Invoice OCR card.

**Editing.** The Vendors tab has an editable grid (**Save registry** validates every row) and an **Import from a spreadsheet** panel: an `.xlsx` whose first sheet has a vendor column (`vendor` / `item` / `name`) plus optional `activity` (or `business`: coaching / newsletter / illustration) and `recurrence` (or `recurrency`). New vendors are added; existing vendors only get an empty activity / recurrence filled.

**CLI:**

```bash
# Build or extend vendors.json from a vendor spreadsheet (merge; hand edits are kept)
.\.venv\Scripts\python.exe -m src.vendor_registry seed-from-xlsx <path-to-vendor-list.xlsx>
# Apply the registry to every stored expense invoice and list the unknown vendors
.\.venv\Scripts\python.exe -m src.vendor_registry apply [--db data/accounting.db] [--registry vendors.json]
```
