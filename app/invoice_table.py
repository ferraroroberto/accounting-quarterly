"""The all-invoices table shape shared by Invoice Explorer and Invoice OCR."""
from __future__ import annotations

import pandas as pd

INVOICE_TABLE_COLUMNS: tuple[str, ...] = (
    "direction", "filename", "extracted_at", "invoice_date",
    "invoice_type", "vendor_name", "vendor_nif",
    "client_name", "client_nif",
    "description", "subtotal_eur", "iva_rate", "iva_amount",
    "irpf_rate", "irpf_amount", "total_eur", "currency",
    "category", "payment_method",
    "supply_date", "due_date", "tax_treatment",
    "deductible_pct_vat", "deductible_pct_irpf", "excluded", "excluded_reason",
    "is_rectificativa", "vat_exempt_reason", "notes",
)


def invoice_display_frame(df: pd.DataFrame, *, with_id: bool = False) -> pd.DataFrame:
    """The table columns present in `df`, with ``extracted_at`` shown as ``date_scanned``.

    ``with_id`` keeps the row id as the first column so a caller can map a table
    selection back to records; it must hide that column before display.
    """
    wanted = (("id",) if with_id else ()) + INVOICE_TABLE_COLUMNS
    return df[[c for c in wanted if c in df.columns]].rename(columns={"extracted_at": "date_scanned"})
