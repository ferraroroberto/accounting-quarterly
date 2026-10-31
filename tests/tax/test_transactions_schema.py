"""The retired per-payment VAT override columns leave `transactions` only when empty."""
from __future__ import annotations

import sqlite3

import pytest

from src.database import init_db
from src.database.schema import _RETIRED_TRANSACTIONS_COLUMNS

_RETIRED_DDL = {
    "vat_treatment": "TEXT", "vat_base_eur": "REAL", "vat_amount_eur": "REAL", "oss_country": "TEXT",
}


def _columns(conn: sqlite3.Connection) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}


@pytest.fixture
def legacy_db(tmp_path):
    """A database created before the drop: `transactions` still carries the four columns."""
    path = tmp_path / "legacy.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    for col, ddl in _RETIRED_DDL.items():
        conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")
    conn.execute(
        "INSERT INTO transactions (id, created_date, converted_amount) VALUES ('t1', '2025-01-01T00:00:00', 10)"
    )
    conn.commit()
    conn.close()
    return path


def test_fresh_database_has_no_retired_columns(tmp_path):
    path = tmp_path / "fresh.db"
    init_db(path)
    conn = sqlite3.connect(str(path))
    assert not set(_RETIRED_TRANSACTIONS_COLUMNS) & _columns(conn)
    conn.close()


def test_all_null_columns_are_dropped_and_rows_kept(legacy_db):
    init_db(legacy_db)
    conn = sqlite3.connect(str(legacy_db))
    assert not set(_RETIRED_TRANSACTIONS_COLUMNS) & _columns(conn)
    assert conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    conn.close()


def test_column_holding_a_value_is_kept(legacy_db):
    conn = sqlite3.connect(str(legacy_db))
    conn.execute("UPDATE transactions SET oss_country = 'DE'")
    conn.commit()
    conn.close()
    init_db(legacy_db)
    conn = sqlite3.connect(str(legacy_db))
    cols = _columns(conn)
    assert "oss_country" in cols
    assert not {"vat_treatment", "vat_base_eur", "vat_amount_eur"} & cols
    assert conn.execute("SELECT oss_country FROM transactions").fetchone()[0] == "DE"
    conn.close()
