# Tax engine reference

Field-by-field box models and the data mechanics that produce them — field names, audit cell names, and the ledger columns the tax engine reads. For *why* each rule is what it is (legal basis, where an external accountant may differ), see [tax-conventions.md](tax-conventions.md). For the quarter-close CLI, see [quarter-close-runbook.md](quarter-close-runbook.md). For the tab-by-tab overview of each model, see the README's [Tax Obligations](../README.md#tax-obligations-spanish-autónomo) section.

## Modelo 303 box model

`compute_modelo_303` returns a `Modelo303Result` whose fields are named after the AEAT boxes (`c07_base`, `c09_cuota`, …, `c110_pendiente_anteriores`); `result.aeat_boxes()` gives them keyed by the box number as printed on the form (`"07"`, `"110"`), in form order. The layout and formulas follow the AEAT form (Manual práctico IVA 2025, cap. 9): rows 01/03 = 4%, 04/06 = 10%, 07/09 = 21%; 27 = sum of the accrued cuotas; 45 = 29 + 31 + 37 + 43 + 44; 46 = 27 − 45; 64 = 46; 66 = 64 × 65 %; 69 = 66 − 78; 71 = 69.

| Box | Source |
|-----|--------|
| 01–09 | Stripe Spain + EU consumers at Spanish 21% (`EU_B2C_ES21`, no OSS) and income invoices `ES_21` (by rate) / `EU_B2C_ES21` |
| 10/11 · 36/37 | `INTRA_EU_RC` purchases: base × 21% self-assessed (accrued), and deducted × `deductible_pct_vat` |
| 12/13 | `NON_EU_RC` purchases (non-EU services, reverse charge — VAT-neutral) and the quarter's Stripe platform (application) fees at 21% (same classified charges as the sales, by charge date; audit source `platform_fee`, one record per charge; `tax.platform_fee_vat_treatment`), deducted in 28/29 |
| 28/29 | `DOMESTIC` invoices × `deductible_pct_vat` (minus any capital-good share), `NON_EU_RC`, Stripe platform fees at 100%, manual `IVA_SOPORTADO` entries (cuota ÷ their rate for the base) |
| 30/31 | Capital goods (unit base > €3,005.06) from the fixed-asset register at their VAT business-use %; `DOMESTIC_CAPITAL` invoices not in the register use the invoice |
| 43 · 44 | Q4 only: capital-goods regularisation (arts. 107–109 LIVA) and the pro-rata regularisation |
| 59 · 60 · 120 | EU B2B sales · exports of goods (none today) · non-EU sales not subject by location rules (Stripe non-EU customers + `NON_EU_NOT_SUBJECT` invoices). Earlier filings by the external accountant put non-EU service invoices in 60 instead of 120 — informational only, no money effect |
| 110 | The previous quarter's **filed** 87 + 72 (imported receipt, see [Importing filed AEAT receipts](../README.md#importing-filed-aeat-receipts)); when not imported, the app's own previous-quarter result, chained back to the first period with data |
| 78 / 87 | 78 = min(110, max(0, 66)); 87 = 110 − 78. Q4 with `refund`: 78 = 110 |
| 72 / 73 | A negative 71 is carried forward in 72; in Q4 it can be refunded (73) instead |

**Pro-rata** (arts. 102–106 LIVA, `tax.prorrata.enabled`, default on). During a year every deductible box is multiplied by the *provisional* %, which is the previous year's *definitive* %. In Q4 the engine computes the year's definitive % (art. 104: operations with the right to deduct — taxed sales plus EU B2B, non-EU and OSS sales that would carry the right if made in Spain — over those plus exempt ones such as `EXEMPT_TEACHING`, rounded **up** to the unit) and puts (definitive − provisional) × the year's deductible VAT into box 44. Record the definitive % under `tax.prorrata.definitive_pct_by_year` once filed. `c46_sin_prorrata` shows box 46 with 100% deduction, for comparing with filings that ignore the pro-rata.

Manual `IVA_SOPORTADO` entries (Tax Obligations → Manual Entries) now carry their VAT rate, from which the box 28 base is derived; the old fixed 21% assumption is gone.

Old snapshots with the pre-#97 field names (`box_01_base`, `box_29_cuota_soportado`, `export_base`, …) still decode: the codec maps them to the new fields and fills in the totals.

## Modelo 349 operators

`compute_modelo_349` returns a `Modelo349Result`: `result.operators()` lists the declarable lines as the form does (`country`, `vat_id` without the country prefix, `name`, `key`, `base`) and `result.aeat_boxes()` the summary boxes — 01 number of operators, 02 total amount, 03/04 rectifications (always 0: rectification lines are out of scope).

| Key | Source | Grouped by | Name |
|-----|--------|-----------|------|
| `I` | Expense invoices with `tax_treatment` `INTRA_EU_RC` | Vendor VAT id: the invoice's `vendor_vat_id_norm` / `vendor_nif`, else the matched vendor-registry `vat_id` | Registry `legal_entity`, else the invoice vendor name |
| `S` | Stripe charges treated `IVA_EU_B2B` (customer `buyer_vat_id`) + income invoices with `tax_treatment` `EU_B2B` (`client_nif`) | Normalised VAT id (`normalize_vat_id`) | Stripe customer e-mail / invoice client name |

