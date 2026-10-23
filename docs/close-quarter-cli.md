# Closing a Quarter

`scripts/close_quarter.py` is the deterministic backbone for the recurring quarterly-close chore — a thin CLI over `src/close_pipeline.py`, invoked interactively via the `/close-quarter` Claude Code skill (`.claude/skills/close-quarter/`), which guides you through it step by step and pauses for review after extraction (`ocr`/`vendors`/`dedupe`) and after `compute`. It can also be run by hand. The full procedure, including filing and paying on the AEAT Sede and archiving, is [`docs/quarter-close-runbook.md`](quarter-close-runbook.md).

## Pipeline

Every step takes `--year Y --quarter Q` (default: the last completed quarter), is idempotent, and prints `[step] N change(s)` with one `+` line per write, `⚠` review items and `❌` errors — a re-run with nothing new prints `[step] no changes`. Exit code 1 only on `❌`.

```bash
.venv/Scripts/python.exe scripts/close_quarter.py sweep                       # 1. copy new invoice PDFs
.venv/Scripts/python.exe scripts/close_quarter.py ocr [--direction in|out|both] [--dry-run] [--model ID]  # 2. extract new/changed PDFs
.venv/Scripts/python.exe scripts/close_quarter.py vendors [--all-periods]     # 3. vendor registry + unknown vendors ⚠
.venv/Scripts/python.exe scripts/close_quarter.py dedupe [--apply [--group N]]  # 4. duplicates / out-of-period
.venv/Scripts/python.exe scripts/close_quarter.py fx [--apply]                # 5. ECB backfill + invoice FX recompute
.venv/Scripts/python.exe scripts/close_quarter.py stripe                      # 6. fetch + backfill-emails + reclassify + warnings
.venv/Scripts/python.exe scripts/close_quarter.py reta --file <bank export>   # 7. RETA (TGSS) debits
.venv/Scripts/python.exe scripts/close_quarter.py compute                     # 8. 303/130/349 (+ OSS, 347) snapshots
.venv/Scripts/python.exe scripts/close_quarter.py reconcile                   # 9. filed vs app, markdown table
.venv/Scripts/python.exe scripts/close_quarter.py sheet                       # 10. filing sheet (form order, deadlines)
.venv/Scripts/python.exe scripts/close_quarter.py gestor-pack [--freeze]      # 11. accountant's pack
.venv/Scripts/python.exe scripts/close_quarter.py all [--apply] [--freeze] [--reta-file F] [--model ID]  # 1-11 in order
```

- **`ocr`** extracts every PDF under `invoice_in_dir` / `invoice_out_dir` that is new or changed since its last extraction (MD5 against the stored `file_hash`) through `src/invoice_ingest.extract_and_save` — the same save path as the Invoice OCR tab: OCR, FX resolution, vendor-registry defaults. A failing file is reported (`❌`) and the rest continue; it stays pending and is retried next run. `--dry-run` lists the pending files. `--model` overrides the hub model (else the `LLM_HUB_MODEL` env var, else `gemini_pro`) — use it when the hub no longer serves the default alias.
- **`vendors`** applies `vendors.json` to the expense invoices dated in the quarter, and lists the quarter's invoices with an unknown vendor. A quarter with a FILED snapshot is not written (⚠). Invoices of other periods are never touched, so a later registry edit cannot re-treat a filed year. `--all-periods` is the explicit opt-in to re-apply the registry to every stored expense invoice, filed periods included; it prints the rows written per year/quarter, marking filed ones `(FILED)`.
- **`dedupe`** runs the five detectors of [Duplicate review](duplicate-review.md) (out-of-period over the files swept into the quarter folder) and numbers the proposed groups sequentially starting at 1; `--apply` writes the exclusions, never over a locked row, and `--apply --group N` (repeatable) writes only the matching group, so one correct proposal can go in without the others (numbers shift after an apply, so review again first). A shared invoice number with a different total or date is listed as a numbering ⚠ and never excluded. A group whose kept row is excluded by the time it is applied is skipped with a ⚠, so no apply leaves a group with every row excluded.
- **`fx`** backfills ECB rates up to today (warns when the stored rates stop short of the quarter end, e.g. network failure) and re-resolves the quarter's stored non-EUR invoices; the recompute writes only with `--apply`.
- **`stripe`** fetches the quarter from Stripe, fills billing email/country from saved raw charges, reclassifies the quarter, and reports new/changed transactions plus the review warnings of `stripe-fetch` (foreign-looking `eur_default`, EU B2C threshold). Use `stripe-fetch` for the full per-transaction table.
- **`reta`** imports a bank export of the Social Security debits (column names from `config.json → social_security`, see [Seguridad Social](social-security.md)); rows already stored are skipped.
- **`compute`** computes the quarter and saves the snapshots only when a figure changed (so a re-run doesn't touch `computed_at`).
- **`reconcile`** reconciles 303/130/349 against the quarter's filed returns when they are imported, otherwise against the previous quarter's (the chain the carry-forwards start from), and writes `reconciliation_<Y>_Q<Q>.md`.
- **`sheet`** writes `filing_sheet_<Y>_Q<Q>.md` from the stored snapshots — see [Filing Sheet](filing-sheet.md) (rendered by `src/filing_sheet.py` through the pluggable `src.close_pipeline.filing_sheet_renderer` hook).
- **`gestor-pack`** (while an external accountant is engaged) copies the quarter's invoices from the ledger into `tmp/close_quarter/<Y>_Q<Q>/invoices/` (every non-excluded `in`/`out` invoice dated in the quarter, including ones OCR'd before the close; pack files no longer in that set are removed, a PDF missing on disk is a ⚠), writes the reclassified Stripe report, `gestor_notes_<Y>_Q<Q>.md` — your free text from the git-ignored `gestor_notes.md` at the repo root (a template when absent) plus the special treatments detected in the ledger (partial business use, exclusions, non-default VAT treatment, fixed assets, foreign currency) — and a draft email `gestor_email_<Y>_Q<Q>.txt` whose invoice counts are the pack's. `--freeze` stores the report as the declared report (below); once declared the pack never regenerates it. Nothing is ever sent.
- **`all`** stops at the first failing step; completed steps are no-ops on the re-run. `--apply` lets `dedupe`/`fx` write, `--freeze` lets `gestor-pack` freeze.

