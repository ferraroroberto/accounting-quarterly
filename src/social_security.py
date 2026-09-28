"""Social Security (Seguridad Social) cuotas import from bank account exports."""
from __future__ import annotations

import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

from src.database import get_connection
from src.logger import get_logger

log = get_logger(__name__)

# Concept keywords used to recognise a TGSS (Tesorería General de la Seguridad
# Social) row when a bank export mixes Social Security payments with other
# movements. Matching is case- and accent-insensitive substring matching.
DEFAULT_CONCEPT_PATTERNS = ["TGSS", "SEG.SOCIAL", "SEGURIDAD SOCIAL", "AUTONOMOS"]

# Excel's date epoch (serial day 0 = 1899-12-30, accounting for the historical
# Lotus 1-2-3 leap-year bug that both Excel and xlrd/openpyxl preserve).
_EXCEL_EPOCH = datetime(1899, 12, 30)

# How many leading rows to scan for the header row when auto-detecting it.
_HEADER_SCAN_ROWS = 25


# ---------------------------------------------------------------------------
# Excel / CSV import helpers
# ---------------------------------------------------------------------------

def _strip_accents(text: str) -> str:
    """Remove diacritics so 'AUTÓNOMOS' matches 'AUTONOMOS'."""
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def _parse_date(value) -> Optional[str]:
    """Attempt to parse a date value to ISO string (YYYY-MM-DD).

    Handles native `datetime`/`date` cells (the common case once openpyxl/xlrd
    parse a date-formatted cell), raw Excel serial numbers (a cell with no
    date formatting, read back as a plain int/float), and common string
    formats.
    """
    if pd.isna(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # Raw Excel serial date (no cell date formatting applied).
        if value > 1000:
            try:
                return (_EXCEL_EPOCH + timedelta(days=float(value))).strftime("%Y-%m-%d")
            except (OverflowError, ValueError):
                return None
        return None
    s = str(value).strip()
    # Drop a trailing time-of-day component (e.g. "2026-08-31 00:00:00").
    if " " in s:
        s = s.split(" ", 1)[0]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _parse_amount(value) -> Optional[float]:
    """Parse a bank export amount and flip its sign to a contribution amount.

    Bank debits (Social Security cuota payments) come through as negative
    numbers; they are stored as positive contribution amounts. Credits
    (refunds, e.g. the automatic *pluriactividad* excess-contribution refund)
    come through as positive numbers; they are stored as negative
    contribution entries so they net off the total in the period received.
    """
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return -float(value)
    s = str(value).strip()
    for ch in ("€", "$", "£", " ", "\xa0"):
        s = s.replace(ch, "")
    if "," in s and "." in s:
        # Assume dot is thousand sep, comma is decimal: 1.234,56 → 1234.56
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return -float(s)
    except ValueError:
        return None


def _matches_concept(text: str, patterns: list[str]) -> bool:
    """Case- and accent-insensitive substring match against any pattern."""
    norm = _strip_accents(text).upper()
    return any(_strip_accents(p).upper() in norm for p in patterns)


def _read_raw_sheet(fp: Path, sheet_name: int | str) -> pd.DataFrame:
    """Read a bank export with no header assumptions, preserving native cell types.

    Excel cells keep their native python type (datetime, float, str) so date
    and amount parsing downstream can rely on it; CSV has no cell typing, so
    it is read as strings.
    """
    suffix = fp.suffix.lower()
    if suffix in (".xlsx", ".xls", ".xlsm"):
        return pd.read_excel(fp, sheet_name=sheet_name, header=None)
    if suffix == ".csv":
        return pd.read_csv(fp, header=None, dtype=str)
    raise ValueError(f"Unsupported file format: {suffix}. Use .xlsx, .xls, or .csv")


def detect_header_row(
    raw: pd.DataFrame,
    date_column: str,
    amount_column: str,
    max_scan: int = _HEADER_SCAN_ROWS,
) -> int:
    """Return the index of the row containing both `date_column` and `amount_column`.

    Bank exports commonly prepend one or more title rows before the real
    header (e.g. "Movimientos de la cuenta ..."), so row 0 cannot be assumed
    to be the header.
    """
    date_norm = date_column.strip().lower()
    amount_norm = amount_column.strip().lower()
    limit = min(max_scan, len(raw))
    for i in range(limit):
        values = {str(v).strip().lower() for v in raw.iloc[i] if not pd.isna(v)}
        if date_norm in values and amount_norm in values:
            return i
    raise ValueError(
        f"Could not auto-detect a header row containing '{date_column}' and "
        f"'{amount_column}' in the first {limit} rows. Pass skiprows explicitly."
    )


def read_raw_preview(file_path: str | Path, sheet_name: int | str = 0, nrows: int = 10) -> pd.DataFrame:
    """Return the first `nrows` raw rows (no header applied) — for UI previews."""
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"Bank export not found: {fp}")
    return _read_raw_sheet(fp, sheet_name).head(nrows)


def _apply_header(raw: pd.DataFrame, header_row: int) -> pd.DataFrame:
    """Slice a raw (header=None) dataframe into a proper header + data frame."""
    df = raw.iloc[header_row + 1:].reset_index(drop=True).copy()
    df.columns = [str(c).strip() for c in raw.iloc[header_row]]
    return df


