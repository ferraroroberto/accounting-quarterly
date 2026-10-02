# Quarter-close runbook

Step by step, from the invoices of a finished quarter to filed, paid and archived Modelo 303, 130 and 349 returns. The rules behind every figure are in [tax-conventions.md](tax-conventions.md). The `/close-quarter` Claude Code skill (`.claude/skills/close-quarter/SKILL.md`) drives steps 1–11 of the pipeline interactively. This runbook is the same procedure written out for a human, plus what happens outside the app: filing on the AEAT Sede, paying, and archiving.

Every pipeline command runs from the repo root with the project venv:

```bash
.venv/Scripts/python.exe scripts/close_quarter.py <step> --year Y --quarter Q [options]
```

`--year` / `--quarter` default to the last completed calendar quarter. Every step is idempotent and prints `[step] N change(s)`, then `+` lines (writes), `⚠` lines (review items) and `❌` lines (errors). A re-run with nothing new prints `[step] no changes`; the exit code is 1 only on `❌`. Outputs go to `tmp/close_quarter/<Y>_Q<Q>/` (git-ignored). Nothing is ever sent or uploaded by the script.

---

## Timeline

| When | What |
|---|---|
| Day 1 after the quarter ends | Pre-flight checklist; Stripe and FX steps (they need the full quarter) |
| First week | Ingest and review invoices; RETA import; compute; reconcile the previous quarter |
| By the 10th (while an accountant is engaged) | Send the accountant's pack (step 11) |
| By the direct-debit deadline (the 15th for Q1–Q3) | File 303 and 130 with direct debit; file the 349 |
| By the filing deadline (the 20th; Q4: 30 January) | Last day to file, paying with an NRC |
| Right after filing | Download receipts, **Mark filed**, import the receipts, reconcile, archive |

The filing sheet prints the exact deadlines of the quarter (moved for weekends and holidays).

---

## 0. Pre-flight checklist

- [ ] **Back up the database** before any write. The SQLite backup API works while the app is running:

  ```bash
  .venv/Scripts/python.exe -c "import sqlite3, datetime, pathlib; pathlib.Path('data/backups').mkdir(exist_ok=True); sqlite3.connect('data/accounting.db').backup(sqlite3.connect(f'data/backups/accounting_{datetime.date.today()}.db'))"
  ```

- [ ] **The quarter is over.** The Stripe fetch (step 6) and the FX backfill (step 5) must run after the last day of the quarter, or the last days are missing.
- [ ] **All invoices are in the folders** `invoice_in_dir` / `invoice_out_dir` (`config.json`), including invoices for the quarter's last month that some vendors only issue in the following month: they belong to the quarter of their **invoice date**, which may be the next one.
- [ ] **FX rates are current:** `fx` backfills them. A `⚠ did not reach` line means the backfill failed (network): fix that before trusting any foreign-currency figure. The stand-alone command is `fx-backfill`.
- [ ] **The Social Security bank export** covers the quarter (for step 7).
- [ ] **The previous quarter's receipts are imported** (303, 130 and 349 of the previous quarter, and the previous year's Q4 130 in Q1). They start the credit chain (303 box 110) and the 130 chains (boxes 05, 13, 15). See step 17.
- [ ] **Config for the year** (`config.json → tax`, or Configuration → Tax Settings): `prorrata.definitive_pct_by_year` holds the previous year's definitive %; `previous_year_net_yield` is set if the previous year's Q4 130 receipt cannot be imported; `oss_registered` still matches reality.
- [ ] **The hub is up** for OCR (`http://127.0.0.1:8000`), unless `invoice_ocr.provider` is `gemini`.

---

## 1. Ingest

### Step 1: sweep

```bash
.venv/Scripts/python.exe scripts/close_quarter.py sweep
```

Copies every invoice PDF not yet catalogued in the database nor copied before into the quarter folder (`IN - <vendor> - <file>.pdf`, `OUT - <file>.pdf`) and updates the cumulative manifest `tmp/close_quarter/invoice_copy_log.json`.