## Other subcommands

```bash
.venv/Scripts/python.exe scripts/close_quarter.py stripe-check [--days N]              # read-only API smoke test
.venv/Scripts/python.exe scripts/close_quarter.py stripe-fetch --year Y --quarter Q    # fetch + classify + persist
.venv/Scripts/python.exe scripts/close_quarter.py stripe-fetch --backfill-fee-split [--from D --to D] [--dry-run]
.venv/Scripts/python.exe scripts/close_quarter.py add-override "<key>" REGION [--type email|name]
.venv/Scripts/python.exe scripts/close_quarter.py reclassify --from YYYY-MM-DD [--to YYYY-MM-DD] [--dry-run]
.venv/Scripts/python.exe scripts/close_quarter.py backfill-emails [--dry-run]           # fill email/country from saved raw charges
.venv/Scripts/python.exe scripts/close_quarter.py report --year Y --quarter Q          # regenerate the Excel report
.venv/Scripts/python.exe scripts/close_quarter.py report --year Y --quarter Q --freeze # ...and freeze it as declared
.venv/Scripts/python.exe scripts/close_quarter.py freeze-sent --year Y --quarter Q --file <sent.xlsx> [--supersede]  # freeze a report sent earlier
.venv/Scripts/python.exe scripts/close_quarter.py fx-backfill                          # backfill ECB FX rates to today
.venv/Scripts/python.exe scripts/close_quarter.py fx-recompute [--dry-run] [--since D] # re-resolve stored invoices' EUR at the ECB rate
.venv/Scripts/python.exe scripts/close_quarter.py archive --year Y --quarter Q         # after filing: quarter folder + DB snapshot -> app.archive_dir
.venv/Scripts/python.exe scripts/close_quarter.py relink [--manifest moves.csv] [--old-in-dir D] [--old-out-dir D] [--apply] # after moving/renaming invoice PDFs
```

