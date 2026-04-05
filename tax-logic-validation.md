# Tax Logic Validation Findings

This document outlines the findings from reviewing the automated tax logic in `src/tax_engine.py` and `src/tax_validator.py` against standard Spanish tax rules for Autónomos (Régimen de Estimación Directa Simplificada).

## Model 130 (IRPF Advance)

**1. Missing 5% Provision for "Gastos de Difícil Justificación"**
- **Current Logic:** `compute_modelo_130` computes `box_02_gastos` simply as the sum of manual entries for `GASTOS_DEDUCIBLES`. Then `box_03_rendimiento = box_01_ingresos - box_02_gastos`.
- **Spanish Tax Law:** For Autónomos in *Estimación Directa Simplificada*, the law allows an automatic 5% deduction over the net yield (rendimiento neto previo) as "gastos de difícil justificación", capped at €2,000 per year.
- **Impact:** The computed profit (`box_03_rendimiento`) is higher than it should be, causing a higher 20% advance payment (`box_05_base`). This leads to discrepancies with what a gestor files, as the gestor will automatically apply this 5% deduction.
- **Recommendation:** Update `compute_modelo_130` to subtract `5% * (ingresos - gastos_justificados)` (up to €2,000/year) before calculating the 20% base.

## Model 347 (Third-party operations > €3,005.06)

**1. VAT not included in total operations**
- **Current Logic:** `compute_modelo_347` sums `converted_amount - converted_amount_refunded` (which is the net amount without VAT, or exactly what Stripe processed). If Stripe transactions include VAT (B2C Spanish sales), then it might be okay. But if the system is building this off a `vat_base_eur` and excluding the VAT amount, or if manual expenses are processed without their VAT, it's incorrect.
- **Spanish Tax Law:** The €3,005.06 threshold and the reported amounts in Model 347 **must include VAT** (IVA incluido).
- **Impact:** Some counterparties might not reach the €3,005.06 threshold in the code, whereas they do in reality when VAT is added. The reported amounts will be lower than what the gestor files.
- **Recommendation:** Ensure the aggregation for Model 347 uses the gross amount (Base + IVA) for each transaction, not just the net base.

## Model 303 (Quarterly VAT)

**1. Soportado Base Approximation**
- **Current Logic:** In `compute_modelo_303`, `box_29_base_soportado` is derived by reverse-calculating the 21% rate: `box_28_iva_soportado / 0.21`.
- **Spanish Tax Law:** Deductible expenses can have various VAT rates (4%, 10%, 21%).
- **Impact:** If the user has expenses with reduced VAT rates, the reverse-calculated base (`box_29`) will not match the real base filed by the gestor, causing a mismatch in the validation tool.
- **Recommendation:** The system should track the actual base of deductible expenses instead of reverse-calculating it from the quota.

**2. 100% Proration Assumption**
- **Current Logic:** The code assumes 100% of the VAT paid (IVA soportado) is deductible (`result.box_46_diferencia = round(result.box_03_cuota - result.box_28_iva_soportado, 2)`).
- **Spanish Tax Law:** Some expenses (like vehicles, representation, home office) are only partially deductible (e.g., 50% or 30%).
- **Impact:** If `quarterly_tax_entries` inputs the full IVA paid rather than the deductible portion, the result will be incorrect. This is an operational note: the user must be careful to only input the *deductible* portion of the IVA into the manual entries.

## Model 349 (Intra-EU Operations)

**1. Handling of Negative Amounts (Refunds)**
- **Current Logic:** `compute_modelo_349` aggregates `_net_amount(row)` for `IVA_EU_B2B`. If a quarter has more refunds than sales for a specific VAT ID, the total could be negative.
- **Spanish Tax Law:** Model 349 does not accept negative amounts. Corrective invoices (refunds) must modify the specific period where the original invoice was declared.
- **Impact:** The code might produce negative totals for a VAT ID in a given quarter, which is invalid for Model 349 submission.
- **Recommendation:** Add logic to handle negative aggregations, either by carrying them back to previous quarters or flagging them for manual review.

## Model 390 (Annual VAT Summary)

**1. Volume of Operations (Box 108)**
- **Current Logic:** `validate_modelo_390` defines `agg_vol_total = base_21 + intracom + export + oss`.
- **Spanish Tax Law:** The volume of operations generally includes all these bases. However, it's worth verifying if any exempt domestic operations are missing from this total if the user ever has them. For the current scope (Coaching/Illustrations/Newsletter), it seems accurate.

## General Note on Validation Tool

**1. Box 04 of Model 130**
- In `tax_validator.py`, the line `ValidationLine("04", "20% del rendimiento (base pago fraccionado)", v.get("04_veinte_pct"), computed.box_05_base)` explicitly compares the 20% calculation. This will immediately show discrepancies due to the missing 5% "gastos de difícil justificación" deduction mentioned above.