### Step 2: ocr

```bash
.venv/Scripts/python.exe scripts/close_quarter.py ocr --dry-run
.venv/Scripts/python.exe scripts/close_quarter.py ocr
```

The dry run lists the pending PDFs (new, or changed since their last extraction by MD5). The real run extracts each through the OCR backend, resolves foreign-currency EUR at the ECB rate and applies the vendor registry. A failing file is a `❌` line; the others still go through and the failed one is retried next run. `--direction in|out|both` limits the folders.

**OCR model.** The hub model defaults to the `gemini_pro` alias, overridden by the `LLM_HUB_MODEL` env var, overridden by `--model`. The hub's default alias is not guaranteed to be served: the hub keeps only its latest models, and a backend can be down. If extraction fails with a model error, list the hub's ids and pass one:

```bash
curl -s http://127.0.0.1:8000/v1/models
.venv/Scripts/python.exe scripts/close_quarter.py ocr --model <hub model id>
```

### Step 3: vendors

```bash
.venv/Scripts/python.exe scripts/close_quarter.py vendors
```

Applies `vendors.json` to the stored expense invoices and lists the quarter's invoices with an unknown vendor (`⚠`). Add each vendor in the app's **Vendors** tab (country, VAT id, default tax treatment, business-use %, activity), then re-run `vendors`.

### Step 4: dedupe

```bash
.venv/Scripts/python.exe scripts/close_quarter.py dedupe
```

Lists duplicate groups (same file, same invoice number, invoice + receipt, email-folder copy) and swept files dated outside the quarter. Nothing is written without `--apply`.

### Review 1: invoices

Before going on, in the app:
- **Invoice Ledger** (expenses, then income, filtered to the quarter): check each row's `tax_treatment`, VAT and IRPF business-use %, capital-asset flag, and for foreign-currency income `eur_received` / `payment_date` when the money was converted on receipt. Register VAT capital goods (unit base above €3,005.06) as fixed assets (**Register as fixed asset**): they need boxes 30/31 and the 5-year regularisation. Below that, registering is optional: a registered asset above €300 is depreciated (the strict IRPF rule); an unregistered one is expensed in full in its quarter, which is simpler and only moves the expense forward in time (see `docs/tax-conventions.md` §5.2).
- Fix extraction errors in the ledger (edited fields are locked against re-OCR).
- Confirm the proposed exclusions, then:

```bash
.venv/Scripts/python.exe scripts/close_quarter.py dedupe --apply
```

### Step 5: fx

```bash
.venv/Scripts/python.exe scripts/close_quarter.py fx
.venv/Scripts/python.exe scripts/close_quarter.py fx --apply
```

Backfills ECB rates to today and lists the quarter's non-EUR invoices whose EUR figure would change (locked rows are skipped). Apply once the list looks right. A converted foreign-currency balance is recorded in the Invoice Ledger's income view (**Exchange rate differences**), not here.

### Step 6: stripe

```bash
.venv/Scripts/python.exe scripts/close_quarter.py stripe-check
.venv/Scripts/python.exe scripts/close_quarter.py stripe
.venv/Scripts/python.exe scripts/close_quarter.py stripe-fetch
```

`stripe-check` is a read-only API smoke test; if it fails, stop and fix the key. `stripe` fetches and stores the quarter's charges, fills billing email/country from the saved raw charges, reclassifies the quarter and prints the warnings: EUR charges that fell to the default geography for a foreign-looking customer, and the EU B2C €10,000 threshold (art. 73 LIVA; `⚠` at 80%). `stripe-fetch` prints the full table; `⚠` rows were classified by a default rule. For each misclassification:

```bash
.venv/Scripts/python.exe scripts/close_quarter.py add-override "<name or email substring>" EU_NOT_SPAIN --type name
.venv/Scripts/python.exe scripts/close_quarter.py stripe
```

