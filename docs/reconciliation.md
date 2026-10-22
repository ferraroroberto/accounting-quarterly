# Reconciliation

The **Reconciliation** tab lines up every box of a filed AEAT return against the value the app computes, so each difference is either fixed or explained once and then recognised automatically.

## How it works

1. Pick a model (303, 130, 349, 390) and a period; the picker defaults to the latest filed period and lists the filed periods on record.
2. Filed values come from the **AEAT receipts imported into the database** (tables `filed_returns` / `filed_349_operators`, see below). `tmp/validation/validation.yaml` (gitignored — never committed) is a fallback, used only for periods whose receipt has not been imported. On an imported return a blank box counts as 0; a YAML filing only knows the boxes it lists.
3. App values come from `src/reconciliation.reconcile(model, year, quarter, conn, config)`: it computes the period's engine result and reads its `aeat_boxes()` (keyed by the box number printed on the form). The Modelo 390 comes from its own engine (`src/modelo_390.py`, see [Annual Pack](annual-pack.md)), built from the year's four 303 results; its drill-down shows the live 390 audit cells.
4. One row per box in the union of both sides: filed / app / diff (**app − filed**) / status / tag / explanation. The Modelo 349 also gets one row per operator, keyed `op:<VATID>:<KEY>` (country prefix + number, operation key) and summed per operator on each side.

| Status | Meaning |
|--------|---------|
| ✅ `exact` | Filed and app agree within €0.01 |
| 🟡 `catalogued` | They differ and a divergence-catalogue entry explains the difference |
| 🔴 `uncatalogued` | They differ and nothing explains it |
| ⚪ `missing` | One side is unavailable: no filed return for the period, or the app does not compute that box |

With no filed return for the period the tab shows guidance and the app's own figures (all ⚪) instead of failing.

- **Drill-down:** pick an app box to see the audit cells behind it — formula, inputs and records — from the latest `tax_audit_log` run for the period (written by **Calculate tax**), or from the live computation when no run is stored.
- **Export:** **⬇️ Download table as markdown** writes the table (with status counts) for a private reconciliation note; `src/reconciliation.to_markdown` is the same function.

## Divergence catalogue (`divergences.json`)

A git-ignored JSON file at the repo root (`divergences.json.example` ships fake entries). Edit it in the tab's **📒 Divergence catalogue** editor — **💾 Save catalogue** validates every row and writes nothing if one is invalid — or by hand:

```json
{"divergences": [
  {"model": "303", "year": 2026, "quarter": 1, "box": "09",
   "expected_delta": 12.34, "rule": null, "tolerance": 0.01,
   "category": "gestor_error", "explanation": "Why the filed value differs"}
]}
```

| Field | Rule |
|-------|------|
| `model` | `303`, `130`, `349` or `390` |
| `year` / `quarter` | The period; `null` = any year / any quarter (390 is annual: quarter must be `null`) |
| `box` | Box as printed (`"07"`, `"110"`; `"7"` is normalised to `"07"`), or a 349 operator key `op:<VATID>:<KEY>` |
| `expected_delta` | Expected app − filed, matched within `tolerance` (default 0.01) |
| `rule` | Instead of a delta: `app_gte_filed`, `app_lte_filed` or `any` — give exactly one of the two |
| `category` | `gestor_error`, `convention` or `app_choice` (shown as the row's tag) |
| `explanation` | Required free text |

A differing box that matches an entry for its model, period and box shows 🟡 with the entry's tag and explanation; the first matching entry wins. A ⚪ row is never catalogued.

`src/tax_validator.py` is only the loader of the filed returns (`load_filings` / `find_filing`: imported receipts first, `validation.yaml` as the fallback); the comparison itself lives in `src/reconciliation.py`.

## Importing filed AEAT receipts

Download the official receipt PDF of each presentation (Modelo 303, 130, 349 and the annual 390) from the AEAT Sede or BILOOP — the gestor does not email them — and import them:

```bash
# Windows — folders are scanned recursively; non-receipt PDFs and unsupported models are skipped
.\.venv\Scripts\python.exe -m src.filed_returns import <folder-or-pdf> [...]
```

It prints one line per file (`imported`, `replaced`, `unchanged`, `superseded` or `skipped`). The same can be done from the **📥 Import filed AEAT receipts** expander at the top of the Reconciliation tab.

- **What is read:** header (model, fiscal year, period, justificante, CSV, presentation timestamp, presenter) and every non-empty box — 303 pages 2–4 (including 60, 64–72 and 110/78/87), 130 boxes 01–19, 349 summary boxes plus every operator row, and the 390 boxes. The 303 "Tipo %" boxes are pre-printed rates and are not stored.
- **How:** `pdfplumber` word coordinates; each amount is paired with the nearest box number to its left on the same row (±7 pt). Text printed in the form-template font (e.g. the 130's "máximo 660,14 euros" note) is ignored. `pdftotext -layout` is deliberately not used — it misaligns the 303 rows.
- **Idempotent:** re-importing a receipt already stored (same justificante) is a no-op. A different receipt for the same model/year/period replaces the stored one if it was presented later (rectificativa) and is ignored if older.
- **Privacy:** receipts contain the taxpayer's and the presenter's NIF. They stay local (the database is git-ignored); never commit a receipt or anything derived from one — tests use synthetic PDFs only.

## Adding a new filing period by hand (fallback)

Uncomment and fill in the appropriate template block in `tmp/validation/validation.yaml`. No code changes are required — the tab reads all entries dynamically.

```yaml
- model: "130"
  year: 2026
  quarter: 1
  filed_date: "2026-04-20"
  result: 0.00
  values:
    "01_ingresos_ytd": 0.00
    # ... (copy from gestor PDF)
```
