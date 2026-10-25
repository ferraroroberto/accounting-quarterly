"""Table definitions, in-place migrations, backfills and ``init_db``."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.tax_codes import (
    derive_geo_region_from_nif,
    derive_tax_treatment_for_invoice,
    derive_vat_treatment_for_invoice,
    normalize_vat_id,
)
from src.database import connection
from src.database.connection import get_connection

log = get_logger(__name__)


_TRANSACTIONS_COLUMNS: dict[str, str] = {
    "card_country": "TEXT",
    "billing_country": "TEXT",
    "amount_original": "REAL",
    "fx_rate": "REAL",
    # Fee split from balance_transaction.fee_details (EUR); NULL = unknown.
    "fee_stripe": "REAL",
    "fee_application": "REAL",
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
    "source": "TEXT NOT NULL DEFAULT 'api'",
    "loaded_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    "updated_at": "TEXT NOT NULL DEFAULT (datetime('now'))",
    # VAT / tax fields
    "vat_treatment": "TEXT",
    "vat_base_eur": "REAL",
    "vat_amount_eur": "REAL",
    "oss_country": "TEXT",
    "buyer_vat_id": "TEXT",
}


def _get_table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def _create_fx_rates_table(conn: sqlite3.Connection) -> None:
    """Create the `fx_rates` table and its index (single schema owner).

    Called both from `init_db` and from `fx_rates.init_fx_table` so the two
    never diverge; `fx_rates._ensure_fx_schema` still ALTERs pre-existing DBs
    created before this table gained `loaded_at`/`updated_at`.
    """
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fx_rates (
            rate_date TEXT NOT NULL,
            currency TEXT NOT NULL,
            rate REAL NOT NULL,
            loaded_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (rate_date, currency)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fx_rates_currency
            ON fx_rates(currency)
    """)


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
        # Geographic / VAT classification (mirrors transactions table)
        "geo_region": "TEXT",        # SPAIN | EU_NOT_SPAIN | OUTSIDE_EU | UNKNOWN
        "vat_treatment": "TEXT",     # IVA_ES_21 | IVA_EU_B2B | IVA_EXPORT | IVA_EXEMPT | OSS_EU
        "activity_type": "TEXT",     # IAE activity: COACHING | NEWSLETTER | ILLUSTRATIONS (NULL until set)
        "supply_country": "TEXT",    # ISO-2 country code of the vendor / client
        # Invoice ledger. The accounting date is `invoice_date`;
        # `supply_date` is informational only.
        "tax_treatment": "TEXT",           # TAX_TREATMENTS_IN / TAX_TREATMENTS_OUT
        "deductible_pct_vat": "REAL",      # backfilled from deductible_pct
        "deductible_pct_irpf": "REAL",     # backfilled from deductible_pct
        "is_capital_asset": "INTEGER NOT NULL DEFAULT 0",
        "asset_class": "TEXT",
        "excluded": "INTEGER NOT NULL DEFAULT 0",  # 1 → ignored by every tax computation
        "excluded_reason": "TEXT",         # EXCLUDED_REASONS
        "eur_received": "REAL",            # EUR actually received (foreign-currency income)
        "payment_date": "TEXT",
        "vendor_vat_id_norm": "TEXT",      # normalize_vat_id(vendor_nif)
        "locked_fields": "TEXT",           # JSON list of user-edited field names
        "reviewed_at": "TEXT",
        # FX resolution: the LLM's own subtotal_eur/iva_amount/total_eur are
        # only a cross-check — the authoritative EUR figure is resolved by
        # src.fx_rates.resolve_invoice_amounts (ECB rate on invoice_date, or the
        # EUR actually charged when the document states it) and written here.
        "charged_eur": "REAL",             # EUR actually charged, when the document states it (expenses)
        "fx_rate_used": "REAL",            # 1 EUR = fx_rate_used units of original_currency
        "fx_rate_date": "TEXT",            # date the rate actually comes from (may differ from invoice_date)
        "fx_source": "TEXT",               # NATIVE_EUR | CHARGED_EUR | ECB | NO_RATE | INVALID_DATE | MISSING_FX_INPUT
        "fx_stale": "INTEGER NOT NULL DEFAULT 0",       # 1 if the ECB rate used was a stale fallback
        "fx_cross_check_diff_pct": "REAL", # |LLM total - resolved total| / resolved total x 100
    }
    for col, ddl in additions.items():
        if col not in existing:
            try:
                conn.execute(f"ALTER TABLE invoices ADD COLUMN {col} {ddl}")
                log.info("ℹ️ Migrated DB: added invoices.%s", col)
            except Exception as exc:
                log.warning("⚠️ DB migration skipped for invoices.%s: %s", col, exc)


