"""Re-point invoice records at their PDFs after the invoice archive is reorganised (#151).

An invoice row is keyed on ``(filename, direction)``. ``filename`` is the PDF's path
relative to the configured ``invoice_in_dir`` / ``invoice_out_dir``. Moving or
renaming a PDF therefore orphans its row, along with the OCR result, the manual
locks, the exclusions and the fixed-asset link. This module finds each row's new
file and rewrites ``filename`` in place, so the row id and every other field
survive.

Matching, per row, in this order:

1. **Manifest:** a move record (CSV with ``src`` and ``dst`` absolute paths; extra
   columns are ignored) whose ``src`` is the row's old absolute path.
2. **Unchanged:** the file still sits at ``filename`` under the current root with
   the same content hash.
3. **Content hash:** exactly one PDF under the current root has the row's
   ``file_hash``.

A row matched by none of these is **unmatched**. A row whose hash matches several
files, or whose new name is already claimed by another row, is **ambiguous**. Both
are reported as their own states and never guessed. ``apply_relink`` backs up the
database, then writes every move in one transaction.
"""
from __future__ import annotations

import csv
import hashlib
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.database import get_connection
from src.invoice_scanner import resolve_invoice_dir, scan_invoice_pdfs
from src.logger import get_logger

log = get_logger(__name__)

DIRECTIONS = ("in", "out")


@dataclass
class RelinkPlan:
    """What a relink would do. Nothing is written until ``apply_relink``."""

    moves: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # id, dir, old, new, how
    unchanged: int = 0
    unmatched: list[tuple[str, str, str]] = field(default_factory=list)       # id, dir, filename
    ambiguous: list[tuple[str, str, str, str]] = field(default_factory=list)  # id, dir, filename, why

    @property
    def clean(self) -> bool:
        return not self.unmatched and not self.ambiguous

    def render(self) -> str:
        lines = [f"matched: {len(self.moves)} to move, {self.unchanged} unchanged; "
                 f"unmatched: {len(self.unmatched)}; ambiguous: {len(self.ambiguous)}"]
        for rid, d, old, new, how in self.moves:
            lines.append(f"  move [{d}] #{rid} ({how}): {old} -> {new}")
        for rid, d, old in self.unmatched:
            lines.append(f"  ⚠ unmatched [{d}] #{rid}: {old}")
        for rid, d, old, why in self.ambiguous:
            lines.append(f"  ⚠ ambiguous [{d}] #{rid}: {old} ({why})")
        return "\n".join(lines)


