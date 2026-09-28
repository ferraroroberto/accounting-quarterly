"""Duplicate / receipt / out-of-period invoice detection (issue #92).

Five detectors, each a pure function over invoice-row dicts (as returned by
``src.database.get_invoices``), proposing a group of losing rows to exclude
and — where the rule has one — the keeper that wins:

1. ``detect_hash_duplicates`` — same ``file_hash`` (byte-identical PDF).
2. ``detect_number_duplicates`` — same (vendor, ``invoice_number``).
3. ``detect_receipt_pairs`` — an invoice/receipt pair: same vendor, same
   total, dates within +/-3 days. The invoice always wins over the receipt.
4. ``detect_email_copies`` — a file under an ``email`` subfolder that
   duplicates a main-folder file.
5. ``detect_out_of_period`` — a row dated outside the quarter being swept.

None of this talks to Streamlit or writes to the DB. ``apply_groups`` is the
only function that mutates state, via ``src.database.set_invoice_exclusion``,
which refuses to touch a row whose ``excluded`` field the user already
locked (a manual decision on a row's exclusion always wins over an automated
one). ``find_duplicate_groups`` runs all detectors in priority order and
never proposes the same row twice.

CLI: ``python -m src.invoice_dedupe scan [--direction in|out] [--apply]
[--year Y --quarter Q]``.
"""
from __future__ import annotations

import argparse
import calendar
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional

from src.database import (
    get_invoices,
    parse_locked_fields,
    set_invoice_exclusion,
)
from src.logger import get_logger

log = get_logger(__name__)

_RECEIPT_KEYWORDS = ("recibo", "receipt")
_RECEIPT_INVOICE_TYPES = {"recibo"}
_RECEIPT_PAIR_MAX_DAYS = 3

ROOT = Path(__file__).parent.parent


@dataclass(frozen=True)
class DuplicateGroup:
    """One proposed exclusion group: ``loser_ids`` lose, ``keeper_id`` (if any) wins."""

    detector: str   # "file_hash" | "invoice_number" | "invoice_receipt_pair" | "email_copy" | "out_of_period"
    reason: str     # one of src.database.EXCLUDED_REASONS
    loser_ids: tuple[str, ...]
    keeper_id: Optional[str] = None
    note: str = ""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_email_path(filename: Optional[str]) -> bool:
    """True if ``filename`` (relative to the invoice dir) has an ``email`` path segment.

    ``filename`` is stored relative-to-invoice-dir and may use either
    separator depending on the OS that ran the OCR pass, so both are checked.
    """
    if not filename:
        return False
    parts = {p.lower() for p in PureWindowsPath(filename).parts}
    parts |= {p.lower() for p in PurePosixPath(filename).parts}
    return "email" in parts


def _is_locked_excluded(row: dict) -> bool:
    return "excluded" in parse_locked_fields(row.get("locked_fields"))


def _is_settled(row: dict) -> bool:
    """True if the row is already excluded, or a user has locked its exclusion state."""
    return bool(row.get("excluded")) or _is_locked_excluded(row)


def _vendor_key(row: dict) -> Optional[str]:
    """``vendor_vat_id_norm`` when known, else the upper-cased vendor name."""
    norm = row.get("vendor_vat_id_norm")
    if norm:
        return norm
    name = (row.get("vendor_name") or "").strip().upper()
    return name or None


def _is_receipt(row: dict) -> bool:
    if (row.get("invoice_type") or "").strip().lower() in _RECEIPT_INVOICE_TYPES:
        return True
    haystack = f"{row.get('filename') or ''} {row.get('description') or ''}".lower()
    return any(kw in haystack for kw in _RECEIPT_KEYWORDS)


def _pick_keeper(rows: list[dict]) -> dict:
    """An invoice always beats a receipt; among the rest, non-``email``-path and
    earliest-ingested wins (S5's tie-break rule). Applies to every detector that
    groups more than a strict invoice/receipt pair (1, 2, 4) so a receipt sharing
    a ``file_hash``/``invoice_number``/email-copy with its invoice is never kept
    over the invoice by accident of ingestion order.
    """
    return min(rows, key=lambda r: (_is_receipt(r), _is_email_path(r.get("filename")), r.get("extracted_at") or ""))


def _eligible_losers(keeper: dict, rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["id"] != keeper["id"] and not _is_settled(r)]


