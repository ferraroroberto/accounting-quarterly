"""Social Security (Seguridad Social) cuotas import from bank account exports."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from src.logger import get_logger

log = get_logger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "accounting.db"


# ---------------------------------------------------------------------------
# Excel / CSV import helpers
# ---------------------------------------------------------------------------

def _parse_date(value) -> Optional[str]:
    """Attempt to parse a date value to ISO string (YYYY-MM-DD)."""
    if pd.isna(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    s = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_amount(value) -> Optional[float]:
    """Parse an amount value, stripping currency symbols and thousand-separators."""
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    # Remove currency symbols and whitespace
    for ch in ("€", "$", "£", " ", "\xa0"):
        s = s.replace(ch, "")
    # European decimal: replace comma with dot if needed
    if "," in s and "." in s:
        # Assume dot is thousand sep, comma is decimal: 1.234,56 → 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return abs(float(s))  # SS payments are debits (negative), store as positive
    except ValueError:
        return None


def load_bank_export(
    file_path: str | Path,
    date_column: str,
    amount_column: str,
    description_column: Optional[str] = None,
    sheet_name: int | str = 0,
    skiprows: int = 0,
) -> list[dict]:
    """Read a bank export Excel/CSV file and return rows with date + amount.

    Returns a list of dicts: {payment_date, amount_eur, description}.
    Rows where date or amount cannot be parsed are skipped with a warning.
    """
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"Bank export not found: {fp}")

    suffix = fp.suffix.lower()
    if suffix in (".xlsx", ".xls", ".xlsm"):
        df = pd.read_excel(fp, sheet_name=sheet_name, skiprows=skiprows, dtype=str)
    elif suffix == ".csv":
        df = pd.read_csv(fp, skiprows=skiprows, dtype=str)
    else:
        raise ValueError(f"Unsupported file format: {suffix}. Use .xlsx, .xls, or .csv")

    # Normalise column names: strip whitespace
    df.columns = [str(c).strip() for c in df.columns]

    if date_column not in df.columns:
        raise ValueError(
            f"Date column '{date_column}' not found. Available: {list(df.columns)}"
        )
    if amount_column not in df.columns:
        raise ValueError(
            f"Amount column '{amount_column}' not found. Available: {list(df.columns)}"
        )

    rows: list[dict] = []
    skipped = 0
    for _, row in df.iterrows():
        payment_date = _parse_date(row[date_column])
        amount = _parse_amount(row[amount_column])
        if payment_date is None or amount is None or amount == 0.0:
            skipped += 1
            continue
        description = ""
        if description_column and description_column in df.columns:
            description = str(row[description_column]).strip() if not pd.isna(row[description_column]) else ""
        rows.append({
            "payment_date": payment_date,
            "amount_eur": round(amount, 2),
            "description": description,
        })

    if skipped:
        log.warning("⚠️ Skipped %d rows with unparseable date/amount", skipped)
    log.info("ℹ️ Parsed %d Social Security payment rows from %s", len(rows), fp.name)
    return rows


# ---------------------------------------------------------------------------
# DB helpers (called by database.py's init_db, but also directly usable)
# ---------------------------------------------------------------------------

def get_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    """Return a sqlite3 connection with row_factory set."""
    from src.database import _get_connection
    return _get_connection(db_path)


def upsert_ss_payments(
    rows: list[dict],
    source_file: str = "",
    db_path: Optional[str | Path] = None,
) -> tuple[int, int]:
    """Insert or ignore Social Security payment rows.

    Deduplication key: (payment_date, amount_eur).  If both match an existing
    row the new row is skipped (no update, since the data is authoritative from
    the bank and we don't want to overwrite user edits).

    Returns (inserted, skipped).
    """
    conn = get_connection(db_path)
    inserted = skipped = 0
    try:
        for row in rows:
            existing = conn.execute(
                "SELECT id FROM social_security_payments WHERE payment_date = ? AND amount_eur = ?",
                (row["payment_date"], row["amount_eur"]),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO social_security_payments
                       (payment_date, amount_eur, description, source_file)
                   VALUES (?, ?, ?, ?)""",
                (row["payment_date"], row["amount_eur"], row.get("description", ""), source_file),
            )
            inserted += 1
        conn.commit()
        log.info("ℹ️ SS payments: %d inserted, %d skipped (duplicates)", inserted, skipped)
    finally:
        conn.close()
    return inserted, skipped


def get_ss_payments(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> list[dict]:
    """Return Social Security payment rows, optionally filtered by date range."""
    conn = get_connection(db_path)
    try:
        where = ["1=1"]
        params: list = []
        if start_date:
            where.append("payment_date >= ?")
            params.append(start_date)
        if end_date:
            where.append("payment_date <= ?")
            params.append(end_date)
        rows = conn.execute(
            f"SELECT * FROM social_security_payments WHERE {' AND '.join(where)} ORDER BY payment_date",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_ss_total_for_period(
    start_date: str,
    end_date: str,
    db_path: Optional[str | Path] = None,
) -> float:
    """Return the total SS amount paid within the given date range."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount_eur), 0) AS total
               FROM social_security_payments
               WHERE payment_date >= ? AND payment_date <= ?""",
            (start_date, end_date),
        ).fetchone()
        return float(row["total"]) if row else 0.0
    finally:
        conn.close()


def get_ss_total_ytd(
    year: int,
    quarter: int,
    db_path: Optional[str | Path] = None,
) -> float:
    """Return cumulative SS total from Jan 1 through end of given quarter."""
    import calendar
    month_end = quarter * 3
    last_day = calendar.monthrange(year, month_end)[1]
    start = f"{year}-01-01"
    end = f"{year}-{month_end:02d}-{last_day:02d}"
    return get_ss_total_for_period(start, end, db_path)


def get_ss_count(db_path: Optional[str | Path] = None) -> int:
    """Return total number of SS payment rows stored."""
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM social_security_payments").fetchone()
        return int(row["cnt"]) if row else 0
    finally:
        conn.close()


def delete_ss_payment(payment_id: int, db_path: Optional[str | Path] = None) -> None:
    """Delete a single SS payment row by primary key."""
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM social_security_payments WHERE id = ?", (payment_id,))
        conn.commit()
    finally:
        conn.close()


def clear_ss_payments(db_path: Optional[str | Path] = None) -> None:
    """Delete all Social Security payment rows (useful for re-import)."""
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM social_security_payments")
        conn.commit()
        log.info("ℹ️ All Social Security payment rows cleared")
    finally:
        conn.close()