def _key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def load_manifests(paths: list[str | Path]) -> dict[str, str]:
    """Merge move records (``src`` → ``dst``) from CSV files, keyed by normalised ``src``."""
    moves: dict[str, str] = {}
    for p in paths:
        with open(p, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            if not reader.fieldnames or not {"src", "dst"} <= set(reader.fieldnames):
                raise ValueError(f"{p}: a move record needs 'src' and 'dst' columns")
            for row in reader:
                if row.get("src") and row.get("dst"):
                    moves[_key(row["src"])] = row["dst"]
    return moves


def _relative(path: str | Path, root: Path) -> Optional[str]:
    """``path`` relative to ``root`` in the scanner's form, or None when it lies outside."""
    rel = os.path.relpath(os.path.normpath(str(path)), os.path.normpath(str(root)))
    if rel == os.pardir or rel.startswith(os.pardir + os.sep) or os.path.isabs(rel):
        return None
    return str(Path(rel))


def plan_relink(
    manifest: dict[str, str],
    old_roots: Optional[dict[str, str | Path]] = None,
    config: Optional[dict[str, Any]] = None,
    db_path: Optional[str | Path] = None,
) -> RelinkPlan:
    """Work out each invoice row's new ``filename`` against the current invoice roots.

    ``old_roots`` are the invoice dirs the stored ``filename`` values were relative
    to. They default to the current ones, which is right when the files were
    renamed in place and the config was not changed.
    """
    new_roots = {d: resolve_invoice_dir(d, config) for d in DIRECTIONS}
    old = {d: Path((old_roots or {}).get(d) or new_roots[d]) for d in DIRECTIONS}
    hash_index: dict[str, dict[str, list[str]]] = {}

    def by_hash(direction: str) -> dict[str, list[str]]:
        if direction not in hash_index:
            idx: dict[str, list[str]] = {}
            for pdf in scan_invoice_pdfs(direction, config):
                idx.setdefault(_md5(pdf), []).append(str(pdf.relative_to(new_roots[direction])))
            hash_index[direction] = idx
        return hash_index[direction]

    plan = RelinkPlan()
    conn = get_connection(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, direction, filename, file_hash FROM invoices ORDER BY id").fetchall()
    finally:
        conn.close()

    for r in rows:
        rid, d, name, fhash = r["id"], r["direction"], r["filename"], r["file_hash"]
        root = new_roots[d]
        dst = manifest.get(_key(old[d] / name))
        if dst is not None:
            rel = _relative(dst, root)
            if rel is None:
                plan.ambiguous.append((rid, d, name, f"moved outside the invoice root: {dst}"))
            elif _key(rel) == _key(name):
                plan.unchanged += 1
            else:
                plan.moves.append((rid, d, name, rel, "manifest"))
            continue
        here = root / name
        if here.exists() and (not fhash or _md5(here) == fhash):
            plan.unchanged += 1
            continue
        candidates = by_hash(d).get(fhash, []) if fhash else []
        if len(candidates) == 1:
            plan.moves.append((rid, d, name, candidates[0], "hash"))
        elif len(candidates) > 1:
            plan.ambiguous.append((rid, d, name, f"{len(candidates)} files share its content"))
        else:
            plan.unmatched.append((rid, d, name))

    # Two rows may not end up on the same file, nor on a file another row keeps.
    targets: dict[tuple[str, str], list[str]] = {}
    for rid, d, _old, new, _how in plan.moves:
        targets.setdefault((d, _key(new)), []).append(rid)
    moved_ids = {m[0] for m in plan.moves}
    kept = {(r["direction"], _key(r["filename"])) for r in rows if r["id"] not in moved_ids}
    clash = {rid for key, ids in targets.items() if len(ids) > 1 or key in kept for rid in ids}
    if clash:
        for m in [m for m in plan.moves if m[0] in clash]:
            plan.moves.remove(m)
            plan.ambiguous.append((m[0], m[1], m[2], f"target {m[3]} is claimed by another row"))
    return plan


def backup_database(db_path: Optional[str | Path] = None, label: str = "pre-relink") -> Path:
    """Copy the database with the SQLite backup API into ``data/backups/`` beside it."""
    conn = get_connection(db_path)
    try:
        src_file = Path(conn.execute("PRAGMA database_list").fetchone()[2])
        dest_dir = src_file.parent / "backups"
        dest_dir.mkdir(exist_ok=True)
        dest = dest_dir / f"accounting_{datetime.now():%Y%m%d-%H%M%S}_{label}.db"
        with sqlite3.connect(dest) as out:
            conn.backup(out)
    finally:
        conn.close()
    return dest


def apply_relink(plan: RelinkPlan, db_path: Optional[str | Path] = None) -> Path:
    """Back up the database, then write every planned move in one transaction.

    Two passes through a placeholder name, so a swap (A takes B's old name while B
    moves on) never trips ``UNIQUE(filename, direction)`` halfway through.
    """
    backup = backup_database(db_path)
    conn = get_connection(db_path)
    try:
        with conn:
            for rid, d, old, _new, _how in plan.moves:
                tmp = f"__relink__{rid}"
                conn.execute("UPDATE invoices SET filename = ? WHERE id = ?", (tmp, rid))
                conn.execute("UPDATE upload_log SET filename = ? WHERE filename = ? AND direction = ?",
                             (tmp, old, d))
            for rid, d, _old, new, _how in plan.moves:
                tmp = f"__relink__{rid}"
                conn.execute("UPDATE invoices SET filename = ? WHERE id = ?", (new, rid))
                conn.execute("UPDATE upload_log SET filename = ? WHERE filename = ? AND direction = ?",
                             (new, tmp, d))
    finally:
        conn.close()
    log.info("✅ Relinked %d invoice record(s); backup at %s", len(plan.moves), backup)
    return backup
