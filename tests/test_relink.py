"""Relink invoice records after the PDFs are moved or renamed (#151).

A temp DB and synthetic PDFs only: fake vendors, no real paths, no network.
"""
from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

import pytest

import src.database as database
from src.close_pipeline import CloseContext, step_archive
from src.database import init_db, upsert_invoice
from src.relink import apply_relink, load_manifests, plan_relink


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = tmp_path / "data" / "accounting.db"
    db.parent.mkdir()
    monkeypatch.setattr(database, "_DB_PATH", db)  # safety net: nothing may reach the real DB
    init_db(db)
    old_in, new_in = tmp_path / "old" / "in", tmp_path / "new" / "in"
    old_out, new_out = tmp_path / "old" / "out", tmp_path / "new" / "out"
    for d in (old_in, new_in, old_out, new_out):
        d.mkdir(parents=True)
    config = {"app": {"invoice_in_dir": str(new_in), "invoice_out_dir": str(new_out)}}
    return {"db": db, "old_in": old_in, "new_in": new_in, "old_out": old_out, "config": config,
            "tmp": tmp_path}


def _pdf(path: Path, content: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"%PDF-1.4 synthetic {content}".encode())
    return hashlib.md5(path.read_bytes()).hexdigest()


def _record(env, rel: str, content: str, **extra) -> str:
    """A parsed invoice whose PDF sits at old_in/rel."""
    h = _pdf(env["old_in"] / rel, content)
    return upsert_invoice({"filename": str(Path(rel)), "direction": "in", "file_hash": h,
                           "invoice_date": "2025-02-10", "total_eur": 100.0, **extra}, db_path=env["db"])


def _move(env, rel: str, new_rel: str) -> tuple[str, str]:
    src, dst = env["old_in"] / rel, env["new_in"] / new_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return str(src), str(dst)


def _manifest(env, rows: list[tuple[str, str]]) -> Path:
    path = env["tmp"] / "moves.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["subject", "src", "dst"])
        for src, dst in rows:
            w.writerow(["autonomo", src, dst])
    return path


def _row(env, rid: str) -> sqlite3.Row:
    conn = sqlite3.connect(env["db"])
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone()
    finally:
        conn.close()


def _plan(env, manifest_rows=()):
    manifest = load_manifests([_manifest(env, list(manifest_rows))]) if manifest_rows else {}
    return plan_relink(manifest, old_roots={"in": env["old_in"], "out": env["old_out"]},
                       config=env["config"], db_path=env["db"])


def test_manifest_match_moves_and_renames(env):
    rid = _record(env, "Example Cloud/202502 - cloud.pdf", "a", total_eur=123.45)
    move = _move(env, "Example Cloud/202502 - cloud.pdf", "Example Cloud/2025-02-10 - 202502 - cloud.pdf")
    plan = _plan(env, [move])
    assert plan.clean and len(plan.moves) == 1 and plan.moves[0][4] == "manifest"
    apply_relink(plan, db_path=env["db"])
    row = _row(env, rid)
    assert row["filename"] == str(Path("Example Cloud/2025-02-10 - 202502 - cloud.pdf"))
    assert row["total_eur"] == 123.45  # same row, fields preserved


def test_hash_fallback_without_manifest_row(env):
    rid = _record(env, "Sample Supplies/inv.pdf", "b")
    _move(env, "Sample Supplies/inv.pdf", "Sample Supplies/2025-03-01 - inv.pdf")
    plan = _plan(env)
    assert plan.clean and plan.moves[0][0] == rid and plan.moves[0][4] == "hash"


def test_unmatched_is_reported_not_guessed(env):
    _record(env, "Gone Vendor/lost.pdf", "c")
    (env["old_in"] / "Gone Vendor/lost.pdf").unlink()
    plan = _plan(env)
    assert not plan.clean and len(plan.unmatched) == 1 and not plan.moves


def test_duplicate_content_is_ambiguous_without_manifest(env):
    _record(env, "Dup Vendor/a.pdf", "same")
    _record(env, "Dup Vendor/b.pdf", "same")
    _move(env, "Dup Vendor/a.pdf", "Dup Vendor/2025 - a.pdf")
    _move(env, "Dup Vendor/b.pdf", "Dup Vendor/2025 - b.pdf")
    plan = _plan(env)
    assert len(plan.ambiguous) == 2 and not plan.moves


def test_duplicate_content_resolves_with_manifest(env):
    _record(env, "Dup Vendor/a.pdf", "same")
    _record(env, "Dup Vendor/b.pdf", "same")
    rows = [_move(env, "Dup Vendor/a.pdf", "Dup Vendor/2025 - a.pdf"),
            _move(env, "Dup Vendor/b.pdf", "Dup Vendor/2025 - b.pdf")]
    assert _plan(env, rows).clean


def test_folder_move_keeps_relative_name_unchanged(env):
    _record(env, "Example Cloud/x.pdf", "d")
    _move(env, "Example Cloud/x.pdf", "Example Cloud/x.pdf")  # new root, same relative path
    plan = _plan(env)
    assert plan.clean and plan.unchanged == 1 and not plan.moves


def test_swap_does_not_trip_the_unique_key(env):
    ra = _record(env, "V/a.pdf", "A")
    rb = _record(env, "V/b.pdf", "B")
    # a.pdf's content now lives at b.pdf and vice versa.
    (env["new_in"] / "V").mkdir(parents=True)
    (env["old_in"] / "V/a.pdf").rename(env["new_in"] / "V/b.pdf")
    (env["old_in"] / "V/b.pdf").rename(env["new_in"] / "V/a.pdf")
    plan = _plan(env)
    assert plan.clean and len(plan.moves) == 2
    apply_relink(plan, db_path=env["db"])
    assert _row(env, ra)["filename"] == str(Path("V/b.pdf"))
    assert _row(env, rb)["filename"] == str(Path("V/a.pdf"))


def test_apply_is_idempotent_and_backs_up(env):
    _record(env, "V/a.pdf", "e")
    move = _move(env, "V/a.pdf", "V/2025 - a.pdf")
    backup = apply_relink(_plan(env, [move]), db_path=env["db"])
    assert backup.exists() and backup.parent.name == "backups"
    again = plan_relink({}, config=env["config"], db_path=env["db"])  # second run, current roots
    assert again.clean and not again.moves and again.unchanged == 1


def test_manifest_without_src_dst_columns_is_rejected(env):
    bad = env["tmp"] / "bad.csv"
    bad.write_text("from,to\nx,y\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load_manifests([bad])


def test_archive_copies_quarter_folder_and_snapshot(env):
    archive = env["tmp"] / "archive"
    config = {**env["config"], "app": {**env["config"]["app"], "archive_dir": str(archive)}}
    ctx = CloseContext(2025, 1, db_path=env["db"], config=config, rules={},
                       out_root=env["tmp"] / "close_quarter")
    (ctx.quarter_dir / "filing_sheet_2025_Q1.md").write_text("sheet", encoding="utf-8")
    first = step_archive(ctx)
    assert first.changed and (archive / "2025T1" / "filing_sheet_2025_Q1.md").exists()
    assert list((archive / "2025T1" / "database").glob("accounting_*.db"))
    assert not step_archive(ctx).changed  # nothing new on a re-run


def test_archive_without_archive_dir_is_an_error(env):
    ctx = CloseContext(2025, 1, db_path=env["db"], config=env["config"], rules={},
                       out_root=env["tmp"] / "close_quarter")
    assert step_archive(ctx).errors
