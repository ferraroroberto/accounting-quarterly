"""The shared year-picker range (#167): first data year to next year, never below/above the current year."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest
from streamlit.testing.v1 import AppTest

import app.year_picker as yp


def test_range_runs_from_the_first_data_year_to_next_year(monkeypatch):
    monkeypatch.setattr(yp, "first_data_year", lambda: 2023)
    this_year = date.today().year
    assert yp.year_choices() == list(range(2023, this_year + 2))


def test_a_first_data_year_in_the_future_still_includes_the_current_year(monkeypatch):
    monkeypatch.setattr(yp, "first_data_year", lambda: date.today().year + 5)
    this_year = date.today().year
    assert yp.year_choices() == [this_year, this_year + 1]


def test_missing_table_falls_back_to_the_default_first_year(monkeypatch):
    def boom():
        raise sqlite3.OperationalError("no such table: transactions")

    monkeypatch.setattr(yp, "first_data_year", boom)
    assert yp.year_choices()[0] == min(yp.DEFAULT_FIRST_YEAR, date.today().year)


def _picker_page() -> None:
    import streamlit as st

    from app.year_picker import year_input

    st.session_state["picked"] = year_input("yp_year", 2099)


def test_year_input_clamps_a_default_outside_the_range(monkeypatch):
    monkeypatch.setattr(yp, "first_data_year", lambda: 2024)
    at = AppTest.from_function(_picker_page).run()
    assert not at.exception, at.exception
    assert at.session_state["picked"] == date.today().year + 1