def backfill_invoice_ledger_fields(conn: sqlite3.Connection) -> dict[str, int]:
    """Fill the #90 ledger columns on rows that predate them (idempotent).

    Only NULL targets are touched, so user edits and earlier backfills are never
    overwritten:

    - ``deductible_pct_vat`` / ``deductible_pct_irpf`` ← ``COALESCE(deductible_pct, 100)``
    - ``vendor_vat_id_norm`` ← ``normalize_vat_id(vendor_nif)``
    - ``tax_treatment`` ← ``derive_tax_treatment_for_invoice`` over the legacy
      ``vat_treatment`` / ``geo_region`` (rows it cannot decide stay NULL).

    Returns the number of rows updated per column.
    """
    counts: dict[str, int] = {}
    for col in ("deductible_pct_vat", "deductible_pct_irpf"):
        cur = conn.execute(
            f"UPDATE invoices SET {col} = COALESCE(deductible_pct, 100.0) WHERE {col} IS NULL"
        )
        counts[col] = cur.rowcount

    rows = conn.execute(
        "SELECT id, vendor_nif FROM invoices WHERE vendor_vat_id_norm IS NULL AND vendor_nif IS NOT NULL"
    ).fetchall()
    n_norm = 0
    for row in rows:
        norm = normalize_vat_id(row["vendor_nif"])
        if norm:
            conn.execute("UPDATE invoices SET vendor_vat_id_norm = ? WHERE id = ?", (norm, row["id"]))
            n_norm += 1
    counts["vendor_vat_id_norm"] = n_norm

    rows = conn.execute(
        """SELECT id, direction, vat_treatment, geo_region, iva_amount
           FROM invoices WHERE tax_treatment IS NULL"""
    ).fetchall()
    n_tt = 0
    for row in rows:
        tt = derive_tax_treatment_for_invoice(
            row["direction"], row["vat_treatment"], row["geo_region"], row["iva_amount"]
        )
        if tt:
            conn.execute("UPDATE invoices SET tax_treatment = ? WHERE id = ?", (tt, row["id"]))
            n_tt += 1
    counts["tax_treatment"] = n_tt

    conn.commit()
    if any(counts.values()):
        log.info("ℹ️ Backfilled invoice ledger fields: %s", counts)
    return counts


def backfill_invoice_classifications(conn: sqlite3.Connection) -> int:
    """Auto-derive geo_region and vat_treatment for invoices where they are NULL.

    Uses vendor_nif (expenses) or client_nif (income) to infer geo_region, then
    derives vat_treatment from geo_region + iva_amount.  Only touches rows where
    geo_region IS NULL; user-set values are never overwritten.

    Returns the number of rows updated.
    """
    rows = conn.execute(
        """SELECT id, direction, vendor_nif, client_nif, iva_amount
           FROM invoices WHERE geo_region IS NULL"""
    ).fetchall()
    updated = 0
    for row in rows:
        nif = row["vendor_nif"] if row["direction"] == "in" else row["client_nif"]
        geo = derive_geo_region_from_nif(nif)
        vat = derive_vat_treatment_for_invoice(
            row["direction"], geo, row["iva_amount"]
        )
        conn.execute(
            "UPDATE invoices SET geo_region = ?, vat_treatment = ? WHERE id = ?",
            (geo, vat, row["id"]),
        )
        updated += 1
    if updated:
        conn.commit()
        log.info("ℹ️ Backfilled geo_region/vat_treatment for %d invoice rows", updated)
    return updated


def _ensure_tax_entries_schema(conn: sqlite3.Connection) -> None:
    """Add ``quarterly_tax_entries.vat_rate`` (percent) on DBs that predate it (#97).

    Manual ``IVA_SOPORTADO`` entries carry the VAT cuota; the rate lets the
    engine derive the matching base for Modelo 303 box 28 instead of assuming
    21%. NULL on older rows — the engine counts their cuota and flags them.
    """
    if "vat_rate" not in _get_table_columns(conn, "quarterly_tax_entries"):
        conn.execute("ALTER TABLE quarterly_tax_entries ADD COLUMN vat_rate REAL")
        log.info("ℹ️ Migrated DB: added quarterly_tax_entries.vat_rate")


def _ensure_audit_schema(conn: sqlite3.Connection) -> None:
    """Create the tax_audit_log table on existing DBs that predate it."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tax_audit_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            computed_at  TEXT NOT NULL,
            year         INTEGER NOT NULL,
            quarter      INTEGER NOT NULL,
            model        TEXT NOT NULL,
            cell         TEXT NOT NULL,
            label        TEXT NOT NULL,
            formula      TEXT NOT NULL,
            inputs_json  TEXT NOT NULL DEFAULT '{}',
            value        REAL NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_audit_log_period
            ON tax_audit_log(year, quarter, model, computed_at)
    """)


# Snapshot statuses (a subset of ``src.tax_models.FilingStatus``).
SNAPSHOT_COMPUTED = "COMPUTED"
SNAPSHOT_FILED = "FILED"

