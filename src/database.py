"""SQLite database for persistent storage of Stripe transactions."""
from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.models import ClassifiedPayment, Payment

log = get_logger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "accounting.db"


def _get_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Optional[str | Path] = None) -> None:
    """Create tables if they don't exist."""
    conn = _get_connection(db_path)
    try:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                created_date TEXT NOT NULL,
                converted_amount REAL NOT NULL,
                converted_amount_refunded REAL NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                fee REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'eur',
                payment_type_meta TEXT,
                event_api_id_meta TEXT,
                email_meta TEXT,
                card_country TEXT,
                activity_type TEXT,
                geo_region TEXT,
                classification_rule TEXT,
                geo_rule TEXT,
                source TEXT NOT NULL DEFAULT 'csv',
                loaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_created
                ON transactions(created_date);
            CREATE INDEX IF NOT EXISTS idx_transactions_activity
                ON transactions(activity_type);
            CREATE INDEX IF NOT EXISTS idx_transactions_geo
                ON transactions(geo_region);

            CREATE TABLE IF NOT EXISTS upload_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
                uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                api_response TEXT,
                UNIQUE(filename, direction)
            );
        """)
        conn.commit()
        log.info("ℹ️ Database initialised at %s", _DB_PATH)
    finally:
        conn.close()


def upsert_payments(payments: list[Payment], source: str = "csv",
                    db_path: Optional[str | Path] = None) -> tuple[int, int]:
    """Insert or update payments. Returns (inserted, updated) counts."""
    conn = _get_connection(db_path)
    inserted = 0
    updated = 0
    try:
        for p in payments:
            existing = conn.execute(
                "SELECT id, converted_amount, fee FROM transactions WHERE id = ?",
                (p.id,),
            ).fetchone()

            if existing is None:
                conn.execute("""
                    INSERT INTO transactions
                        (id, created_date, converted_amount, converted_amount_refunded,
                         description, fee, currency, payment_type_meta,
                         event_api_id_meta, email_meta, source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    p.id,
                    p.created_date.isoformat(),
                    p.converted_amount,
                    p.converted_amount_refunded,
                    p.description,
                    p.fee,
                    p.currency,
                    p.payment_type_meta,
                    p.event_api_id_meta,
                    p.email_meta,
                    source,
                ))
                inserted += 1
            else:
                if (existing["converted_amount"] != p.converted_amount or
                        existing["fee"] != p.fee):
                    conn.execute("""
                        UPDATE transactions SET
                            converted_amount = ?, converted_amount_refunded = ?,
                            description = ?, fee = ?, currency = ?,
                            payment_type_meta = ?, event_api_id_meta = ?,
                            email_meta = ?, updated_at = datetime('now')
                        WHERE id = ?
                    """, (
                        p.converted_amount, p.converted_amount_refunded,
                        p.description, p.fee, p.currency,
                        p.payment_type_meta, p.event_api_id_meta,
                        p.email_meta, p.id,
                    ))
                    updated += 1
        conn.commit()
        log.info("ℹ️ Upserted payments: %d inserted, %d updated", inserted, updated)
    finally:
        conn.close()
    return inserted, updated


def upsert_classified(payments: list[ClassifiedPayment],
                      db_path: Optional[str | Path] = None) -> None:
    """Update classification columns for already-stored transactions."""
    conn = _get_connection(db_path)
    try:
        for p in payments:
            conn.execute("""
                UPDATE transactions SET
                    activity_type = ?, geo_region = ?,
                    classification_rule = ?, geo_rule = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (p.activity_type, p.geo_region,
                  p.classification_rule, p.geo_rule, p.id))
        conn.commit()
    finally:
        conn.close()


def load_payments(start_date: Optional[datetime] = None,
                  end_date: Optional[datetime] = None,
                  db_path: Optional[str | Path] = None) -> list[Payment]:
    """Load payments from database, optionally filtered by date range."""
    conn = _get_connection(db_path)
    try:
        query = "SELECT * FROM transactions WHERE 1=1"
        params: list = []
        if start_date:
            query += " AND created_date >= ?"
            params.append(start_date.isoformat())
        if end_date:
            query += " AND created_date <= ?"
            params.append(end_date.isoformat())
        query += " ORDER BY created_date"

        rows = conn.execute(query, params).fetchall()
        payments = []
        for row in rows:
            payments.append(Payment(
                id=row["id"],
                created_date=datetime.fromisoformat(row["created_date"]),
                converted_amount=row["converted_amount"],
                converted_amount_refunded=row["converted_amount_refunded"],
                description=row["description"],
                fee=row["fee"],
                currency=row["currency"],
                payment_type_meta=row["payment_type_meta"],
                event_api_id_meta=row["event_api_id_meta"],
                email_meta=row["email_meta"],
            ))
        return payments
    finally:
        conn.close()


def get_latest_transaction_date(db_path: Optional[str | Path] = None) -> Optional[datetime]:
    """Get the most recent transaction date in the database."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT MAX(created_date) as max_date FROM transactions"
        ).fetchone()
        if row and row["max_date"]:
            return datetime.fromisoformat(row["max_date"])
        return None
    finally:
        conn.close()


def get_transaction_count_db(db_path: Optional[str | Path] = None) -> int:
    conn = _get_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM transactions").fetchone()
        return row["cnt"]
    finally:
        conn.close()


def record_upload(filename: str, direction: str, api_response: str = "",
                  db_path: Optional[str | Path] = None) -> bool:
    """Record an invoice upload. Returns True if new, False if already uploaded."""
    conn = _get_connection(db_path)
    try:
        existing = conn.execute(
            "SELECT id FROM upload_log WHERE filename = ? AND direction = ?",
            (filename, direction),
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO upload_log (filename, direction, api_response) VALUES (?, ?, ?)",
            (filename, direction, api_response),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_uploaded_files(direction: str,
                       db_path: Optional[str | Path] = None) -> list[dict]:
    """Get list of already-uploaded invoice files."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT filename, uploaded_at, api_response FROM upload_log "
            "WHERE direction = ? ORDER BY uploaded_at DESC",
            (direction,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
