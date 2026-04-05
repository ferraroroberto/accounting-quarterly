# Tax Logic Validation Findings

This document outlines the findings from reviewing the automated tax logic in `src/tax_engine.py` and `src/tax_validator.py` against standard Spanish tax rules for Autónomos (Régimen de Estimación Directa Simplificada).

---

## Assessment Summary (2026-04-05)

| # | Finding | Correct? | Action Taken |
|---|---------|----------|--------------|
| 1 | M130: Missing 5% gastos de difícil justificación | **Yes** | **Implemented** — added deduction in `compute_modelo_130`, capped at €2,000/year |
| 2 | M347: VAT not included in total operations | **Partially correct** | **No code change** — Stripe `converted_amount` already includes VAT; see note below |
| 3 | M303: Soportado base reverse-calculation at 21% | **Yes** | **No code change** — only affects display of box_29, not actual tax result; would require expense-level VAT rate tracking |
| 4 | M303: 100% proration assumption | **Yes (operational note)** | **No code change** — user must input only the deductible portion of IVA soportado |
| 5 | M349: Negative amounts from refunds | **Yes** | **Implemented** — negative VAT ID totals are now excluded from rows and flagged in `notes` |
| 6 | M390: Volume of operations (Box 108) | **Correct for current scope** | **No change needed** |
| 7 | M130 Box 04 validation discrepancy | **Yes (consequence of #1)** | **Resolved** by fix #1 |

---

## Model 130 (IRPF Advance)

**1. Missing 5% Provision for "Gastos de Difícil Justificación"**
- **Current Logic:** `compute_modelo_130` computes `box_02_gastos` simply as the sum of manual entries for `GASTOS_DEDUCIBLES`. Then `box_03_rendimiento = box_01_ingresos - box_02_gastos`.
- **Spanish Tax Law:** For Autónomos in *Estimación Directa Simplificada*, the law allows an automatic 5% deduction over the net yield (rendimiento neto previo) as "gastos de difícil justificación", capped at €2,000 per year.
- **Impact:** The computed profit (`box_03_rendimiento`) is higher than it should be, causing a higher 20% advance payment (`box_05_base`). This leads to discrepancies with what a gestor files, as the gestor will automatically apply this 5% deduction.
- **Recommendation:** Update `compute_modelo_130` to subtract `5% * (ingresos - gastos_justificados)` (up to €2,000/year) before calculating the 20% base.

> **Assessment: CORRECT. Implemented.**
> - Added `gastos_dificil_justificacion` and `rendimiento_neto` fields to `Modelo130Result`.
> - `compute_modelo_130` now computes `5% × rendimiento_neto_previo` (capped at €2,000/year) and subtracts it before applying the 20% rate.
> - The 5% deduction is only applied when rendimiento neto previo is positive.
> - Validator (`tax_validator.py`) updated to show the new intermediate lines (03b, 03c).
> - All existing tests updated and passing.

## Model 347 (Third-party operations > €3,005.06)

**1. VAT not included in total operations**
- **Current Logic:** `compute_modelo_347` sums `converted_amount - converted_amount_refunded` (which is the net amount without VAT, or exactly what Stripe processed). If Stripe transactions include VAT (B2C Spanish sales), then it might be okay. But if the system is building this off a `vat_base_eur` and excluding the VAT amount, or if manual expenses are processed without their VAT, it's incorrect.
- **Spanish Tax Law:** The €3,005.06 threshold and the reported amounts in Model 347 **must include VAT** (IVA incluido).
- **Impact:** Some counterparties might not reach the €3,005.06 threshold in the code, whereas they do in reality when VAT is added. The reported amounts will be lower than what the gestor files.
- **Recommendation:** Ensure the aggregation for Model 347 uses the gross amount (Base + IVA) for each transaction, not just the net base.

> **Assessment: PARTIALLY CORRECT. No code change needed for current data source.**
> - Stripe's `converted_amount` is the gross amount the customer paid, which **already includes VAT** for Spanish B2C transactions. The current code (`converted_amount - converted_amount_refunded`) therefore already uses IVA-inclusive amounts for Stripe-sourced data.
> - The concern would be valid if manual expense entries were added to Model 347, but the current implementation only aggregates Stripe transactions with `geo_region = 'SPAIN'`.
> - **Note for future:** If manual invoices or non-Stripe sources are added to Model 347, the aggregation must explicitly use `base + IVA` amounts.

## Model 303 (Quarterly VAT)

**1. Soportado Base Approximation**
- **Current Logic:** In `compute_modelo_303`, `box_29_base_soportado` is derived by reverse-calculating the 21% rate: `box_28_iva_soportado / 0.21`.
- **Spanish Tax Law:** Deductible expenses can have various VAT rates (4%, 10%, 21%).
- **Impact:** If the user has expenses with reduced VAT rates, the reverse-calculated base (`box_29`) will not match the real base filed by the gestor, causing a mismatch in the validation tool.
- **Recommendation:** The system should track the actual base of deductible expenses instead of reverse-calculating it from the quota.

> **Assessment: CORRECT, but low priority. No code change.**
> - The reverse-calculated `box_29` is used only for validation display purposes. The actual tax computation (`box_46_diferencia`, `box_48_resultado`) uses `box_28_iva_soportado` directly and is unaffected.
> - Fixing this properly would require tracking individual expense VAT rates in `quarterly_tax_entries` (adding a new column or entry type per rate), which is a larger schema change.
> - **Acknowledged as a known limitation** — validation mismatches on box 28/29 should be interpreted with this caveat.

**2. 100% Proration Assumption**
- **Current Logic:** The code assumes 100% of the VAT paid (IVA soportado) is deductible (`result.box_46_diferencia = round(result.box_03_cuota - result.box_28_iva_soportado, 2)`).
- **Spanish Tax Law:** Some expenses (like vehicles, representation, home office) are only partially deductible (e.g., 50% or 30%).
- **Impact:** If `quarterly_tax_entries` inputs the full IVA paid rather than the deductible portion, the result will be incorrect. This is an operational note: the user must be careful to only input the *deductible* portion of the IVA into the manual entries.

> **Assessment: CORRECT as an operational note. No code change.**
> - This is an input responsibility, not a code bug. The system correctly uses whatever value is entered in `quarterly_tax_entries` for `IVA_SOPORTADO`.
> - **User guidance:** When entering IVA soportado, always enter only the deductible portion (e.g., 50% of vehicle IVA, not 100%).

## Model 349 (Intra-EU Operations)

**1. Handling of Negative Amounts (Refunds)**
- **Current Logic:** `compute_modelo_349` aggregates `_net_amount(row)` for `IVA_EU_B2B`. If a quarter has more refunds than sales for a specific VAT ID, the total could be negative.
- **Spanish Tax Law:** Model 349 does not accept negative amounts. Corrective invoices (refunds) must modify the specific period where the original invoice was declared.
- **Impact:** The code might produce negative totals for a VAT ID in a given quarter, which is invalid for Model 349 submission.
- **Recommendation:** Add logic to handle negative aggregations, either by carrying them back to previous quarters or flagging them for manual review.

> **Assessment: CORRECT. Implemented.**
> - `compute_modelo_349` now excludes VAT IDs with negative totals from the output rows.
> - When a negative total is detected, a warning is added to `Modelo349Result.notes` explaining that corrective invoices must modify the original declaration period.
> - Added `notes` field to `Modelo349Result` dataclass.

## Model 390 (Annual VAT Summary)

**1. Volume of Operations (Box 108)**
- **Current Logic:** `validate_modelo_390` defines `agg_vol_total = base_21 + intracom + export + oss`.
- **Spanish Tax Law:** The volume of operations generally includes all these bases. However, it's worth verifying if any exempt domestic operations are missing from this total if the user ever has them. For the current scope (Coaching/Illustrations/Newsletter), it seems accurate.

> **Assessment: CORRECT for current scope. No change needed.**
> - The current business activities (Coaching, Illustrations, Newsletter) are fully covered by the existing categories.
> - If exempt domestic operations are added in the future, the volume calculation would need updating.

## General Note on Validation Tool

**1. Box 04 of Model 130**
- In `tax_validator.py`, the line `ValidationLine("04", "20% del rendimiento (base pago fraccionado)", v.get("04_veinte_pct"), computed.box_05_base)` explicitly compares the 20% calculation. This will immediately show discrepancies due to the missing 5% "gastos de difícil justificación" deduction mentioned above.

> **Assessment: CORRECT. Resolved by Model 130 fix.**
> - The validator now computes `box_05_base` using the corrected rendimiento neto (after the 5% deduction), so the 20% calculation will match the gestor's filing.
> - Two new validation lines (03b, 03c) have been added to show the 5% deduction transparently.
