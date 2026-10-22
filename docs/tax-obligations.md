# Tax Obligations (Spanish Autónomo)

The **Tax Obligations** tab turns the classified transaction data into pre-filled Spanish tax filings. It covers the standard obligations for an autónomo in *régimen de estimación directa simplificada*. The legal basis of each rule, and where an external accountant may differ, is in [`docs/tax-conventions.md`](tax-conventions.md).

## Stored calculations

Computed figures are **not** recalculated on every page load. Click **Calculate tax** to run the engines and persist results to the `tax_computation_snapshots` table in SQLite (per selected year and quarter; Modelo **347** is annual and stored with quarter `0`). After you sync Stripe data, change manual tax entries, or adjust classifications, run **Calculate tax** again to refresh.

## Supported models

| Model | Name | Frequency | What it computes |
|-------|------|-----------|-----------------|
| **Modelo 303** | Declaración IVA Trimestral | Quarterly | Every AEAT box: accrued (01–13, 27), deductible (28–46, incl. reverse charge, capital goods and pro-rata), informational (59/60/120) and the result with the credit chain (64–73, 110/78/87) — see [Modelo 303 box model](tax-engine-reference.md#modelo-303-box-model) |
| **Modelo 130** | Pago Fraccionado IRPF | Quarterly | Every AEAT box 01–19: YTD income/expenses (02 includes the 5% allowance), 20% advance, withholdings, the art. 110.3.c reduction (13) and the negative-result carry (15) — see [Modelo 130 box model](tax-engine-reference.md#modelo-130-box-model) |
| **Modelo 349** | Operaciones Intracomunitarias | Quarterly | Key `I` (services acquired from EU businesses) and key `S` (services supplied to EU businesses), one line per VAT id and key — see [Modelo 349 operators](tax-engine-reference.md#modelo-349-operators) |
| **OSS Return** | One Stop Shop | Quarterly | B2C digital services to EU non-Spain customers, grouped by country — only when `oss_registered` is true |
| **EU B2C threshold** | Art. 73 LIVA | Live | Year-to-date EU B2C sales (ex-VAT) vs €10,000; warns at 80%, flags the previous year too |
| **Modelo 347** | Operaciones con Terceros | Annual | Spain counterparties with total operations > €3,005.06 (**importe IVA incluido**): sales here, purchases in the [Annual Pack](annual-pack.md) |
| **Modelo 390** | Resumen Anual IVA | Annual | Built from the four 303s: rows by rate, deductions, pro-rata, result and compensation, volume of operations — see [Annual Pack](annual-pack.md) |

## VAT treatment classification

VAT treatment is derived on-the-fly by the tax engine using each transaction's activity × geography (and, for EU sales, the customer's VAT id) — it is not stored per transaction. The mapping is:

| Activity | Geography | Customer VAT id | Treatment | IVA |
|----------|-----------|-----------------|-----------|-----|
| Any | OUTSIDE_EU | — | `IVA_EXPORT` | 0% |
| Any | SPAIN | — | `IVA_ES_21` | 21% |
| Any | EU_NOT_SPAIN | Known | `IVA_EU_B2B` | 0% (reverse charge) |
| Any | EU_NOT_SPAIN | Unknown | `EU_B2C_ES21` (default, not OSS-registered) | 21% Spanish IVA, Modelo 303 boxes 07/09 |
| Any | EU_NOT_SPAIN | Unknown | `OSS_EU` (only when `oss_registered: true`) | Buyer country rate, OSS return |

**B2B vs. B2C follows the customer's status, not the activity** (art. 69/70 LIVA). A sale to an EU business with a valid VAT id is reverse-charged (`IVA_EU_B2B`, Modelo 349 key S); a sale to an EU consumer is taxed in Spain below the €10,000 threshold. The VAT id comes from **Configuration → Geographic Rules → Customer VAT IDs** (an email or name/description override, mirroring the geographic overrides — see [Customer VAT ID overrides](classification.md#customer-vat-id-overrides-eu-b2b-vs-b2c)) or, as a read-only fallback, a Stripe `customer.tax_ids` entry already present in the stored raw charge (not currently requested by the Stripe fetch, so this fallback is a no-op on today's data). **VIES validation of the id is out of scope** — the app trusts whatever VAT id is on file; verify it yourself before relying on the reverse charge.

**EU consumers below the threshold.** Under art. 73 LIVA, electronically supplied services to consumers in other EU countries stay located in Spain — Spanish 21% IVA — while the year's and the previous year's EU B2C sales are at or below €10,000 (ex-VAT) and you have not opted into OSS. That is `EU_B2C_ES21`. If `default_vat_treatment_eu_<activity>` says `OSS_EU` but `oss_registered` is not true, the engine uses `EU_B2C_ES21` (there is no OSS return to declare it on). The **Tax Obligations → EU B2C / OSS** tab tracks the threshold and a warning banner appears at 80%.

## VAT-inclusive pricing (Stripe amounts)

Stripe records the gross amount charged to the customer, which for Spain and EU sales **includes VAT**. The engine extracts the taxable base by dividing the net received amount by `(1 + rate)`:

| Treatment | Base formula | Example |
|-----------|-------------|---------|
| `IVA_ES_21` | `net ÷ 1.21` | €121 gross → €100.00 base + €21.00 IVA |
| `EU_B2C_ES21` | `net ÷ 1.21` | €121 gross (AT consumer) → €100.00 base + €21.00 IVA |
| `OSS_EU` | `net ÷ (1 + country_rate)` | €120 (AT, 20%) → €100.00 base + €20.00 IVA |
| `IVA_EXPORT` | `net` (no VAT) | €100 gross = €100.00 base |
| `IVA_EU_B2B` | `net` (reverse charge) | Full amount is income base |

If `vat_base_eur` is already set on a transaction (manual override), that value is used directly and no extraction is performed.

For **Modelo 130** (IRPF), income reported in Box 01 is the ex-VAT base — IVA collected is a pass-through to AEAT and is not part of your rendimiento.

## Aggregated audit records

The audit trail in the Tax Audit tab mirrors the **Quarter Report** view that the gestor receives: one aggregated line per (geo_region × activity × VAT treatment) bucket, rather than one line per individual Stripe transaction. The totals are identical — only the presentation changes.

## Tax configuration

Add a `tax` section to `config.json` (see `config.json.example`), or use the **Configuration → Tax Settings** tab:

```json
{
  "tax": {
    "regime": "estimacion_directa_simplificada",
    "vat_registered": true,
    "oss_registered": false,
    "activity_start_date": "2025-01-01",
    "prorrata": {"enabled": true, "definitive_pct_by_year": {"2025": 100}},
    "modelo303_q4_negative_result": "compensate",
    "platform_fee_vat_treatment": "NON_EU_RC",
    "previous_year_net_yield": {"2025": 5000.00},
    "vat_proration_percentage": 100,
    "default_vat_treatment_eu_coaching": "EU_B2C_ES21",
    "default_vat_treatment_eu_newsletter": "EU_B2C_ES21",
    "default_vat_treatment_eu_illustrations": "EU_B2C_ES21"
  }
}
```

Every key above drives a computation:

| Setting | Effect on computation |
|---------|----------------------|
| `regime` | Gates the 5% *gastos de difícil justificación* in Modelo 130 — only `estimacion_directa_simplificada` is eligible (Art. 30.2.4ª LIRPF). |
| `vat_registered` | When `false`, Spanish sales are treated as `IVA_EXEMPT` (no IVA devengado) and no input IVA is deducted in Modelo 303. |
| `oss_registered` | Default `false` (OSS is opt-in, Modelo 035). Unless `true`, no OSS return is generated (an audit note records why) and EU B2C sales are `EU_B2C_ES21`. |
| `activity_start_date` | ISO date (`YYYY-MM-DD`) the business activity began. Absent → no lower bound. Set, it floors every quarter/YTD range: a Stripe charge or invoice dated before it is left out of 303, 130, 349, 390 and 347 (income on the date itself is still counted), and the 303 box 110 / 130 box 05-15 chains never reach back before it. An audit note records what was excluded. |
| `prorrata.enabled` | VAT pro-rata (arts. 102–106 LIVA), default `true`. See [Modelo 303 box model](tax-engine-reference.md#modelo-303-box-model). |
| `prorrata.definitive_pct_by_year` | The definitive pro-rata % of each year once filed (Q4 303 / 390); it is the next year's provisional %. Years not listed fall back to the % the app computes from that year's data, then 100. |
| `previous_year_net_yield` | Previous year's net yield of economic activities for Modelo 130 box 13 — a number, or `{"<year>": amount}`. Only used when the previous year's Q4 130 receipt is not imported; without either, the app's own previous-year figure is used. |
| `modelo303_q4_negative_result` | `compensate` (default, box 72) or `refund` (box 73) for a negative Q4 result. Q1–Q3 always carry forward. |
| `platform_fee_vat_treatment` | `NON_EU_RC` (default): the quarter's Stripe platform (application) fees are self-assessed as a non-EU reverse charge — base in 303 box 12, 21% in 13, deducted in 28/29. `NONE` leaves them out of the 303 (e.g. to mirror an accountant during a shadow run); the 130 expenses them either way. |
| `pl_allocation` | P&L per activity: where RETA (`reta`), depreciation (`depreciation`) and lines without an activity (`unallocated`) go — an activity (`COACHING`, `NEWSLETTER`, `ILLUSTRATIONS`) or `BY_INCOME` (split by directly attributed income). Default `COACHING` (IAE 826) for all three, as the external accountant does. |
| `vat_proration_percentage` | Legacy flat pro-rata %. Only used, as the provisional %, when it is not `100` and the previous year has no `prorrata.definitive_pct_by_year` entry. |
| `default_vat_treatment_eu_coaching` / `default_vat_treatment_eu_newsletter` / `default_vat_treatment_eu_illustrations` | Pick the EU B2C sub-treatment (`EU_B2C_ES21` or `OSS_EU`) per activity for a sale **without** a known customer VAT id. Default `EU_B2C_ES21` for every activity. Does not select B2B: `IVA_EU_B2B` applies only when the customer has a VAT id on file (see [VAT treatment classification](#vat-treatment-classification)) — a legacy `IVA_EU_B2B` value here is accepted but ignored. |

## Modelo 303, 130 and 349 box models

Field-by-field box tables (AEAT box → source, field names, audit cell names) are the tax-engine reference, not a tab overview: [docs/tax-engine-reference.md](tax-engine-reference.md#modelo-303-box-model) covers the Modelo 303 box model, the [Modelo 130 box model](tax-engine-reference.md#modelo-130-box-model) and [Modelo 349 operators](tax-engine-reference.md#modelo-349-operators). The legal basis behind each box is in [tax-conventions.md](tax-conventions.md).

## Invoice data in tax calculations

OCR-extracted invoices (from the Invoice OCR tab) feed directly into all tax models alongside Stripe transactions:

| Model | Source | Contribution |
|-------|--------|-------------|
| **Modelo 303** | Expense invoices (`direction='in'`) | By `tax_treatment`: `DOMESTIC` → 28/29, `DOMESTIC_CAPITAL` / registered capital goods → 30/31, `INTRA_EU_RC` → 10/11 + 36/37, `NON_EU_RC` → 12/13 + 28/29, each weighted by `deductible_pct_vat` |
| **Modelo 303** | Income invoices (`direction='out'`) | `ES_21` → 01/03, 04/06 or 07/09 by rate, `EU_B2C_ES21` → 07/09, `EU_B2B` → 59, `NON_EU_NOT_SUBJECT` → 120, `EXEMPT_TEACHING` → pro-rata denominator only |
| **Modelo 130** box 01 | Non-Stripe income invoices (`direction='out'`) | Subtotal ingresos YTD (gross of the withholding) — `eur_received` when set, otherwise the stored (ECB-resolved) `subtotal_eur`, plus `fx_exchange_differences.gain_loss_eur` recorded in the period |
| **Modelo 130** box 02 | Expense invoices (`direction='in'`, not `is_capital_asset`) | Subtotal gastos (weighted by `deductible_pct_irpf`) + the input VAT the 303 does not deduct, in the same share YTD (see [tax conventions §3.1](tax-conventions.md#31-domestic-purchases-boxes-2829)) |
| **Modelo 130** box 02 | `fixed_assets` table | Depreciation YTD (see [Fixed Assets](fixed-assets.md)) |
| **Modelo 130** box 02 | `social_security_payments` table | SS cuotas YTD, net of refunds (fully deductible) |
| **Modelo 130** box 06 | Outgoing invoices | IRPF withheld (`irpf_amount`) YTD |
| **Modelo 347** | Income invoices | Spanish-client invoice operations alongside Stripe. Both sources accumulate on one VAT-inclusive basis so the single threshold compares like with like: Stripe uses `converted_amount − converted_amount_refunded`, invoices use `subtotal_eur + iva_amount`. Not `total_eur` — that is net of the IRPF retención, which is a withholding on payment rather than a smaller operation. |
| **Modelo 349** | Expense invoices (`direction='in'`) | `INTRA_EU_RC` → key `I`, per vendor VAT id |
| **Modelo 349** | Income invoices | `EU_B2B` → key `S` alongside the Stripe B2B charges |

Geographic classification is auto-derived from the vendor NIF (expenses) or client NIF (income) at OCR extraction time. Existing rows are backfilled automatically on database init.

Invoices are assigned to a quarter by **`invoice_date`** (the accounting date); `supply_date` is informational only, so an invoice dated in April for a service supplied in March counts in Q2. Rows marked `excluded = 1` (duplicates, receipts, personal, other period, superseded) are ignored by every model.

## Manual entries

Items that cannot be derived from Stripe or invoices (additional overrides, one-off corrections) are entered via the **Manual Entries** sub-tab and stored in `quarterly_tax_entries`.
