"""Shared data-loading utilities for Streamlit pages (with caching)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime
from typing import Optional

import streamlit as st

from src.classifier import classify_batch
from src.config import load_config, reload_config, save_config
from src.csv_importer import merge_csv_files, parse_stripe_csv
from src.models import ClassifiedPayment, Payment


QUARTER_MONTHS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}


def quarter_dates(year: int, quarter: int) -> tuple[datetime, datetime]:
    start_month, end_month = QUARTER_MONTHS[quarter]
    import calendar
    last_day = calendar.monthrange(year, end_month)[1]
    start = datetime(year, start_month, 1)
    end = datetime(year, end_month, last_day, 23, 59, 59)
    return start, end


@st.cache_data(ttl=300, show_spinner=False)
def load_all_payments(csv_old: str, csv_new: str) -> list[Payment]:
    """Load and merge both CSV files."""
    old_path = ROOT / csv_old
    new_path = ROOT / csv_new
    if old_path.exists() and new_path.exists():
        return merge_csv_files(old_path, new_path)
    elif old_path.exists():
        return parse_stripe_csv(old_path)
    elif new_path.exists():
        return parse_stripe_csv(new_path)
    return []


@st.cache_data(ttl=300, show_spinner=False)
def load_payments_for_period(
    csv_old: str,
    csv_new: str,
    start_date: datetime,
    end_date: datetime,
) -> list[Payment]:
    old_path = ROOT / csv_old
    new_path = ROOT / csv_new
    if old_path.exists() and new_path.exists():
        return merge_csv_files(old_path, new_path, start_date, end_date)
    elif old_path.exists():
        return parse_stripe_csv(old_path, start_date, end_date)
    elif new_path.exists():
        return parse_stripe_csv(new_path, start_date, end_date)
    return []


@st.cache_data(ttl=300, show_spinner=False)
def classify_payments(payments_tuple: tuple) -> list[ClassifiedPayment]:
    """Classify a tuple of payments (hashable for caching).

    Payments are serialised to JSON strings by the caller so they are
    hashable for st.cache_data. We deserialise them back here before
    passing to classify_batch which expects Payment objects.
    """
    payments = [Payment.model_validate_json(p) for p in payments_tuple]
    cfg = load_config()
    classified, _ = classify_batch(payments, cfg)
    return classified


def get_classified_for_period(
    year: int,
    quarter: Optional[int],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
) -> list[ClassifiedPayment]:
    cfg = load_config()
    app_cfg = cfg.get("app", {})
    csv_old = app_cfg.get("csv_path", "tmp/unified_payments_all_old.csv")
    csv_new = app_cfg.get("csv_path_new", "tmp/unified_payments_all.csv")

    if start_date is None or end_date is None:
        if quarter:
            start_date, end_date = quarter_dates(year, quarter)
        else:
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31, 23, 59, 59)

    payments = load_payments_for_period(csv_old, csv_new, start_date, end_date)
    classified = classify_payments(tuple(p.model_dump_json() for p in payments))
    return classified


def invalidate_cache():
    st.cache_data.clear()
