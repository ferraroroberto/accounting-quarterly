from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import pandas as pd

from src.exceptions import CSVParseError
from src.logger import get_logger
from src.models import Payment

log = get_logger(__name__)

_AMOUNT_RE = re.compile(r"[\s]")


def _normalise_amount(value) -> float:
    """Convert European locale number strings ('1.234,56') to float."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0.0
    s = str(value).strip()
    s = s.replace("\xa0", "").replace(" ", "")
    if "," in s and "." in s:
        if s.rindex(",") > s.rindex("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        log.warning("⚠️ Could not parse amount: %r → 0.0", value)
        return 0.0


def _detect_currency_from_row(row: pd.Series) -> str:
    """Infer currency when column is missing. Default to 'eur'."""
    for col in row.index:
        if "currency" in str(col).lower():
            v = row[col]
            if pd.notna(v):
                return str(v).lower().strip()
    return "eur"


def parse_stripe_csv(
    path: str | Path,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    deduplicate: bool = True,
) -> list[Payment]:
    """Parse a Stripe CSV export (with or without Currency column) into Payment objects."""
    path = Path(path)
    if not path.exists():
        raise CSVParseError(f"CSV file not found: {path}")

    try:
        df = pd.read_csv(path, encoding="utf-8", low_memory=False)
    except UnicodeDecodeError:
        df = pd.read_csv(path, encoding="iso-8859-1", low_memory=False)

    df.columns = [c.strip() for c in df.columns]

    col_map = _detect_columns(df)
    has_currency = "currency" in col_map

    log.info("ℹ️ Loaded %d rows from %s (currency column: %s)", len(df), path.name, has_currency)

    payments: list[Payment] = []
    seen_ids: set[str] = set()

    for _, row in df.iterrows():
        try:
            pid = str(row[col_map["id"]]).strip()

            if deduplicate:
                if pid in seen_ids:
                    log.debug("Duplicate skipped: %s", pid)
                    continue
                seen_ids.add(pid)

            if pid.startswith("ch_test_") or pid.startswith("py_test_"):
                log.debug("Test transaction skipped: %s", pid)
                continue

            raw_date = str(row[col_map["created_date"]]).strip()
            try:
                created_dt = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                created_dt = datetime.fromisoformat(raw_date)

            if start_date and created_dt < start_date:
                continue
            if end_date and created_dt > end_date:
                continue

            amount = _normalise_amount(row[col_map["converted_amount"]])
            refunded = _normalise_amount(row.get(col_map.get("converted_amount_refunded", ""), 0))
            fee = _normalise_amount(row[col_map["fee"]])

            description = ""
            if col_map.get("description"):
                v = row[col_map["description"]]
                description = "" if pd.isna(v) else str(v).strip()

            if has_currency:
                currency_val = row[col_map["currency"]]
                currency = "eur" if pd.isna(currency_val) else str(currency_val).lower().strip()
            else:
                currency = "eur"

            payment_type_meta = _get_meta(row, col_map, "payment_type_meta")
            event_api_id_meta = _get_meta(row, col_map, "event_api_id_meta")
            email_meta = _get_meta(row, col_map, "email_meta")

            p = Payment(
                id=pid,
                created_date=created_dt,
                converted_amount=amount,
                converted_amount_refunded=refunded,
                description=description,
                fee=fee,
                currency=currency,
                payment_type_meta=payment_type_meta,
                event_api_id_meta=event_api_id_meta,
                email_meta=email_meta,
            )
            payments.append(p)

        except Exception as exc:
            log.warning("⚠️ Skipping row (parse error): %s | %s", row.get(col_map.get("id", "id"), "?"), exc)

    log.info("ℹ️ Parsed %d valid payments from %s", len(payments), path.name)
    return payments


def _get_meta(row: pd.Series, col_map: dict, key: str) -> Optional[str]:
    col = col_map.get(key)
    if not col or col not in row.index:
        return None
    v = row[col]
    if pd.isna(v) or str(v).strip() == "":
        return None
    return str(v).strip()


def _detect_columns(df: pd.DataFrame) -> dict:
    """Map logical field names to actual CSV column names (case-insensitive partial match)."""
    cols_lower = {c.lower(): c for c in df.columns}

    def find(patterns: list[str]) -> Optional[str]:
        for p in patterns:
            for col_l, col_orig in cols_lower.items():
                if p in col_l:
                    return col_orig
        return None

    m = {}

    id_col = find(["id"])
    non_meta_id = next(
        (c for c in df.columns if c.lower().strip() == "id"),
        id_col,
    )
    m["id"] = non_meta_id

    m["created_date"] = find(["created date", "created_date"])
    m["converted_amount"] = find(["converted amount"])
    m["fee"] = find(["fee"])
    m["description"] = find(["description"])

    if find(["currency"]):
        m["currency"] = find(["currency"])

    refund_col = find(["converted amount refunded", "refunded"])
    if refund_col:
        m["converted_amount_refunded"] = refund_col

    m["payment_type_meta"] = find(["payment_type"])
    m["event_api_id_meta"] = find(["event_api_id"])
    m["email_meta"] = find(["email (metadata)", "email"])

    missing = [k for k, v in m.items() if v is None and k not in ("currency", "converted_amount_refunded", "payment_type_meta", "event_api_id_meta", "email_meta")]
    if missing:
        raise CSVParseError(f"Required columns not found: {missing}")

    return {k: v for k, v in m.items() if v is not None}


def merge_csv_files(
    primary_path: str | Path,
    secondary_path: str | Path,
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> list[Payment]:
    """Merge two CSV files, deduplicating by ID. Primary takes precedence for currency info."""
    primary = parse_stripe_csv(primary_path, start_date, end_date, deduplicate=True)
    secondary = parse_stripe_csv(secondary_path, start_date, end_date, deduplicate=True)

    primary_ids = {p.id for p in primary}
    merged = list(primary)
    added = 0
    for p in secondary:
        if p.id not in primary_ids:
            merged.append(p)
            added += 1

    log.info("ℹ️ Merged CSVs: %d primary + %d new from secondary = %d total", len(primary), added, len(merged))
    merged.sort(key=lambda p: p.created_date)
    return merged
