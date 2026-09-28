---
name: close-quarter
description: Guided quarterly close for the Stripe accounting dashboard — sweep invoice PDFs, verify the Stripe import, fetch and classify transactions, walk geo/activity overrides interactively, regenerate the Excel report. Outputs land in git-ignored tmp/close_quarter/<year>_Q<quarter>/; nothing is sent or uploaded. Use for "/close-quarter", "close the quarter", "run the quarterly sweep", "prepare invoices for the accountant", "rerun the sweep".
---

# close-quarter

**Goal:** walk the user through closing a Stripe accounting quarter, reusing
the deterministic helper at `scripts/close_quarter.py` for every mechanical
step, and pausing for the user's judgement where it is actually needed
(unreviewed invoices, ambiguous geo classification). Never do a mechanical
step by hand (inline Python, manually editing `classification_rules.json`)
when the script already has a subcommand for it — the point of this skill is
that the same procedure runs the same way every month.

All commands run from the repo root with the project venv:
`.venv/Scripts/python.exe scripts/close_quarter.py <subcommand> ...`
(POSIX: `.venv/bin/python scripts/close_quarter.py ...`)

## Arguments

If the user passes `<year> Q<N>` (e.g. `/close-quarter 2026 Q3`), use that.
Otherwise let the script default to the most recently completed calendar
quarter (`close_quarter.py`'s `previous_quarter()`) and state which
year/quarter you resolved to before doing anything else, so the user can
correct it early.

## Steps

### 1. Sweep invoices

Run `sweep --year Y --quarter Q`. It copies any received/sent invoice PDFs
not yet copied or catalogued into `tmp/close_quarter/<Y>_Q<Q>/`, flattening
subfolder paths into the filename (`IN - vendor - file.pdf` /
`OUT - file.pdf`) and updating the cumulative manifest
(`tmp/close_quarter/invoice_copy_log.json`) so a later re-run only picks up
files added since.

Report the counts and the list of newly copied files.

**STOP.** Wait for the user to confirm before continuing — they may want to
eyeball the folder, add more invoices, or tell you something's missing.

### 2. Verify Stripe

Run `stripe-check` (read-only, no DB writes). If it fails, report the error
verbatim and stop — do not proceed to a full fetch on a broken connection.

### 3. Fetch + classify

Run `stripe-fetch --year Y --quarter Q`. This is a real write: it fetches
the quarter's charges from Stripe, classifies them, and persists to the
local SQLite DB (idempotent — safe to rerun). Show the user the totals and
the full per-transaction table it prints, and call out the transactions it
flagged with ⚠ (classified by a *default* geo rule — i.e. no client-specific
override matched, so they're the ones most likely to be wrong). Call out
separately the "EUR charge(s) fell to eur_default for a customer that looks
foreign" list — those sit in SPAIN by default although the card/billing
country or email domain says otherwise — and the EU B2C threshold line
(warn the user loudly if it says ⚠: at 80% of €10,000 or exceeded).

Ask: **"Any misclassifications to correct? Give me the client name/email and
the correct region, or say it looks good."**

### 4. Apply overrides (loop)

For each correction the user gives:
- Run `add-override "<key>" <REGION> --type name|email` (name = substring
  matched against the transaction description; email = matched against
  Stripe's email metadata — default to `name` unless the user's key is
  clearly an email that appears in Stripe's separate email field rather than
  in the description text).
- Re-run `stripe-fetch --year Y --quarter Q` to reclassify with the updated
  rule and persist it, and show the updated table + remaining flag count.
- If the override should also fix earlier quarters, run
  `reclassify --from <date> --dry-run`, show the listed changes, and only
  after the user agrees run it again without `--dry-run`.

Keep looping until the user confirms it's correct. Don't guess at a region
from currency/card metadata yourself — always ask the user, since this is
the one step that genuinely needs their judgement.

### 5. Regenerate the report

Once the user confirms the classification is correct, run
`report --year Y --quarter Q`. It reclassifies the quarter's stored rows
(printing any change), reads the classification back from the DB and writes
`Stripe_Report_Q<Q>_<Y>.xlsx` into the same `tmp/close_quarter/<Y>_Q<Q>/`
folder as the swept invoices — one folder, ready to hand off.

**Freeze only when the user says this is the version they will send.** Ask
explicitly; on a yes run `report --year Y --quarter Q --freeze`, which stores
the report as the quarter's immutable declared report (the tax engine then
uses its EUR amounts for the quarter). If the quarter is already declared the
script refuses; `--freeze --supersede` is for a corrected re-send only —
never pass it without the user asking for exactly that.

### 6. Summarize

State the final folder path, the quarter's totals (income, fees, by
activity/region), whether the report was frozen (version + sha256), and
remind the user explicitly that nothing has been sent or uploaded anywhere — `tmp/` is git-ignored and local only. Do not commit,
push, or touch git as part of this skill.
