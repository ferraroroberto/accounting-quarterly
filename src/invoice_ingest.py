"""Invoice PDF ingestion: OCR extraction → FX resolution → vendor registry → DB.

The single save path for an extracted invoice, shared by the Invoice OCR tab
(``app/invoice_ocr_tab.py``) and the quarter-close pipeline
(``scripts/close_quarter.py ocr``, via ``src/close_pipeline.py``). A file needs
extraction when it has no stored row yet or its MD5 differs from the stored
``file_hash`` (the PDF changed since it was extracted).
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Optional

from src.database import get_invoice_hash
from src.fx_rates import resolve_invoice_amounts
from src.invoice_ocr import extract_invoice
from src.invoice_scanner import resolve_invoice_dir, scan_invoice_pdfs
from src.logger import get_logger
from src.vendor_registry import upsert_invoice_with_registry

log = get_logger(__name__)


def compute_file_hash(
    filename: str, direction: str, config: Optional[dict[str, Any]] = None
) -> str:
    """MD5 of the PDF at ``filename`` (relative to the ``direction`` invoice dir)."""
    pdf_path = resolve_invoice_dir(direction, config) / filename
    return hashlib.md5(pdf_path.read_bytes()).hexdigest()


def needs_extraction(
    filename: str,
    direction: str,
    config: Optional[dict[str, Any]] = None,
    db_path: Optional[str | Path] = None,
) -> bool:
    """Return True if the file has not been extracted yet or the PDF has changed."""
    stored_hash = get_invoice_hash(filename, direction, db_path=db_path)
    if stored_hash is None:
        return True
    return compute_file_hash(filename, direction, config) != stored_hash


def list_invoice_files(direction: str, config: Optional[dict[str, Any]] = None) -> list[str]:
    """Every PDF under the ``direction`` invoice dir, relative to it (sorted)."""
    base = resolve_invoice_dir(direction, config)
    return [str(p.relative_to(base)) for p in scan_invoice_pdfs(direction, config)]


def pending_files(
    direction: str,
    config: Optional[dict[str, Any]] = None,
    db_path: Optional[str | Path] = None,
) -> list[str]:
    """PDFs of ``direction`` that are new or changed since their last extraction."""
    return [f for f in list_invoice_files(direction, config)
            if needs_extraction(f, direction, config, db_path)]


def extract_and_save(
    filename: str,
    direction: str,
    *,
    config: Optional[dict[str, Any]] = None,
    db_path: Optional[str | Path] = None,
    model: Optional[str] = None,
) -> dict:
    """Run extraction via the configured OCR backend and persist to DB. Returns the stored record.

    ``model`` overrides the hub model (else the ``LLM_HUB_MODEL`` env var, else
    the ``gemini_pro`` alias). Registry defaults are applied to expense rows,
    never over locked fields.
    """
    pdf_path = resolve_invoice_dir(direction, config) / filename
    data = extract_invoice(pdf_path, model=model)

    # FX (#93): the LLM's own subtotal_eur/iva_amount/total_eur are only a
    # cross-check for a foreign-currency document — the authoritative EUR
    # figures come from the ECB rate on invoice_date, or the EUR actually
    # charged when the document states it (charged_eur wins for expenses).
    fx = resolve_invoice_amounts(direction, data, db_path)
    if fx.fx_warning:
        log.warning("⚠️ FX resolution for %s: %s", filename, fx.fx_warning)

    record = {
        "filename": filename,
        "direction": direction,
        "file_hash": data.get("_file_hash"),
        "invoice_number": data.get("invoice_number"),
        "invoice_date": data.get("invoice_date"),
        "vendor_name": data.get("vendor_name"),
        "vendor_nif": data.get("vendor_nif"),
        "vendor_address": data.get("vendor_address"),
        "client_name": data.get("client_name"),
        "client_nif": data.get("client_nif"),
        "client_address": data.get("client_address"),
        "description": data.get("description"),
        "subtotal_eur": fx.subtotal_eur,
        "iva_rate": data.get("iva_rate"),
        "iva_amount": fx.iva_amount,
        "irpf_rate": data.get("irpf_rate"),
        "irpf_amount": data.get("irpf_amount"),
        "total_eur": fx.total_eur,
        "currency": data.get("currency", "EUR"),
        "original_currency": data.get("original_currency"),
        "original_amount": data.get("original_amount"),
        "fx_rate": data.get("fx_rate"),
        "charged_eur": data.get("charged_eur"),
        "fx_rate_used": fx.fx_rate_used,
        "fx_rate_date": fx.fx_rate_date,
        "fx_source": fx.fx_source,
        "fx_stale": fx.fx_stale,
        "fx_cross_check_diff_pct": fx.fx_cross_check_diff_pct,
        "payment_method": data.get("payment_method"),
        "category": data.get("category"),
        "notes": data.get("notes"),
        "raw_json": data.get("_raw_response"),
        # Enhanced Spanish accounting fields
        "invoice_type": data.get("invoice_type"),
        "supply_date": data.get("supply_date"),
        "due_date": data.get("due_date"),
        "is_rectificativa": 1 if data.get("is_rectificativa") else 0,
        "rectified_invoice_ref": data.get("rectified_invoice_ref"),
        "vat_exempt_reason": data.get("vat_exempt_reason"),
        "iva_breakdown": json.dumps(data.get("iva_breakdown")) if data.get("iva_breakdown") else None,
        "deductible_pct": data.get("deductible_pct"),
        "billing_period_start": data.get("billing_period_start"),
        "billing_period_end": data.get("billing_period_end"),
    }
    upsert_invoice_with_registry(record, db_path=db_path)  # vendor-registry defaults, never over locked fields
    return record