Regions are `SPAIN`, `EU_NOT_SPAIN`, `OUTSIDE_EU`. An EU business customer also needs its VAT id under **Configuration → Geographic Rules → Customer VAT IDs**, or it stays B2C. If an override should also fix earlier quarters:

```bash
.venv/Scripts/python.exe scripts/close_quarter.py reclassify --from YYYY-MM-DD --dry-run
.venv/Scripts/python.exe scripts/close_quarter.py reclassify --from YYYY-MM-DD
```

Only reclassify a quarter that has **not** been filed; a filed quarter is corrected with a rectifying return, not by moving its data.

### Step 7: reta

```bash
.venv/Scripts/python.exe scripts/close_quarter.py reta [--file <bank export .xls/.xlsx/.csv>]
```

Without `--file`, the export is read from `social_security.bank_export_file` in `config.json`.

Imports the Social Security (TGSS) debits; column names come from `config.json → social_security`. Rows already stored are skipped. A refund (a credit in the export) is stored negative. `⚠ no RETA payment` means the export does not cover the quarter. A missing month or a refund can be added by hand in the **Seguridad Social** tab.

---

## 2. Compute and review

### Step 8: compute

```bash
.venv/Scripts/python.exe scripts/close_quarter.py compute
```

Computes Modelo 303, 130 and 349 (plus OSS and the annual 347) and stores the snapshots, only when a figure changed. The same as **Calculate tax** in the Tax Obligations tab.

### Review 2: figures

Walk the box summary (and the **Tax Audit** tab for any figure you cannot explain):
- **303:** 07/09 against the quarter's Spanish + EU-consumer sales; 10/11 = 36/37 at 100% deduction; 12/13 non-EU services; 28/29; 30/31 capital goods; the provisional pro-rata % and its source; in Q4, boxes 43 and 44; 110 and its source (`filed` expected).
- **130:** 01 and 02 are year-to-date; the 5% allowance; box 13 and its source; 05 and 15 chains (`filed` expected for the earlier quarters).
- **349:** one line per operator; nothing under *excluded* or *unidentified* (fix the VAT id in the ledger or the vendor registry).

Fix data with the steps above or in the app, then re-run `compute`.

### Step 9: reconcile

```bash
.venv/Scripts/python.exe scripts/close_quarter.py reconcile
```

Before filing, this reconciles the **previous** quarter (its filed receipt against the app), the chain this quarter starts from. It writes `reconciliation_<Y>_Q<Q>.md`. Every `🔴 uncatalogued` box is either an app/data error to fix or a divergence to record in `divergences.json` (Reconciliation tab → **Divergence catalogue**) with its category and explanation. `⚠ no filed Modelo` means the receipt still needs importing (step 17).

### Step 10: sheet

```bash
.venv/Scripts/python.exe scripts/close_quarter.py sheet
```

Writes `filing_sheet_<Y>_Q<Q>.md` from the stored snapshots: the non-zero boxes of each form in form order, the 303 credit chain, each result, the 349 operator list and the deadlines. The **Filing Sheet** tab shows the same, with each value in a copyable block formatted as the Sede expects (`1234,56`). `⚠ run compute first` means a snapshot is missing.

---

## 3. Shadow period: the accountant's pack

While an external accountant still files the returns, the app runs in parallel and its figures are compared with theirs.

### Step 11: gestor-pack

```bash
.venv/Scripts/python.exe scripts/close_quarter.py gestor-pack
.venv/Scripts/python.exe scripts/close_quarter.py gestor-pack --freeze
```

