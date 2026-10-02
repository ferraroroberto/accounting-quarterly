---
name: close-quarter
description: Guided quarterly close for the accounting dashboard — sweep and OCR invoice PDFs, vendor/dedupe/FX review, Stripe fetch with interactive geo overrides, RETA import, compute 303/130/349, reconcile against filed returns, filing sheet and the accountant's pack (Stripe report, notes, draft email). Outputs land in git-ignored tmp/close_quarter/<year>_Q<quarter>/; nothing is sent or uploaded. Use for "/close-quarter", "close the quarter", "run the quarterly sweep", "prepare invoices for the accountant", "rerun the sweep".
---

# close-quarter

**Goal:** walk the user through closing an accounting quarter, reusing the
deterministic helper at `scripts/close_quarter.py` (logic in
`src/close_pipeline.py`) for every mechanical step, and pausing for the
user's judgement where it is actually needed. Never do a mechanical step by
hand (inline Python, manually editing `classification_rules.json`, SQL on the
DB) when the script already has a subcommand for it — the point of this skill
is that the same procedure runs the same way every quarter.

All commands run from the repo root with the project venv:
`.venv/Scripts/python.exe scripts/close_quarter.py <subcommand> --year Y --quarter Q ...`
(POSIX: `.venv/bin/python scripts/close_quarter.py ...`)

Every pipeline step is idempotent and prints `[step] N change(s)` followed by
`+` lines (what it wrote), plain lines (context), `⚠` lines (review items) and
`❌` lines (errors); a re-run with nothing new prints `[step] no changes`.
Relay the `+`, `⚠` and `❌` lines to the user. A step exits non-zero only on
`❌` errors.

## Arguments

