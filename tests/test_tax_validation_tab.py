"""Regression coverage for the Tax Validation tab's empty-reference-data path.

`tmp/validation/validation.yaml` is git-ignored (README, "Tax Validation"), so
every fresh clone opens this tab with zero filed declarations loaded. Before
accounting-quarterly#78 the summary-card row called `st.columns(0)`, which
raises `StreamlitInvalidColumnSpecError` on the pinned streamlit 1.56 — the tab
crashed on exactly the state a new machine lands in.

Driven through `AppTest` rather than by calling `render()` directly: outside a
script run the `st.*` calls are no-ops that never reach the column-spec check,
so a direct call could not have caught the original crash.
"""
from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _render_with_no_filings() -> None:
    """Script body for AppTest: render the tab with no reference data loaded."""
    from app import tax_validation

    tax_validation._cached_validations = lambda: []
    tax_validation.render()


def test_tab_renders_guidance_instead_of_crashing_without_reference_data():
    at = AppTest.from_function(_render_with_no_filings).run()

    assert not at.exception, f"Tax Validation tab raised: {at.exception}"
    warnings = [w.value for w in at.warning]
    assert warnings, "expected a warning telling the user how to add filed values"
    assert "validation.yaml" in warnings[0]


def test_summary_section_is_skipped_when_there_is_nothing_to_compare():
    at = AppTest.from_function(_render_with_no_filings).run()

    # The early return must land before the summary cards — no "Summary"
    # heading, and no metrics, when there are no filings to show.
    assert not at.metric, "summary cards rendered with no validation results"
    assert not any("Summary" in md.value for md in at.markdown)
