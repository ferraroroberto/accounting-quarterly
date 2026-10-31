"""Shared data-loading utilities for Streamlit tabs.

Period loading, FX conversion and classification live in ``src/payments_loader.py``,
which the tabs call directly: the "db" mode they render with only reads stored
classifications, so there is nothing to cache here. This module holds the
first-year lookup and the cache reset.
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


def invalidate_cache():
    st.cache_data.clear()
