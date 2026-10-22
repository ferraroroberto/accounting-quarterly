# Tax Audit Trail

The **Tax Audit** tab makes every calculated cell in every tax model fully inspectable. After running **Calculate Tax**, open this tab to see exactly how each figure was derived.

## How it works

Each time **Calculate Tax** runs, the engine writes one `AuditEntry` per cell to the `tax_audit_log` SQLite table alongside the usual snapshot. Entries are keyed by `(year, quarter, model, computed_at)` — re-running always replaces the previous entries for the same period.

## What is audited

| Model | Cells audited |
|-------|--------------|
| **Modelo 303** | one entry per AEAT box (`c01_base` … `c73_a_devolver`, with the contributing records on the base boxes), plus `oss_base` and `exempt_base` |
| **Modelo 130** | one entry per AEAT box (`c01_ingresos` … `c19_resultado`, with the records behind 01, 02 and 06 and the quarter chain behind 05/15), plus the box 02 split: `c02_gastos_reales`, `c02_gastos_dificil_justificacion` (with cap flag), `c02_amortizaciones` (per-asset breakdown), `c02_capital_assets_excluded` |
| **Modelo 349** | one entry per operator line (`op_<KEY>_<VATID>`, with the invoices / charges summed; `excluded_…` / `unidentified_…` for lines not declared) + `c01_operadores`, `c02_importe` |
| **OSS** | base + cuota per country + totals |
| **Modelo 347** | one entry per counterparty above threshold + summary |

## Per-cell detail

Each entry records:
- **Formula** — the rule applied (e.g. `"min(5% × max(0, 01 − gastos_reales), 2000) [art. 30.2.4ª LIRPF]"`)
- **Inputs** — named JSON dict of all values that fed the calculation (e.g. `{"base": 18400.00, "rate": 0.05, "cap_eur": 2000.0, "cap_applied": false}`)
- **Records** — the individual transactions and invoices included in the figure (date, counterparty, description, amounts), shown as a full DataFrame in the drill-down
- **Value** — the resulting EUR figure

The UI shows a summary table plus an expandable drill-down per cell. Each expander header shows how many records contribute to that figure. Results can be downloaded as JSON.

## Known approximations (documented in audit)

| Cell | Approximation | Impact |
|------|--------------|--------|
| `c28_base` (M303) | Manual `IVA_SOPORTADO` entries without a VAT rate add their cuota to 29 but nothing to 28 | Flagged in the 303 notes — re-enter them with the rate |
| `c01_ingresos` (M130) | Ex-VAT base extracted from VAT-inclusive Stripe amounts | Correct for estimación directa — IVA is a pass-through, not income |