def load_bank_export(
    file_path: str | Path,
    date_column: str,
    amount_column: str,
    description_column: Optional[str] = None,
    concept_column: Optional[str] = None,
    concept_patterns: Optional[list[str]] = None,
    sheet_name: int | str = 0,
    skiprows: Optional[int] = None,
) -> list[dict]:
    """Read a bank export Excel (.xlsx/.xls) or CSV file and return SS payment rows.

    The header row is auto-detected by scanning the first rows for one
    containing both `date_column` and `amount_column` — pass `skiprows` to
    override with an explicit header row index instead.

    When `concept_column` is given, rows are filtered to those whose concept
    text matches one of `concept_patterns` (default: `DEFAULT_CONCEPT_PATTERNS`,
    e.g. "TGSS", "SEGURIDAD SOCIAL") — a case/accent-insensitive substring
    match. Without `concept_column`, no filtering is applied (the export is
    assumed to already contain only Social Security movements).

    Amounts are sign-flipped: bank debits (negative) become positive
    contribution amounts; credits (positive, e.g. refunds) become negative
    contribution entries.

    Returns a list of dicts: {payment_date, amount_eur, description}.
    Rows where date or amount cannot be parsed, or that don't match the
    concept filter, are skipped.
    """
    fp = Path(file_path)
    if not fp.exists():
        raise FileNotFoundError(f"Bank export not found: {fp}")

    raw = _read_raw_sheet(fp, sheet_name)
    header_row = skiprows if skiprows is not None else detect_header_row(raw, date_column, amount_column)
    df = _apply_header(raw, header_row)

    if date_column not in df.columns:
        raise ValueError(
            f"Date column '{date_column}' not found. Available: {list(df.columns)}"
        )
    if amount_column not in df.columns:
        raise ValueError(
            f"Amount column '{amount_column}' not found. Available: {list(df.columns)}"
        )

    patterns = concept_patterns if concept_patterns else DEFAULT_CONCEPT_PATTERNS

    rows: list[dict] = []
    skipped = 0
    filtered_out = 0
    for _, row in df.iterrows():
        if concept_column and concept_column in df.columns:
            concept_val = "" if pd.isna(row[concept_column]) else str(row[concept_column])
            if not _matches_concept(concept_val, patterns):
                filtered_out += 1
                continue

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
    if filtered_out:
        log.info("ℹ️ Filtered out %d rows not matching the concept filter", filtered_out)
    log.info("ℹ️ Parsed %d Social Security payment rows from %s", len(rows), fp.name)
    return rows


# ---------------------------------------------------------------------------
# DB helpers (called by database.py's init_db, but also directly usable)
# ---------------------------------------------------------------------------

def upsert_ss_payments(
    rows: list[dict],
    source_file: str = "",
    db_path: Optional[str | Path] = None,
) -> tuple[int, int]:
    """Insert or ignore Social Security payment rows.

    Deduplication key: (payment_date, amount_eur, description). If all three
    match an existing row the new row is skipped (no update, since the data
    is authoritative from the bank and we don't want to overwrite user
    edits). Description is part of the key so a same-day/same-amount
    contribution and refund (or two distinct concepts) don't collide.

    Returns (inserted, skipped).
    """
    conn = get_connection(db_path)
    inserted = skipped = 0
    try:
        for row in rows:
            description = row.get("description", "")
            existing = conn.execute(
                "SELECT id FROM social_security_payments "
                "WHERE payment_date = ? AND amount_eur = ? AND description = ?",
                (row["payment_date"], row["amount_eur"], description),
            ).fetchone()
            if existing:
                skipped += 1
                continue
            conn.execute(
                """INSERT INTO social_security_payments
                       (payment_date, amount_eur, description, source_file)
                   VALUES (?, ?, ?, ?)""",
                (row["payment_date"], row["amount_eur"], description, source_file),
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


def add_manual_ss_entry(
    payment_date: str,
    amount_eur: float,
    description: str = "",
    db_path: Optional[str | Path] = None,
) -> int:
    """Insert a single manually-entered SS contribution (or refund) row.

    Used by the Seguridad Social tab's manual-entry fallback for months not
    covered by a bank export. Subject to the same (date, amount, description)
    dedupe key as imported rows — re-submitting the same entry is a no-op.

    Returns 1 if inserted, 0 if skipped as a duplicate.
    """
    inserted, _skipped = upsert_ss_payments(
        [{"payment_date": payment_date, "amount_eur": round(amount_eur, 2), "description": description}],
        source_file="manual",
        db_path=db_path,
    )
    return inserted


# ---------------------------------------------------------------------------
# Reporting helpers
# ---------------------------------------------------------------------------

def get_ss_period_totals(year: int, db_path: Optional[str | Path] = None) -> dict:
    """Return quarterly and yearly SS contribution totals (net of refunds) for `year`.

    Shape: {"year": year, "quarters": {1: total, 2: total, 3: total, 4: total},
    "yearly_total": total}. Intended for the Modelo 130 engine (box 02 YTD) and
    for reporting; `tax_engine.py` currently computes its own YTD sum inline
    from the same table and is not yet wired to call this.
    """
    rows = get_ss_payments(start_date=f"{year}-01-01", end_date=f"{year}-12-31", db_path=db_path)
    quarters = {1: 0.0, 2: 0.0, 3: 0.0, 4: 0.0}
    for row in rows:
        month = int(str(row["payment_date"])[5:7])
        q = (month - 1) // 3 + 1
        quarters[q] = round(quarters[q] + float(row["amount_eur"]), 2)
    yearly_total = round(sum(quarters.values()), 2)
    return {"year": year, "quarters": quarters, "yearly_total": yearly_total}