Copies the quarter's invoices into `invoices/` inside the quarter folder: every non-excluded received and issued invoice dated in the quarter, taken from the ledger, so invoices OCR'd before the close are included (the `sweep` copies are only "what's new" and are not the pack). It also writes the Stripe report (`Stripe_Report_Q<Q>_<Y>.xlsx`, reclassified first), `gestor_notes_<Y>_Q<Q>.md` (your own notes from the git-ignored `gestor_notes.md` at the repo root, plus the special treatments found in the ledger) and a draft email `gestor_email_<Y>_Q<Q>.txt`, whose invoice counts are the files in `invoices/`. A ledger row whose PDF is missing on disk is a ⚠ and is left out. Send it yourself, attaching the `invoices/` folder.

Run `--freeze` **only for the version you actually send**: it stores the report as the quarter's immutable declared report, and the tax engine then uses its EUR amounts. A corrected re-send is `report --freeze --supersede`.

A quarter whose report was sent **before freezing existed** is frozen from the sent file itself:

```bash
.venv/Scripts/python.exe scripts/close_quarter.py freeze-sent --year Y --quarter Q --file tmp/close_quarter/<Y>_Q<Q>/Stripe_Report_Q<Q>_<Y>.xlsx
```

It reads the file's `import` sheet and stores its EUR amounts (not the live rows) as the declared report, so non-EUR charges stop drifting with the stored ECB rate and the reconciliation loses that FX noise. A row dated outside the quarter, a duplicate id or an unreadable row aborts it with nothing stored. File ids missing from the live table are listed but frozen anyway, and live charges missing from the file keep their live amounts. Only amounts are frozen; activity and region stay live. `--supersede` works as for `report`.

In a shadow quarter:
1. Run steps 1–10 and keep `filing_sheet_<Y>_Q<Q>.md` as the app's frozen figures **before** the accountant files. Do not **Mark filed**: that records figures *you* presented.
2. When the accountant has filed, download their receipts and import them (step 17).
3. Re-run `reconcile` for the quarter: every difference must be fixed or catalogued as `gestor_error`, `convention` or `app_choice`.

---

## 4. File on the AEAT Sede

On sede.agenciatributaria.gob.es, identify with your electronic certificate or Cl@ve. File in your own name; an accountant who filed for you as *colaborador social* keeps that authorisation until you revoke it, but it is not needed for you to file. Keep the filing sheet open next to the form. Menu labels on the Sede change from time to time; the order of the boxes does not.

General rules for the three forms:
- Type amounts as the sheet's copy column shows them (comma decimal separator, no thousands separator).
- Type only the boxes the form lets you type. The form computes the totals itself (303: 27, 45, 46, 64, 66, 69, 71; 130: 03, 04, 07, 12, 14, 17, 19). Each computed total must equal the sheet: a difference means a box was mistyped.
- Use **Validar** before signing. A warning about box 110 or the 130 carry-overs usually means the AEAT holds a different figure for an earlier period than the imported receipt. Stop and check it; don't type over it.
- If the Sede opens an assistant pre-filled with the AEAT's own data instead of the blank form, compare every pre-filled box against the sheet.

### Step 12: Modelo 303

