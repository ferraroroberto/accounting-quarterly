"""SQLite connection and the default database path."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional


_DB_PATH = Path(__file__).parent.parent.parent / "data" / "accounting.db"


def get_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn
