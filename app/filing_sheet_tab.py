"""Filing Sheet tab — what to type into each AEAT form, and "Mark filed".

Per quarter: the stored 303 / 130 / 349 snapshots as boxes in form order,
each value in a copyable code block (formatted as the Sede form expects), the
303 credit chain, the 349 operators and the deadlines. "Mark filed" freezes
the snapshot as an immutable FILED version. All logic lives in
``src/filing_sheet.py``; this module only renders.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.year_picker import year_input
from src.database import get_connection
from src.filing_sheet import (
    SOURCES,
    FilingSheet,
    ModelSheet,
    SheetBox,
    aeat_amount,
    build_filing_sheet,
    mark_filed,
    render_markdown,
)
from src.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data access (each opens and closes its own connection; tests replace them)
# ---------------------------------------------------------------------------

def _load_sheet(year: int, quarter: int) -> FilingSheet:
    conn = get_connection()
    try:
        return build_filing_sheet(year, quarter, conn)
    finally:
        conn.close()


def _mark_filed(year: int, quarter: int, model: str, justificante: str, presented_on: date) -> int:
    conn = get_connection()
    try:
        return mark_filed(conn, year, quarter, model, justificante, presented_on)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

def _default_period() -> tuple[int, int]:
    """The last completed quarter — the one being filed."""
    today = date.today()
    q = (today.month - 1) // 3
    return (today.year, q) if q else (today.year - 1, 4)


def _render_period_picker() -> tuple[int, int, bool]:
    year0, q0 = _default_period()
    col_year, col_quarter, col_zero = st.columns([1, 1, 2])
    with col_year:
        year = year_input("fs_year", year0)
    with col_quarter:
        quarter = st.selectbox("Quarter", options=[1, 2, 3, 4], index=q0 - 1,
                               format_func=lambda q: f"Q{q}", key="fs_quarter")
    with col_zero:
        st.write("")
        include_zero = st.checkbox("Show zero boxes", value=False, key="fs_include_zero")
    return year, int(quarter), include_zero


def _render_deadlines(sheet: FilingSheet) -> None:
    rows = []
    for s in sheet.models.values():
        d = s.deadline
        rows.append({
            "Model": s.model,
            "Direct debit until": d.direct_debit_last_day.isoformat() if d.direct_debit_last_day else "— (no payment)",
            "File until": d.last_day.isoformat(),
            "Days left": (d.last_day - date.today()).days,
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    st.caption("Sources: " + "; ".join(SOURCES) + ". Regional/local holidays other than Maundy "
               "Thursday are not modelled — check the AEAT calendar.")


def _render_box_rows(boxes: list[SheetBox]) -> None:
    """One row per box: number, concept, and the value in a copyable code block."""
    for b in boxes:
        c_box, c_label, c_value = st.columns([1, 5, 2])
        c_box.markdown(f"**{b.box}**")
        c_label.markdown(b.label or "—")
        with c_value:
            st.code(b.typed, language=None)


def _render_boxes(s: ModelSheet, include_zero: bool) -> None:
    boxes = s.visible_boxes(include_zero)
    if not boxes:
        st.caption("All boxes are zero.")
        return
    sections = list(dict.fromkeys(b.section for b in boxes))
    for section in sections:
        if section:
            st.markdown(f"**{section}**")
        _render_box_rows([b for b in boxes if b.section == section])


def _render_credit_chain(s: ModelSheet) -> None:
    st.markdown("**Credit chain**")
    st.dataframe(
        pd.DataFrame([{"Box": b.box, "Concept": b.label, "Value (EUR)": b.value} for b in s.credit_chain]),
        width="stretch", hide_index=True,
        column_config={"Value (EUR)": st.column_config.NumberColumn(format="%.2f")},
    )
    st.caption(f"Carried to next quarter's box 110 (87 + 72): €{(s.carry_forward or 0.0):,.2f}"
               + (f" · box 110 source: {s.credit_source}" if s.credit_source else ""))


def _render_operators(s: ModelSheet) -> None:
    st.markdown("**Operators**")
    if not s.operators:
        st.caption("No declarable operators.")
        return
    for o in s.operators:
        c_country, c_vat, c_name, c_key, c_base = st.columns([1, 3, 3, 1, 2])
        c_country.markdown(o.get("country") or "—")
        with c_vat:
            st.code(o.get("vat_id") or "", language=None)
        c_name.markdown(o.get("name") or "—")
        c_key.markdown(o.get("key") or "—")
        with c_base:
            st.code(aeat_amount(float(o.get("base") or 0.0)), language=None)


def _render_mark_filed(sheet: FilingSheet, s: ModelSheet) -> None:
    if s.status == "FILED":
        st.success(f"✅ Filed — version {s.version}, justificante {s.justificante}, presented {s.presented_on}. "
                   "This version is frozen; a recompute stores a new draft version.")
        return
    key = f"fs_mark_{s.model}_{sheet.year}_{sheet.quarter}"
    with st.form(key=key):
        st.markdown(f"**Mark Modelo {s.model} filed** — freezes snapshot version {s.version} as a new "
                    "immutable FILED version.")
        c_j, c_d = st.columns([2, 1])
        with c_j:
            justificante = st.text_input("Justificante (receipt number)", key=f"{key}_justificante")
        with c_d:
            presented_on = st.date_input("Presentation date", value=date.today(), key=f"{key}_date")
        submitted = st.form_submit_button("Mark filed", key=f"{key}_submit")
    if submitted:
        try:
            version = _mark_filed(sheet.year, sheet.quarter, s.model, justificante, presented_on)
        except ValueError as exc:
            st.error(str(exc))
            return
        except Exception as exc:  # DB failure: show it, don't blank the tab
            log.exception("❌ Mark filed failed for Modelo %s %s Q%s", s.model, sheet.year, sheet.quarter)
            st.error(f"Could not mark the return filed: {exc}")
            return
        st.session_state["fs_marked"] = (s.model, version)
        st.rerun()


def _render_model(sheet: FilingSheet, s: ModelSheet, include_zero: bool) -> None:
    st.subheader(s.title)
    if s.status is None:
        st.info("No stored calculation for this quarter — click **Calculate tax** in the Tax Obligations "
                "tab (or run `scripts/close_quarter.py compute`).")
        return
    st.caption(f"Snapshot version {s.version} · {s.status} · computed {s.computed_at}")
    st.markdown(f"**Result:** {s.result}")
    if s.changes_since_filed:
        st.warning(f"Recomputed after filing: this draft differs from filed version {s.filed_version} in "
                   f"{len(s.changes_since_filed)} box(es).")
        st.dataframe(
            pd.DataFrame([{"Box": c.box, "Filed": c.filed, "Current draft": c.current,
                           "Diff": round(c.current - c.filed, 2)} for c in s.changes_since_filed]),
            width="stretch", hide_index=True,
        )
    _render_boxes(s, include_zero)
    if s.model == "303":
        _render_credit_chain(s)
    if s.model == "349":
        _render_operators(s)
    if s.notes:
        st.caption(f"Notes: {s.notes}")
    _render_mark_filed(sheet, s)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("Filing Sheet")
    st.markdown(
        "What to type into each AEAT form for the quarter, from the stored calculation. Each value "
        "is in a copyable block, formatted as the Sede form expects (decimal comma). After filing, "
        "**Mark filed** freezes the figures with the receipt number."
    )
    marked = st.session_state.pop("fs_marked", None)
    if marked:
        model, version = marked
        st.success(f"Modelo {model} marked filed (snapshot version {version}).")
        st.info("Now import the official receipt PDF: **Reconciliation** tab → **📥 Import filed AEAT "
                "receipts** (or `python -m src.filed_returns import <pdf>`), so the filed return can be "
                "reconciled box by box.")

    year, quarter, include_zero = _render_period_picker()
    try:
        sheet = _load_sheet(year, quarter)
    except Exception as exc:  # decode/DB failure: show it, don't blank the tab
        log.exception("❌ Filing sheet failed for %s Q%s", year, quarter)
        st.error(f"Could not build the filing sheet: {exc}")
        return

    st.markdown("#### Deadlines")
    _render_deadlines(sheet)
    st.download_button(
        "⬇️ Download filing sheet (markdown)",
        data=render_markdown(sheet, include_zero),
        file_name=f"filing_sheet_{year}_Q{quarter}.md",
        mime="text/markdown",
        key="fs_download",
    )
    for s in sheet.models.values():
        st.markdown("---")
        _render_model(sheet, s, include_zero)
