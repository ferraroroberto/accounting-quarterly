"""One-off cleanup for accounting-quarterly#191: reset non-activity `invoices.activity_type` to NULL.

Before #191 `upsert_invoice` copied the OCR expense `category` (SOFTWARE, TRAINING, ...)
into `activity_type` when no activity was given, so old rows mix two vocabularies. The
column now holds IAE activities only (COACHING | NEWSLETTER | ILLUSTRATIONS). The copied
value is still in the row's `category` column, so nothing is lost by clearing it.

Run from the repo root with the project venv. The default is a dry run that only lists
what would change; nothing is written without --apply:
    .venv/Scripts/python.exe scripts/clean_invoice_activity_type.py            # dry run
    .venv/Scripts/python.exe scripts/clean_invoice_activity_type.py --apply    # write
    .venv/Scripts/python.exe scripts/clean_invoice_activity_type.py --db PATH  # other DB
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.database import get_connection  # noqa: E402
from src.vendor_registry import ACTIVITIES  # noqa: E402

_NON_ACTIVITY = (
    "activity_type IS NOT NULL "
    f"AND UPPER(TRIM(activity_type)) NOT IN ({', '.join('?' for _ in ACTIVITIES)})"
)


def find_non_activity_values(conn: sqlite3.Connection) -> dict[str, int]:
    """Count of invoices per stored `activity_type` value that is not an IAE activity."""
    rows = conn.execute(
        f"SELECT activity_type, COUNT(*) AS n FROM invoices WHERE {_NON_ACTIVITY} "
        "GROUP BY activity_type ORDER BY activity_type",
        ACTIVITIES,
    ).fetchall()
    return {str(r[0]): int(r[1]) for r in rows}


def clear_non_activity_values(conn: sqlite3.Connection) -> int:
    """Set every non-activity `activity_type` to NULL; returns the rows changed."""
    cur = conn.execute(f"UPDATE invoices SET activity_type = NULL WHERE {_NON_ACTIVITY}", ACTIVITIES)
    conn.commit()
    return cur.rowcount


def run(db_path: Optional[Path] = None, apply: bool = False) -> int:
    conn = get_connection(db_path)
    try:
        found = find_non_activity_values(conn)
        total = sum(found.values())
        if not total:
            print("✅ No invoice holds a non-activity activity_type — nothing to do.")
            return 0
        for value, n in found.items():
            print(f"  {value!r}: {n} invoice(s)")
        if not apply:
            print(f"ℹ️ Dry run: {total} invoice(s) would be set to NULL. Re-run with --apply to write.")
            return 0
        changed = clear_non_activity_values(conn)
        print(f"✅ Set activity_type to NULL on {changed} invoice(s).")
        return 0
    finally:
        conn.close()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="write the change (default: dry run)")
    parser.add_argument("--dry-run", action="store_true", help="list only (the default; explicit form)")
    parser.add_argument("--db", type=Path, default=None, help="SQLite file (default: data/accounting.db)")
    args = parser.parse_args(argv)
    if args.apply and args.dry_run:
        parser.error("--apply and --dry-run are mutually exclusive")
    return run(args.db, apply=args.apply)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
