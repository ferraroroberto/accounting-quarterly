"""AppTest coverage for the Filing Sheet tab (app/filing_sheet_tab.py).

The data-access functions are replaced so no test touches a real DB.
"""
from __future__ import annotations

import sys
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _render_sheet() -> None:
    from app import filing_sheet_tab
    from src.filing_sheet import FilingSheet, ModelSheet, SheetBox, filing_deadline

    def _sheet(year: int, quarter: int) -> FilingSheet:
        m303 = ModelSheet(
            "303", filing_deadline("303", year, quarter), status="COMPUTED", version=1,
            computed_at="2026-07-01T10:00:00", result="To pay (box 71): €21.00",
            boxes=[SheetBox("07", "Base 21 %", 100.0, "Page 1 — IVA devengado"),
                   SheetBox("09", "Cuota 21 %", 21.0, "Page 1 — IVA devengado"),
                   SheetBox("71", "Resultado", 21.0, "Page 3 — Resultado")],
            credit_chain=[SheetBox("110", "Pendiente", 0.0), SheetBox("71", "Resultado", 21.0)],
            carry_forward=0.0,
        )
        m130 = ModelSheet("130", filing_deadline("130", year, quarter))
        m349 = ModelSheet(
            "349", filing_deadline("349", year, quarter), status="FILED", version=2,
            justificante="J349", presented_on="2026-07-15", result="Informative",
            boxes=[SheetBox("01", "Operadores", 1.0, count=True), SheetBox("02", "Importe", 300.0)],
            operators=[{"country": "DE", "vat_id": "999999999", "name": "Example GmbH", "key": "S", "base": 300.0}],
        )
        return FilingSheet(year, quarter, {"303": m303, "130": m130, "349": m349})

    calls: list[tuple] = []

    def _mark(year, quarter, model, justificante, presented_on):
        calls.append((year, quarter, model, justificante, presented_on))
        return 2

    filing_sheet_tab._load_sheet = _sheet
    filing_sheet_tab._mark_filed = _mark
    filing_sheet_tab.render()


def test_tab_renders_boxes_as_copyable_values():
    at = AppTest.from_function(_render_sheet).run()
    assert not at.exception, f"Filing Sheet tab raised: {at.exception}"
    codes = [c.value for c in at.code]
    assert {"100,00", "21,00", "1", "300,00", "999999999"} <= set(codes)
    assert any("No stored calculation" in i.value for i in at.info)       # 130 has no snapshot
    assert any("Filed — version 2" in s.value for s in at.success)       # 349 already filed


def test_mark_filed_submits_and_prompts_the_receipt_import():
    at = AppTest.from_function(_render_sheet).run()
    key = f"fs_mark_303_{at.number_input(key='fs_year').value}_{at.selectbox(key='fs_quarter').value}"
    at.text_input(key=f"{key}_justificante").input("3031234567890")
    at.button(key=f"{key}_submit").click().run()
    assert not at.exception, f"Filing Sheet tab raised: {at.exception}"
    assert any("marked filed" in s.value for s in at.success)
    assert any("Import filed AEAT receipts" in i.value for i in at.info)
