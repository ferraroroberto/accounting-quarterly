"""SQLite database for persistent storage of Stripe transactions."""
from __future__ import annotations

import json
import re
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


def get_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
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


# ---------------------------------------------------------------------------
# Geographic / VAT classification helpers for invoices
# ---------------------------------------------------------------------------

# EU VAT country prefixes (ISO 2-letter codes of EU member states, excl. Spain)
_EU_VAT_PREFIXES: frozenset[str] = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "FI", "FR",
    "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL",
    "PL", "PT", "RO", "SE", "SI", "SK",
})

# Matches Spanish CIF (B12345678), DNI (12345678A), NIE (X1234567A), optionally ES-prefixed
_SPANISH_NIF_RE = re.compile(
    r"^(ES)?"
    r"([A-HJ-NP-SUVW]\d{7}[0-9A-J]"   # CIF (companies / entities)
    r"|\d{8}[A-Z]"                       # DNI (individuals)
    r"|[XYZ]\d{7}[A-Z])$",              # NIE (foreign residents)
    re.IGNORECASE,
)


def derive_geo_region_from_nif(nif: str | None) -> str:
    """Return SPAIN / EU_NOT_SPAIN / OUTSIDE_EU / UNKNOWN from a NIF or VAT number."""
    if not nif or not nif.strip():
        return "UNKNOWN"
    n = nif.strip().upper()
    # Explicit ES prefix or matches Spanish NIF/CIF/DNI/NIE pattern
    if n.startswith("ES") or _SPANISH_NIF_RE.match(n):
        return "SPAIN"
    # EU VAT number: starts with a 2-letter EU country prefix
    if len(n) >= 4 and n[:2] in _EU_VAT_PREFIXES:
        return "EU_NOT_SPAIN"
    # Anything else (US EIN, no NIF at all after stripping, etc.)
    return "OUTSIDE_EU"


def derive_vat_treatment_for_invoice(
    direction: str,
    geo_region: str,
    iva_amount: float | None,
) -> str:
    """Infer vat_treatment for an invoice row given direction + geo + IVA presence.

    direction='in'  (expense): what IVA regime applies to our input VAT
    direction='out' (income):  what regime applies to our output VAT
    """
    has_iva = bool(iva_amount and iva_amount > 0)
    if direction == "in":
        if has_iva:
            return "IVA_ES_21"          # Spanish VAT charged → deductible soportado
        if geo_region == "EU_NOT_SPAIN":
            return "IVA_EU_B2B"         # reverse charge — no box_28 impact
        return "IVA_EXEMPT"             # outside EU or exempt — no IVA
    else:  # direction == 'out'
        if geo_region == "SPAIN":
            return "IVA_ES_21" if has_iva else "IVA_EXEMPT"
        if geo_region == "EU_NOT_SPAIN":
            return "IVA_EU_B2B"         # intracom B2B (ISP) — box_59
        if geo_region == "OUTSIDE_EU":
            return "IVA_EXPORT"         # export exemption — Art. 21 LIVA
        return "IVA_EXEMPT"


# ---------------------------------------------------------------------------
# Invoice ledger: tax treatment, exclusion reasons, locks (issue #90)
# ---------------------------------------------------------------------------

# Per-invoice tax treatment. Expense (direction='in') and income ('out') use
# disjoint value sets so a treatment always implies its direction.
TAX_TREATMENTS_IN: tuple[str, ...] = (
    "DOMESTIC",           # Spanish VAT charged by the vendor → deductible input VAT
    "DOMESTIC_CAPITAL",   # as DOMESTIC, but a capital good (303 boxes 30/31)
    "INTRA_EU_RC",        # intra-EU acquisition, reverse charge (boxes 10/11 + 36/37)
    "NON_EU_RC",          # non-EU service, reverse charge (boxes 12/13 + 28/29)
    "NO_VAT",             # no VAT involved (bank fees, exempt supplies, …)
    "NOT_DEDUCTIBLE",     # VAT charged but not deductible
)
TAX_TREATMENTS_OUT: tuple[str, ...] = (
    "ES_21",              # Spanish 21% to a Spanish client
    "EU_B2C_ES21",        # EU consumer charged Spanish 21% (no OSS)
    "EU_B2B",             # intra-EU B2B service, reverse charge at the client (box 59)
    "NON_EU_NOT_SUBJECT", # non-EU client, not subject by location rules
    "EXEMPT_TEACHING",    # exempt teaching (art. 20.1.9º LIVA)
)
EXCLUDED_REASONS: tuple[str, ...] = (
    "duplicate", "receipt", "personal", "other_period", "superseded",
)