def _parse_date(value: str) -> date:
    return date.fromisoformat(str(value)[:10])


def quarter_bounds(year: int, quarter: int) -> tuple[str, str]:
    """Inclusive ISO ``(start, end)`` date bounds for ``year``-Q``quarter``."""
    month_start = (quarter - 1) * 3 + 1
    month_end = quarter * 3
    last_day = calendar.monthrange(year, month_end)[1]
    return f"{year}-{month_start:02d}-01", f"{year}-{month_end:02d}-{last_day:02d}"


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_hash_duplicates(rows: list[dict]) -> list[DuplicateGroup]:
    """Same ``file_hash`` — byte-identical PDFs ingested under different filenames."""
    buckets: dict[str, list[dict]] = {}
    for row in rows:
        h = row.get("file_hash")
        if h:
            buckets.setdefault(h, []).append(row)
    groups: list[DuplicateGroup] = []
    for h, members in buckets.items():
        if len(members) < 2:
            continue
        keeper = _pick_keeper(members)
        losers = _eligible_losers(keeper, members)
        if losers:
            groups.append(DuplicateGroup(
                detector="file_hash", reason="duplicate",
                loser_ids=tuple(r["id"] for r in losers), keeper_id=keeper["id"],
                note=f"{len(members)} rows share file_hash {h[:8]}...",
            ))
    return groups


def detect_number_duplicates(rows: list[dict]) -> list[DuplicateGroup]:
    """Same (vendor key, ``invoice_number``) — the same invoice ingested twice."""
    buckets: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        number = (row.get("invoice_number") or "").strip()
        vendor = _vendor_key(row)
        if number and vendor:
            buckets.setdefault((vendor, number.upper()), []).append(row)
    groups: list[DuplicateGroup] = []
    for (vendor, number), members in buckets.items():
        if len(members) < 2:
            continue
        keeper = _pick_keeper(members)
        losers = _eligible_losers(keeper, members)
        if losers:
            groups.append(DuplicateGroup(
                detector="invoice_number", reason="duplicate",
                loser_ids=tuple(r["id"] for r in losers), keeper_id=keeper["id"],
                note=f"{len(members)} rows share vendor {vendor} / invoice number {number}",
            ))
    return groups


def detect_receipt_pairs(rows: list[dict]) -> list[DuplicateGroup]:
    """Invoice + receipt pair: same vendor, same total, dates within +/-3 days.

    The invoice always wins; only rows sharing a ``direction`` are paired.
    """
    groups: list[DuplicateGroup] = []
    by_direction: dict[str, list[dict]] = {}
    for row in rows:
        by_direction.setdefault(row.get("direction", "in"), []).append(row)

    for direction_rows in by_direction.values():
        used: set[str] = set()
        for i, a in enumerate(direction_rows):
            if a["id"] in used or a.get("total_eur") is None or not a.get("invoice_date"):
                continue
            vendor_a = _vendor_key(a)
            if vendor_a is None:
                continue
            for b in direction_rows[i + 1:]:
                if b["id"] in used or b.get("total_eur") is None or not b.get("invoice_date"):
                    continue
                if _vendor_key(b) != vendor_a:
                    continue
                if round(a["total_eur"], 2) != round(b["total_eur"], 2):
                    continue
                days = abs((_parse_date(a["invoice_date"]) - _parse_date(b["invoice_date"])).days)
                if days > _RECEIPT_PAIR_MAX_DAYS:
                    continue
                a_is_receipt, b_is_receipt = _is_receipt(a), _is_receipt(b)
                if a_is_receipt == b_is_receipt:
                    continue  # need exactly one receipt side to call it a pair
                invoice_row, receipt_row = (b, a) if a_is_receipt else (a, b)
                if _is_settled(receipt_row):
                    continue
                groups.append(DuplicateGroup(
                    detector="invoice_receipt_pair", reason="receipt",
                    loser_ids=(receipt_row["id"],), keeper_id=invoice_row["id"],
                    note=(f"receipt {receipt_row.get('filename')} duplicates invoice "
                          f"{invoice_row.get('filename')} (vendor {vendor_a}, "
                          f"{a['total_eur']:.2f} EUR, {days}d apart)"),
                ))
                used.add(receipt_row["id"])
                break
    return groups


