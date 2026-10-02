"""Deterministic helper for closing an accounting quarter.

Run from the repo root with the project venv:
    .venv/Scripts/python.exe scripts/close_quarter.py <subcommand> [options]

Pipeline (issue #102) — run in this order, or all at once with `all`. Every
step is idempotent and prints what it changed ("no changes" on a re-run):
    sweep         Copy new invoice PDFs (received + sent) not yet copied or
                  catalogued into tmp/close_quarter/<year>_Q<quarter>/, and
                  update the cumulative copy-log manifest.
    ocr           Extract new/changed invoice PDFs (MD5 vs stored hash) via the
                  OCR backend (local-llm-hub by default), resolve FX and apply
                  the vendor registry. A failing file is reported and retried
                  next run. --dry-run lists them; --model overrides the hub
                  model (else LLM_HUB_MODEL, else gemini_pro).
    vendors       Apply the vendor registry to the quarter's expense invoices (none
                  when the quarter is FILED); list its unknown vendors (⚠).
                  --all-periods re-applies it everywhere, with per-period counts.
    dedupe        Duplicate / receipt / out-of-period groups (out-of-period =
                  swept files dated outside the quarter). Writes only with --apply.
    fx            Backfill ECB rates to today, then re-resolve the quarter's
                  stored non-EUR invoices. The recompute writes only with --apply.
    stripe        Stripe fetch + billing-email backfill + reclassify the quarter,
                  then review warnings (foreign-looking eur_default, art. 73 LIVA).
    reta          Import the RETA (TGSS) debits of a bank export (--file).
    compute       Modelo 303/130/349 (+ OSS, 347) snapshots — saved only when a
                  figure changed.
    reconcile     Filed vs app, box by box: this quarter if already filed, else
                  the previous quarter. Writes reconciliation_<Y>_Q<Q>.md.
    sheet         Filing sheet from the stored snapshots: boxes in AEAT form order,
                  credit chain, deadlines. Writes filing_sheet_<Y>_Q<Q>.md.
    gestor-pack   Stripe report (--freeze stores it as the declared report),
                  notes on special treatments (from git-ignored gestor_notes.md)
                  and a draft email. Never sends anything.
    all           Every step above in order (--apply for dedupe/fx, --freeze for
                  the pack, --reta-file for reta); stops at a failing step.

Other subcommands:
    stripe-check  Read-only Stripe API smoke test. No DB writes.
    stripe-fetch  Fetch + classify + persist the quarter's Stripe charges and
                  print the full review table (default geo rule ⚠, foreign
                  eur_default charges, EU B2C threshold).
                  --backfill-fee-split [--from D --to D] [--dry-run] instead
                  re-fetches the range (default: the quarter) and fills only
                  the stored rows' Stripe / platform fee split (#135).
    add-override  Add a geographic classification override (name/email
                  substring -> region) to classification_rules.json.
    reclassify    Re-run the classifier over STORED transactions from a date
                  (after a rule change), logging every change; --dry-run
                  only reports.
    backfill-emails
                  Fill empty stored email_meta / billing_country from each
                  row's already-saved raw Stripe charge JSON, no API call;
                  never overwrites a non-empty value. --dry-run only reports.
    report        Reclassify the quarter's stored rows and regenerate its Excel
                  report. --freeze stores it as the quarter's immutable
                  declared report (--supersede for a corrected re-send).
    freeze-sent   Freeze a Stripe report file sent before freezing existed
                  (--file): its `import` sheet's EUR amounts become the
                  quarter's declared basis (--supersede if already declared).
    fx-backfill   Backfill ECB FX rates up to today.
    fx-recompute  Re-resolve every stored non-EUR invoice at the ECB rate.
    archive       After filing: copy the quarter folder and a dated database
                  snapshot into app.archive_dir/<year>T<quarter>/ (runbook step 19).
    relink        After moving or renaming invoice PDFs: re-point the stored
                  invoice records at their files (--manifest move records,
                  content-hash fallback). Dry run unless --apply (#151).

All outputs are written under tmp/, which is git-ignored.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from src.classifier import eur_default_foreign_warning  # noqa: E402
from src.close_pipeline import (  # noqa: E402
    DEFAULT_GEO_RULES,
    STEP_ORDER,
    CloseContext,
    StepResult,
    run_all,
    step_compute,
    step_dedupe,
    step_fx,
    step_gestor_pack,
    step_archive,
    step_ocr,
    step_reconcile,
    step_reta,
    step_sheet,
    step_stripe,
    step_sweep,
    step_vendors,
    freeze_sent_stripe_report,
    write_stripe_report,
)
from src.database import init_db  # noqa: E402
from src.exceptions import InvalidSentReportError, ReportAlreadyFrozenError  # noqa: E402
from src.rules_engine import load_rules, save_rules  # noqa: E402
from src.stripe_client import fetch_charges  # noqa: E402

if TYPE_CHECKING:
    from src.reclassify import ReclassifyResult

# app.data_loader pulls in Streamlit (for @st.cache_data); import it lazily,
# only inside the subcommands that actually need it, so the other subcommands
# stay free of Streamlit's "no runtime found" cache warning.


def previous_quarter(today: datetime | None = None) -> tuple[int, int]:
    """Return (year, quarter) for the most recently completed calendar quarter."""
    today = today or datetime.now()
    q = (today.month - 1) // 3 + 1
    if q == 1:
        return today.year - 1, 4
    return today.year, q - 1


def _context(args: argparse.Namespace) -> CloseContext:
    init_db()  # same idempotent migrations the app runs at start, so new columns exist
    return CloseContext(args.year, args.quarter)


def _emit(result: StepResult, with_output: bool = True) -> int:
    print(result.render())
    if with_output and result.output:
        print()
        print(result.output)
    return 1 if result.errors else 0


def _stripe_fetcher(year: int, quarter: int) -> Callable[[], object]:
    def fetch() -> object:
        from app.data_loader import get_classified_for_period
        return get_classified_for_period(year, quarter, input_mode="api")
    return fetch


# ---------------------------------------------------------------------------
# Pipeline subcommands
# ---------------------------------------------------------------------------

def cmd_sweep(args: argparse.Namespace) -> int:
    return _emit(step_sweep(_context(args)))


def cmd_ocr(args: argparse.Namespace) -> int:
    directions = ("in", "out") if args.direction == "both" else (args.direction,)
    return _emit(step_ocr(_context(args), directions=directions, dry_run=args.dry_run, model=args.model))


def cmd_vendors(args: argparse.Namespace) -> int:
    return _emit(step_vendors(_context(args), all_periods=args.all_periods))


def cmd_dedupe(args: argparse.Namespace) -> int:
    return _emit(step_dedupe(_context(args), apply=args.apply))


def cmd_fx(args: argparse.Namespace) -> int:
    return _emit(step_fx(_context(args), apply=args.apply))


def cmd_stripe(args: argparse.Namespace) -> int:
    return _emit(step_stripe(_context(args), fetch=_stripe_fetcher(args.year, args.quarter)))


def cmd_reta(args: argparse.Namespace) -> int:
    ctx = _context(args)
    export = args.file or ((ctx.config or {}).get("social_security") or {}).get("bank_export_file")
    if not export:
        raise SystemExit("No bank export: pass --file or set social_security.bank_export_file in config.json")
    path = Path(export) if Path(export).is_absolute() else ROOT / export
    if not path.exists():
        raise SystemExit(f"Bank export not found: {path}")
    return _emit(step_reta(ctx, path))


def cmd_compute(args: argparse.Namespace) -> int:
    return _emit(step_compute(_context(args)))


def cmd_reconcile(args: argparse.Namespace) -> int:
    return _emit(step_reconcile(_context(args)))


def cmd_sheet(args: argparse.Namespace) -> int:
    return _emit(step_sheet(_context(args)))


def cmd_gestor_pack(args: argparse.Namespace) -> int:
    return _emit(step_gestor_pack(_context(args), freeze=args.freeze))


def cmd_all(args: argparse.Namespace) -> int:
    ctx = _context(args)
    print(f"Closing {ctx.period} (apply={args.apply}, freeze={args.freeze}) -> {ctx.quarter_dir}")

    def on_step(n: int, result: StepResult) -> None:
        print()
        print(f"== {n}/{len(STEP_ORDER)} ==")
        _emit(result, with_output=False)

    results = run_all(
        ctx, apply=args.apply, freeze=args.freeze, reta_file=args.reta_file, model=args.model,
        stripe_fetch=_stripe_fetcher(args.year, args.quarter), on_step=on_step,
    )
    print()
    print("Summary:")
    for r in results:
        print(f"  {r.step:<12} {len(r.changes):>3} change(s)  {len(r.warnings):>3} warning(s)  "
              f"{len(r.errors):>3} error(s)")
    if len(results) < len(STEP_ORDER):
        print(f"❌ Stopped after '{results[-1].step}' — fix it and rerun (completed steps are no-ops).")
    return 1 if any(r.errors for r in results) else 0


# ---------------------------------------------------------------------------
# Other subcommands
# ---------------------------------------------------------------------------

def cmd_stripe_check(args: argparse.Namespace) -> int:
    end = datetime.now()
    start = end - timedelta(days=args.days)
    payments = fetch_charges(start, end)
    print(f"OK: Stripe API reachable, {len(payments)} charges in the last {args.days} days "
          f"(read-only, no DB writes).")
    return 0


def cmd_stripe_fetch(args: argparse.Namespace) -> int:
    from app.data_loader import get_classified_for_period, quarter_dates

    if args.backfill_fee_split:
        return _backfill_fee_split(args, *quarter_dates(args.year, args.quarter))
    from src.aggregator import calculate_grand_totals, get_transaction_count
    from src.classifier import validate_classifications

    start, end = quarter_dates(args.year, args.quarter)
    payments = get_classified_for_period(
        args.year, args.quarter, start, end,
        input_mode="api",
    )
    grand = calculate_grand_totals(payments)
    counts = get_transaction_count(payments)
    val = validate_classifications(payments)

    print(f"Q{args.quarter} {args.year}: {len(payments)} transactions, "
          f"{grand.get('total_income', 0):,.2f} EUR income, {grand.get('total_fee', 0):,.2f} EUR fees")
    print(f"Validation: {val['activity_errors']} activity errors, {val['geo_errors']} geo errors, "
          f"{val['unknown_activity']} unclassified")
    print()
    header = f"{'Date':<12} {'ID':<24} {'Amt EUR':>9} {'Activity':<14} {'Geo':<14} {'Geo rule':<28} {'Description'}"
    print(header)
    for p in sorted(payments, key=lambda x: x.created_date):
        flag = " ⚠" if p.geo_rule in DEFAULT_GEO_RULES else ""
        print(f"{p.created_date.strftime('%Y-%m-%d'):<12} {p.id:<24} {p.converted_amount:>9,.2f} "
              f"{p.activity_type:<14} {p.geo_region:<14} {(p.geo_rule + flag):<28} {p.description[:40]}")
    flagged = [p for p in payments if p.geo_rule in DEFAULT_GEO_RULES]
    print()
    print(f"{len(flagged)} transaction(s) on a DEFAULT geo rule (no client-specific override) "
          f"— worth a manual check.")

    foreign = [(p, w) for p in payments if (w := eur_default_foreign_warning(p))]
    if foreign:
        print()
        print(f"⚠ {len(foreign)} EUR charge(s) fell to eur_default for a customer that looks "
              f"foreign — add an override (add-override) if they are not in Spain:")
        for p, warning in foreign:
            print(f"  ⚠ {p.created_date.strftime('%Y-%m-%d')} {p.id} {p.converted_amount:,.2f} EUR "
                  f"— {warning}")

    _print_eu_b2c_threshold(args.year, args.quarter)
    return 0


def _backfill_fee_split(args: argparse.Namespace, q_start: datetime, q_end: datetime) -> int:
    from src.stripe_client import backfill_fee_split

    start = datetime.strptime(args.from_date, "%Y-%m-%d") if args.from_date else q_start
    end = (datetime.strptime(args.to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
           if args.to_date else q_end)
    result = backfill_fee_split(start, end, dry_run=args.dry_run)
    verb = "would update" if result.dry_run else "updated"
    print(f"Fee split backfill {start:%Y-%m-%d}..{end:%Y-%m-%d}: {result.fetched} fetched, "
          f"{verb} {result.updated}, {result.unchanged} unchanged, "
          f"{result.not_stored} not stored (run stripe-fetch first), "
          f"{result.split_unknown} without a readable split.")
    return 0


def _print_eu_b2c_threshold(year: int, quarter: int) -> None:
    from src.database import get_connection
    from src.tax_engine import compute_eu_b2c_threshold, load_app_config

    conn = get_connection()
    try:
        tracker = compute_eu_b2c_threshold(year, quarter, conn, load_app_config())
    finally:
        conn.close()
    print()
    print(("⚠ " if tracker.status != "OK" else "") + tracker.message)


def _print_reclassify(result: ReclassifyResult) -> None:
    for change in result.changes:
        print(f"  {change.describe()}")
    verb = "would change" if result.dry_run else "changed"
    print(f"Reclassify: scanned {result.scanned}, {verb} {len(result.changes)}.")
    for (year, quarter), n in result.by_quarter().items():
        print(f"  {year} Q{quarter}: {n}")


def cmd_reclassify(args: argparse.Namespace) -> int:
    from src.reclassify import reclassify_stored

    start = datetime.strptime(args.from_date, "%Y-%m-%d")
    end = (datetime.strptime(args.to_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
           if args.to_date else None)
    _print_reclassify(reclassify_stored(start, end, dry_run=args.dry_run))
    return 0


def cmd_backfill_emails(args: argparse.Namespace) -> int:
    from src.stripe_client import backfill_billing_details_from_raw_source

    result = backfill_billing_details_from_raw_source(dry_run=args.dry_run)
    verb = "would update" if result.dry_run else "updated"
    print(
        f"Backfill billing details: scanned {result.scanned}, {verb} {result.updated} "
        f"({result.email_filled} emails, {result.country_filled} countries)."
    )
    return 0


def cmd_add_override(args: argparse.Namespace) -> int:
    rules = load_rules()
    geo = rules.setdefault("geographic_rules", {})
    key = args.key.strip().lower()
    bucket = "email_overrides" if args.type == "email" else "geographic_overrides"
    geo.setdefault(bucket, {})[key] = args.region
    save_rules(rules)
    print(f"Added override to {bucket}: {key!r} -> {args.region}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    try:
        result = write_stripe_report(_context(args), freeze=args.freeze, supersede=args.supersede)
    except ReportAlreadyFrozenError as exc:
        raise SystemExit(str(exc)) from exc
    return _emit(result)


def cmd_freeze_sent(args: argparse.Namespace) -> int:
    try:
        result = freeze_sent_stripe_report(_context(args), Path(args.file), supersede=args.supersede)
    except (InvalidSentReportError, ReportAlreadyFrozenError) as exc:
        raise SystemExit(str(exc)) from exc
    return _emit(result)


def cmd_archive(args: argparse.Namespace) -> int:
    return _emit(step_archive(_context(args)))


def cmd_relink(args: argparse.Namespace) -> int:
    """Re-point invoice records at moved/renamed PDFs (#151). Exit 1 while anything is unresolved."""
    from src.relink import apply_relink, load_manifests, plan_relink

    init_db()
    old_roots = {"in": args.old_in_dir, "out": args.old_out_dir}
    plan = plan_relink(load_manifests(args.manifest), old_roots=old_roots)
    print(("APPLY" if args.apply else "DRY RUN — nothing written") + ": " + plan.render())
    if args.apply and plan.moves:
        backup = apply_relink(plan)
        print(f"Wrote {len(plan.moves)} move(s); backup: {backup}")
    return 0 if plan.clean else 1


def cmd_fx_backfill(args: argparse.Namespace) -> int:
    """Fetch and store ECB rates from the last stored date up to today (#93).

    Idempotent — safe to rerun at every close-quarter. Uses every currency
    seen in stored invoices/transactions, not just the hard-coded default list.
    """
    from src.fx_rates import backfill_to_today

    stored = backfill_to_today()
    print(f"FX backfill: stored {stored} rate entries.")
    return 0


def cmd_fx_recompute(args: argparse.Namespace) -> int:
    """Re-resolve stored non-EUR invoices' EUR figures at the ECB rate (#93 follow-up).

    Invoices stored before the FX resolver existed (or before its most recent
    fix) still carry whatever EUR figure the LLM guessed. Writes unless
    `--dry-run` is given.
    """
    from src.fx_rates import recompute_stored_invoice_fx

    result = recompute_stored_invoice_fx(dry_run=args.dry_run, since=args.since)
    mode = "DRY RUN — nothing written" if result.dry_run else "APPLIED"
    print(f"FX recompute ({mode}): scanned {result.scanned}, changed {result.changed}, "
          f"stale {result.stale}, cross-check >1% {result.cross_check_flagged}, "
          f"locked (skipped) {result.locked_skipped}.")
    for row in result.rows:
        if row.locked_skipped:
            print(f"  SKIP (locked): {row.filename} ({row.direction}, {row.currency})")
        else:
            print(f"  {row.filename} ({row.direction}, {row.currency}): "
                  f"{row.old_total_eur} -> {row.new_total_eur} EUR [{row.fx_source}]"
                  f"{' STALE' if row.fx_stale else ''}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    default_year, default_quarter = previous_quarter()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_yq(p: argparse.ArgumentParser) -> None:
        p.add_argument("--year", type=int, default=default_year)
        p.add_argument("--quarter", type=int, default=default_quarter, choices=[1, 2, 3, 4])

    def add_step(name: str, func: Callable[[argparse.Namespace], int], help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        add_yq(p)
        p.set_defaults(func=func)
        return p

    add_step("sweep", cmd_sweep, "1. Copy new invoice PDFs into the quarter's tmp folder")

    p_ocr = add_step("ocr", cmd_ocr, "2. Extract new/changed invoice PDFs (OCR + FX + vendor registry)")
    p_ocr.add_argument("--direction", choices=["in", "out", "both"], default="both")
    p_ocr.add_argument("--dry-run", action="store_true", help="List the pending PDFs without extracting")
    p_ocr.add_argument("--model", default=None,
                       help="Hub model id/alias (overrides LLM_HUB_MODEL; default gemini_pro)")

    p_vendors = add_step("vendors", cmd_vendors, "3. Apply the vendor registry to the quarter; list unknown vendors")
    p_vendors.add_argument("--all-periods", action="store_true",
                           help="Re-apply the registry to every period, filed ones included; "
                                "prints the rows written per year/quarter")

    p_dedupe = add_step("dedupe", cmd_dedupe, "4. Duplicate/receipt/out-of-period review")
    p_dedupe.add_argument("--apply", action="store_true", help="Write the proposed exclusions")

    p_fx_step = add_step("fx", cmd_fx, "5. ECB backfill + recompute the quarter's stored non-EUR invoices")
    p_fx_step.add_argument("--apply", action="store_true", help="Write the recomputed EUR figures")

    add_step("stripe", cmd_stripe, "6. Stripe fetch + backfill-emails + reclassify + override warnings")

    p_reta = add_step("reta", cmd_reta, "7. Import RETA (TGSS) debits from a bank export")
    p_reta.add_argument("--file", default=None,
                        help="Bank export (.xls/.xlsx/.csv); default social_security.bank_export_file")

    add_step("compute", cmd_compute, "8. Compute and snapshot Modelo 303/130/349 (+ OSS, 347)")
    add_step("reconcile", cmd_reconcile, "9. Filed vs app (this quarter if filed, else the previous)")
    add_step("sheet", cmd_sheet, "10. Filing sheet from the stored snapshots (AEAT form order, deadlines)")

    p_pack = add_step("gestor-pack", cmd_gestor_pack, "11. Stripe report + notes + draft email for the accountant")
    p_pack.add_argument("--freeze", action="store_true",
                        help="Freeze the Stripe report as the quarter's declared report")

    p_all = add_step("all", cmd_all, "Run steps 1-11 in order")
    p_all.add_argument("--apply", action="store_true", help="Let dedupe and fx write")
    p_all.add_argument("--freeze", action="store_true", help="Let gestor-pack freeze the Stripe report")
    p_all.add_argument("--reta-file", default=None, help="Bank export for the reta step (skipped if absent)")
    p_all.add_argument("--model", default=None, help="Hub model for the ocr step (overrides LLM_HUB_MODEL)")

    p_check = sub.add_parser("stripe-check", help="Read-only Stripe API smoke test")
    p_check.add_argument("--days", type=int, default=90)
    p_check.set_defaults(func=cmd_stripe_check)

    p_fetch = add_step("stripe-fetch", cmd_stripe_fetch, "Fetch + classify + persist the quarter, full review table")
    p_fetch.add_argument("--backfill-fee-split", action="store_true",
                         help="Only fill the stored rows' Stripe/platform fee split from a re-fetch")
    p_fetch.add_argument("--from", dest="from_date", help="YYYY-MM-DD (backfill start, default: quarter start)")
    p_fetch.add_argument("--to", dest="to_date", help="YYYY-MM-DD (backfill end, inclusive, default: quarter end)")
    p_fetch.add_argument("--dry-run", action="store_true", help="With --backfill-fee-split: report, don't write")

    p_override = sub.add_parser("add-override", help="Add a geographic classification override")
    p_override.add_argument("key", help="Substring to match (client name or email)")
    p_override.add_argument("region", choices=["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"])
    p_override.add_argument("--type", choices=["name", "email"], default="name")
    p_override.set_defaults(func=cmd_add_override)

    p_reclassify = sub.add_parser(
        "reclassify", help="Re-run the classifier over stored transactions after a rule change")
    p_reclassify.add_argument("--from", dest="from_date", required=True, help="YYYY-MM-DD (inclusive)")
    p_reclassify.add_argument("--to", dest="to_date", help="YYYY-MM-DD (inclusive, default: latest)")
    p_reclassify.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    p_reclassify.set_defaults(func=cmd_reclassify)

    p_backfill_emails = sub.add_parser(
        "backfill-emails",
        help="Fill empty stored email/billing country from each row's saved raw Stripe charge",
    )
    p_backfill_emails.add_argument("--dry-run", action="store_true", help="Report changes without writing")
    p_backfill_emails.set_defaults(func=cmd_backfill_emails)

    p_report = add_step("report", cmd_report, "Regenerate the quarter's Excel report")
    p_report.add_argument("--freeze", action="store_true",
                          help="Store this report as the quarter's immutable declared report")
    p_report.add_argument("--supersede", action="store_true",
                          help="With --freeze: add a new declared version for a corrected re-send")

    p_freeze_sent = add_step("freeze-sent", cmd_freeze_sent,
                             "Freeze a previously sent Stripe report file as the declared report")
    p_freeze_sent.add_argument("--file", required=True, help="The sent Stripe_Report_Q<Q>_<Y>.xlsx")
    p_freeze_sent.add_argument("--supersede", action="store_true",
                               help="Add a new declared version when the quarter is already declared")

    add_step("archive", cmd_archive, "19. After filing: copy the quarter folder + a DB snapshot to app.archive_dir")

    p_relink = sub.add_parser("relink", help="Re-point invoice records after the PDFs were moved or renamed")
    p_relink.add_argument("--manifest", action="append", default=[],
                          help="Move record CSV with src,dst absolute paths (repeatable)")
    p_relink.add_argument("--old-in-dir", default=None,
                          help="Received-invoices root the stored names refer to (default: the current one)")
    p_relink.add_argument("--old-out-dir", default=None,
                          help="Issued-invoices root the stored names refer to (default: the current one)")
    p_relink.add_argument("--apply", action="store_true", help="Back up the DB, then write the moves")
    p_relink.set_defaults(func=cmd_relink)

    p_fx = sub.add_parser("fx-backfill", help="Backfill ECB FX rates up to today")
    p_fx.set_defaults(func=cmd_fx_backfill)

    p_fx_recompute = sub.add_parser(
        "fx-recompute", help="Re-resolve stored non-EUR invoices' EUR figures at the ECB rate",
    )
    p_fx_recompute.add_argument("--dry-run", action="store_true", dest="dry_run",
                                help="Report changes without writing")
    p_fx_recompute.add_argument("--since", default=None, help="Only invoices dated on/after this ISO date")
    p_fx_recompute.set_defaults(func=cmd_fx_recompute)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