# Legacy `vat_treatment` (+ geo_region for the ambiguous IVA_EXEMPT bucket) →
# `tax_treatment`. The mapping preserves what the tax engine did with the
# legacy value: VAT-charged expenses stay deductible, zero-VAT rows stay
# VAT-neutral. `None` means "cannot be decided from the legacy data — review".
#   in  IVA_ES_21                    → DOMESTIC
#   in  IVA_EU_B2B                   → INTRA_EU_RC
#   in  IVA_EXEMPT + OUTSIDE_EU      → NON_EU_RC
#   in  IVA_EXEMPT + other geo / any other legacy value → NO_VAT
#   out IVA_ES_21                    → ES_21
#   out OSS_EU                       → EU_B2C_ES21
#   out IVA_EU_B2B                   → EU_B2B
#   out IVA_EXPORT                   → NON_EU_NOT_SUBJECT
#   out IVA_EXEMPT + SPAIN           → EXEMPT_TEACHING
#   out IVA_EXEMPT + other geo       → None (review)
_LEGACY_TO_TAX_TREATMENT: dict[tuple[str, str], str] = {
    ("in", "IVA_ES_21"): "DOMESTIC",
    ("in", "IVA_EU_B2B"): "INTRA_EU_RC",
    ("out", "IVA_ES_21"): "ES_21",
    ("out", "OSS_EU"): "EU_B2C_ES21",
    ("out", "IVA_EU_B2B"): "EU_B2B",
    ("out", "IVA_EXPORT"): "NON_EU_NOT_SUBJECT",
}

# Reverse direction: keeps the legacy `vat_treatment` column (still read by the
# engine until the #97 box model lands) consistent when `tax_treatment` is edited.
_TAX_TREATMENT_TO_LEGACY: dict[str, str] = {
    "DOMESTIC": "IVA_ES_21",
    "DOMESTIC_CAPITAL": "IVA_ES_21",
    "INTRA_EU_RC": "IVA_EU_B2B",
    "NON_EU_RC": "IVA_EXEMPT",
    "NO_VAT": "IVA_EXEMPT",
    "NOT_DEDUCTIBLE": "IVA_EXEMPT",
    "ES_21": "IVA_ES_21",
    "EU_B2C_ES21": "IVA_ES_21",
    "EU_B2B": "IVA_EU_B2B",
    "NON_EU_NOT_SUBJECT": "IVA_EXPORT",
    "EXEMPT_TEACHING": "IVA_EXEMPT",
}

# Fields a user may edit on an invoice. Any edited field is added to
# `locked_fields`; re-extraction (`upsert_invoice`) never overwrites it.
INVOICE_EDITABLE_FIELDS: tuple[str, ...] = (
    # OCR-extracted fields (corrections)
    "invoice_number", "invoice_date", "supply_date",
    "vendor_name", "vendor_nif", "client_name", "client_nif", "description",
    "subtotal_eur", "iva_rate", "iva_amount", "irpf_rate", "irpf_amount", "total_eur",
    "original_currency", "original_amount", "fx_rate", "category", "notes",
    # Ledger fields
    "tax_treatment", "deductible_pct_vat", "deductible_pct_irpf",
    "is_capital_asset", "asset_class", "excluded", "excluded_reason",
    "eur_received", "payment_date",
)

# Ledger-owned columns the OCR never produces: `upsert_invoice` only writes them
# on conflict when the caller passes them explicitly, so a re-extract keeps them.
_INVOICE_LEDGER_ONLY_FIELDS: tuple[str, ...] = (
    "is_capital_asset", "asset_class", "excluded", "excluded_reason",
    "eur_received", "payment_date",
)