def detect_email_copies(rows: list[dict]) -> list[DuplicateGroup]:
    """A file under an ``email`` subfolder that duplicates a main-folder file.

    Matches on (vendor key, ``invoice_number``) when the number is known,
    else on (vendor key, ``total_eur``, ``invoice_date``) — covers image-only
    receipts OCR may not assign a stable invoice_number to.
    """
    groups: list[DuplicateGroup] = []
    non_email = [r for r in rows if not _is_email_path(r.get("filename"))]
    email_rows = [r for r in rows if _is_email_path(r.get("filename"))]

    for e in email_rows:
        if _is_settled(e):
            continue
        vendor = _vendor_key(e)
        if vendor is None:
            continue
        number = (e.get("invoice_number") or "").strip().upper()
        match = None
        for n in non_email:
            if n["direction"] != e["direction"] or _vendor_key(n) != vendor:
                continue
            if number:
                if (n.get("invoice_number") or "").strip().upper() == number:
                    match = n
                    break
            elif (n.get("total_eur") is not None and e.get("total_eur") is not None
                    and round(n["total_eur"], 2) == round(e["total_eur"], 2)
                    and n.get("invoice_date") == e.get("invoice_date")):
                match = n
                break
        if match is not None:
            groups.append(DuplicateGroup(
                detector="email_copy", reason="duplicate",
                loser_ids=(e["id"],), keeper_id=match["id"],
                note=f"{e.get('filename')} is an email copy of {match.get('filename')}",
            ))
    return groups


def detect_out_of_period(rows: list[dict], year: int, quarter: int) -> list[DuplicateGroup]:
    """Rows whose ``invoice_date`` falls outside the quarter being swept.

    ``rows`` should be scoped by the caller to the sweep in question (e.g. the
    invoices matching the files copied into ``tmp/close_quarter/<year>_Q<quarter>/``)
    — a full-table scan would flag every other quarter's invoices too.
    """
    start, end = quarter_bounds(year, quarter)
    groups: list[DuplicateGroup] = []
    for row in rows:
        inv_date = row.get("invoice_date")
        if not inv_date or start <= inv_date <= end or _is_settled(row):
            continue
        groups.append(DuplicateGroup(
            detector="out_of_period", reason="other_period",
            loser_ids=(row["id"],), keeper_id=None,
            note=f"{row.get('filename')} dated {inv_date} falls outside {year}-Q{quarter} ({start}..{end})",
        ))
    return groups


# ---------------------------------------------------------------------------
# Combined scan + apply
# ---------------------------------------------------------------------------

def find_duplicate_groups(
    rows: list[dict],
    sweep_rows: Optional[list[dict]] = None,
    year: Optional[int] = None,
    quarter: Optional[int] = None,
) -> list[DuplicateGroup]:
    """Run all detectors over ``rows`` in priority order (1 to 5 above).

    A row already proposed as a loser by an earlier detector is dropped from
    consideration by later ones, so no row is ever proposed twice with
    conflicting reasons. Detector 5 (out-of-period) only runs when
    ``sweep_rows``, ``year`` and ``quarter`` are all given, and only sees
    ``sweep_rows`` (see ``detect_out_of_period``).
    """
    groups: list[DuplicateGroup] = []
    claimed: set[str] = set()

    def _run(detector_groups: list[DuplicateGroup]) -> None:
        for g in detector_groups:
            remaining = tuple(lid for lid in g.loser_ids if lid not in claimed)
            if not remaining:
                continue
            claimed.update(remaining)
            groups.append(g if remaining == g.loser_ids else DuplicateGroup(
                g.detector, g.reason, remaining, g.keeper_id, g.note
            ))

    _run(detect_hash_duplicates(rows))
    _run(detect_number_duplicates([r for r in rows if r["id"] not in claimed]))
    _run(detect_receipt_pairs([r for r in rows if r["id"] not in claimed]))
    _run(detect_email_copies([r for r in rows if r["id"] not in claimed]))
    if sweep_rows is not None and year is not None and quarter is not None:
        _run(detect_out_of_period([r for r in sweep_rows if r["id"] not in claimed], year, quarter))
    return groups