Invoices count by `invoice_date` and `excluded` rows are skipped; bases are the stored EUR values (ECB rate resolved at OCR time; `eur_received` for income invoices), so — lines not declared aside — key `I` adds up to 303 box 10 and key `S` to box 59. Lines that cannot be declared are listed apart with a warning: an operator whose quarter total is **zero or negative** (`result.excluded` — rectify the original period instead) and records **without a VAT id** (`result.unidentified` — add the id to the invoice or the vendor registry). A VAT id without an EU country prefix is declared but flagged. The **Modelo 349** tab of Tax Obligations shows the boxes and the operator table; the Tax Audit trail has one cell per line (`op_<KEY>_<VATID>`, with the records summed) plus `c01_operadores` / `c02_importe`. Snapshots stored before #99 (EU B2B sales only) decode as key `S` lines.

## Modelo 130 box model

`compute_modelo_130` returns a `Modelo130Result` whose fields are named after the AEAT boxes (`c01_ingresos` … `c19_resultado`); `result.aeat_boxes()` gives boxes `"01"`–`"19"` keyed as printed on the form, in form order. Formulas follow the AEAT Sede *Modelo 130 — Instrucciones*. Only 04 and 12 are floored at 0; 03, 07, 14, 17 and 19 may be negative, as on the form.