If the user passes `<year> Q<N>` (e.g. `/close-quarter 2026 Q3`), use that.
Otherwise let the script default to the most recently completed calendar
quarter (`close_quarter.py`'s `previous_quarter()`) and state which
year/quarter you resolved to before doing anything else, so the user can
correct it early.

If the user asks for an unattended run, `all --year Y --quarter Q` runs steps
1–11 in order and stops at the first failing step (`--apply` lets `dedupe` and
`fx` write, `--freeze` lets `gestor-pack` freeze the Stripe report,
`--reta-file <export>` runs `reta`, `--model <id>` is passed to `ocr`). Never
add `--apply` or `--freeze` unless the user asked for exactly that. Otherwise
walk the steps below one by one.

## Steps

### 1. Sweep invoices — `sweep`

Copies any received/sent invoice PDF not yet catalogued in the DB nor copied
before into `tmp/close_quarter/<Y>_Q<Q>/` (`IN - vendor - file.pdf` /
`OUT - file.pdf`), updating the cumulative manifest
`tmp/close_quarter/invoice_copy_log.json`. Report the copied files.

### 2. Extract new invoices — `ocr`

Run `ocr --dry-run` first and tell the user how many PDFs are pending (new, or
changed since their last extraction — MD5 against the stored hash). Then run
`ocr`. It extracts each through the OCR backend (local-llm-hub by default),
resolves foreign-currency EUR at the ECB rate and applies the vendor registry.
A failing file is a `❌` line; the others still go through and the failed one
is retried on the next run. If the hub rejects the default model alias, rerun
with `--model <hub model id>` (it overrides the `LLM_HUB_MODEL` env var; list
the hub's ids with `GET http://127.0.0.1:8000/v1/models`). `--direction in|out`
limits it to one folder.

### 3. Vendors — `vendors`

Applies the vendor registry (`vendors.json`) to stored expense invoices and
lists the quarter's invoices with an unknown vendor (`⚠`). For each, the user
adds the vendor in the app's Vendors tab; then rerun `vendors`.

### 4. Duplicates — `dedupe`

Lists duplicate groups (same file, same invoice number, invoice + receipt
pair, email copy) and swept files dated outside the quarter
(out-of-period). Without `--apply` nothing is written.

**STOP (review 1).** Show the user the `ocr`, `vendors` and `dedupe` results.
Wait for them to fix unknown vendors / extraction errors and to confirm the
proposed exclusions. Only on their OK run `dedupe --apply`.

### 5. FX — `fx`

Backfills ECB rates up to today, then lists the quarter's stored non-EUR
invoices whose EUR figure would change at the ECB rate (locked rows are
skipped). A `⚠ did not reach` line means the rate backfill failed (network) —
report it, don't proceed as if rates were current. If invoices would change,
show them and run `fx --apply` once the user agrees.

### 6. Stripe — `stripe`, `stripe-fetch`, `add-override`, `reclassify`

Run `stripe-check` first (read-only); if it fails, report the error verbatim
and stop. Then `stripe` fetches and persists the quarter's charges, backfills
billing email/country from the saved raw charges, reclassifies the quarter
with the current rules and prints review warnings: EUR charges that fell to
`eur_default` for a foreign-looking customer, classification errors and the
EU B2C art. 73 LIVA threshold (warn loudly on ⚠ at 80% or exceeded).

For the per-transaction table (⚠ = classified by a default geo rule, the rows
most likely to be wrong) run `stripe-fetch`. Ask: **"Any misclassifications
to correct? Give me the client name/email and the correct region, or say it
looks good."** For each correction:
- Run `add-override "<key>" <REGION> --type name|email` (name = substring of
  the description; email = Stripe's email metadata — default to `name` unless
  the key is clearly an email that lives in Stripe's email field).
- Re-run `stripe` and show the updated warnings.
- If the override should also fix earlier quarters, run
  `reclassify --from <date> --dry-run`, show the changes, and only after the
  user agrees run it again without `--dry-run`.

Keep looping until the user confirms. Don't guess a region from currency or
card metadata yourself — always ask. (`backfill-emails [--dry-run]` is the
same billing backfill on its own, for all periods.)

### 7. RETA — `reta [--file <export>]`

Ask the user for the bank export of the Social Security (TGSS) debits
(`.xls`/`.xlsx`/`.csv`; column names from `config.json → social_security`,
defaults `Fecha` / `Importe`). Without `--file` the step reads
`social_security.bank_export_file`. Rows already stored are skipped. Report the
quarter's RETA total; a `⚠ no RETA payment` line means the export does not
cover the quarter. Skip the step if the user has no new export.

### 8. Compute — `compute`

Computes Modelo 303/130/349 (+ OSS and the annual 347) and saves the
snapshots only when a figure changed. Show the box summary lines.

**STOP (review 2).** Walk the user through the 303/130/349 figures and ask
whether anything looks wrong before reconciling and preparing the pack. Fix
data issues with the steps above (or the app), then rerun `compute`.

### 9. Reconcile — `reconcile`

Compares the app's boxes with the filed returns: this quarter if a filed
return is already imported, otherwise the previous quarter (the chain this
quarter's carry-forwards start from). Writes
`reconciliation_<Y>_Q<Q>.md` into the quarter folder and prints the markdown.
Call out every `🔴` (uncatalogued) box; a `⚠ no filed Modelo` line means the
AEAT receipt still needs importing (`python -m src.filed_returns import <pdf>`).

### 10. Filing sheet — `sheet`

Writes `filing_sheet_<Y>_Q<Q>.md` from the stored `compute` snapshots: the
non-zero boxes of the 303 (form order), 130 and 349 (+ operator list), the 303
credit chain, each model's result and the deadlines (direct debit / filing).
A `⚠ Recomputed after filing` block lists boxes that differ from the filed
version. `⚠ run compute first` means the snapshots are missing. After filing,
the user marks each model filed in the **Filing Sheet** tab (justificante +
date) and imports the receipt PDF (`python -m src.filed_returns import <pdf>`).

### 11. Accountant's pack — `gestor-pack`

While an external accountant is still engaged: copies the quarter's
non-excluded invoices from the ledger into `invoices/` in the quarter folder
(including ones OCR'd before the close — not the `sweep` copies), writes the
Stripe report (`Stripe_Report_Q<Q>_<Y>.xlsx`, reclassified first), the notes file
`gestor_notes_<Y>_Q<Q>.md` (the user's own notes from the git-ignored
`gestor_notes.md` at the repo root, else a template, plus the special
treatments detected in the ledger — partial business use, exclusions,
non-default VAT treatments, fixed assets, foreign currency) and a draft email
`gestor_email_<Y>_Q<Q>.txt`. It never sends anything.

**Freeze only when the user says this is the version they will send.** Ask
explicitly; on a yes run `gestor-pack --freeze`, which stores the report as
the quarter's immutable declared report (the tax engine then uses its EUR
amounts). Once frozen, `gestor-pack` never regenerates it and warns if live
rows drift from it. `report [--freeze [--supersede]]` regenerates the report
on its own; `--supersede` is for a corrected re-send only — never pass it
without the user asking for exactly that. For a quarter whose report was
sent before freezing existed, `freeze-sent --file <sent xlsx>` freezes that
file's EUR amounts instead (same `--supersede` rule); it aborts, storing
nothing, on a row dated outside the quarter or a duplicate id, and lists file
ids missing from the live table. `fx-backfill` and
`fx-recompute [--dry-run] [--since D]` remain as the stand-alone FX commands.

After the returns are filed (not part of this guided close), `archive` copies
the quarter folder and a dated database snapshot into `app.archive_dir`. If the
user has moved or renamed invoice PDFs, run
`relink [--manifest <csv>] [--old-in-dir D] [--old-out-dir D]` first, as a dry
run. Only re-run it with `--apply` once it reports no unmatched or ambiguous
rows, and never while `ocr` would still see the renamed files as new.

### 12. Summarize

State the final folder path, the quarter's figures (compute summary), the
reconciliation result, whether the Stripe report was frozen (version +
sha256), and remind the user explicitly that nothing has been sent or
uploaded anywhere — `tmp/` is git-ignored and local only. Do not commit,
push, or touch git as part of this skill.