def apply_groups(groups: list[DuplicateGroup], db_path: Optional[str] = None) -> dict:
    """Write each group's losers as ``excluded`` (ledger-only, unlocked).

    Returns ``{"applied": n, "skipped_locked": n, "by_detector": {...}}``.
    Never overrides a row the user already locked — ``set_invoice_exclusion``
    enforces that per row.
    """
    applied = 0
    skipped = 0
    by_detector: dict[str, int] = {}
    for g in groups:
        for loser_id in g.loser_ids:
            ok = set_invoice_exclusion(loser_id, True, g.reason, db_path=db_path)
            if ok:
                applied += 1
                by_detector[g.detector] = by_detector.get(g.detector, 0) + 1
            else:
                skipped += 1
                log.info(
                    "ℹ️ Skipped auto-exclusion of invoice %s (%s): "
                    "excluded is locked by a manual edit", loser_id, g.detector,
                )
    if applied or skipped:
        log.info("ℹ️ Invoice dedupe applied %d exclusion(s), skipped %d locked row(s): %s",
                  applied, skipped, by_detector)
    return {"applied": applied, "skipped_locked": skipped, "by_detector": by_detector}


# ---------------------------------------------------------------------------
# Sweep-folder scoping for detector 5 (best-effort — see the sweep manifest's
# lossy filename mangling in scripts/close_quarter.py:cmd_sweep)
# ---------------------------------------------------------------------------

def load_sweep_rows(year: int, quarter: int, db_path: Optional[str] = None) -> list[dict]:
    """Invoice rows whose swept copy lives in ``tmp/close_quarter/<year>_Q<quarter>/``.

    ``scripts/close_quarter.py``'s sweep renames files (``"IN - " + rel path
    with separators flattened to " - "``), so the reverse match here is
    best-effort: it recovers the original relative filename assuming the
    filename itself contains no literal ``" - "``, and looks it up by
    (direction, filename). Files that don't resolve to a known invoice row are
    skipped. Returns ``[]`` if the sweep folder doesn't exist.
    """
    sweep_dir = ROOT / "tmp" / "close_quarter" / f"{year}_Q{quarter}"
    if not sweep_dir.is_dir():
        return []
    rows_by_key = {
        (r["direction"], r["filename"]): r for r in get_invoices(db_path=db_path)
    }
    matched: list[dict] = []
    for path in sweep_dir.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name.startswith("IN - "):
            direction, rel = "in", name[len("IN - "):]
        elif name.startswith("OUT - "):
            direction, rel = "out", name[len("OUT - "):]
        else:
            continue
        for sep in ("/", "\\"):
            candidate = rel.replace(" - ", sep)
            row = rows_by_key.get((direction, candidate))
            if row is not None:
                matched.append(row)
                break
    return matched


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_groups(groups: list[DuplicateGroup]) -> None:
    if not groups:
        print("No duplicate/receipt/out-of-period groups found.")
        return
    by_detector: dict[str, int] = {}
    for g in groups:
        by_detector[g.detector] = by_detector.get(g.detector, 0) + len(g.loser_ids)
        keeper = f" keeps {g.keeper_id}" if g.keeper_id else ""
        print(f"[{g.detector}/{g.reason}]{keeper} excludes {', '.join(g.loser_ids)} -- {g.note}")
    print()
    print("Summary: " + ", ".join(f"{k}={v}" for k, v in sorted(by_detector.items())))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.invoice_dedupe")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Detect duplicate/receipt/out-of-period invoice groups")
    scan.add_argument("--direction", choices=("in", "out"), default=None)
    scan.add_argument("--year", type=int, default=None, help="Sweep year (enables the out-of-period detector)")
    scan.add_argument("--quarter", type=int, choices=(1, 2, 3, 4), default=None)
    scan.add_argument("--apply", action="store_true", help="Write the proposed exclusions to the DB")
    scan.add_argument("--db", default=None, help="DB path override (default: data/accounting.db)")

    args = parser.parse_args(argv)

    rows = get_invoices(direction=args.direction, db_path=args.db)
    sweep_rows = None
    if args.year and args.quarter:
        sweep_rows = load_sweep_rows(args.year, args.quarter, db_path=args.db)
        if args.direction:
            sweep_rows = [r for r in sweep_rows if r["direction"] == args.direction]
    groups = find_duplicate_groups(rows, sweep_rows=sweep_rows, year=args.year, quarter=args.quarter)
    _print_groups(groups)
    if args.apply and groups:
        result = apply_groups(groups, db_path=args.db)
        print(f"\nApplied {result['applied']} exclusion(s); skipped {result['skipped_locked']} locked row(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
