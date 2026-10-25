"""Shared types of the quarter-close pipeline: the step result, the close context and file helpers."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from src.database import get_connection, load_classified_payments
from src.invoice_dedupe import SWEEP_ROOT
from src.models import ClassifiedPayment
from src.periods import quarter_datetime_bounds, quarter_iso_bounds
from src.tax_data import load_app_config

ROOT = Path(__file__).parent.parent.parent
DEFAULT_OUT_ROOT = SWEEP_ROOT  # single source: src/invoice_dedupe.py
GESTOR_NOTES_PATH = ROOT / "gestor_notes.md"  # git-ignored free-text notes for the accountant


@dataclass
class StepResult:
    """Outcome of one pipeline step. ``changes`` empty ⇒ the step was a no-op."""

    step: str
    changes: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    output: str = ""  # a document the CLI prints after the summary (markdown table, sheet)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def merge(self, other: "StepResult") -> None:
        self.changes += other.changes
        self.info += other.info
        self.warnings += other.warnings
        self.errors += other.errors

    def render(self) -> str:
        head = f"[{self.step}] " + (f"{len(self.changes)} change(s)" if self.changes else "no changes")
        lines = [head]
        lines += [f"  + {c}" for c in self.changes]
        lines += [f"    {i}" for i in self.info]
        lines += [f"  ⚠ {w}" for w in self.warnings]
        lines += [f"  ❌ {e}" for e in self.errors]
        return "\n".join(lines)


@dataclass
class CloseContext:
    """Where one quarter close reads and writes. Defaults are the real app paths."""

    year: int
    quarter: int
    db_path: Optional[Path] = None          # None → data/accounting.db
    config: Optional[dict[str, Any]] = None  # None → config.json (empty if missing)
    rules: Optional[dict[str, Any]] = None   # None → classification_rules.json
    out_root: Path = DEFAULT_OUT_ROOT
    notes_path: Path = GESTOR_NOTES_PATH

    def __post_init__(self) -> None:
        if self.quarter not in (1, 2, 3, 4):
            raise ValueError(f"quarter must be 1-4, got {self.quarter}")
        if self.config is None:
            self.config = load_app_config()

    @property
    def period(self) -> str:
        return f"{self.year} Q{self.quarter}"

    @property
    def quarter_dir(self) -> Path:
        d = self.out_root / f"{self.year}_Q{self.quarter}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def manifest_path(self) -> Path:
        return self.out_root / "invoice_copy_log.json"

    @property
    def date_bounds(self) -> tuple[str, str]:
        """Inclusive ISO date bounds of the quarter."""
        return quarter_iso_bounds(self.year, self.quarter)

    @property
    def datetime_bounds(self) -> tuple[datetime, datetime]:
        """Quarter start 00:00:00 and end 23:59:59 (the Stripe transaction window)."""
        return quarter_datetime_bounds(self.year, self.quarter)

    def previous_period(self) -> tuple[int, int]:
        return (self.year - 1, 4) if self.quarter == 1 else (self.year, self.quarter - 1)

    def connect(self) -> sqlite3.Connection:
        return get_connection(self.db_path)


def write_if_changed(path: Path, text: str) -> bool:
    """Write ``text`` to ``path`` unless it already holds exactly that. True if written."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def same_file(a: Path, b: Path) -> bool:
    return b.exists() and a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()


def is_in_quarter(ctx: CloseContext, iso_date: Optional[str]) -> bool:
    start, end = ctx.date_bounds
    return bool(iso_date) and start <= str(iso_date)[:10] <= end


def quarter_payments(ctx: CloseContext) -> list[ClassifiedPayment]:
    start, end = ctx.datetime_bounds
    return load_classified_payments(start, end, db_path=ctx.db_path)