_VAT_ID_SEPARATORS_RE = re.compile(r"[\s.\-/_]")


def normalize_vat_id(raw: Optional[str]) -> Optional[str]:
    """Canonical VAT id for matching: upper-case, separators stripped, Spanish ids ES-prefixed.

    ``"es-b12.345.678"`` and ``"B12345678"`` both become ``"ESB12345678"``; other
    ids keep whatever country prefix they carry. Returns ``None`` for blank input.
    """
    if not raw or not raw.strip():
        return None
    n = _VAT_ID_SEPARATORS_RE.sub("", raw.strip().upper())
    if not n:
        return None
    if not n.startswith("ES") and _SPANISH_NIF_RE.match(n):
        n = "ES" + n
    return n


def derive_tax_treatment_for_invoice(
    direction: str,
    vat_treatment: Optional[str],
    geo_region: Optional[str],
    iva_amount: Optional[float] = None,
) -> Optional[str]:
    """Map the legacy ``vat_treatment`` (derived first when missing) to a ``tax_treatment``.

    See the mapping table above ``_LEGACY_TO_TAX_TREATMENT``. Returns ``None``
    when the legacy data cannot decide (an income invoice without VAT to a
    non-Spanish or unknown counterparty).
    """
    geo = geo_region or "UNKNOWN"
    legacy = vat_treatment or derive_vat_treatment_for_invoice(direction, geo, iva_amount)
    mapped = _LEGACY_TO_TAX_TREATMENT.get((direction, legacy))
    if mapped:
        return mapped
    if direction == "in":
        return "NON_EU_RC" if (legacy == "IVA_EXEMPT" and geo == "OUTSIDE_EU") else "NO_VAT"
    if legacy == "IVA_EXEMPT" and geo == "SPAIN":
        return "EXEMPT_TEACHING"
    return None


def legacy_vat_treatment_for(tax_treatment: str) -> Optional[str]:
    """Legacy ``vat_treatment`` equivalent of a ``tax_treatment`` (None if unknown)."""
    return _TAX_TREATMENT_TO_LEGACY.get(tax_treatment)


def parse_locked_fields(raw: Optional[str]) -> list[str]:
    """Decode the ``locked_fields`` JSON list (tolerates NULL / malformed values)."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("⚠️ Ignoring malformed invoices.locked_fields value: %r", raw)
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


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
        "activity_type": "TEXT",     # CONSULTING | TRAINING | SOFTWARE | SUBSCRIPTIONS | …
        "supply_country": "TEXT",    # ISO-2 country code of the vendor / client
        # Invoice ledger (#90). The accounting date is `invoice_date`;
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


# Pre-e08a3ff9 (#42) Modelo303Result field names -> current AEAT-casilla-matching
# names. Mirrors src.tax_snapshot_codec._MODELO303_LEGACY_RENAMES; kept here too so
# stale rows are rewritten in place at startup, not just tolerated on read.
_MODELO303_LEGACY_SNAPSHOT_RENAMES: dict[str, str] = {
    "box_28_iva_soportado": "box_29_cuota_soportado",
    "box_29_base_soportado": "box_28_base_soportado",
}


def backfill_tax_snapshot_legacy_keys(conn: sqlite3.Connection) -> int:
    """Rewrite Modelo 303 snapshot rows still using pre-rename box_28/29 keys.

    Commit e08a3ff9 renamed ``Modelo303Result.box_28_iva_soportado`` to
    ``box_29_cuota_soportado`` and ``box_29_base_soportado`` to
    ``box_28_base_soportado``. Snapshots persisted before that rename still carry
    the legacy keys in ``payload_json`` and fail to decode (``decode_snapshot``
    tolerates this on read, but the stored row stays stale until rewritten here).
    Returns the number of rows migrated.
    """
    rows = conn.execute(
        "SELECT year, quarter, payload_json FROM tax_computation_snapshots WHERE model = '303'"
    ).fetchall()
    updated = 0
    for row in rows:
        data = json.loads(row["payload_json"])
        if not any(old_key in data for old_key in _MODELO303_LEGACY_SNAPSHOT_RENAMES):
            continue
        for old_key, new_key in _MODELO303_LEGACY_SNAPSHOT_RENAMES.items():
            if old_key in data:
                data[new_key] = data.pop(old_key)
        conn.execute(
            """UPDATE tax_computation_snapshots SET payload_json = ?
               WHERE year = ? AND quarter = ? AND model = '303'""",
            (json.dumps(data, ensure_ascii=False), row["year"], row["quarter"]),
        )
        updated += 1
    if updated:
        conn.commit()
        log.info("ℹ️ Migrated %d stale Modelo 303 snapshot row(s) to current box_28/29 field names", updated)
    return updated


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

            CREATE TABLE IF NOT EXISTS tax_computation_snapshots (
                year         INTEGER NOT NULL,
                quarter      INTEGER NOT NULL,
                model        TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                computed_at  TEXT NOT NULL DEFAULT (datetime('now')),
                PRIMARY KEY (year, quarter, model)
            );

            CREATE INDEX IF NOT EXISTS idx_tax_snapshots_year
                ON tax_computation_snapshots(year);

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
        """)
        _create_fx_rates_table(conn)
        _ensure_transactions_schema(conn)
        _ensure_invoices_schema(conn)
        _ensure_audit_schema(conn)
        conn.commit()
        backfill_invoice_classifications(conn)
        backfill_invoice_ledger_fields(conn)
        backfill_tax_snapshot_legacy_keys(conn)
        log.info("ℹ️ Database initialised at %s", _DB_PATH)
    finally:
        conn.close()