1. Open the Modelo 303 procedure (Impuestos y tasas → IVA → Modelo 303) and choose **Presentar**, then the web form (**formulario**).
2. **Identification:** fiscal year and period (1T–4T). Answer the questionnaire: régimen general, no cash-basis regime, no special pro-rata, not in SII, not REDEME, not foral. In 4T it also asks whether you are exempt from the annual 390 (the annual pack #103 covers the 390).
3. **Liquidación (page 1):** type the *IVA devengado* boxes (01–13) and the *IVA deducible* boxes (28–44) in the sheet's order.
4. **Información adicional (page 3):** 59, 60, 120, 123.
5. **Resultado:** check 65 = 100; box 110 (credits from earlier periods) against the sheet's credit chain; type 78 (credit applied) if the form asks for it; check 87 (credit left for later) and 71.
6. **Type of return:** payment by direct debit (IBAN of an account in your name) or by NRC (step 15); a negative result is *a compensar* (Q1–Q3) or, in Q4, *a compensar* or *devolución* as the sheet says (72 vs 73); *sin actividad / resultado cero* when 71 is 0.
7. **Firmar y enviar.** Save the receipt (step 16).

### Step 13: Modelo 130

1. Open the Modelo 130 procedure (Impuestos y tasas → IRPF → Modelo 130), **Presentar**, web form.
2. **Identification:** fiscal year and period.
3. **Section I (actividades económicas en estimación directa):** type 01, 02, 05, 06. Leave section II (agriculture, 08–11) empty.
4. **Totals:** type 13 (reduction), 15 (negative results of earlier quarters) and 16 (0); 18 (0 unless a complementary return).
5. **Result 19:** positive → direct debit or NRC. Negative → *negativa*, nothing to pay, carried into box 15 of the next quarters of the same year. Zero → *resultado cero*.
6. **Firmar y enviar.** Save the receipt.

### Step 14: Modelo 349

Only when the sheet lists at least one operator (no operations, no 349).

1. Open the Modelo 349 procedure (Impuestos y tasas → IVA → Modelo 349), **Presentar**, web form.
2. **Identification:** fiscal year and period (1T–4T, quarterly).
3. **Operators:** add one line per row of the sheet's operator table: country code, VAT id without the country prefix, name, operation key (`I` services acquired, `S` services supplied) and base. Rectifications of earlier periods: none from the app.
4. Check the summary: number of operators (box 01) and total (box 02) equal the sheet.
5. **Firmar y enviar.** Save the receipt. There is no payment.

### Step 15: payment

- **Direct debit** (*domiciliación*): chosen in the 303/130 form; available only until the direct-debit deadline on the sheet (the 15th for Q1–Q3, 27 January for Q4 2026). The amount is charged on the last day of the filing period.
- **NRC:** after the direct-debit deadline, or to pay at once: pay the exact amount through your bank's online banking (tax payments: model, period, NIF, amount) or the Sede's payment service with a bank card or account; the bank returns an NRC (reference number). Type the NRC in the form and then file. File on the same day you get it.
- A result you cannot pay can be filed with a request for deferral or with *reconocimiento de deuda*; interest applies. The return must still be filed on time.

---

## 5. After filing

### Step 16: download the receipts

For each form, download the **justificante** PDF (the receipt with the justificante number and the CSV code) from the confirmation page or later from the Sede's "consultar declaraciones presentadas". Store them in your private archive folder (step 19), never in the repo.

### Step 17: Mark filed and import the receipts

1. **Filing Sheet** tab → for each model, **Mark filed** with the justificante number and the presentation date. This stores a new, immutable FILED version of the computed figures and flags the period FILED. A later recompute never changes it; a recompute that differs shows a **Recomputed after filing** table.
2. Import the receipts, from the Reconciliation tab (**📥 Import filed AEAT receipts**) or the CLI (folders are scanned recursively):

   ```bash
   .venv/Scripts/python.exe -m src.filed_returns import <folder-or-pdf> [...]
   ```

   The imported boxes are what the next quarter's chains read (303 box 110, 130 boxes 05 and 15, and in Q4 the next year's box 13).

### Step 18: reconcile the filed quarter

```bash
.venv/Scripts/python.exe scripts/close_quarter.py reconcile
```

Now that the quarter's receipt is imported, this compares the quarter itself. For a return you typed from the sheet, every box should be `✅ exact`. A `🔴` means a box was mistyped: fix it with a rectifying return (a complementary return when more tax is due) and import its receipt: a later receipt for the same model and period replaces the earlier one.

### Step 19: archive

```bash
.venv/Scripts/python.exe scripts/close_quarter.py archive
```

