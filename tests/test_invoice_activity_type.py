"""`invoices.activity_type` holds IAE activities only (#191): the OCR expense
`category` is never copied into it, and the P&L per activity does not change.

All names and figures are synthetic."""
from __future__ import annotations

import sqlite3

import pytest

from src.database import init_db, upsert_invoice
from src.pl_by_activity import compute_pl_by_activity
from src.vendor_registry import Vendor, VendorRegistry

YEAR = 2025
CFG: dict = {"tax": {}}


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "activity.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _expense(db, filename, category, activity=None, vendor="Synthetic Vendor Ltd") -> str:
    return upsert_invoice({
        "filename": filename, "direction": "in", "invoice_date": "2025-03-01",
        "vendor_name": vendor, "subtotal_eur": 100.0, "iva_rate": 21, "iva_amount": 21.0,
        "total_eur": 121.0, "category": category, "activity_type": activity,
        "geo_region": "SPAIN",
    }, db_path=db)


def _stored(conn, record_id):
    return conn.execute("SELECT category, activity_type FROM invoices WHERE id = ?",
                        (record_id,)).fetchone()


def test_ocr_category_is_not_copied_into_activity_type(db, conn):
    rid = _expense(db, "a.pdf", "SOFTWARE")
    row = _stored(conn, rid)
    assert row["category"] == "SOFTWARE"
    assert row["activity_type"] is None


def test_explicit_activity_is_kept_next_to_a_category(db, conn):
    rid = _expense(db, "b.pdf", "TRAINING", activity="ILLUSTRATIONS")
    row = _stored(conn, rid)
    assert row["category"] == "TRAINING"
    assert row["activity_type"] == "ILLUSTRATIONS"


def test_pl_per_activity_ignores_the_ocr_category(db, conn):
    _expense(db, "shared.pdf", "SOFTWARE")                                   # no activity → shared
    _expense(db, "ill.pdf", "TRAINING", activity="ILLUSTRATIONS")
    _expense(db, "acme.pdf", "SOFTWARE", vendor="Acme Mailer Ltd")           # registry → NEWSLETTER
    registry = VendorRegistry(vendors=[Vendor(key="acme mailer", activity="NEWSLETTER")])
    pl = compute_pl_by_activity(YEAR, conn, CFG, registry)
    by = {a.iae: a for a in pl.activities}
    assert by["861"].expenses_invoices == 100.0
    assert by["751"].expenses_invoices == 100.0
    assert by["826"].other_expenses == 100.0


# ---------------------------------------------------------------------------
# One-off cleanup script (temp DBs only)
# ---------------------------------------------------------------------------

@pytest.fixture
def cleanup():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).parent.parent / "scripts" / "clean_invoice_activity_type.py"
    spec = importlib.util.spec_from_file_location("clean_invoice_activity_type", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mixed(db, conn):
    """Old-style rows: two OCR categories, one real activity (mixed case), one NULL."""
    for i, value in enumerate(["SOFTWARE", "SOFTWARE", "TRAINING", "coaching", None]):
        conn.execute(
            "INSERT INTO invoices (id, filename, direction, activity_type) VALUES (?, ?, 'in', ?)",
            (f"m{i}", f"m{i}.pdf", value),
        )
    conn.commit()
    return db


def _activities(db):
    c = sqlite3.connect(str(db))
    try:
        return [r[0] for r in c.execute("SELECT activity_type FROM invoices ORDER BY id")]
    finally:
        c.close()


def test_cleanup_defaults_to_a_dry_run(cleanup, mixed, capsys):
    assert cleanup.main(["--db", str(mixed)]) == 0
    out = capsys.readouterr().out
    assert "'SOFTWARE': 2" in out and "'TRAINING': 1" in out
    assert "Dry run: 3 invoice(s)" in out
    assert _activities(mixed) == ["SOFTWARE", "SOFTWARE", "TRAINING", "coaching", None]


def test_cleanup_apply_nulls_only_non_activities(cleanup, mixed, capsys):
    assert cleanup.main(["--db", str(mixed), "--apply"]) == 0
    assert "NULL on 3 invoice(s)" in capsys.readouterr().out
    assert _activities(mixed) == [None, None, None, "coaching", None]
    assert cleanup.main(["--db", str(mixed), "--apply"]) == 0           # idempotent
    assert "nothing to do" in capsys.readouterr().out


def test_cleanup_rejects_apply_with_dry_run(cleanup, mixed):
    with pytest.raises(SystemExit):
        cleanup.main(["--db", str(mixed), "--apply", "--dry-run"])