def upsert_payments(payments: list[Payment], source: str = "api",
                    db_path: Optional[str | Path] = None) -> tuple[int, int]:
    """Insert or update payments. Returns (inserted, updated) counts."""
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    """Insert or update a parsed invoice record keyed on (filename, direction). Returns its id.

    Re-extraction rules (#90):

    - Every field named in the stored row's ``locked_fields`` keeps its stored
      value — a user correction always survives a re-OCR. Derived columns
      (``geo_region``, ``vat_treatment``, ``tax_treatment``,
      ``vendor_vat_id_norm``) are recomputed from the merged values.
    - Ledger-only columns (``excluded``, ``eur_received``, …) are written on
      conflict only when ``data`` carries them, so a plain re-extract keeps them.
    - ``locked_fields`` and ``reviewed_at`` are owned by
      ``update_invoice_fields`` / ``unlock_invoice_fields`` and never written here.
    """
    import uuid
    data = dict(data)
    conn = get_connection(db_path)
    try:
        direction = data.get("direction", "in")
        filename = data.get("filename", "")
        existing = conn.execute(
            "SELECT * FROM invoices WHERE filename = ? AND direction = ?",
            (filename, direction),
        ).fetchone()
        if existing is not None:
            record_id = existing["id"]
            locked = parse_locked_fields(existing["locked_fields"])
            for field in locked:
                if field in existing.keys():
                    data[field] = existing[field]
            if locked:
                log.info("ℹ️ Re-extract of %s kept %d locked field(s): %s",
                         filename, len(locked), ", ".join(locked))
        else:
            record_id = data.get("id") or str(uuid.uuid4())

        iva_amount = data.get("iva_amount")
        vendor_nif = data.get("vendor_nif")
        client_nif = data.get("client_nif")
        # Auto-derive geo_region from NIF if not explicitly provided
        nif_for_geo = vendor_nif if direction == "in" else client_nif
        geo_region = data.get("geo_region") or derive_geo_region_from_nif(nif_for_geo)
        tax_treatment = data.get("tax_treatment")
        if tax_treatment:
            vat_treatment = (data.get("vat_treatment")
                             or legacy_vat_treatment_for(tax_treatment)
                             or derive_vat_treatment_for_invoice(direction, geo_region, iva_amount))
        else:
            vat_treatment = data.get("vat_treatment") or derive_vat_treatment_for_invoice(
                direction, geo_region, iva_amount
            )
            tax_treatment = derive_tax_treatment_for_invoice(
                direction, vat_treatment, geo_region, iva_amount
            )
        # Map category → activity_type when not explicitly set
        activity_type = data.get("activity_type") or data.get("category")
        # The split VAT/IRPF percentages default to the OCR's single deductible_pct.
        ded_pct = data.get("deductible_pct", 100)
        ded_default = 100.0 if ded_pct is None else ded_pct
        ded_vat = data.get("deductible_pct_vat")
        ded_irpf = data.get("deductible_pct_irpf")

        values: dict = {
            "id": record_id,
            "filename": filename,
            "direction": direction,
            "invoice_number": data.get("invoice_number"),
            "invoice_date": data.get("invoice_date"),
            "vendor_name": data.get("vendor_name"),
            "vendor_nif": vendor_nif,
            "vendor_address": data.get("vendor_address"),
            "client_name": data.get("client_name"),
            "client_nif": client_nif,
            "client_address": data.get("client_address"),
            "description": data.get("description"),
            "subtotal_eur": data.get("subtotal_eur"),
            "iva_rate": data.get("iva_rate"),
            "iva_amount": iva_amount,
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
            "deductible_pct": ded_pct,
            "billing_period_start": data.get("billing_period_start"),
            "billing_period_end": data.get("billing_period_end"),
            "geo_region": geo_region,
            "vat_treatment": vat_treatment,
            "activity_type": activity_type,
            "supply_country": data.get("supply_country"),
            "tax_treatment": tax_treatment,
            "deductible_pct_vat": ded_default if ded_vat is None else ded_vat,
            "deductible_pct_irpf": ded_default if ded_irpf is None else ded_irpf,
            "vendor_vat_id_norm": normalize_vat_id(vendor_nif),
            "is_capital_asset": 1 if data.get("is_capital_asset") else 0,
            "asset_class": data.get("asset_class"),
            "excluded": 1 if data.get("excluded") else 0,
            "excluded_reason": data.get("excluded_reason"),
            "eur_received": data.get("eur_received"),
            "payment_date": data.get("payment_date"),
        }
        update_cols = [
            c for c in values
            if c not in ("id", "filename", "direction")
            and (c not in _INVOICE_LEDGER_ONLY_FIELDS or c in data)
        ]
        cols = ", ".join(values)
        params = ", ".join(f":{c}" for c in values)
        sets = ",\n                ".join(f"{c} = excluded.{c}" for c in update_cols)
        conn.execute(f"""
            INSERT INTO invoices ({cols}) VALUES ({params})
            ON CONFLICT(filename, direction) DO UPDATE SET
                {sets},
                extracted_at = datetime('now')
        """, values)
        conn.commit()
        return record_id
    finally:
        conn.close()


