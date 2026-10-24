"""Stripe ``transactions`` table: upsert, load and search."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.models import ClassifiedPayment, Payment
from src.database.connection import get_connection
from src.database.schema import _ensure_transactions_schema, _get_table_columns

log = get_logger(__name__)


def _fee_split_changed(existing: sqlite3.Row, p: Payment) -> bool:
    """True when ``p`` carries a known fee split that differs from the stored one.

    An unknown split (None, e.g. a key without balance-transaction access) never
    counts as a change and never overwrites a known one (the UPDATE COALESCEs).
    """
    if p.fee_stripe is None or p.fee_application is None:
        return False
    return existing["fee_stripe"] != p.fee_stripe or existing["fee_application"] != p.fee_application


# Every ``transactions`` column ``upsert_payments`` writes from a ``Payment`` (besides ``id``,
# ``created_date`` and ``source``). One tuple drives the SELECT, INSERT, change check and UPDATE:
# a new column is added here and on ``Payment``, nowhere else.
_PAYMENT_COLUMNS = (
    "converted_amount", "converted_amount_refunded", "description", "fee", "fee_stripe", "fee_application",
    "currency", "payment_type_meta", "event_api_id_meta", "email_meta", "card_country", "billing_country",
    "amount_original", "fx_rate",
    "stripe_customer_id", "stripe_payment_intent_id", "stripe_balance_transaction_id", "stripe_invoice_id",
    "raw_source_type", "raw_source_json",
)
# An unknown split (None) must never overwrite a known one: the UPDATE COALESCEs these.
_PAYMENT_FEE_SPLIT_COLUMNS = ("fee_stripe", "fee_application")

_PAYMENT_SELECT_SQL = f"SELECT id, {', '.join(_PAYMENT_COLUMNS)} FROM transactions WHERE id = ?"
_PAYMENT_INSERT_SQL = (
    f"INSERT INTO transactions (id, created_date, {', '.join(_PAYMENT_COLUMNS)}, source) "
    f"VALUES ({', '.join('?' * (len(_PAYMENT_COLUMNS) + 3))})"
)
_PAYMENT_UPDATE_SQL = (
    "UPDATE transactions SET "
    + ", ".join(
        f"{c} = COALESCE(?, {c})" if c in _PAYMENT_FEE_SPLIT_COLUMNS else f"{c} = ?"
        for c in _PAYMENT_COLUMNS
    )
    + ", source = ?, updated_at = datetime('now') WHERE id = ?"
)


def _payment_values(p: Payment) -> dict:
    """The ``_PAYMENT_COLUMNS`` values of ``p`` (``raw_source`` serialised to JSON)."""
    values = {c: getattr(p, c) for c in _PAYMENT_COLUMNS if c != "raw_source_json"}
    values["raw_source_json"] = (
        json.dumps(p.raw_source, ensure_ascii=False, default=str) if p.raw_source else None
    )
    return values


def _load_raw_source(pid: str, raw_json: Optional[str]) -> Optional[dict]:
    """Parse a stored ``raw_source_json`` back into the ``raw_source`` dict.

    The classifier reads the charge's billing / customer address from it, so
    a reclassify of a loaded row must see it (#187). Unparseable JSON is logged
    and treated as absent rather than failing the whole load.
    """
    if not raw_json:
        return None
    try:
        raw = json.loads(raw_json)
    except ValueError:
        log.warning("⚠️ Unparseable raw_source_json on transaction %s; ignoring it", pid)
        return None
    return raw if isinstance(raw, dict) else None


def upsert_payments(payments: list[Payment], source: str = "api",
                    db_path: Optional[str | Path] = None) -> tuple[int, int]:
    """Insert or update payments. Returns (inserted, updated) counts."""
    conn = get_connection(db_path)
    inserted = 0
    updated = 0
    try:
        _ensure_transactions_schema(conn)
        for p in payments:
            existing = conn.execute(_PAYMENT_SELECT_SQL, (p.id,)).fetchone()
            values = _payment_values(p)
            params = tuple(values[c] for c in _PAYMENT_COLUMNS)

            if existing is None:
                conn.execute(_PAYMENT_INSERT_SQL, (p.id, p.created_date.isoformat(), *params, source))
                inserted += 1
            else:
                changed = _fee_split_changed(existing, p) or any(
                    existing[c] != values[c] for c in _PAYMENT_COLUMNS if c not in _PAYMENT_FEE_SPLIT_COLUMNS
                )
                if changed:
                    conn.execute(_PAYMENT_UPDATE_SQL, (*params, source, p.id))
                    updated += 1
        conn.commit()
        log.info("ℹ️ Upserted payments: %d inserted, %d updated", inserted, updated)
    finally:
        conn.close()
    return inserted, updated


def upsert_classified(payments: list[ClassifiedPayment],
                      db_path: Optional[str | Path] = None) -> None:
    """Update classification columns for already-stored transactions.

    Also writes ``buyer_vat_id`` (the customer's EU VAT id, resolved by
    ``src.classifier.customer_vat_id`` — accounting-quarterly#113) so the
    tax engine's on-the-fly ``vat_treatment`` derivation and Modelo 349 key S
    grouping (``src.tax_data.load_classified_for_quarter``) can read it
    straight from the ``transactions`` table.
    """
    conn = get_connection(db_path)
    try:
        _ensure_transactions_schema(conn)
        for p in payments:
            conn.execute("""
                UPDATE transactions SET
                    activity_type = ?, geo_region = ?,
                    classification_rule = ?, geo_rule = ?,
                    buyer_vat_id = ?,
                    updated_at = datetime('now')
                WHERE id = ?
            """, (p.activity_type, p.geo_region,
                  p.classification_rule, p.geo_rule, p.buyer_vat_id, p.id))
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
    conn = get_connection(db_path)
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
                fee_stripe=row["fee_stripe"],
                fee_application=row["fee_application"],
                currency=row["currency"],
                payment_type_meta=row["payment_type_meta"],
                event_api_id_meta=row["event_api_id_meta"],
                email_meta=row["email_meta"],
                card_country=row["card_country"],
                billing_country=row["billing_country"],
                amount_original=row["amount_original"],
                fx_rate=row["fx_rate"],
                raw_source=_load_raw_source(row["id"], row["raw_source_json"]),
                raw_source_type=row["raw_source_type"],
                activity_type=row["activity_type"] or "UNKNOWN",
                geo_region=row["geo_region"] or "UNKNOWN",
                classification_rule=row["classification_rule"] or "",
                geo_rule=row["geo_rule"] or "",
                buyer_vat_id=row["buyer_vat_id"],
            ))
        return payments
    finally:
        conn.close()


def get_transaction_date_bounds(
    db_path: Optional[str | Path] = None,
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Return (min_created_date, max_created_date) from transactions."""
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM transactions").fetchone()
        return row["cnt"]
    finally:
        conn.close()


def get_latest_stripe_sync_at(db_path: Optional[str | Path] = None) -> Optional[datetime]:
    """Get the latest DB load timestamp for rows sourced from Stripe API."""
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
            "billing_country",
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
