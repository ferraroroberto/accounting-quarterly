# Fixed Assets

Durable purchases are depreciated instead of expensed (`src/fixed_assets.py`, **Fixed Assets** tab).

**Creating assets.** In the **Invoice Ledger** tab, pick an expense invoice and open **Register as fixed asset**: the form is prefilled from the invoice (base, VAT, invoice date, IRPF/VAT business-use %, class) and saving flags the invoice `is_capital_asset`, so the Modelo 130 stops expensing it. One asset row is one unit — an invoice with several units gets one asset per unit. Assets can also be added by hand in the Fixed Assets tab, and edited in its grid (changing the class resets the coefficient to the class maximum). Deleting the last asset of an invoice unflags the invoice.

**Depreciation (IRPF).** *Tabla de amortizaciones simplificada* of *estimación directa simplificada* — Orden de 27 de marzo de 1998 (BOE 28/03/1998), art. 30 RIRPF, checked on 2026-09-28 against the AEAT IRPF 2025 practical manual:

| Class key | Group | Max coefficient | Max period |
|-----------|-------|-----------------|------------|
| `buildings` | Edificios y otras construcciones | 3% | 68 y |
| `installations` | Instalaciones, mobiliario y enseres | 10% | 20 y |
| `machinery` | Maquinaria | 12% | 18 y |
| `vehicles` | Elementos de transporte | 16% | 14 y |
| `it_equipment` | Equipos para tratamiento de la información y sistemas y programas informáticos | 26% | 10 y |
| `tools` | Útiles y herramientas | 30% | 8 y |
| `other` | Resto del inmovilizado material | 10% | 20 y |

- Charge = base × business-use % × coefficient × days in use ÷ days in the year (365; 366 in a leap year, so a full year is exactly the coefficient), from `start_of_use` (default: acquisition date) until the day before `disposal_date`. The coefficient defaults to the class maximum and can be lowered, never raised.
- Cumulative depreciation is capped at base × business-use %.
- Unit bases at or below `assets.threshold_eur` (default 300) are expensed in full in the quarter they are acquired.
- `assets.posting_mode`: `annual_q4` (default) books the full year's depreciation in Q4 (Q1–Q3 YTD carry none); `quarterly` books each quarter's days.
- The Modelo 130 adds `depreciation_for_period(year, quarter, conn, ytd=True, config=...)` to box 02; the Tax Audit tab shows it as `amortizaciones` with a per-asset breakdown, plus `capital_assets_excluded` (flagged invoices removed from expenses; any flagged invoice without a registered asset is listed there and logged as a warning).

**VAT capital goods.** An asset whose unit base is above €3,005.06 is a *bien de inversión* (art. 108 LIVA; auto-detected, overridable). `capital_goods_vat_for_period` gives its Modelo 303 boxes **30/31** in the quarter of acquisition: base × VAT business-use %, and VAT × VAT business-use % (or the `vat_deducted_eur` override). The **regularisation register** covers the year of acquisition + 4: record the VAT business-use % actually applied each year, and a year whose % differs from the acquisition year's by more than 10 points gets an adjustment of VAT borne ÷ 5 × (% of the year − initial %) (arts. 107–109 LIVA), for 303 box 43 (*regularización bienes de inversión*) at Q4. The 303 takes boxes 30/31 from this register and leaves the capital-good share of the linked invoice out of 28/29. The one-off disposal adjustment of art. 110 LIVA is not computed.

Configuration (`config.json`):

```json
{
  "assets": {
    "threshold_eur": 300,
    "posting_mode": "annual_q4"
  }
}
```

Storage: `fixed_assets` (one row per unit) and `fixed_asset_vat_usage` (asset, year, % used), created on first use.
