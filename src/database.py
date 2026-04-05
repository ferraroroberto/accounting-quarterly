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
    # VAT / tax fields
    "vat_treatment": "TEXT",
    "vat_base_eur": "REAL",
    "vat_amount_eur": "REAL",
    "oss_country": "TEXT",
    "buyer_vat_id": "TEXT",
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


def _ensure_invoices_schema(conn: sqlite3.Connection) -> None:
    """Add missing columns to the `invoices` table."""
    existing = _get_table_columns(conn, "invoices")
    additions = {
        "file_hash": "TEXT",
        # Enhanced Spanish accounting fields
        "invoice_type": "TEXT",
        "supply_date": "TEXT",
        "due_date": "TEXT",
        "is_rectificativa": "INTEGER DEFAULT 0",
        "rectified_invoice_ref": "TEXT",
        "vat_exempt_reason": "TEXT",
        "iva_breakdown": "TEXT",
        "deductible_pct": "REAL DEFAULT 100",
        "billing_period_start": "TEXT",
        "billing_period_end": "TEXT",
    }
    for col, ddl in additions.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {ddl}")
                log.info("ℹ️ Migrated DB: added invoices.%s", col)
            except Exception as exc:
                log.warning("⚠️ DB migration skipped for invoices.%s: %s", col, exc)


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

            CREATE TABLE IF NOT EXISTS invoices (
                id TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
                invoice_number TEXT,
                invoice_date TEXT,
                vendor_name TEXT,
                vendor_nif TEXT,
                vendor_address TEXT,
                client_name TEXT,
                client_nif TEXT,
                client_address TEXT,
                description TEXT,
                subtotal_eur REAL,
                iva_rate REAL,
                iva_amount REAL,
                irpf_rate REAL,
                irpf_amount REAL,
                total_eur REAL,
                currency TEXT DEFAULT 'EUR',
                original_currency TEXT,
                original_amount REAL,
                fx_rate REAL,
                payment_method TEXT,
                category TEXT,
                notes TEXT,
                raw_json TEXT,
                file_hash TEXT,
                extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
                UNIQUE(filename, direction)
            );

            CREATE INDEX IF NOT EXISTS idx_invoices_direction
                ON invoices(direction);
            CREATE INDEX IF NOT EXISTS idx_invoices_date
                ON invoices(invoice_date);

            CREATE TABLE IF NOT EXISTS quarterly_tax_entries (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                year        INTEGER NOT NULL,
                quarter     INTEGER NOT NULL,
                entry_type  TEXT NOT NULL,
                amount_eur  REAL NOT NULL DEFAULT 0,
                description TEXT,
                notes       TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_tax_entries_period
                ON quarterly_tax_entries(year, quarter);

            CREATE TABLE IF NOT EXISTS tax_filing_status (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                year        INTEGER NOT NULL,
                quarter     INTEGER,
                model       TEXT NOT NULL,
                status      TEXT NOT NULL DEFAULT 'PENDING',
                filed_at    TEXT,
                amount_eur  REAL,
                notes       TEXT,
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE UNIQUE INDEX IF NOT EXISTS idx_filing_status_key
                ON tax_filing_status(year, model, COALESCE(quarter, -1));
        """)
        _ensure_transactions_schema(conn)
        _ensure_invoices_schema(conn)
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
        _ensure_transactions_schema(conn)
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


def get_transaction_date_bounds(
    db_path: Optional[str | Path] = None,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return (min_created_date, max_created_date) from transactions."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT MIN(created_date) AS min_date, MAX(created_date) AS max_date FROM transactions"
        ).fetchone()
        if not row:
            return None, None
        min_dt = datetime.fromisoformat(row["min_date"]) if row["min_date"] else None
        max_dt = datetime.fromisoformat(row["max_date"]) if row["max_date"] else None
        return min_dt, max_dt
    finally:
        conn.close()


def get_transaction_count_db(db_path: Optional[str | Path] = None) -> int:
    conn = _get_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM transactions").fetchone()
        return row["cnt"]
    finally:
        conn.close()


def get_latest_stripe_sync_at(db_path: Optional[str | Path] = None) -> Optional[datetime]:
    """Get the latest DB load timestamp for rows sourced from Stripe API."""
    conn = _get_connection(db_path)
    try:
        _ensure_transactions_schema(conn)
        row = conn.execute(
            "SELECT MAX(loaded_at) AS max_loaded_at FROM transactions WHERE source = 'api'"
        ).fetchone()
        if row and row["max_loaded_at"]:
            return datetime.fromisoformat(row["max_loaded_at"])
        return None
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


# ---------------------------------------------------------------------------
# invoices table helpers
# ---------------------------------------------------------------------------

def upsert_invoice(data: dict, db_path: Optional[str | Path] = None) -> str:
    """Insert or replace a parsed invoice record. Returns the record id."""
    import uuid
    conn = _get_connection(db_path)
    try:
        record_id = data.get("id") or str(uuid.uuid4())
        conn.execute("""
            INSERT INTO invoices (
                id, filename, direction, invoice_number, invoice_date,
                vendor_name, vendor_nif, vendor_address,
                client_name, client_nif, client_address,
                description, subtotal_eur, iva_rate, iva_amount,
                irpf_rate, irpf_amount, total_eur,
                currency, original_currency, original_amount, fx_rate,
                payment_method, category, notes, raw_json, file_hash,
                invoice_type, supply_date, due_date, is_rectificativa,
                rectified_invoice_ref, vat_exempt_reason, iva_breakdown,
                deductible_pct, billing_period_start, billing_period_end
            ) VALUES (
                :id, :filename, :direction, :invoice_number, :invoice_date,
                :vendor_name, :vendor_nif, :vendor_address,
                :client_name, :client_nif, :client_address,
                :description, :subtotal_eur, :iva_rate, :iva_amount,
                :irpf_rate, :irpf_amount, :total_eur,
                :currency, :original_currency, :original_amount, :fx_rate,
                :payment_method, :category, :notes, :raw_json, :file_hash,
                :invoice_type, :supply_date, :due_date, :is_rectificativa,
                :rectified_invoice_ref, :vat_exempt_reason, :iva_breakdown,
                :deductible_pct, :billing_period_start, :billing_period_end
            )
            ON CONFLICT(filename, direction) DO UPDATE SET
                invoice_number = excluded.invoice_number,
                invoice_date = excluded.invoice_date,
                vendor_name = excluded.vendor_name,
                vendor_nif = excluded.vendor_nif,
                vendor_address = excluded.vendor_address,
                client_name = excluded.client_name,
                client_nif = excluded.client_nif,
                client_address = excluded.client_address,
                description = excluded.description,
                subtotal_eur = excluded.subtotal_eur,
                iva_rate = excluded.iva_rate,
                iva_amount = excluded.iva_amount,
                irpf_rate = excluded.irpf_rate,
                irpf_amount = excluded.irpf_amount,
                total_eur = excluded.total_eur,
                currency = excluded.currency,
                original_currency = excluded.original_currency,
                original_amount = excluded.original_amount,
                fx_rate = excluded.fx_rate,
                payment_method = excluded.payment_method,
                category = excluded.category,
                notes = excluded.notes,
                raw_json = excluded.raw_json,
                file_hash = excluded.file_hash,
                invoice_type = excluded.invoice_type,
                supply_date = excluded.supply_date,
                due_date = excluded.due_date,
                is_rectificativa = excluded.is_rectificativa,
                rectified_invoice_ref = excluded.rectified_invoice_ref,
                vat_exempt_reason = excluded.vat_exempt_reason,
                iva_breakdown = excluded.iva_breakdown,
                deductible_pct = excluded.deductible_pct,
                billing_period_start = excluded.billing_period_start,
                billing_period_end = excluded.billing_period_end,
                extracted_at = datetime('now')
        """, {
            "id": record_id,
            "filename": data.get("filename", ""),
            "direction": data.get("direction", "in"),
            "invoice_number": data.get("invoice_number"),
            "invoice_date": data.get("invoice_date"),
            "vendor_name": data.get("vendor_name"),
            "vendor_nif": data.get("vendor_nif"),
            "vendor_address": data.get("vendor_address"),
            "client_name": data.get("client_name"),
            "client_nif": data.get("client_nif"),
            "client_address": data.get("client_address"),
            "description": data.get("description"),
            "subtotal_eur": data.get("subtotal_eur"),
            "iva_rate": data.get("iva_rate"),
            "iva_amount": data.get("iva_amount"),
            "irpf_rate": data.get("irpf_rate"),
            "irpf_amount": data.get("irpf_amount"),
            "total_eur": data.get("total_eur"),
            "currency": data.get("currency", "EUR"),
            "original_currency": data.get("original_currency"),
            "original_amount": data.get("original_amount"),
            "fx_rate": data.get("fx_rate"),
            "payment_method": data.get("payment_method"),
            "category": data.get("category"),
            "notes": data.get("notes"),
            "raw_json": data.get("raw_json"),
            "file_hash": data.get("file_hash"),
            "invoice_type": data.get("invoice_type"),
            "supply_date": data.get("supply_date"),
            "due_date": data.get("due_date"),
            "is_rectificativa": data.get("is_rectificativa", 0),
            "rectified_invoice_ref": data.get("rectified_invoice_ref"),
            "vat_exempt_reason": data.get("vat_exempt_reason"),
            "iva_breakdown": data.get("iva_breakdown"),
            "deductible_pct": data.get("deductible_pct", 100),
            "billing_period_start": data.get("billing_period_start"),
            "billing_period_end": data.get("billing_period_end"),
        })
        conn.commit()
        return record_id
    finally:
        conn.close()


def get_invoices(direction: Optional[str] = None,
                 db_path: Optional[str | Path] = None) -> list[dict]:
    """Return all invoice records, optionally filtered by direction."""
    conn = _get_connection(db_path)
    try:
        if direction:
            rows = conn.execute(
                "SELECT * FROM invoices WHERE direction = ? ORDER BY invoice_date DESC, extracted_at DESC",
                (direction,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM invoices ORDER BY invoice_date DESC, extracted_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_invoice_by_filename(filename: str, direction: str,
                             db_path: Optional[str | Path] = None) -> Optional[dict]:
    """Return a single invoice record by filename+direction, or None."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM invoices WHERE filename = ? AND direction = ?",
            (filename, direction),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_invoice_hash(filename: str, direction: str,
                     db_path: Optional[str | Path] = None) -> Optional[str]:
    """Return the stored MD5 hash for a file, or None if not extracted yet."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT file_hash FROM invoices WHERE filename = ? AND direction = ?",
            (filename, direction),
        ).fetchone()
        return row["file_hash"] if row else None
    finally:
        conn.close()


def delete_invoice(filename: str, direction: str,
                   db_path: Optional[str | Path] = None) -> bool:
    """Delete an invoice record. Returns True if a row was deleted."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM invoices WHERE filename = ? AND direction = ?",
            (filename, direction),
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def delete_invoices_by_ids(ids: list[str],
                           db_path: Optional[str | Path] = None) -> int:
    """Delete invoice records by their UUID ids. Returns number deleted."""
    if not ids:
        return 0
    conn = _get_connection(db_path)
    try:
        placeholders = ",".join("?" * len(ids))
        cursor = conn.execute(
            f"DELETE FROM invoices WHERE id IN ({placeholders})", ids
        )
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def clear_invoices(db_path: Optional[str | Path] = None) -> int:
    """Delete ALL invoice records. Returns number of rows deleted."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute("DELETE FROM invoices")
        conn.commit()
        return cursor.rowcount
    finally:
        conn.close()


def get_invoice_stats(db_path: Optional[str | Path] = None) -> dict:
    """Return invoice counts and latest extracted_at per direction.

    Returns::

        {
            "in":  {"count": int, "last_extracted_at": str | None},
            "out": {"count": int, "last_extracted_at": str | None},
        }
    """
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            """
            SELECT direction,
                   COUNT(*) AS cnt,
                   MAX(extracted_at) AS last_at
            FROM invoices
            GROUP BY direction
            """
        ).fetchall()
        result: dict = {
            "in":  {"count": 0, "last_extracted_at": None},
            "out": {"count": 0, "last_extracted_at": None},
        }
        for row in rows:
            d = row["direction"]
            if d in result:
                result[d]["count"] = row["cnt"]
                result[d]["last_extracted_at"] = row["last_at"]
        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# quarterly_tax_entries helpers
# ---------------------------------------------------------------------------

def get_tax_entries(year: int, quarter: int,
                    db_path: Optional[str | Path] = None) -> list[dict]:
    """Return all manual tax entries for the given year/quarter."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM quarterly_tax_entries WHERE year = ? AND quarter = ? ORDER BY id",
            (year, quarter),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_tax_entry(year: int, quarter: int, entry_type: str, amount_eur: float,
                  description: str = "", notes: str = "",
                  db_path: Optional[str | Path] = None) -> int:
    """Insert a manual tax entry. Returns the new row id."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO quarterly_tax_entries
               (year, quarter, entry_type, amount_eur, description, notes)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (year, quarter, entry_type, amount_eur, description, notes),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def delete_tax_entry(entry_id: int, db_path: Optional[str | Path] = None) -> bool:
    """Delete a manual tax entry by id. Returns True if deleted."""
    conn = _get_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM quarterly_tax_entries WHERE id = ?", (entry_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def get_tax_entries_ytd(year: int, quarter: int, entry_type: str,
                        db_path: Optional[str | Path] = None) -> float:
    """Sum a given entry_type from Q1 through the given quarter (YTD)."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount_eur), 0) AS total
               FROM quarterly_tax_entries
               WHERE year = ? AND quarter <= ? AND entry_type = ?""",
            (year, quarter, entry_type),
        ).fetchone()
        return float(row["total"])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tax_filing_status helpers
# ---------------------------------------------------------------------------

def get_filing_status(year: int, model: str, quarter: Optional[int] = None,
                      db_path: Optional[str | Path] = None) -> Optional[dict]:
    """Return the filing status record for the given year/model/quarter, or None."""
    conn = _get_connection(db_path)
    try:
        if quarter is None:
            row = conn.execute(
                "SELECT * FROM tax_filing_status WHERE year = ? AND model = ? AND quarter IS NULL",
                (year, model),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM tax_filing_status WHERE year = ? AND model = ? AND quarter = ?",
                (year, model, quarter),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def upsert_filing_status(year: int, model: str, quarter: Optional[int],
                         status: str, amount_eur: Optional[float] = None,
                         notes: str = "", filed_at: Optional[str] = None,
                         db_path: Optional[str | Path] = None) -> None:
    """Insert or update a filing status record."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """INSERT INTO tax_filing_status (year, model, quarter, status, amount_eur, notes, filed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(year, model, COALESCE(quarter, -1)) DO UPDATE SET
                   status = excluded.status,
                   amount_eur = excluded.amount_eur,
                   notes = excluded.notes,
                   filed_at = excluded.filed_at""",
            (year, model, quarter, status, amount_eur, notes, filed_at),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_filing_statuses(year: int,
                             db_path: Optional[str | Path] = None) -> list[dict]:
    """Return all filing status records for the given year."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM tax_filing_status WHERE year = ? ORDER BY model, quarter",
            (year,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_vat_treatment(payment_id: str, vat_treatment: str,
                         vat_base_eur: float, vat_amount_eur: float,
                         oss_country: Optional[str] = None,
                         buyer_vat_id: Optional[str] = None,
                         db_path: Optional[str | Path] = None) -> None:
    """Write VAT treatment fields back to the transactions table."""
    conn = _get_connection(db_path)
    try:
        conn.execute(
            """UPDATE transactions SET
                   vat_treatment = ?, vat_base_eur = ?, vat_amount_eur = ?,
                   oss_country = ?, buyer_vat_id = ?, updated_at = datetime('now')
               WHERE id = ?""",
            (vat_treatment, vat_base_eur, vat_amount_eur,
             oss_country, buyer_vat_id, payment_id),
        )
        conn.commit()
    finally:
        conn.close()
