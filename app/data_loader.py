"""Shared data-loading utilities for Streamlit tabs (with caching).

Thin UI wrapper — the actual period loading, FX conversion and classification
logic lives in ``src/payments_loader.py`` (#166) so the CLI and close pipeline
can use it without importing Streamlit. This module only adds the
``@st.cache_data`` layer.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from datetime import datetime
from typing import Optional

import streamlit as st

from src.database import get_transaction_date_bounds
from src.models import ClassifiedPayment, Payment
from src.payments_loader import classify_payments as _classify_payments_uncached
from src.payments_loader import get_classified_for_period as _get_classified_for_period

# Fallback first year when no transactions are stored yet (e.g. a fresh DB).
DEFAULT_FIRST_YEAR = 2023


def first_data_year(min_tx_dt: Optional[datetime] = None) -> int:
    """Earliest year with stored transaction data, or ``DEFAULT_FIRST_YEAR``.

    Pass an already-fetched ``min_tx_dt`` (e.g. from `get_transaction_date_bounds`)
    to avoid a second query; omit it to have this call fetch the bound itself.
    """
    if min_tx_dt is None:
        min_tx_dt, _ = get_transaction_date_bounds()
    return min_tx_dt.year if min_tx_dt else DEFAULT_FIRST_YEAR


@st.cache_data(ttl=300, show_spinner=False)
def _classify_payments_cached(payments_tuple: tuple) -> list[ClassifiedPayment]:
    """Classify a tuple of payments (hashable for caching)."""
    payments = [Payment.model_validate_json(p) for p in payments_tuple]
    return _classify_payments_uncached(payments)


def _classify_cached(payments: list[Payment]) -> list[ClassifiedPayment]:
    return _classify_payments_cached(tuple(p.model_dump_json() for p in payments))


def get_classified_for_period(
    year: int,
    quarter: Optional[int],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    *,
    input_mode: Optional[str] = None,
) -> list[ClassifiedPayment]:
    """Streamlit-facing wrapper over ``src.payments_loader.get_classified_for_period``,
    with classification routed through the ``@st.cache_data``-wrapped classifier
    above so repeated tab renders don't re-run the rules engine."""
    return _get_classified_for_period(
        year, quarter, start_date, end_date,
        input_mode=input_mode, classify_fn=_classify_cached,
    )


def invalidate_cache():
    st.cache_data.clear()