def _coerce_invoice_field(field: str, value, direction: str):
    """Validate and normalise one user-edited invoice field; raises ValueError."""
    if isinstance(value, float) and value != value:  # NaN from pandas → cleared cell
        value = None
    if isinstance(value, str) and not value.strip():
        value = None  # blank text box → NULL; non-blank text is kept verbatim
    if field == "tax_treatment":
        allowed = TAX_TREATMENTS_IN if direction == "in" else TAX_TREATMENTS_OUT
        if value is not None and value not in allowed:
            raise ValueError(
                f"tax_treatment {value!r} is not valid for direction {direction!r} "
                f"(allowed: {', '.join(allowed)})"
            )
    elif field == "excluded_reason":
        if value is not None and value not in EXCLUDED_REASONS:
            raise ValueError(
                f"excluded_reason {value!r} is not one of {', '.join(EXCLUDED_REASONS)}"
            )
    elif field in ("excluded", "is_capital_asset"):
        value = 1 if value else 0
    elif field in ("deductible_pct_vat", "deductible_pct_irpf"):
        if value is not None:
            value = float(value)
            if not 0.0 <= value <= 100.0:
                raise ValueError(f"{field} must be between 0 and 100, got {value}")
    elif field in ("invoice_date", "supply_date", "payment_date"):
        if value is not None:
            value = str(value)[:10]
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError as exc:
                raise ValueError(f"{field} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc
    elif field in ("subtotal_eur", "iva_rate", "iva_amount", "irpf_rate", "irpf_amount",
                   "total_eur", "original_amount", "fx_rate", "eur_received"):
        if value is not None:
            value = float(value)
    return value


def update_invoice_fields(
    invoice_id: str,
    changes: dict,
    db_path: Optional[str | Path] = None,
) -> list[str]:
    """Apply user edits to one invoice, lock every changed field and stamp ``reviewed_at``.

    Only fields whose value actually differs from the stored one are written and
    added to ``locked_fields`` (so re-extraction keeps them). Editing
    ``tax_treatment`` also re-syncs the legacy ``vat_treatment``; editing
    ``vendor_nif`` re-derives ``vendor_vat_id_norm``. ``reviewed_at`` is stamped
    even when nothing changed (saving an unchanged form marks it reviewed).

    Returns the list of fields that changed. Raises ``KeyError`` for an unknown
    invoice and ``ValueError`` for a non-editable field or an invalid value.
    """
    unknown = [f for f in changes if f not in INVOICE_EDITABLE_FIELDS]
    if unknown:
        raise ValueError(f"Not user-editable invoice field(s): {', '.join(unknown)}")
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            raise KeyError(f"No invoice with id {invoice_id!r}")
        direction = row["direction"]
        updates: dict = {}
        for field, raw in changes.items():
            value = _coerce_invoice_field(field, raw, direction)
            if value != row[field]:
                updates[field] = value
        changed = list(updates)

        if updates.get("tax_treatment"):
            legacy = legacy_vat_treatment_for(updates["tax_treatment"])
            if legacy:
                updates["vat_treatment"] = legacy
        if "vendor_nif" in updates:
            updates["vendor_vat_id_norm"] = normalize_vat_id(updates["vendor_nif"])

        locked = sorted(set(parse_locked_fields(row["locked_fields"])) | set(changed))
        updates["locked_fields"] = json.dumps(locked) if locked else None
        updates["reviewed_at"] = datetime.now().isoformat(timespec="seconds")

        sets = ", ".join(f"{c} = :{c}" for c in updates)
        conn.execute(f"UPDATE invoices SET {sets} WHERE id = :_id", {**updates, "_id": invoice_id})
        conn.commit()
        if changed:
            log.info("ℹ️ Invoice %s edited and locked: %s", row["filename"], ", ".join(changed))
        return changed
    finally:
        conn.close()


def unlock_invoice_fields(
    invoice_id: str,
    fields: Optional[list[str]] = None,
    db_path: Optional[str | Path] = None,
) -> list[str]:
    """Release locks so the next re-extract may overwrite those fields again.

    ``fields=None`` releases every lock. Stored values are left as they are.
    Returns the fields that remain locked.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT locked_fields FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        if row is None:
            raise KeyError(f"No invoice with id {invoice_id!r}")
        locked = parse_locked_fields(row["locked_fields"])
        remaining = [] if fields is None else [f for f in locked if f not in set(fields)]
        conn.execute(
            "UPDATE invoices SET locked_fields = ? WHERE id = ?",
            (json.dumps(remaining) if remaining else None, invoice_id),
        )
        conn.commit()
        return remaining
    finally:
        conn.close()


def set_invoice_exclusion(
    invoice_id: str,
    excluded: bool,
    reason: Optional[str],
    db_path: Optional[str | Path] = None,
) -> bool:
    """Set ``excluded``/``excluded_reason`` directly, without locking (issue #92).

    Unlike ``update_invoice_fields``, this never adds to ``locked_fields`` or
    stamps ``reviewed_at`` — an automated dedupe proposal stays overridable by a
    plain re-extract or a later manual edit, per #90's rule that only a user
    edit locks a field. Refuses (returns ``False``, no write) when the row
    already has ``excluded`` in its ``locked_fields`` — a user decision on that
    row's exclusion always wins over an automated one.

    Raises ``KeyError`` for an unknown invoice and ``ValueError`` for an
    invalid ``reason``.
    """
    if reason is not None and reason not in EXCLUDED_REASONS:
        raise ValueError(f"excluded_reason {reason!r} is not one of {', '.join(EXCLUDED_REASONS)}")
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT locked_fields FROM invoices WHERE id = ?", (invoice_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"No invoice with id {invoice_id!r}")
        if "excluded" in parse_locked_fields(row["locked_fields"]):
            return False
        conn.execute(
            "UPDATE invoices SET excluded = ?, excluded_reason = ? WHERE id = ?",
            (1 if excluded else 0, reason, invoice_id),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def get_invoices(direction: Optional[str] = None,
                 db_path: Optional[str | Path] = None) -> list[dict]:
    """Return all invoice records, optionally filtered by direction."""
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
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
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM tax_filing_status WHERE year = ? ORDER BY model, quarter",
            (year,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# Quarter value for annual-only snapshots (e.g. Modelo 347).
TAX_SNAPSHOT_QUARTER_ANNUAL = 0


def upsert_tax_snapshot_conn(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    model: str,
    payload_json: str,
    computed_at: str,
) -> None:
    """Insert or replace one stored tax computation snapshot."""
    conn.execute(
        """INSERT INTO tax_computation_snapshots (year, quarter, model, payload_json, computed_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(year, quarter, model) DO UPDATE SET
             payload_json = excluded.payload_json,
             computed_at = excluded.computed_at""",
        (year, quarter, model, payload_json, computed_at),
    )


def load_tax_snapshots_for_period(
    year: int,
    quarter: int,
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Load snapshots for quarterly models for ``quarter``, plus annual Modelo 347 (``quarter`` 0)."""
    return conn.execute(
        """SELECT model, quarter, payload_json, computed_at
           FROM tax_computation_snapshots
           WHERE year = ? AND (quarter = ? OR (model = '347' AND quarter = ?))""",
        (year, quarter, TAX_SNAPSHOT_QUARTER_ANNUAL),
    ).fetchall()


# ---------------------------------------------------------------------------
# tax_audit_log helpers
# ---------------------------------------------------------------------------

def upsert_audit_entries_conn(
    conn: sqlite3.Connection,
    entries: list,  # list[AuditEntry]
    computed_at: str,
) -> None:
    """Persist a list of AuditEntry objects for the given computed_at timestamp.

    Replaces any existing entries for the same (year, quarter, model, computed_at)
    so re-running a calculation always gives a fresh, consistent audit trail.
    """
    if not entries:
        return
    first = entries[0]
    conn.execute(
        "DELETE FROM tax_audit_log WHERE year = ? AND quarter = ? AND model = ? AND computed_at = ?",
        (first.year, first.quarter, first.model, computed_at),
    )
    conn.executemany(
        """INSERT INTO tax_audit_log
               (computed_at, year, quarter, model, cell, label, formula, inputs_json, value)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (computed_at, e.year, e.quarter, e.model, e.cell,
             e.label, e.formula, e.inputs_json, e.value)
            for e in entries
        ],
    )


def load_audit_entries(
    year: int,
    quarter: int,
    model: str,
    computed_at: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> list[dict]:
    """Load audit log rows for a given period and model.

    If *computed_at* is None, returns entries from the most recent computation run.
    """
    conn = get_connection(db_path)
    try:
        _ensure_audit_schema(conn)
        if computed_at is None:
            row = conn.execute(
                """SELECT MAX(computed_at) AS ts FROM tax_audit_log
                   WHERE year = ? AND quarter = ? AND model = ?""",
                (year, quarter, model),
            ).fetchone()
            if not row or not row["ts"]:
                return []
            computed_at = row["ts"]
        rows = conn.execute(
            """SELECT id, computed_at, year, quarter, model, cell, label, formula, inputs_json, value
               FROM tax_audit_log
               WHERE year = ? AND quarter = ? AND model = ? AND computed_at = ?
               ORDER BY id""",
            (year, quarter, model, computed_at),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
