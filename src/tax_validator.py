"""Loader for the gestor-filed AEAT declarations the Reconciliation compares against.

Reference data comes first from the filed AEAT receipts imported into the
database (`src/filed_returns.py`, tables `filed_returns` /
`filed_349_operators`). `tmp/validation/validation.yaml` (gitignored) is a
fallback for periods whose receipt has not been imported. The box-by-box
comparison itself lives in `src/reconciliation.py`.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

import yaml

from src.filed_returns import load_filings as load_db_filings

_YAML_PATH = Path(__file__).parent.parent / "tmp" / "validation" / "validation.yaml"


# ---------------------------------------------------------------------------
# Reference-data loaders
# ---------------------------------------------------------------------------

def _load_yaml_filings() -> list[dict]:
    """Load all filed declarations from the YAML reference file."""
    if not _YAML_PATH.exists():
        return []
    with _YAML_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("filings", []) if data else []


def _filing_key(f: dict) -> tuple[str, int, Optional[int]]:
    return (str(f.get("model")), int(f.get("year", 0)), f.get("quarter"))


def _load_filings(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Filed declarations: imported AEAT receipts first, YAML as fallback.

    A YAML entry is used only for a (model, year, quarter) that has no
    imported receipt in the database.
    """
    db_filings = load_db_filings(conn) if conn is not None else []
    have = {_filing_key(f) for f in db_filings}
    yaml_filings = [
        {**f, "source": "yaml"} for f in _load_yaml_filings() if _filing_key(f) not in have
    ]
    return db_filings + yaml_filings


def _find_filing(filings: list[dict], model: str, year: int, quarter: int | None) -> dict | None:
    for f in filings:
        if _filing_key(f) == (model, year, quarter):
            return f
    return None


def load_filings(conn: Optional[sqlite3.Connection] = None) -> list[dict]:
    """Every filed declaration (imported receipts first, validation.yaml fallback)."""
    return _load_filings(conn)


def find_filing(
    filings: list[dict], model: str, year: int, quarter: Optional[int]
) -> Optional[dict]:
    """The filing for (model, year, quarter) — quarter None for annual — or None."""
    return _find_filing(filings, model, year, quarter)
