# Tax conventions

The durable reference for **every tax rule the app applies** when it computes the Spanish quarterly returns of a self-employed person (*autónomo*) in *estimación directa simplificada* and the VAT general regime: Modelo 303 (VAT), 130 (IRPF advance payment) and 349 (intra-EU recapitulative statement). For each rule it gives:

- **Rule**: what the engine does, and the config key or ledger column that controls it;
- **Legal basis**: the article, order or AEAT manual it rests on;
- **External accountant**: where a *gestor* or *asesor* commonly does it differently. Each such difference, once confirmed in the Reconciliation tab, belongs in `divergences.json` with the category `convention` (a legitimate alternative), `gestor_error` (the filed figure is wrong) or `app_choice` (a deliberate app position).

How to *run* a quarter, including filing on the AEAT Sede, is in [quarter-close-runbook.md](quarter-close-runbook.md). Box-by-box mechanics (field names, audit cells) are in [tax-engine-reference.md](tax-engine-reference.md): [Modelo 303 box model](tax-engine-reference.md#modelo-303-box-model), [Modelo 130 box model](tax-engine-reference.md#modelo-130-box-model) and [Modelo 349 operators](tax-engine-reference.md#modelo-349-operators). The annual returns (390, 347 purchases, P&L per activity) belong to the annual pack (#103) and are not covered here.

> This is a working record of the positions the app takes, not tax advice. The law changes: re-check a rule against its source when a figure looks surprising, and before relying on it for a new situation.

Abbreviations: **LIVA** Ley 37/1992 del IVA · **RIVA** RD 1624/1992 · **LIRPF** Ley 35/2006 del IRPF · **RIRPF** RD 439/2007 · **LIS** Ley 27/2014 del Impuesto sobre Sociedades · **LGT** Ley 58/2003 General Tributaria. Links are under [Sources](#sources).

---

## 1. Which quarter a record belongs to

**Rule.** Invoices (received and issued) are assigned to a quarter by `invoice_date`; `supply_date` is informational. Stripe charges count by their charge date. RETA (Social Security) contributions count by payment date. Rows with `excluded = 1` (duplicate, receipt, personal, other period, superseded) are ignored by every model.

**Legal basis.**
- Input VAT: the right to deduct is exercised in the return of the period in which the invoice is received, or in later ones within four years (art. 99.Tres LIVA). Booking a received invoice in the quarter of its date is therefore always admissible.
- Output VAT accrues when the service is performed (art. 75.Uno.2º LIVA). A B2B invoice may be issued up to the 16th of the month after the supply (art. 11 RD 1619/2012, invoicing regulation), so an invoice dated early in a quarter can cover a supply of the previous one.
- IRPF: income and expenses are allocated on the accrual basis of the corporate-tax rules (art. 14.1.b LIRPF); art. 7.2 RIRPF lets activities in *estimación directa* opt for the cash basis (*criterio de cobros y pagos*).

**External accountant.** Most book on the invoice date too, which is why the app does. Where a sale invoiced in quarter N was supplied in quarter N−1, a strict reading puts its output VAT in N−1; the app does not do that. Vendors that invoice a monthly fee in the following month push the last month of each quarter into the next one: this is expected, not a missing invoice.

### 1.1 Activity start date (a floor on every range above)

**Rule.** `tax.activity_start_date` (ISO date, absent by default) is a lower bound on the date ranges above: a Stripe charge or invoice dated **before** it is excluded from every model (303, 130, 349, 390, 347), and the 303 box 110 credit chain and the 130 boxes 05/15 chain never reach back before it — a period that ends before the start date gets `c110_source = "none"` / `c05_source = "none"` instead of chaining into pre-activity data. A record dated on the start date itself is counted. An audit note on the 303 and 130 gives the count and total of what was excluded. Absent the key, every range keeps its natural bound (unchanged behaviour, issue #133).

**Legal basis.** A return only covers the period in which the taxable person carried on the activity; nothing accrues before *alta* in the Censo de Empresarios (Modelo 036/037) and the corresponding IAE registration (art. 5 LIVA, art. 27.1 LIRPF activities begin on the date declared to the AEAT).

**External accountant.** A few Stripe test charges or a pre-registration invoice sometimes linger in the data from before the *alta*; the accountant's own working papers exclude them by hand. Setting this key does the same thing consistently, without editing or deleting the underlying rows.

---

## 2. Output VAT (sales)

The treatment of a Stripe charge is derived from activity × geography × customer VAT id by `src/vat_rules.py`; issued invoices carry their own `tax_treatment` (`ES_21`, `EU_B2C_ES21`, `EU_B2B`, `NON_EU_NOT_SUBJECT`, `EXEMPT_TEACHING`).

### 2.1 Spanish customers: 21% (boxes 01–09)

**Rule.** `IVA_ES_21` / `ES_21`: 21% (an issued invoice at 4% or 10% goes to rows 01/03 or 04/06 by its rate). Stripe amounts are VAT-inclusive, so the base is `gross ÷ 1.21`: €121 charged → €100 base + €21 VAT.

**Legal basis.** Services to a consumer are located where the supplier is established (art. 69.Uno.2º LIVA); services to a Spanish business are located in Spain (art. 69.Uno.1º). General rate: art. 90 LIVA.

### 2.2 EU consumers: Spanish 21% below €10,000, not OSS (boxes 07/09)

**Rule.** A sale to a customer in another EU member state with **no VAT id on file** is B2C. With `tax.oss_registered: false` (the default) it is `EU_B2C_ES21`: Spanish 21%, in boxes 07/09 with the domestic sales. A `default_vat_treatment_eu_<activity>` of `OSS_EU` is coerced to `EU_B2C_ES21` unless `oss_registered` is `true`. The EU B2C threshold tracker (Tax Obligations → EU B2C / OSS, and `close_quarter.py stripe`) sums the year's EU B2C sales excluding VAT against €10,000, warns at 80% and also checks the previous year.

**Legal basis.** Telecommunication, broadcasting and electronically supplied services to EU consumers are located in the customer's member state (art. 70.Uno.4º LIVA), *except* while the supplier's total cross-border EU B2C sales of those services and of distance sales of goods stay at or below €10,000 (excluding VAT) in the previous year **and** in the current one; then they stay located in Spain (art. 73 LIVA). The threshold is EU-wide, not per country. The supplier may opt for destination taxation below it. Above it, or after opting, the supplier either registers for the One-Stop-Shop Union scheme (Modelo 035 to enrol, quarterly Modelo 369 return) or registers for VAT in each customer country. Services that are *not* electronically supplied (for example live one-to-one sessions) are located in Spain for a consumer under the general rule (art. 69.Uno.2º), whatever the threshold.

**External accountant.** May tax EU consumers at 21% in some quarters and not in others, or leave a few small EU consumer sales out of box 07. The app is consistent; small differences in 07/09 from this are usually `gestor_error`. Once the threshold is crossed during a year, the sales **after** the crossing are destination-taxed: the app only warns, it does not switch treatment itself. Set `oss_registered: true` (and file the 035) when that happens.

### 2.3 EU businesses: reverse charge only with a VAT id (box 59, Modelo 349 key S)

**Rule.** A sale to an EU (non-Spanish) customer is B2B (`IVA_EU_B2B` / `EU_B2B`: no Spanish VAT, box 59, Modelo 349 key `S`) **only** when the customer's VAT id is known: from `classification_rules.json → customer_vat_ids` (email or name override), from the invoice's `client_nif`, or from a Stripe `customer.tax_ids` already stored. Without an id the sale is B2C (2.2), whatever the activity. The app does not check the id against VIES.

**Legal basis.** B2B services are located where the business customer is established (art. 69.Uno.1º LIVA), so they are not subject to Spanish VAT. The supplier may treat an EU customer as a business when the customer has given its VAT identification number and the supplier has confirmed its validity, e.g. in VIES (art. 18.1.a Council Implementing Regulation (EU) 282/2011). Box 59 is labelled "Entregas intracomunitarias de bienes y servicios" (intra-EU supplies of goods, exempt under art. 25 LIVA, which also require the buyer's VAT id; and services to EU businesses). The Modelo 349 declares them under key `S` (Orden EHA/769/2010).

**External accountant.** May treat every EU sale of a given activity as B2B regardless of the customer's status, or put EU B2B services in box 120 rather than 59. The money effect is nil for 59 vs 120, but a sale booked B2B without a verified id moves VAT. Check the id in VIES and keep the screenshot with the invoice.

### 2.4 Non-EU customers: not subject by location rules (box 120)

**Rule.** Stripe `OUTSIDE_EU` charges (`IVA_EXPORT`) and issued invoices `NON_EU_NOT_SUBJECT` carry no VAT and go to the informational box 120 ("operaciones no sujetas por reglas de localización"). Box 60 (exports) is reserved for exports of goods and stays 0.

**Legal basis.** B2B: located where the customer is (art. 69.Uno.1º LIVA). B2C with a customer established outside the EU: art. 69.Dos LIVA takes a list of services out of Spanish VAT, among them copyright and licence transfers, consulting-type services and electronically supplied services. A service **not** on that list, supplied to a non-EU consumer, stays located in Spain and would carry 21%. The app assumes every non-EU sale is on the list; check new kinds of sale against art. 69.Dos.

**External accountant.** Often puts non-EU service invoices in box 60 and leaves non-EU Stripe sales out of the informational boxes altogether. Both are informational only, with no money effect: catalogue them as `convention`. Note that box 120 counts in the pro-rata numerator (section 4) either way.

### 2.5 Exempt teaching (no box; pro-rata denominator only)

**Rule.** Issued invoices with `tax_treatment = EXEMPT_TEACHING` carry no VAT and appear in no 303 box. Their base (`exempt_base` in the audit) is an operation **without** the right to deduct, so it enters the pro-rata denominator (section 4). Also counted in Modelo 130 box 01 like any income.

**Legal basis.** Art. 20.Uno.9º LIVA exempts education (including professional training) provided by public bodies or by private entities authorised for it. Art. 20.Uno.10º exempts private classes given by individuals on subjects in the curricula of the education system. The code labels the treatment "art. 20.1.9º". Which paragraph, if any, covers a given engagement is a matter of fact: who provides the teaching, whether the subject is in an official curriculum, whether the class is given on one's own account.

**External accountant.** May consider the class taxable at 21% (then invoices must be corrected and the pro-rata disappears), or may accept the exemption but ignore its pro-rata effect. The exemption is a decision to take once, per kind of engagement, and record in the ledger.

---

## 3. Input VAT (purchases)

Expense invoices carry a `tax_treatment`: `DOMESTIC`, `DOMESTIC_CAPITAL`, `INTRA_EU_RC`, `NON_EU_RC`, `NO_VAT` or `NOT_DEDUCTIBLE`. The [vendor registry](../README.md#vendor-registry) (`vendors.json`) sets the default per vendor; a ledger edit wins and is locked.

### 3.1 Domestic purchases (boxes 28/29)

**Rule.** `DOMESTIC`: base × `deductible_pct_vat` into 28 and VAT × `deductible_pct_vat` into 29 (the capital-good share of an invoice goes to 30/31 instead, section 5). A foreign vendor that charges Spanish VAT on its invoice is `DOMESTIC` too. `NO_VAT` (bank fees, exempt services) and `NOT_DEDUCTIBLE` contribute nothing. Manual `IVA_SOPORTADO` entries add their cuota to 29 and, when they carry a VAT rate, the derived base to 28.

**Legal basis.** Arts. 92–99 LIVA; the deduction needs the invoice (art. 97 LIVA) and use in the activity (art. 95, section 3.4).

### 3.2 Services bought from EU businesses: reverse charge (boxes 10/11 + 36/37, Modelo 349 key I)

**Rule.** `INTRA_EU_RC`: the buyer self-assesses Spanish VAT. Base and VAT at the invoice's Spanish rate (21% when the invoice shows a foreign rate or none) go to the accrued boxes 10/11 **in full**, and the same amounts × `deductible_pct_vat` × the provisional pro-rata go to the deductible boxes 36/37. Each vendor VAT id gets a Modelo 349 line with key `I`; lines without a VAT id or with a zero or negative quarter total are listed apart with a warning, not declared.

**Legal basis.** A service supplied to a Spanish business is located in Spain (art. 69.Uno.1º LIVA) and the recipient is the taxable person when the supplier is not established in Spain (art. 84.Uno.2º.a LIVA). Box 10/11 is "adquisiciones intracomunitarias de bienes y servicios"; its deduction is 36/37. Key `I` of the Modelo 349 is for intra-EU acquisitions of services located in Spain (Orden EHA/769/2010).

**External accountant.** Usually does this, but may put an EU vendor's invoice in 12/13 instead of 10/11, or leave it off the 349. Both are VAT-neutral at 100% deduction; the missing 349 line is still an inaccurate information return (penalty regime of art. 198/199 LGT). Catalogue as `gestor_error`.

### 3.3 Services bought from non-EU businesses: reverse charge (boxes 12/13 + 28/29)

**Rule.** `NON_EU_RC` (a non-EU vendor charging no VAT, e.g. a US software subscription): base and self-assessed VAT go to 12/13 ("otras operaciones con inversión del sujeto pasivo"), and are deducted in 28/29 × `deductible_pct_vat` × the provisional pro-rata. No 349 line.

The Stripe platform (application) fees that connected platforms keep from the charges (section 8) follow the same rule: those platforms are established outside the EU and charge no VAT. The quarter's `fee_application` total, for the same classified charges as the sales and by charge date, goes to 12 with 21% in 13, and is deducted in 28/29 at 100% × the provisional pro-rata (audit source `platform_fee`, one record per charge; the 390 puts it in 27/28 and the 21% deductible row). A charge with an unknown fee split is counted in the result notes, never estimated. `tax.platform_fee_vat_treatment: NONE` leaves the fees out of the 303 (default `NON_EU_RC`), e.g. to mirror an accountant during a shadow run.

**Legal basis.** Same location rule (art. 69.Uno.1º LIVA) and reverse charge (art. 84.Uno.2º.a LIVA). The AEAT form puts the deduction of these operations with the current domestic ones (28/29).

**External accountant.** Frequently skips the self-assessment because it is VAT-neutral. It is only neutral while the deduction is 100%: with a pro-rata below 100% (section 4) or a business-use share below 100%, the self-assessed VAT becomes partly a cost, and skipping it under-declares. Catalogue a skipped self-assessment as `convention` while neutral, and correct it otherwise.

### 3.4 Business-use share (VAT and IRPF separately)

**Rule.** Every expense invoice has `deductible_pct_vat` (303) and `deductible_pct_irpf` (130), default 100, set per vendor in the registry or per invoice in the ledger. Home-office utilities (water, electricity, gas, phone, internet of the home where the activity is carried on) use for IRPF the share of the home's floor area used for the activity × 30%: an office of 15 m² in a 100 m² home gives 15% × 30% = 4.5%.

**Legal basis.**
- VAT: input VAT is deductible only on goods and services used "directa y exclusivamente" in the activity (art. 95.Uno LIVA). For **capital goods** partial use is allowed in the proportion foreseeably used for the activity (art. 95.Tres.1ª; cars and motorcycles are presumed 50%, 95.Tres.2ª), regularised when the real use differs (95.Tres.3ª). For current (non-capital) goods and services, a partial percentage is not provided by the text of art. 95; administrative doctrine and case law have varied, notably for utilities of a home office.
- IRPF: only the parts of an asset that can be used separately can be partially assigned to the activity; an indivisible asset can never be partially assigned (art. 22.3 RIRPF), and private use that is merely incidental (outside working hours) does not break the assignment (art. 29.2 LIRPF, art. 22.4 RIRPF). Home-office utilities: art. 30.2.5ª.b LIRPF (30% of the area share, unless a higher share is proven).

**External accountant.** May deduct 100% of mixed-use devices (a phone, a computer) or 0% of a home-office utility's VAT. For an indivisible asset used privately beyond incidental use, the strict IRPF position is 0%, not a share. Record the position you take per item, with the reason, in the ledger notes.

---

## 4. VAT pro-rata

**Rule.** `tax.prorrata.enabled` (default `true`) applies the general pro-rata once the taxpayer has exempt operations without the right to deduct (`EXEMPT_TEACHING`).
- During a year every deductible box (29, 31, 37 and their bases) is multiplied by the **provisional %**: the previous year's definitive % from `tax.prorrata.definitive_pct_by_year[<year−1>]`, else the legacy `tax.vat_proration_percentage` when it is not 100, else the previous year's definitive % computed from the app's data, else 100.
- In Q4 the engine computes the year's **definitive %**: operations with the right to deduct (boxes 01/04/07 bases + 59 + 60 + 120 + the OSS base) over those plus the exempt ones, **rounded up** to the next whole number. Box 44 = (definitive − provisional) × the whole year's deductible VAT at 100%.
- Record the definitive % under `tax.prorrata.definitive_pct_by_year` once the Q4 303 is filed: it becomes the next year's provisional %.
- `c46_sin_prorrata` (audit and reconciliation) shows box 46 at 100% deduction, for comparing with a filing that ignores the pro-rata.

**Legal basis.** Arts. 102–106 LIVA. General pro-rata when both kinds of operation exist (art. 102–103). Numerator and denominator per calendar year, including operations located abroad that would give the right to deduct if made in Spain (arts. 94.Uno.2º and 104.Dos); the result "se redondeará en la unidad superior" (art. 104.Dos). The provisional % of a year is the previous year's definitive one (art. 105.Uno); a different provisional % can be requested when circumstances change significantly (art. 105.Dos); the definitive % is computed and the year regularised in the last return of the year (art. 105.Cuatro). The special pro-rata (art. 103.Dos, 106) is **not** modelled.

**External accountant.** In the first year with exempt operations the provisional % is still the previous year's (often 100%), so Q1–Q3 may match an accountant who ignores the pro-rata altogether; the difference shows up in the Q4 box 44, which such an accountant would leave at 0. Catalogue that as `gestor_error` if the exemption stands.

---

## 5. Capital goods and fixed assets

### 5.1 VAT: capital goods (boxes 30/31, regularisation in box 43)

**Rule.** A fixed asset whose **unit** base is above €3,005.06 is a VAT capital good (`vat_capital_good`, auto-detected, overridable). In the quarter of acquisition its base × VAT business-use % and its VAT × VAT business-use % (or the `vat_deducted_eur` override) go to boxes 30/31, and the linked invoice's capital share is left out of 28/29. An expense invoice flagged `DOMESTIC_CAPITAL` without a registered asset goes to 30/31 straight from the invoice, with a note. The regularisation register covers the year of acquisition plus four: record the VAT business-use % actually applied each year (`fixed_asset_vat_usage`); a year that differs from the acquisition year by more than 10 points gets VAT ÷ 5 × (that year's % − initial %) in box 43 of its Q4 303. The one-off adjustment on disposal (art. 110 LIVA) is not computed.

**Legal basis.** Capital goods: tangible goods normally used for more than a year, excluding those whose acquisition value is at most €3,005.06 (500,000 pesetas, art. 108 LIVA). Regularisation over the four years after acquisition when the deduction % differs by more than ten points (arts. 107–109 LIVA), applied to changes in the degree of business use by art. 95.Tres.3ª LIVA.

**External accountant.** May deduct the whole VAT of a mixed-use capital good in 28/29. That is two differences: the box (30/31, not 28/29) and the amount (business-use share only). The second changes the result; catalogue it as `gestor_error`.

### 5.2 IRPF: depreciation and the €300 threshold (inside box 02)

**Rule.** Assets are registered per unit in the Fixed Assets tab (from a ledger invoice: **Register as fixed asset**, which flags the invoice `is_capital_asset` so it is no longer expensed). Units at or below `assets.threshold_eur` (default €300) are expensed in full in their quarter. Above it, straight-line depreciation with the *tabla de amortizaciones simplificada*: base × IRPF business-use % × coefficient × days in use ÷ days in the year, capped at base × business-use %. The coefficient defaults to the class maximum and may be lowered, never raised. `assets.posting_mode`: `annual_q4` (default, the whole year's charge in the Q4 130) or `quarterly`. Only registered assets are depreciated: an invoice that is not registered is expensed in full in its quarter, whatever its amount. Registering is required for VAT capital goods (section 5.1) and optional below that; expensing a device above €300 instead of depreciating it is a choice some owners and accountants make for simplicity, at the cost of a timing difference against the strict rule.

| Class | Group | Max coefficient | Max period |
|---|---|---|---|
| `buildings` | Edificios y otras construcciones | 3% | 68 years |
| `installations` | Instalaciones, mobiliario y enseres | 10% | 20 years |
| `machinery` | Maquinaria | 12% | 18 years |
| `vehicles` | Elementos de transporte | 16% | 14 years |
| `it_equipment` | Equipos para tratamiento de la información y sistemas y programas informáticos | 26% | 10 years |
| `tools` | Útiles y herramientas | 30% | 8 years |
| `other` | Resto del inmovilizado material | 10% | 20 years |

**Legal basis.** Simplified table: Orden de 27 de marzo de 1998, applicable through art. 30.1ª RIRPF (straight-line, on the simplified table). Low-value assets: new tangible fixed assets with a unit value of at most €300 can be freely depreciated up to €25,000 a year (art. 12.3.e LIS, applied to IRPF activities through art. 28.1 LIRPF). Partial assignment of assets: section 3.4.

**External accountant.** May expense every device in full in its quarter (typical for laptops and phones), or depreciate only at year end (the app's default too). A device above €300 expensed in full is a timing difference in box 02 across the years of its life: `convention` if the accountant applies another free-depreciation rule, otherwise `gestor_error`.

---

## 6. Foreign currency

**Rule.**
- **Expense invoices:** the EUR actually charged, when the document states it (`charged_eur`); otherwise the original amount at the ECB reference rate of the invoice date (`fx_source` `CHARGED_EUR` / `ECB`). A missing rate is never passed through unconverted (`NO_RATE`); a fallback to a rate more than five days old is flagged `fx_stale`.
- **Income invoices:** the ECB rate on the invoice date, stored at extraction. That figure is final when the money stays in a foreign-currency account. When the amount was converted on receipt, `eur_received` (Invoice Ledger) replaces it in every model.
- **Exchange differences:** when a foreign-currency balance booked at the ECB rate is converted later, the gain or loss (EUR obtained − EUR booked) is recorded in the Invoice Ledger's income view (`fx_exchange_differences`) and added to Modelo 130 box 01 of the quarter of the conversion. VAT figures do not move.
- **Stripe charges:** converted with the stored ECB rate of the charge date. Once a quarter's Stripe report is frozen as declared, its EUR amounts win over any later recomputation.

**Legal basis.** For VAT, an amount fixed in a currency other than the euro is converted at the "tipo de cambio vendedor, fijado por el Banco de España, que esté vigente en el momento del devengo" (art. 79.Once LIVA; wording as reproduced in the AEAT *Manual práctico IVA 2025*). The euro rates the Banco de España publishes are the ECB reference rates, which is what the app stores (via the Frankfurter API). For IRPF, activity income follows the corporate-tax rules (art. 28.1 LIRPF), under which realised exchange differences are financial income or loss of the period in which they arise.

**External accountant.** May book foreign-currency income at the EUR received even when nothing was converted, or at some other date's rate. The difference sits in 130 box 01 and in the informational 303 box (120 or 60); catalogue it as `convention`, with the rate used in the explanation.

---

## 7. Modelo 303: result and credit chain

**Rule.** 27 = accrued cuotas; 45 = 29 + 31 + 37 + 43 + 44; 46 = 27 − 45; 64 = 46; 65 = 100% (common territory); 66 = 64 × 65%.
- **110**, credit pending from earlier periods = the previous quarter's **filed** 87 + 72, read from its imported AEAT receipt. Without a receipt, the app's own previous-quarter result, chained back to a filed quarter or the first period with data.
- **78** = the credit applied = min(110, positive 66); **87** = 110 − 78; 69 = 66 − 78; 71 = 69.
- A negative 71 goes to **72** (to compensate). In Q4, `tax.modelo303_q4_negative_result: refund` puts it in **73** (refund) instead, and 78 may then take the whole of 110.

**Legal basis.** Box layout and formulas: AEAT Modelo 303 form (*Manual práctico IVA 2025*, cap. 9). An excess of deductions can be compensated in later returns within four years of the return in which it arose (art. 99.Cinco LIVA); a refund can be requested in the last return of the year (art. 115 LIVA).

**External accountant.** A credit already declared on a filed return is the operative figure until that return is rectified, even if the app computes a different one for that quarter. That is why the chain starts from the filed receipts: import every filed receipt, including the ones the accountant filed.

---

## 8. Modelo 130: IRPF advance payment

**Rule** (all income and expense figures are cumulative from 1 January to the quarter end):

| Box | What the app puts there |
|---|---|
| 01 | Stripe VAT bases (ex-VAT) + issued invoices **gross** of the IRPF withheld (`eur_received` when set) + exchange differences of the period |
| 02 | Real expenses + the 5% allowance. Real expenses = expense invoices × `deductible_pct_irpf` (excluded rows and capital assets out) + RETA contributions as paid, **net of refunds** + Stripe platform (application) fees + depreciation + manual `GASTOS_DEDUCIBLES` |
| 03 · 04 | 03 = 01 − 02 (may be negative); 04 = 20% of a positive 03 |
| 05 | Σ positive 07 − Σ 16 of the year's earlier quarters |
| 06 | IRPF withheld on issued invoices (`irpf_amount`, exact cents) + manual `RETENCIONES_SOPORTADAS` |
| 07 | 04 − 05 − 06 (may be negative) |
| 12 | max(0, 07 + 11); 08–11 (agriculture) are 0 |
| 13 | Reduction by the previous year's net yield: ≤ €9,000 → 100; ≤ €10,000 → 75; ≤ €11,000 → 50; ≤ €12,000 → 25; above → 0 |
| 14 | 12 − 13 (may be negative) |
| 15 | Only when 14 > 0: negative 19s of the year's earlier quarters not yet used, up to 14 |
| 17 · 19 | 17 = 14 − 15 − 16; 19 = 17 − 18 (16, 18 = 0). A negative 19 is carried to 15 of later quarters of the same year |

- **5% allowance** (*gastos de difícil justificación*): 5% of the positive (01 − real expenses), capped at €2,000 per year, only when `tax.regime` is `estimacion_directa_simplificada`. The audit shows it as `c02_gastos_dificil_justificacion` next to `c02_gastos_reales`.
- **Box 13 source:** the previous year's **filed** Q4 130 box 03 (imported receipt), else `tax.previous_year_net_yield`, else the app's own previous-year figure (no activity counts as 0). It applies even when 12 is 0, leaving a negative 14 that a later quarter uses through 15.
- **Boxes 05 and 15** chain through each earlier quarter's **filed** 130 when its receipt is imported, else the app's own computation (`c05_source`: `filed`, `app_chain`, `mixed`, `none`).
- **Stripe fees:** each charge's balance-transaction fee is stored split in two (`fee_stripe`, `fee_application`, EUR, from `fee_details`). Stripe's own processing fee (every entry other than `application_fee`) is expensed from Stripe's monthly tax invoices in the ledger, so it is **not** added again. The `application_fee` a connected platform keeps (e.g. a newsletter platform's percentage of each subscription) is on no invoice to the taxpayer, so its year-to-date total is added to real expenses by charge date, for the same classified charges as box 01 (audit cell `c02_platform_fees`, one record per charge). Refunds follow the charge's balance transaction: a platform fee the platform did not return stays an expense. A charge fetched before the split was stored has an unknown split: it is counted in the audit and the result notes ("fee split unknown for N Stripe charge(s); re-fetch"), never estimated. Fill it with `close_quarter.py stripe-fetch --backfill-fee-split --from D --to D`. In the 303 the platform fee is self-assessed as a non-EU reverse charge (section 3.3).
- **RETA:** imported from the bank export (`reta` step, Seguridad Social tab). Debits are contributions; credits (e.g. an automatic refund of excess contributions for multiple activity, *pluriactividad*) are stored negative and net off in the period received.

**Legal basis.**
- Advance payment of 20% of the year-to-date net yield, minus earlier payments and withholdings: art. 110.1.a and 110.3 RIRPF.
- Box 13: art. 110.3.c RIRPF.
- Allowance: the 5% is set by art. 30.2ª RIRPF and the €2,000 annual cap by art. 30.2.4ª LIRPF. The cap applies across all the taxpayer's activities.
- Withholdings on professional income: 15%, or 7% in the year the activity starts and the two following (art. 101.5.a LIRPF, art. 95.1 RIRPF). They are payments on account by the client, so the income is the gross amount and the withholding is deducted in box 06.
- Platform fees: an expense necessary to obtain the activity's income (art. 28.1 LIRPF, which applies the corporate-tax rules to the net yield). The supporting document is the Stripe balance transaction and the platform's own statement of fees.
- RETA contributions paid by the business owner are a deductible expense of the activity (AEAT *Manual práctico de Renta*, rendimientos de actividades económicas en estimación directa, gastos fiscalmente deducibles).
- Exemption: a professional does not have to file the 130 if at least 70% of the previous year's activity income was subject to withholding (art. 109 RIRPF).

**External accountant.**
- May book an issued invoice net of the withholding (income too low, withholding lost from 06). That is a `gestor_error`; the invoice's own rounding should also show the exact cents.
- Shows the 5% inside box 02, as the app does since #98.
- May expense only what arrives as an invoice, which leaves the platform fees out of box 02 and overstates the net yield: `gestor_error`. Returns filed before #135 from the app's own figures have the same gap.
- May book a refund of RETA contributions in the year of the contributions instead of the year received; there are views both ways, so catalogue as `convention`.

---

## 9. Modelo 349

**Rule.** One line per (VAT id, key), as the form expects: country, VAT id without the country prefix, name, key, base. Key `I`: expense invoices `INTRA_EU_RC`, grouped by vendor VAT id (the invoice's, else the vendor registry's), named by the registry's `legal_entity`. Key `S`: Stripe `IVA_EU_B2B` charges and income invoices `EU_B2B`, grouped by customer VAT id. Declared lines of key `I` add up to 303 box 10 and key `S` to box 59. Summary boxes: 01 number of operators, 02 total, 03/04 rectifications (always 0; rectifying a line of an earlier period is out of scope).

**Legal basis.** Arts. 78–81 RIVA and Orden EHA/769/2010. Filed only for periods with operations to declare. Quarterly while the intra-EU supplies of goods and services do not exceed €50,000 (excluding VAT) in the quarter or in any of the four previous ones, otherwise monthly (art. 81 RIVA).

**External accountant.** May leave out reverse-charge purchases from small EU vendors. Catalogue each missing operator as `gestor_error` (information return, art. 198/199 LGT).

---

## 10. Informational boxes at a glance

| Box | What the app puts there | Common alternative |
|---|---|---|
| 59 | EU B2B sales (Stripe with VAT id, `EU_B2B` invoices) | 120 |
| 60 | Exports of goods: none today, stays 0 | non-EU service invoices |
| 120 | Non-EU sales (Stripe `OUTSIDE_EU`, `NON_EU_NOT_SUBJECT` invoices) | 60, or nothing for Stripe |
| 123 | OSS base, only when `oss_registered: true` | — |

None of them changes the result; all of them count in the pro-rata numerator.

---

## 11. Filing and direct-debit deadlines

**Rule.** `src/filing_sheet.py` computes, per quarter:
- **Filing** of the 303, 130 and 349: 1–20 of the month after the quarter; Q4: 1–30 January; moved to the next business day on a weekend or holiday.
- **Direct debit** (303 and 130 only; the 349 has no payment): the latest day leaving at least three business days or five calendar days before the end of the filing period, moved back to a business day. That is the 15th for a 20th, and 27 January for 30 January 2026.
- Only national holidays plus Maundy Thursday and Good Friday are built in. Check the AEAT *calendario del contribuyente* each period.

**Legal basis.** 303: art. 71.4 RIVA. 130: art. 111.1 RIRPF. 349: art. 81 RIVA. Direct debit: art. 3 of Orden EHA/1658/2009 as amended by Orden HAC/241/2025 (BOE-A-2025-5048); AEAT "Plazos de presentación de autoliquidaciones con domiciliación bancaria".

The annual calendar (390 by 30 January, 347 in February, the income-tax return from April to 30 June) is part of the annual pack (#103) and of the [runbook](quarter-close-runbook.md#annual-calendar).

---

## 12. Carry-forwards chain from filed returns

**Rule.** Every figure that carries from one period to the next is read from the **imported AEAT receipt** of the earlier period (tables `filed_returns` / `filed_349_operators`), not from the app's recomputation of it:

| Carry | Read from |
|---|---|
| 303 box 110 | Previous quarter's filed 87 + 72 |
| 130 box 05 | Each earlier quarter of the year: filed 07 and 16 |
| 130 box 15 | Each earlier quarter of the year: filed 19 and 15 |
| 130 box 13 | Previous year's filed Q4 box 03 |

Only when a receipt is missing does the app fall back to its own figure, and it says so (`c110_source`, `c05_source`, the audit trail). The FILED snapshot that **Mark filed** stores records what *you* presented; the chain itself reads the receipts.

**Legal basis.** A filed return (*autoliquidación*) stands until it is rectified (art. 120 LGT). Rights and debts derived from it prescribe after four years (art. 66 LGT).

**External accountant.** Not a difference in method, but a consequence: an error in a return the accountant filed carries into the app's later quarters until it is rectified. Catalogue it on the quarter where it arose; don't let it hide in a later box.

---

## Sources

Consolidated legal texts (BOE):
- LIVA, Ley 37/1992: <https://www.boe.es/buscar/act.php?id=BOE-A-1992-28740>
- RIVA, RD 1624/1992: <https://www.boe.es/buscar/act.php?id=BOE-A-1992-28925>
- LIRPF, Ley 35/2006: <https://www.boe.es/buscar/act.php?id=BOE-A-2006-20764>
- RIRPF, RD 439/2007: <https://www.boe.es/buscar/act.php?id=BOE-A-2007-6820>
- LIS, Ley 27/2014: <https://www.boe.es/buscar/act.php?id=BOE-A-2014-12328>
- LGT, Ley 58/2003: <https://www.boe.es/buscar/act.php?id=BOE-A-2003-23186>
- Invoicing regulation, RD 1619/2012: <https://www.boe.es/buscar/act.php?id=BOE-A-2012-14696>
- Orden de 27 de marzo de 1998 (simplified depreciation table): BOE-A-1998-7200
- Orden EHA/769/2010 (Modelo 349): <https://www.boe.es/buscar/act.php?id=BOE-A-2010-5098>
- Orden HAC/241/2025 (direct-debit period): BOE-A-2025-5048
- Council Implementing Regulation (EU) 282/2011: <https://eur-lex.europa.eu/eli/reg_impl/2011/282/oj>

AEAT (sede.agenciatributaria.gob.es): *Manual práctico IVA 2025* (cap. 4 base imponible, cap. 9 Modelo 303 and 349); *Manual práctico de Renta* (rendimientos de actividades económicas en estimación directa); Modelo 130 *Instrucciones*; *calendario del contribuyente*.

**Checked on 2026-09-28** against the article text: LIVA arts. 20.Uno.9º–10º, 69, 73, 79.Once, 84.Uno.2º, 95, 99, 104, 105, 107, 108; RIVA arts. 71.4 and 81; LIRPF art. 30.2 (reglas 4ª and 5ª); RIRPF arts. 22, 30, 109, 110, 111; LIS art. 12.3.e (through the AEAT IRPF manual). Cited from the existing code or from secondary sources without a fresh check of the article text: art. 18 Regulation 282/2011; arts. 25, 75, 90, 94, 97, 115 LIVA; arts. 14.1.b, 28.1, 29.2 and 101.5 LIRPF; arts. 7.2 and 95 RIRPF; arts. 78–80 RIVA; art. 11 RD 1619/2012; arts. 66, 120, 198/199 LGT; the RETA deductibility reference; the 303 box definitions of 59/60/120.
