"""Shared data-loading utilities for Streamlit tabs (with caching)."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import calendar
from datetime import datetime
from typing import Optional

import streamlit as st

from src.classifier import classify_batch
from src.fx_rates import convert_to_eur, init_fx_table
from src.models import ClassifiedPayment, Payment
from src.rules_engine import load_rules
from src.database import load_classified_payments, upsert_classified, upsert_payments
from src.stripe_client import fetch_charges


QUARTER_MONTHS = {1: (1, 3), 2: (4, 6), 3: (7, 9), 4: (10, 12)}


def quarter_dates(year: int, quarter: int) -> tuple[datetime, datetime]:
    start_month, end_month = QUARTER_MONTHS[quarter]
    last_day = calendar.monthrange(year, end_month)[1]
    start = datetime(year, start_month, 1)
    end = datetime(year, end_month, last_day, 23, 59, 59)
    return start, end


def load_payments_for_period_api(
    start_date: datetime,
    end_date: datetime,
    *,
    force_refresh_token: Optional[str] = None,
) -> list[Payment]:
    """Load payments from Stripe API.

    This function is intentionally not Streamlit-cached. Pass a unique
    `force_refresh_token` (e.g. an ISO timestamp) to make the call-site intent explicit
    when you want to guarantee a fresh API fetch.
    """
    _ = force_refresh_token  # explicit cache-busting token (not used directly here)
    return fetch_charges(start_date, end_date)


def apply_fx_conversion(payments: list[Payment]) -> list[Payment]:
    """Convert non-EUR payments to EUR using stored FX rates."""
    init_fx_table()
    converted = []
    for p in payments:
        if p.currency != "eur" and p.fx_rate is None:
            tx_date = p.created_date.date()
            amount_eur, rate = convert_to_eur(p.converted_amount, p.currency, tx_date)
            fee_eur, _ = convert_to_eur(p.fee, p.currency, tx_date)
            refund_eur, _ = convert_to_eur(p.converted_amount_refunded, p.currency, tx_date)
            p = p.model_copy(update={
                "amount_original": p.converted_amount,
                "converted_amount": amount_eur,
                "converted_amount_refunded": refund_eur,
                "fee": fee_eur,
                "fx_rate": rate,
            })
        elif p.currency == "eur" and p.fx_rate is None:
            p = p.model_copy(update={"fx_rate": 1.0})
        converted.append(p)
    return converted


@st.cache_data(ttl=300, show_spinner=False)
def classify_payments(payments_tuple: tuple) -> list[ClassifiedPayment]:
    """Classify a tuple of payments (hashable for caching)."""
    payments = [Payment.model_validate_json(p) for p in payments_tuple]
    rules = load_rules()
    classified, _ = classify_batch(payments, rules)
    return classified


def get_classified_for_period(
    year: int,
    quarter: Optional[int],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    *,
    input_mode: Optional[str] = None,
    force_refresh_token: Optional[str] = None,
) -> list[ClassifiedPayment]:
    mode = (input_mode or "api").lower()

    if start_date is None or end_date is None:
        if quarter:
            start_date, end_date = quarter_dates(year, quarter)
        else:
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31, 23, 59, 59)

    if mode == "db":
        # Return stored classifications directly — no re-classification needed.
        return load_classified_payments(start_date, end_date)

    # API mode: fetch fresh data from Stripe, classify, and persist.
    payments = load_payments_for_period_api(
        start_date,
        end_date,
        force_refresh_token=force_refresh_token,
    )
    payments = apply_fx_conversion(payments)
    upsert_payments(payments, source="api")

    classified = classify_payments(tuple(p.model_dump_json() for p in payments))
    # Persist classification back to DB (best-effort).
    try:
        upsert_classified(classified)
    except Exception:
        # UI can still function even if DB update fails (e.g. readonly file)
        pass
    return classified


def invalidate_cache():
    st.cache_data.clear()