| Box | Rule |
|-----|------|
| 01 | Year-to-date income: Stripe VAT bases (frozen declared-report amounts when frozen) + issued invoices gross of the withholding (`eur_received` when set) + exchange differences |
| 02 | Real expenses + the 5% *gastos de difícil justificación*. Real expenses = expense invoices × `deductible_pct_irpf` (excluded rows and capital assets out) + RETA as paid, net of refunds + Stripe platform (application) fees + depreciation (posting mode) + manual `GASTOS_DEDUCIBLES`. Stripe's own processing fee is not added: it is already expensed from Stripe's invoices (see [tax conventions §8](tax-conventions.md#8-modelo-130-irpf-advance-payment)). The allowance is 5% of the positive (01 − real expenses), capped at €2,000 a year, only under `estimacion_directa_simplificada` (art. 30.2.4ª LIRPF). The UI and the audit (`c02_gastos_reales`, `c02_platform_fees`, `c02_gastos_dificil_justificacion`) show the split; charges with an unknown fee split are counted in `c02_platform_fees` and the notes |
| 03 · 04 | 03 = 01 − 02; 04 = 20% of the positive 03 |
| 05 | Σ positive 07 − Σ 16 of the earlier quarters of the year |
| 06 | Year-to-date withholdings: `irpf_amount` of issued invoices (exact cents) + manual `RETENCIONES_SOPORTADAS` |
| 07 | 04 − 05 − 06 |
| 08–11 | Agricultural activities — 0 |
| 12 | max(0, 07 + 11) |
| 13 | Art. 110.3.c RIRPF reduction by the previous year's net yield: ≤ 9,000 → 100; ≤ 10,000 → 75; ≤ 11,000 → 50; ≤ 12,000 → 25; otherwise 0. The net comes from the previous year's **filed** Q4 130 box 03, else `tax.previous_year_net_yield`, else the app's previous-year Q4 box 03 (no activity counts as 0). It applies even when 12 is 0, leaving a negative 14 |
| 14 | 12 − 13 |
| 15 | Only when 14 is positive: the negative 19s of earlier quarters of the year not yet deducted, up to 14 |
| 16 · 18 | Housing-loan deduction · complementary return — 0 |
| 17 · 19 | 17 = 14 − 15 − 16; 19 = 17 − 18. A negative 19 is carried into 15 of later quarters (`negativos_pendientes_posteriores`) |

Boxes 05 and 15 chain through the earlier quarters of the year: each quarter's **filed** 130 is used when its receipt is imported (see [Importing filed AEAT receipts](../README.md#importing-filed-aeat-receipts)); otherwise the app computes that quarter itself. `c05_source` says which (`filed`, `app_chain`, `mixed`, `none`).  The old `tax_filing_status` "previous payments" are no longer read.

Old snapshots with the pre-#98 field names (`box_01_ingresos`, `box_05_base`, `box_16_resultado`, …) still decode: `box_02_gastos` becomes the real expenses, 02 gets the allowance added back, and 07/12/14/17 are derived (legacy results had no 13/15).

## Modelo 390 box model

`compute_modelo_390(year, conn, config)` is built from the year's four `compute_modelo_303` results (their boxes and audit records), so it can never drift from the quarterly returns; `aeat_boxes()` returns every box keyed as printed, and the reconciliation and the validator use it. Box layout from the AEAT *Modelo 390. Instrucciones* (Sede, procedure G412, layout valid since ejercicio 2024):

| Section | Boxes |
|---------|-------|
| IVA devengado | 01–06 (régimen ordinario 4/10/21 %), 545–552 (intra-EU acquisitions of services by rate), 27/28 (other reverse charge), 33/34 totals, 47 |
| IVA deducible | 190/191, 603/604, 605/606 → 48/49 (current domestic, incl. non-EU reverse charge); 196/197, 611–614 → 50/51 (capital goods); 587/588, 635–638 → 597/598 (intra-EU services); 63 (capital-goods regularisation); 522 (pro-rata regularisation, the Q4 303 box 44); 64, 65 = 47 − 64 |
| Result | 84, 85 (credit of earlier years applied: FIFO walk of the quarters, each box 78 consuming Q1's box 110 first, capped at each quarter's 110), 86 = 84 − 85, 95 (Σ positive 71), 97/98 (Q4 72/73), 662 (credit generated this year still pending: Q4 87 minus what is left of earlier years' credit) |
| Volume | 99 (taxed sales), 103 (intra-EU B2B), 104 (exports), 105 (exempt teaching), 110 (non-EU services not subject — the 303's box 120), 126 (OSS), 108 total |
| Pro-rata | 115/116/118 (general pro-rata, box 117 = G) — only when exempt operations exist |

Deductible bases are "sin prorratear" (the 303's `base_100`), cuotas after the pro-rata. The per-rate split follows the rate of each 303 audit record and is rounded so the rates add up to the section total. `INTRA_EU_RC` purchases are services (349 key I). The external accountant reported non-EU services in 104; the app follows the instructions (110). `230`/`232` (exempt / non-deductible purchases) are not modelled. The audit of boxes 85 and 662 records the per-quarter credit walk and its `flags`: `carried_in_from_app_chain` when Q1's box 110 comes from the app's own chain rather than the filed 4T return of the previous year, and `chain_break_QN` when a quarter's 110 differs from the previous quarter's 87 + 72; both also appear in the 390 notes.

## Ledger columns

Added by an idempotent migration on startup:

| Column | Meaning |
|--------|---------|
| `tax_treatment` | Expenses: `DOMESTIC`, `DOMESTIC_CAPITAL`, `INTRA_EU_RC`, `NON_EU_RC`, `NO_VAT`, `NOT_DEDUCTIBLE`. Income: `ES_21`, `EU_B2C_ES21`, `EU_B2B`, `NON_EU_NOT_SUBJECT`, `EXEMPT_TEACHING`. |
| `deductible_pct_vat` / `deductible_pct_irpf` | Business-use share for the VAT deduction (303) and the IRPF expense (130), independently. Backfilled from the legacy `deductible_pct`. |
| `is_capital_asset`, `asset_class` | Capital-asset flag and class. A flagged invoice is not expensed in the Modelo 130; register it as a fixed asset so its cost enters through depreciation (see [Fixed Assets](../README.md#fixed-assets)). |
| `excluded`, `excluded_reason` | `1` removes the row from every tax computation; reason ∈ `duplicate`, `receipt`, `personal`, `other_period`, `superseded`. |
| `eur_received`, `payment_date` | EUR actually received for foreign-currency income, and when. |
| `vendor_vat_id_norm` | `vendor_nif` normalised for matching: upper-case, separators stripped, Spanish ids `ES`-prefixed. |
| `locked_fields`, `reviewed_at` | User-edited fields (never overwritten by re-OCR) and last review time. |
| `charged_eur` | EUR actually charged to the card, when the document states it (expenses) — wins over the ECB rate. The one FX field you may correct by hand. |
| `fx_rate_used`, `fx_rate_date`, `fx_source`, `fx_stale`, `fx_cross_check_diff_pct` | FX resolution metadata (#93) — see [Foreign-currency invoices: EUR resolution](../README.md#foreign-currency-invoices-eur-resolution). Derived; re-resolved on the next OCR extraction, not directly editable. |

## Backfill of `tax_treatment`

From the legacy `vat_treatment` (still stored and kept in sync when you edit the treatment). The mapping preserves what the engine did with the legacy value:

| Direction | Legacy `vat_treatment` (+ `geo_region`) | `tax_treatment` |
|-----------|------------------------------------------|-----------------|
| in | `IVA_ES_21` | `DOMESTIC` |
| in | `IVA_EU_B2B` | `INTRA_EU_RC` |
| in | `IVA_EXEMPT` + `OUTSIDE_EU` | `NON_EU_RC` |
| in | `IVA_EXEMPT` + any other region | `NO_VAT` |
| out | `IVA_ES_21` | `ES_21` |
| out | `OSS_EU` | `EU_B2C_ES21` |
| out | `IVA_EU_B2B` | `EU_B2B` |
| out | `IVA_EXPORT` | `NON_EU_NOT_SUBJECT` |
| out | `IVA_EXEMPT` + `SPAIN` | `EXEMPT_TEACHING` |
| out | `IVA_EXEMPT` + other / unknown region | left empty — review it in the Ledger tab |

Rows with no `tax_treatment` are derived from the legacy `vat_treatment` on the fly.
