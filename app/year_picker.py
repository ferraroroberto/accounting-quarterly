"""One year range for every tab's year picker: the first year with data up to next year."""
from __future__ import annotations

import sqlite3
from datetime import date
from typing import Optional

import streamlit as st

from app.data_loader import DEFAULT_FIRST_YEAR, first_data_year
from src.logger import get_logger

log = get_logger(__name__)


def year_choices() -> list[int]:
    """Years a picker offers, ascending: first data year to current year + 1."""
    this_year = date.today().year
    try:
        first = first_data_year()
    except sqlite3.Error as exc:   # no transactions table yet: offer the default range, not a crash
        log.warning("⚠️ Year picker could not read the first data year (%s); using %d", exc, DEFAULT_FIRST_YEAR)
        first = DEFAULT_FIRST_YEAR
    return list(range(min(first, this_year), this_year + 2))


def year_input(key: str, default: Optional[int] = None, *, label: str = "Year", disabled: bool = False) -> int:
    """A year ``st.number_input`` bounded by ``year_choices()`` (the default is clamped into the range)."""
    years = year_choices()
    value = min(max(default if default is not None else date.today().year, years[0]), years[-1])
    return int(st.number_input(
        label, min_value=years[0], max_value=years[-1], value=value, step=1, key=key, disabled=disabled,
    ))