Copies `tmp/close_quarter/<Y>_Q<Q>/` (filing sheet, reconciliation, accountant's pack if any, swept invoice copies) and a dated database snapshot into `app.archive_dir/<Y>T<Q>/`, your private archive folder outside the repo, which should be backed up. It only adds or updates copies and never deletes. Put the justificantes (step 16) in the same folder.

Keep them at least as long as the returns can be reviewed: four years from the end of the filing period (art. 66 LGT), and longer for anything that still affects later years (a capital good's regularisation period, an asset still being depreciated, a credit still being compensated). Record the definitive pro-rata % under `tax.prorrata.definitive_pct_by_year` after the Q4 303.

---

## Unattended run

```bash
.venv/Scripts/python.exe scripts/close_quarter.py all
.venv/Scripts/python.exe scripts/close_quarter.py all --apply --freeze --reta-file <export> --model <hub model id>
```

`all` runs steps 1–11 in order and stops at the first failing step (a re-run skips what is done). `--apply` lets `dedupe` and `fx` write, `--freeze` lets `gestor-pack` freeze the Stripe report, `--reta-file` runs `reta` (skipped without it), and `--model` goes to `ocr`. It skips both reviews, so use it for a re-run after the reviews, not for a first pass.

---

## Other commands

| Command | Use |
|---|---|
| `stripe-check [--days N]` | Read-only Stripe API smoke test |
| `stripe-fetch` | Fetch, classify and store the quarter; full review table |
| `add-override "<key>" REGION [--type name\|email]` | Geographic override in `classification_rules.json` |
| `reclassify --from YYYY-MM-DD [--to YYYY-MM-DD] [--dry-run]` | Re-run the classifier over stored charges after a rule change |
| `backfill-emails [--dry-run]` | Fill empty email/country from the saved raw charges |
| `report [--freeze [--supersede]]` | Regenerate (and freeze) the quarter's Stripe report |
| `freeze-sent --file <xlsx> [--supersede]` | Freeze a Stripe report file sent before freezing existed (its EUR amounts become the declared basis) |
| `fx-backfill` | ECB rates up to today |
| `fx-recompute [--dry-run] [--since YYYY-MM-DD]` | Re-resolve every stored non-EUR invoice at the ECB rate |
| `relink [--manifest <csv>] [--old-in-dir D] [--old-out-dir D] [--apply]` | Re-point invoice records after the PDFs were moved or renamed (see README, "Moving or renaming the invoice archive") |

---

## Annual calendar

| When | Return | Where it comes from |
|---|---|---|
| 1–20 April, July, October | 303, 130, 349 of Q1–Q3 | This runbook |
| 1–30 January | 303, 130, 349 of Q4 (the Q4 303 carries the pro-rata regularisation, box 44, and the capital-goods regularisation, box 43) | This runbook |
| 1–30 January | Modelo 390, VAT annual summary | Annual pack (#103) |
| February | Modelo 347, operations with third parties above €3,005.06 (VAT included) | Annual pack (#103); the app computes the income side today |
| April – 30 June | Income-tax return (Renta) for the previous year: activity yield per activity, depreciation, RETA | Annual pack (#103) for the P&L per activity |

Direct-debit deadlines come a few days before each filing deadline; the filing sheet prints them per quarter. Check the AEAT *calendario del contribuyente* each year for regional holidays and changes.

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ocr` fails on every file with a model error | The hub does not serve the alias: `ocr --model <id>` (list ids with `GET /v1/models`), or set `LLM_HUB_MODEL` |
| `fx` prints `⚠ did not reach` | The ECB backfill failed (network); re-run `fx` or `fx-backfill` before trusting foreign-currency figures |
| `sheet` prints `⚠ run compute first` | No stored snapshot for a model: run `compute` |
| 303 box 110 source is `app_chain` | The previous quarter's receipt is not imported: import it (step 17) |
| 130 box 13 source is `app` or `config` | The previous year's Q4 130 receipt is not imported |
| A 349 line is under *unidentified* | The vendor or customer has no VAT id: add it in the ledger or the vendor registry |
| Stripe warning about the EU B2C threshold | Plan for OSS registration before it is crossed ([tax conventions §2.2](tax-conventions.md#22-eu-consumers-spanish-21-below-10000-not-oss-boxes-0709)) |
