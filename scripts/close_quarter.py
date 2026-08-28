"""Deterministic helper for closing a Stripe accounting quarter.

Run from the repo root with the project venv:
    .venv/Scripts/python.exe scripts/close_quarter.py <subcommand> [options]

Subcommands:
    sweep         Copy new invoice PDFs (received + sent) not yet copied or
                  catalogued into tmp/close_quarter/<year>_Q<quarter>/, and
                  update the cumulative copy-log manifest so re-runs only
                  pick up files added since the last sweep.
    stripe-check  Read-only Stripe API smoke test. No DB writes.
    stripe-fetch  Fetch + classify + persist the target quarter's Stripe
                  charges, then print a review table flagging transactions
                  classified by a default geo rule (no client-specific
                  override) so they can be double-checked.
    add-override  Add a geographic classification override (name/email
                  substring -> region) to classification_rules.json.
    report        Regenerate the quarter's Excel report from the DB and
                  save it into the same tmp/close_quarter/<year>_Q<quarter>/
                  folder as the swept invoices.

All outputs are written under tmp/, which is git-ignored.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

from src.config import load_config  # noqa: E402
from src.invoice_scanner import resolve_invoice_dir, scan_invoice_pdfs  # noqa: E402
from src.rules_engine import load_rules, save_rules  # noqa: E402
from src.stripe_client import fetch_charges  # noqa: E402

# app.data_loader pulls in Streamlit (for @st.cache_data); import it lazily,
# only inside the subcommands that actually need it, so `sweep` / `stripe-check`
# / `add-override` stay free of Streamlit's "no runtime found" cache warning.

MANIFEST_PATH = ROOT / "tmp" / "close_quarter" / "invoice_copy_log.json"
DEFAULT_GEO_RULES = {"eur_default", "eur_newsletter_default", "non_eur_default"}


def previous_quarter(today: datetime | None = None) -> tuple[int, int]:
    """Return (year, quarter) for the most recently completed calendar quarter."""
    today = today or datetime.now()
    q = (today.month - 1) // 3 + 1
    if q == 1:
        return today.year - 1, 4
    return today.year, q - 1


def quarter_out_dir(year: int, quarter: int) -> Path:
    d = ROOT / "tmp" / "close_quarter" / f"{year}_Q{quarter}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_manifest() -> dict:
    if MANIFEST_PATH.exists():
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    return {"in": [], "out": []}


def _save_manifest(manifest: dict) -> None:
    MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def _relname(p: Path, base: Path) -> str:
    return str(p.relative_to(base))


def cmd_sweep(args: argparse.Namespace) -> None:
    cfg = load_config()
    in_dir = resolve_invoice_dir("in", cfg)
    out_dir = resolve_invoice_dir("out", cfg)
    dest = quarter_out_dir(args.year, args.quarter)

    conn = sqlite3.connect(ROOT / "data" / "accounting.db")
    conn.row_factory = sqlite3.Row
    known_in = {r["filename"] for r in conn.execute("SELECT filename FROM invoices WHERE direction='in'")}
    known_out = {r["filename"] for r in conn.execute("SELECT filename FROM invoices WHERE direction='out'")}
    conn.close()

    manifest = _load_manifest()
    already_in = set(manifest.get("in", []))
    already_out = set(manifest.get("out", []))

    pdfs_in = scan_invoice_pdfs("in", cfg)
    pdfs_out = scan_invoice_pdfs("out", cfg)

    new_in = [p for p in pdfs_in
              if _relname(p, in_dir) not in known_in and _relname(p, in_dir) not in already_in]
    new_out = [p for p in pdfs_out
               if _relname(p, out_dir) not in known_out and _relname(p, out_dir) not in already_out]

    copied_in, copied_out = [], []
    for p in new_in:
        rel = _relname(p, in_dir)
        dest_name = "IN - " + rel.replace("\\", " - ").replace("/", " - ")
        shutil.copy2(p, dest / dest_name)
        copied_in.append(rel)
    for p in new_out:
        rel = _relname(p, out_dir)
        dest_name = "OUT - " + rel.replace("\\", " - ").replace("/", " - ")
        shutil.copy2(p, dest / dest_name)
        copied_out.append(rel)

    manifest["in"] = sorted(already_in | set(copied_in))
    manifest["out"] = sorted(already_out | set(copied_out))
    manifest["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    _save_manifest(manifest)

    print(f"Copied {len(copied_in)} received + {len(copied_out)} sent invoices -> {dest}")
    for rel in copied_in:
        print(f"  IN  {rel}")
    for rel in copied_out:
        print(f"  OUT {rel}")
    if not copied_in and not copied_out:
        print("No new invoices found.")


def cmd_stripe_check(args: argparse.Namespace) -> None:
    end = datetime.now()
    start = end - timedelta(days=args.days)
    payments = fetch_charges(start, end)
    print(f"OK: Stripe API reachable, {len(payments)} charges in the last {args.days} days "
          f"(read-only, no DB writes).")


def cmd_stripe_fetch(args: argparse.Namespace) -> None:
    from app.data_loader import get_classified_for_period, quarter_dates
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


def cmd_add_override(args: argparse.Namespace) -> None:
    rules = load_rules()
    geo = rules.setdefault("geographic_rules", {})
    key = args.key.strip().lower()
    bucket = "email_overrides" if args.type == "email" else "geographic_overrides"
    geo.setdefault(bucket, {})[key] = args.region
    save_rules(rules)
    print(f"Added override to {bucket}: {key!r} -> {args.region}")


def cmd_report(args: argparse.Namespace) -> None:
    from app.data_loader import get_classified_for_period, quarter_dates
    from src.excel_exporter import create_excel_report, generate_report_filename

    start, end = quarter_dates(args.year, args.quarter)
    payments = get_classified_for_period(args.year, args.quarter, start, end, input_mode="db")
    filename = generate_report_filename(args.year, args.quarter)
    dest = quarter_out_dir(args.year, args.quarter) / filename
    create_excel_report(payments, dest, args.year, args.quarter, f"Q{args.quarter}_{args.year}")
    print(f"Saved: {dest}")


def main() -> None:
    default_year, default_quarter = previous_quarter()

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_yq(p: argparse.ArgumentParser) -> None:
        p.add_argument("--year", type=int, default=default_year)
        p.add_argument("--quarter", type=int, default=default_quarter, choices=[1, 2, 3, 4])

    p_sweep = sub.add_parser("sweep", help="Copy new invoice PDFs into the quarter's tmp folder")
    add_yq(p_sweep)
    p_sweep.set_defaults(func=cmd_sweep)

    p_check = sub.add_parser("stripe-check", help="Read-only Stripe API smoke test")
    p_check.add_argument("--days", type=int, default=90)
    p_check.set_defaults(func=cmd_stripe_check)

    p_fetch = sub.add_parser("stripe-fetch", help="Fetch + classify + persist the target quarter")
    add_yq(p_fetch)
    p_fetch.set_defaults(func=cmd_stripe_fetch)

    p_override = sub.add_parser("add-override", help="Add a geographic classification override")
    p_override.add_argument("key", help="Substring to match (client name or email)")
    p_override.add_argument("region", choices=["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"])
    p_override.add_argument("--type", choices=["name", "email"], default="name")
    p_override.set_defaults(func=cmd_add_override)

    p_report = sub.add_parser("report", help="Regenerate the quarter's Excel report")
    add_yq(p_report)
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
