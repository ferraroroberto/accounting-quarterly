"""``invoices`` and ``uploaded_files`` tables: upload log, upsert, edit, lock, delete, stats."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.tax_codes import (
    EXCLUDED_REASONS,
    TAX_TREATMENTS_IN,
    TAX_TREATMENTS_OUT,
    derive_geo_region_from_nif,
    derive_tax_treatment_for_invoice,
    derive_vat_treatment_for_invoice,
    legacy_vat_treatment_for,
    normalize_vat_id,
)
from src.database.connection import get_connection

log = get_logger(__name__)


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
    # FX (#93) — charged_eur is the one FX field a user may correct by hand;
    # the rest (fx_rate_used, fx_source, ...) are derived and re-resolved on
    # the next OCR extraction, not directly editable.
    "charged_eur",
)

# Ledger-owned columns the OCR never produces: `upsert_invoice` only writes them
# on conflict when the caller passes them explicitly, so a re-extract keeps them.
_INVOICE_LEDGER_ONLY_FIELDS: tuple[str, ...] = (
    "is_capital_asset", "asset_class", "excluded", "excluded_reason",
    "eur_received", "payment_date",
)

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


def record_upload(filename: str, direction: str, api_response: str = "",
                  db_path: Optional[str | Path] = None) -> bool:
    """Record an invoice upload. Returns True if new, False if already uploaded.

    A leftover ``ERROR: ...`` row from an old failed upload does not count as
    uploaded: it is replaced by this record.
    """
    conn = get_connection(db_path)
    try:
        conn.execute(
            "DELETE FROM upload_log WHERE filename = ? AND direction = ? AND api_response LIKE 'ERROR:%'",
            (filename, direction),
        )
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
        # activity_type is an IAE activity only (COACHING | NEWSLETTER | ILLUSTRATIONS), set by
        # the vendor registry or the ledger. The OCR expense `category` lives in its own column.
        activity_type = data.get("activity_type")
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
            "charged_eur": data.get("charged_eur"),
            "fx_rate_used": data.get("fx_rate_used"),
            "fx_rate_date": data.get("fx_rate_date"),
            "fx_source": data.get("fx_source"),
            "fx_stale": 1 if data.get("fx_stale") else 0,
            "fx_cross_check_diff_pct": data.get("fx_cross_check_diff_pct"),
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
                   "total_eur", "original_amount", "fx_rate", "eur_received", "charged_eur"):
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