# Versioned tax snapshots: one row per (year, quarter, model, version).
# The latest COMPUTED version is the working draft, rewritten by every
# recompute; a FILED version is born FILED ("Mark filed") and is immutable.
_TAX_SNAPSHOTS_TABLE_SQL = """
    CREATE TABLE IF NOT EXISTS {name} (
        year             INTEGER NOT NULL,
        quarter          INTEGER NOT NULL,
        model            TEXT NOT NULL,
        snapshot_version INTEGER NOT NULL DEFAULT 1,
        status           TEXT NOT NULL DEFAULT 'COMPUTED' CHECK (status IN ('COMPUTED', 'FILED')),
        payload_json     TEXT NOT NULL,
        computed_at      TEXT NOT NULL DEFAULT (datetime('now')),
        justificante     TEXT,
        presented_on     TEXT,
        filed_at         TEXT,
        PRIMARY KEY (year, quarter, model, snapshot_version)
    )
"""

# Same pattern as the declared_reports triggers: the database itself refuses to
# rewrite or delete a filed snapshot, and a row can only become FILED by being
# inserted as such (so a COMPUTED draft cannot be flipped to FILED in place).
_TAX_SNAPSHOTS_TRIGGERS_SQL = """
    CREATE TRIGGER IF NOT EXISTS tax_snapshots_filed_no_update
        BEFORE UPDATE ON tax_computation_snapshots
        WHEN OLD.status = 'FILED' OR NEW.status = 'FILED'
        BEGIN SELECT RAISE(ABORT, 'filed tax snapshots are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS tax_snapshots_filed_no_delete
        BEFORE DELETE ON tax_computation_snapshots
        WHEN OLD.status = 'FILED'
        BEGIN SELECT RAISE(ABORT, 'filed tax snapshots are immutable'); END;
"""


def _ensure_tax_snapshots_schema(conn: sqlite3.Connection) -> None:
    """Create the versioned ``tax_computation_snapshots`` table, migrating the old one.

    Before #101 the table was keyed (year, quarter, model) and every recompute
    overwrote the row. The key now includes ``snapshot_version``; SQLite cannot
    change a primary key in place, so an old table is rebuilt once, each
    existing row becoming version 1, status COMPUTED.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'tax_computation_snapshots'"
    ).fetchone()
    if exists and "snapshot_version" not in _get_table_columns(conn, "tax_computation_snapshots"):
        conn.executescript(
            "BEGIN;"
            + _TAX_SNAPSHOTS_TABLE_SQL.format(name="tax_computation_snapshots_v2") + ";"
            + """INSERT INTO tax_computation_snapshots_v2 (year, quarter, model, payload_json, computed_at)
                   SELECT year, quarter, model, payload_json, computed_at FROM tax_computation_snapshots;
                 DROP TABLE tax_computation_snapshots;
                 ALTER TABLE tax_computation_snapshots_v2 RENAME TO tax_computation_snapshots;
                 COMMIT;"""
        )
        log.info("ℹ️ Migrated DB: tax_computation_snapshots is now versioned (snapshot_version, status)")
    conn.executescript(
        _TAX_SNAPSHOTS_TABLE_SQL.format(name="tax_computation_snapshots") + ";"
        + "CREATE INDEX IF NOT EXISTS idx_tax_snapshots_year ON tax_computation_snapshots(year);"
        + _TAX_SNAPSHOTS_TRIGGERS_SQL
    )


def init_db(db_path: Optional[str | Path] = None) -> None:
    """Create tables if they don't exist."""
    conn = get_connection(db_path)
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
                source TEXT NOT NULL DEFAULT 'api',
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

            CREATE TABLE IF NOT EXISTS social_security_payments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                payment_date TEXT NOT NULL,
                amount_eur   REAL NOT NULL,
                description  TEXT NOT NULL DEFAULT '',
                source_file  TEXT NOT NULL DEFAULT '',
                imported_at  TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_ss_payments_date
                ON social_security_payments(payment_date);

            CREATE TABLE IF NOT EXISTS fx_exchange_differences (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id      TEXT,
                conversion_date TEXT NOT NULL,
                currency        TEXT NOT NULL,
                foreign_amount  REAL NOT NULL,
                eur_obtained    REAL NOT NULL,
                booked_eur      REAL NOT NULL,
                gain_loss_eur   REAL NOT NULL,
                notes           TEXT,
                created_at      TEXT NOT NULL DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_fx_exchange_diff_date
                ON fx_exchange_differences(conversion_date);
        """)
        _create_fx_rates_table(conn)
        _ensure_transactions_schema(conn)
        _ensure_invoices_schema(conn)
        _ensure_tax_entries_schema(conn)
        _ensure_audit_schema(conn)
        conn.commit()
        _ensure_tax_snapshots_schema(conn)
        backfill_invoice_classifications(conn)
        backfill_invoice_ledger_fields(conn)
        log.info("ℹ️ Database initialised at %s", connection._DB_PATH)
    finally:
        conn.close()
