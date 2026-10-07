"""AppTest coverage for the Reconciliation tab (app/tax_validation.py).

Regression intent from accounting-quarterly#78 is kept: a fresh clone has no
filed returns, and the tab must render guidance instead of crashing (the old
summary row called `st.columns(0)`, which raises on the pinned streamlit).

Driven through `AppTest` rather than by calling `render()` directly: outside a
script run the `st.*` calls are no-ops that never reach Streamlit's checks.
The cached data-access functions are replaced so no test touches a real DB;
the divergence catalogue path is redirected to a temp file by conftest.
"""
from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _render_without_filed_return() -> None:
    """No filed return for the period; the app still computes two boxes."""
    from app import tax_validation
    from src.reconciliation import Reconciliation, ReconLine

    tax_validation._cached_filed_periods = lambda: []
    tax_validation._cached_logged_audit = lambda *a: []
    tax_validation._cached_reconciliation = lambda model, year, quarter: Reconciliation(
        model=model, year=year, quarter=quarter, filed_found=False,
        lines=[ReconLine("07", "Base 21 %", None, 100.0), ReconLine("09", "Cuota 21 %", None, 21.0)],
    )
    tax_validation.render()


def _render_with_nothing_at_all() -> None:
    """No filed return and no app box: the emptiest possible state."""
    from app import tax_validation
    from src.reconciliation import Reconciliation

    tax_validation._cached_filed_periods = lambda: []
    tax_validation._cached_logged_audit = lambda *a: []
    tax_validation._cached_reconciliation = lambda model, year, quarter: Reconciliation(
        model=model, year=year, quarter=quarter, filed_found=False,
    )
    tax_validation.render()


def _render_with_filed_return() -> None:
    from app import tax_validation
    from src.reconciliation import Reconciliation, ReconLine

    tax_validation._cached_filed_periods = lambda: [("303", 2025, 2)]
    tax_validation._cached_logged_audit = lambda *a: []
    tax_validation._cached_reconciliation = lambda model, year, quarter: Reconciliation(
        model=model, year=year, quarter=quarter, filed_found=True,
        filed_source="db", filed_date="2025-07-18",
        lines=[ReconLine("07", "Base 21 %", 100.0, 100.0), ReconLine("46", "Resultado", 5.0, 9.0),
               ReconLine("110", "", 3.0, None)],
        live_audit=[{"cell": "box_01_base", "label": "Base", "formula": "f", "value": 100.0,
                     "inputs_json": "{}", "computed_at": "live"}],
    )
    tax_validation.render()


def test_tab_renders_guidance_instead_of_crashing_without_a_filed_return():
    at = AppTest.from_function(_render_without_filed_return).run()

    assert not at.exception, f"Reconciliation tab raised: {at.exception}"
    warnings = [w.value for w in at.warning]
    assert warnings, "expected guidance on how to add the filed return"
    assert "Import" in warnings[0] and "validation.yaml" in warnings[0]
    assert at.dataframe, "the app's own figures should still be shown"


def test_summary_cards_are_skipped_without_a_filed_return():
    at = AppTest.from_function(_render_without_filed_return).run()
    assert not at.metric, "summary cards rendered with no filed return to compare"


def test_empty_state_with_no_app_boxes_either():
    at = AppTest.from_function(_render_with_nothing_at_all).run()
    assert not at.exception, f"Reconciliation tab raised: {at.exception}"
    assert any("Nothing to compare" in i.value for i in at.info)


def test_filed_return_renders_summary_table_and_export():
    at = AppTest.from_function(_render_with_filed_return).run()

    assert not at.exception, f"Reconciliation tab raised: {at.exception}"
    labels = {m.label: m.value for m in at.metric}
    assert labels == {"✅ exact": "1", "🟡 catalogued": "0", "🔴 uncatalogued": "1", "⚪ missing": "1"}
    assert at.get("download_button"), "markdown export button missing"
    # Drill-down falls back to the live audit trail when nothing is logged.
    assert any("live computation" in c.value for c in at.caption)


def test_saving_the_catalogue_editor_writes_the_file(isolated_divergence_catalogue):
    at = AppTest.from_function(_render_with_filed_return).run()
    assert not at.exception
    at.button(key="rc_catalogue_save").click().run()
    # Empty editor → an empty (valid) catalogue is written.
    assert not at.exception
    assert isolated_divergence_catalogue.exists()