- **`sweep`** (pipeline step 1) diffs `invoice_in_dir` / `invoice_out_dir` (recursively) against both the `invoices` DB table and a cumulative manifest (`tmp/close_quarter/invoice_copy_log.json`), copies only the files not seen before into `tmp/close_quarter/<year>_Q<quarter>/`, and updates the manifest — safe to rerun after adding more invoices.
- **`stripe-fetch`** flags transactions classified by a *default* geo rule (no client-specific override matched) so they can be double-checked before the report goes out, lists EUR charges that fell to `eur_default` for a foreign-looking customer (⚠), and prints the EU B2C year-to-date total against the €10,000 art. 73 LIVA threshold. With **`--backfill-fee-split`** it instead re-fetches `--from`/`--to` (default: the `--year`/`--quarter` bounds) from the Stripe API and writes only the `fee_stripe` / `fee_application` split of rows already stored — no insert, no reclassification, no other column — so closed quarters cannot move; `--dry-run` reports counts without writing. Run it for every year whose Modelo 130 is still computed by the app.
- **`reclassify`** re-runs the classifier over the transactions *already stored* from `--from` (optionally up to `--to`) with the current rules and overrides, prints every change (`old activity/geo (rule) -> new`) plus a per-quarter count, and writes only the changed rows. `--dry-run` reports without writing. Idempotent: a second run changes nothing. Run it after any rule change that should apply retroactively.
- **`backfill-emails`** fills empty stored `email_meta` / `billing_country` from each row's already-saved raw Stripe charge JSON (`raw_source_json`) — no Stripe API call, and a non-empty stored value is never overwritten. Use it to pick up `billing_details.email` / `billing_details.address.country` for rows whose columns are empty. `--dry-run` reports counts without writing.
- **`report`** first reclassifies the quarter's stored rows (so a stale row can never be exported), then writes the Excel report. The exporter also refuses — `StaleClassificationError` — to write a non-EUR charge whose geography came from a EUR rule. With **`--freeze`** the written file becomes the quarter's immutable declared report (`declared_reports`); freezing an already-declared quarter needs `--supersede` (a new version, for a corrected re-send). Once a quarter is declared, a plain `report` writes `Stripe_Report_Q<Q>_<Y>_live.xlsx` instead of overwriting the sent file, and prints how the live rows differ from the declared ones.
- **`freeze-sent`** freezes a Stripe report file that was sent to the accountant without being frozen. The declared lines come from the file's `import` sheet (columns matched by header name, so files written by older versions of the app still read): id, created date, currency, `Converted Amount`, `Converted Amount Refunded`, fee and net, not from today's live rows. The file's SHA-256 is stored as with `report --freeze`. The sheet has no original-currency amount, so that column stays empty. Everything is checked before anything is stored: a row dated outside the quarter, a duplicate id, an unreadable cell or a `Net Amount` that is not amount minus refunds aborts with the row numbers and stores nothing. File ids with no live `transactions` row are frozen anyway and listed as a warning, as are live charges of the quarter missing from the file (those keep their live amounts). As with `report --freeze`, only the EUR amounts are used; activity and region stay live, so a region mistake in the sent file is not re-imported. An already-declared quarter needs `--supersede`. Keep the sent file in `tmp/close_quarter/<Y>_Q<Q>/` under its original name, or `gestor-pack` warns that the frozen file is missing.
- **`add-override`** appends to `classification_rules.json`'s `geographic_overrides` / `email_overrides` — the same mechanism as the Transaction Browser tab's "Add Geographic Override" form.
- **`fx-backfill`** fetches and stores ECB rates from the last stored date up to today for every currency seen in stored invoices/transactions (`src.fx_rates.backfill_to_today`) — idempotent, safe to rerun every close.
- **`fx-recompute`** re-runs `resolve_invoice_amounts` over every *already-stored* non-EUR invoice (`src.fx_rates.recompute_stored_invoice_fx`) — corrects stored invoices whose EUR figure predates the current resolver, in place. Writes by default; pass `--dry-run` to preview (scanned/changed/stale/cross-check/locked-skipped counts plus a per-row old→new EUR list) without touching the DB. `--since YYYY-MM-DD` restricts the scan to invoices dated on/after that date. Never overwrites a row with `subtotal_eur`/`iva_amount`/`total_eur` in its `locked_fields` — those are reported as skipped, not silently kept or dropped — and never touches `eur_received`, which already wins over `subtotal_eur` in the tax engine regardless. Idempotent: a second run reports zero changes. The same recompute is available as a preview-then-apply button in the Currency tab.
- **`archive`** (runbook step 19) copies the quarter folder and a dated database snapshot into `app.archive_dir/<year>T<quarter>/`. It only adds or updates copies, never deletes, and reports no changes on a same-day re-run. It fails with a clear error when `app.archive_dir` is not set.
- **`relink`**: see [Moving or renaming the invoice archive](#moving-or-renaming-the-invoice-archive) below.
- All output lives under `tmp/close_quarter/` (git-ignored), apart from `archive`'s copies. Nothing is uploaded or sent anywhere by this script.

## Moving or renaming the invoice archive

An invoice record is keyed on its PDF's path **relative to** `invoice_in_dir` / `invoice_out_dir`. Moving a whole root therefore only needs the config change. **Renaming** files, or moving them between sub-folders, needs a relink, or every renamed PDF looks new to `ocr` and the old record, with its OCR result, locks, exclusions and fixed-asset link, is orphaned.

1. Back up the database (the runbook's pre-flight command).
2. Move or rename the files. Keep a move record: a CSV with `src` and `dst` absolute paths (other columns are ignored), such as the log a re-sorting tool writes.
3. Point `invoice_in_dir` / `invoice_out_dir` at the new roots.
4. Dry run:

   ```bash
   .venv/Scripts/python.exe scripts/close_quarter.py relink --manifest moves.csv --old-in-dir <old in root> --old-out-dir <old out root>
   ```

   Every record is reported as one of:
   - **moved:** matched by the move record, or by content hash when there is no move row;
   - **unchanged:** still at its path with the same content;
   - **⚠ unmatched:** no file found;
   - **⚠ ambiguous:** several files share its content, or two records would land on one file.

   The exit code is 1 while anything is unmatched or ambiguous.
5. Resolve the ⚠ rows, then repeat with `--apply`. It backs up the database to `data/backups/` and rewrites the paths in one transaction; row ids and every other field are kept.
