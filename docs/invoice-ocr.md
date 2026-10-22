# Invoice OCR (AI Extraction)

The **Invoice OCR** tab extracts Spanish accounting data from any PDF — invoices, receipts, tickets, foreign bills — and stores the results in the `invoices` SQLite table.

## Extraction provider

Extraction always routes through local-llm-hub at `http://127.0.0.1:8000`, via the Anthropic SDK and a `document` content block, model alias `gemini_pro` — no Google credentials are needed on this machine, since the hub holds the Google session. This keeps all LLM access flowing through the local-llm-hub for central LAN access and observability. Override the hub endpoint/alias with `LLM_HUB_BASE_URL` / `LLM_HUB_MODEL` if needed.

## Invoice directories

Configured via `config.json` (`invoice_in_dir` / `invoice_out_dir`). Both accept absolute paths. PDFs are scanned **recursively**, so subdirectories (e.g. year/quarter folders) are included automatically.

| Direction | Default path | Accounting role |
|-----------|-------------|-----------------|
| **In** (expenses) | `data/invoices/in` | Facturas recibidas — IVA soportado |
| **Out** (income) | `data/invoices/out` | Facturas emitidas — IVA repercutido |

Re-extraction is skipped automatically when the PDF has not changed (MD5 hash comparison).

## Extracted fields

All fields required for AEAT compliance (Libro de IVA, SII, Modelo 303/347/349):

| Field | Description |
|-------|-------------|
| `invoice_number`, `invoice_date` | Document identification |
| `invoice_type` | `factura_completa`, `factura_simplificada`, `ticket`, `recibo`, `nota_gastos` |
| `supply_date`, `due_date` | Fecha prestación / fecha vencimiento |
| `vendor_name`, `vendor_nif`, `vendor_address` | Emisor |
| `client_name`, `client_nif`, `client_address` | Receptor |
| `subtotal_eur`, `iva_rate`, `iva_amount` | Base imponible and main IVA |
| `iva_breakdown` | JSON array — one entry per IVA rate line (supports mixed-rate invoices and recargo de equivalencia) |
| `irpf_rate`, `irpf_amount` | IRPF retention |
| `total_eur` | Total a pagar |
| `vat_exempt_reason` | Legal basis for 0% IVA (Art. 20 LIVA, intracomunitaria, exportación, etc.) |
| `deductible_pct` | Deductibility percentage (default 100; 50 for vehicles, home office, etc.) |
| `is_rectificativa`, `rectified_invoice_ref` | Factura rectificativa handling |
| `billing_period_start`, `billing_period_end` | Subscription billing period |
| `payment_method`, `category`, `notes` | Classification and flags |

## Foreign-currency invoices: EUR resolution

The LLM's own `subtotal_eur`/`iva_amount`/`total_eur` guess for a foreign-currency document is used only as a **cross-check** — the authoritative EUR figure comes from `src.fx_rates.resolve_invoice_amounts`, called right after extraction (`src/invoice_ingest.extract_and_save`, shared by the Invoice OCR tab and `close_quarter.py ocr`), in this order:

1. **`charged_eur`** — when the document itself states the EUR actually charged to the card (e.g. *"Charged 42.50 EUR using 1 USD = 0.8500 EUR"*), that wins. The OCR prompt extracts it into the `charged_eur` field, left `null` when the document doesn't state it.
2. Otherwise, **the ECB rate on `invoice_date`** — `original_amount` divided by the daily rate from `fx_rates`, with the fallback and staleness behaviour of [Currency Conversion](currency-conversion.md).

The resolved rate, its date and its source are stored per invoice (`fx_rate_used`, `fx_rate_date`, `fx_source` ∈ `NATIVE_EUR` / `CHARGED_EUR` / `ECB` / `NO_RATE` / `INVALID_DATE` / `MISSING_FX_INPUT`, `fx_stale`), and the LLM's own estimate is compared against the resolved figure: a difference over 1% is stored as `fx_cross_check_diff_pct` and surfaced as a ⚠️ warning in the Invoice OCR and Invoice Ledger tabs.

**Income invoices (`direction='out'`):** the same ECB resolution applies at extraction time, and it is **final**, not provisional — per art. 79.Once LIVA, income kept in a foreign-currency account (never converted) is booked at the ECB rate on the invoice date. If the money **was** actually converted on receipt, set `eur_received` in the Invoice Ledger tab once it's known; it then wins over the stored ECB figure in every tax computation that reads invoice income (Modelo 130 box 01, Modelo 303 box 120). See [Exchange rate differences](invoice-ledger.md#exchange-rate-differences) for what happens when a foreign-currency balance booked at the ECB rate is converted later.

**Resolution runs at extraction time only.** A stored invoice keeps the EUR figure it was saved with until it is re-resolved. Run `close_quarter.py fx-recompute` (or the Currency tab's **Recompute FX for stored invoices** button) to re-resolve every stored non-EUR invoice in place; see [Closing a Quarter](close-quarter-cli.md) for the command and its guarantees (locked fields skipped, `eur_received` untouched, idempotent).

## All Records tab features

- **Date scanned** column shows when each invoice was extracted.
- **Row-selection checkboxes** — select one or more records and click **Delete selected**.
- **Clear invoice table** — wipes all records (with confirmation); PDF files are never touched.

## Credentials

Extraction routes through local-llm-hub and needs no Google API key — see [Extraction provider](#extraction-provider) above.
