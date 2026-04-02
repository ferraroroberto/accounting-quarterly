"""SQLite database for persistent storage of Stripe transactions."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.models import ClassifiedPayment, Payment

log = get_logger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "accounting.db"

_TRANSACTIONS_COLUMNS: dict[str, str] = {
    "card_country": "TEXT",
    "amount_original": "REAL",
    "fx_rate": "REAL",
    "activity_type": "TEXT",
    "geo_region": "TEXT",
    "classification_rule": "TEXT",
    "geo_rule": "TEXT",
    "stripe_customer_id": "TEXT",
    "stripe_payment_intent_id": "TEXT",
    "stripe_balance_transaction_id": "TEXT",
    "stripe_invoice_id": "TEXT",
    "raw_source_type": "TEXT",
    "raw_source_json": "TEXT",
    "source": "TEXT NOT NULL DEFAULT 'csv'",
    "loaded_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    "updated_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
}


def _get_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _get_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _ensure_transactions_schema(conn: sqlite3.Connection) -> None:
    """Add missing columns to older `transactions` tables (best-effort)."""
    existing = _get_table_columns(conn, "transactions")
    for col, ddl in _TRANSACTIONS_COLUMNS.items():
        if col in existing:
            continue
        try:
            conn.execute(f"ALTER TABLE transactions ADD COLUMN {col} {ddl}")
            log.info("ℹ️ Migrated DB: added transactions.%s", col)
        except Exception as exc:
            # If multiple app instances race, or SQLite rejects certain defaults, ignore safely.
            log.warning("⚠️ DB migration skipped for %s: %s", col, exc)


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
                amount_original REAL,
                fx_rate REAL,
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

            CREATE TABLE IF NOT EXISTS fx_rates (
                rate_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                rate REAL NOT NULL,
                PRIMARY KEY (rate_date, currency)
            );

            CREATE INDEX IF NOT EXISTS idx_fx_rates_currency
                ON fx_rates(currency);

            CREATE TABLE IF NOT EXISTS upload_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filename TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
                uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
                api_response TEXT,
                UNIQUE(filename, direction)
            );
        """)
        _ensure_transactions_schema(conn)
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
        _ensure_transactions_schema(conn)
        for p in payments:
            existing = conn.execute(
                "SELECT id, converted_amount, converted_amount_refunded, description, fee, currency, "
                "payment_type_meta, event_api_id_meta, email_meta, card_country, amount_original, fx_rate, "
                "stripe_customer_id, stripe_payment_intent_id, stripe_balance_transaction_id, stripe_invoice_id, "
                "raw_source_type, raw_source_json "
                "FROM transactions WHERE id = ?",
                (p.id,),
            ).fetchone()

            if existing is None:
                raw_json = json.dumps(p.raw_source, ensure_ascii=False, default=str) if p.raw_source else None
                conn.execute("""
                    INSERT INTO transactions
                        (id, created_date, converted_amount, converted_amount_refunded,
                         description, fee, currency, payment_type_meta,
                         event_api_id_meta, email_meta, card_country,
                         amount_original, fx_rate,
                         stripe_customer_id, stripe_payment_intent_id,
                         stripe_balance_transaction_id, stripe_invoice_id,
                         raw_source_type, raw_source_json,
                         source)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    p.card_country,
                    p.amount_original,
                    p.fx_rate,
                    p.stripe_customer_id,
                    p.stripe_payment_intent_id,
                    p.stripe_balance_transaction_id,
                    p.stripe_invoice_id,
                    p.raw_source_type,
                    raw_json,
                    source,
                ))
                inserted += 1
            else:
                raw_json = json.dumps(p.raw_source, ensure_ascii=False, default=str) if p.raw_source else None
                changed = (
                    existing["converted_amount"] != p.converted_amount
                    or existing["converted_amount_refunded"] != p.converted_amount_refunded
                    or existing["description"] != p.description
                    or existing["fee"] != p.fee
                    or existing["currency"] != p.currency
                    or existing["payment_type_meta"] != p.payment_type_meta
                    or existing["event_api_id_meta"] != p.event_api_id_meta
                    or existing["email_meta"] != p.email_meta
                    or existing["card_country"] != p.card_country
                    or existing["amount_original"] != p.amount_original
                    or existing["fx_rate"] != p.fx_rate
                    or existing["stripe_customer_id"] != p.stripe_customer_id
                    or existing["stripe_payment_intent_id"] != p.stripe_payment_intent_id
                    or existing["stripe_balance_transaction_id"] != p.stripe_balance_transaction_id
                    or existing["stripe_invoice_id"] != p.stripe_invoice_id
                    or existing["raw_source_type"] != p.raw_source_type
                    or existing["raw_source_json"] != raw_json
                )
                if changed:
                    conn.execute("""
                        UPDATE transactions SET
                            converted_amount = ?, converted_amount_refunded = ?,
                            description = ?, fee = ?, currency = ?,
                            payment_type_meta = ?, event_api_id_meta = ?,
                            email_meta = ?, card_country = ?,
                            amount_original = ?, fx_rate = ?,
                            stripe_customer_id = ?, stripe_payment_intent_id = ?,
                            stripe_balance_transaction_id = ?, stripe_invoice_id = ?,
                            raw_source_type = ?, raw_source_json = ?,
                            source = ?, updated_at = datetime('now')
                        WHERE id = ?
                    """, (
                        p.converted_amount, p.converted_amount_refunded,
                        p.description, p.fee, p.currency,
                        p.payment_type_meta, p.event_api_id_meta,
                        p.email_meta, p.card_country,
                        p.amount_original, p.fx_rate,
                        p.stripe_customer_id, p.stripe_payment_intent_id,
                        p.stripe_balance_transaction_id, p.stripe_invoice_id,
                        p.raw_source_type, raw_json,
                        source, p.id,
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
        _ensure_transactions_schema(conn)
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


def load_classified_payments(
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    db_path: Optional[str | Path] = None,
) -> list[ClassifiedPayment]:
    """Load payments with their stored classification from the database.

    Use this when you want to display already-classified data without running
    the classifier again. Classification columns default to UNKNOWN / empty
    string for rows that were never classified.
    """
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
            payments.append(ClassifiedPayment(
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
                card_country=row["card_country"],
                amount_original=row["amount_original"],
                fx_rate=row["fx_rate"],
                activity_type=row["activity_type"] or "UNKNOWN",
                geo_region=row["geo_region"] or "UNKNOWN",
                classification_rule=row["classification_rule"] or "",
                geo_rule=row["geo_rule"] or "",
            ))
        return payments
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


def search_transactions_raw(
    *,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    search_text: str = "",
    activity_type: str = "All",
    geo_region: str = "All",
    limit: int = 2000,
    db_path: Optional[str | Path] = None,
) -> tuple[str, list, list[dict]]:
    """Query raw rows from `transactions` with common filters.

    Returns (sql, params, rows_as_dicts).
    """
    conn = _get_connection(db_path)
    try:
        _ensure_transactions_schema(conn)
        cols = _get_table_columns(conn, "transactions")
        where = ["1=1"]
        params: list = []

        if start_date is not None:
            where.append("created_date >= ?")
            params.append(start_date.isoformat())
        if end_date is not None:
            where.append("created_date <= ?")
            params.append(end_date.isoformat())

        if search_text.strip():
            q = f"%{search_text.strip().lower()}%"
            where.append("(lower(description) LIKE ? OR lower(coalesce(email_meta,'')) LIKE ?)")
            params.extend([q, q])

        if activity_type and activity_type != "All":
            where.append("activity_type = ?")
            params.append(activity_type)

        if geo_region and geo_region != "All":
            where.append("geo_region = ?")
            params.append(geo_region)

        desired = [
            "id",
            "created_date",
            "description",
            "email_meta",
            "card_country",
            "currency",
            "converted_amount",
            "converted_amount_refunded",
            "fee",
            "fx_rate",
            "amount_original",
            "activity_type",
            "geo_region",
            "classification_rule",
            "geo_rule",
            "stripe_customer_id",
            "stripe_payment_intent_id",
            "stripe_balance_transaction_id",
            "stripe_invoice_id",
            "raw_source_type",
            "raw_source_json",
            "source",
            "loaded_at",
            "updated_at",
        ]
        select_cols = [c for c in desired if c in cols]
        if not select_cols:
            select_cols = ["*"]

        sql = (
            f"SELECT {', '.join(select_cols)} "
            f"FROM transactions "
            f"WHERE {' AND '.join(where)} "
            f"ORDER BY created_date DESC "
            f"LIMIT ?"
        )

        params_with_limit = [*params, int(limit)]
        rows = conn.execute(sql, params_with_limit).fetchall()
        return sql, params_with_limit, [dict(r) for r in rows]
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
