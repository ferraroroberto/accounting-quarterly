"""The Welcome tab's guide is the single source of the dashboard's tab names."""
from __future__ import annotations

from app.welcome import TAB_GUIDE, TAB_NAMES


def test_tab_names_are_welcome_plus_the_guide_in_order():
    assert TAB_NAMES[0] == "Welcome"
    assert TAB_NAMES[1:] == [name for name, _ in TAB_GUIDE]
    assert len(set(TAB_NAMES)) == len(TAB_NAMES)


def test_guide_covers_current_tabs_and_not_the_retired_one():
    names = set(TAB_NAMES)
    assert {"Invoice Ledger", "Duplicate Review", "Vendors", "Fixed Assets", "Filing Sheet",
            "Reconciliation"} <= names
    assert "Tax Validation" not in names
    assert all(desc.strip() for _, desc in TAB_GUIDE)


def test_streamlit_app_builds_its_tabs_from_the_guide():
    from pathlib import Path

    src = (Path(__file__).parent.parent / "app" / "streamlit_app.py").read_text(encoding="utf-8")
    assert "st.tabs(TAB_NAMES)" in src
    assert src.count("= st.tabs(") == 1
