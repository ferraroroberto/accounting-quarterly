"""Tax Validation tab — compare gestor-filed AEAT figures against DB-computed values."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import streamlit as st
import pandas as pd

from src.database import get_connection
from src.filed_returns import import_pdf
from src.tax_validator import (
    ModelValidationResult,
    ValidationLine,
    run_all_validations,
)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_validations() -> list[ModelValidationResult]:
    """Run all validations once and cache the results for 5 minutes.

    Creates and closes its own DB connection so the result is picklable
    (sqlite3.Connection objects cannot be cached by Streamlit).
    """
    conn = get_connection()
    try:
        return run_all_validations(conn)
    finally:
        conn.close()


def _render_receipt_import() -> None:
    """Upload filed AEAT receipt PDFs (303/130/349/390) into the database."""
    with st.expander("📥 Import filed AEAT receipts (PDF)", expanded=False):
        st.caption(
            "Upload the official receipts downloaded from the AEAT Sede (Modelo 303, 130, "
            "349, 390). Re-importing the same receipt is a no-op; a later receipt for the "
            "same period (rectificativa) replaces the earlier one. For a whole folder use "
            "`python -m src.filed_returns import <folder>`."
        )
        files = st.file_uploader(
            "Receipt PDFs", type=["pdf"], accept_multiple_files=True, key="tv_receipt_upload"
        )
        if files and st.button("Import receipts", key="tv_receipt_import"):
            conn = get_connection()
            try:
                results = [import_pdf(conn, f, f.name) for f in files]
            finally:
                conn.close()
            for r in results:
                if r.filed is None:
                    st.warning(f"{r.source_file}: skipped ({r.detail})")
                else:
                    f = r.filed
                    st.success(
                        f"{r.source_file}: Modelo {f.model} {f.year} {f.period} — "
                        f"{r.status} ({len(f.boxes)} boxes"
                        + (f", {len(f.operators)} operators" if f.model == "349" else "")
                        + ")"
                    )
            _cached_validations.clear()

_STATUS_ICONS = {
    "OK":      "✅",
    "DB_HIGH": "⬆️",
    "DB_LOW":  "⬇️",
    "N/A":     "➖",
}
_STATUS_COLOURS = {
    "OK":      "#2d7a2d",
    "DB_HIGH": "#b35900",
    "DB_LOW":  "#b30000",
    "N/A":     "#888888",
}


def _fmt(v: float | None) -> str:
    if v is None:
        return "—"
    sign = "-" if v < 0 else ""
    return f"{sign}€{abs(v):,.2f}"


def _diff_badge(line: ValidationLine) -> str:
    d = line.diff
    if d is None:
        return "—"
    return f"{'+' if d >= 0 else '-'}€{abs(d):,.2f}"


def _lines_to_df(lines: list[ValidationLine]) -> pd.DataFrame:
    rows = []
    for ln in lines:
        rows.append({
            "Casilla": ln.casilla,
            "Description": ln.description,
            "Filed (gestor)": _fmt(ln.filed),
            "DB computed": _fmt(ln.computed),
            "Diff (DB − filed)": _diff_badge(ln),
            "Status": f"{_STATUS_ICONS.get(ln.status, '')} {ln.status}",
            "_status": ln.status,
        })
    return pd.DataFrame(rows)


def _status_summary(result: ModelValidationResult) -> str:
    if not result.lines:
        return "No data"
    total = len([ln for ln in result.lines if ln.status != "N/A"])
    ok = result.ok_count
    diff = result.diff_count
    if diff == 0:
        return f"✅ All {ok} lines match"
    return f"⚠️ {diff} difference(s) out of {total} lines"


def _render_model_section(result: ModelValidationResult) -> None:
    col_title, col_status = st.columns([3, 2])
    with col_title:
        st.subheader(f"Modelo {result.model} — {result.period}")
        st.caption(f"Gestor filed: {result.filed_date}")
    with col_status:
        st.markdown("**Summary:**")
        st.markdown(_status_summary(result))

    df = _lines_to_df(result.lines)

    display_df = df.drop(columns=["_status"])

    # Colour the Status and Diff columns via per-cell map
    def highlight_status(val: str) -> str:
        for key, colour in _STATUS_COLOURS.items():
            if key in val:
                return f"color: {colour}; font-weight: bold"
        return ""

    def highlight_diff(val: str) -> str:
        if val.startswith("+") or (val.startswith("€") and not val.startswith("-")):
            return f"color: {_STATUS_COLOURS['DB_HIGH']}"
        if val.startswith("-€"):
            return f"color: {_STATUS_COLOURS['DB_LOW']}"
        return ""

    styled2 = (
        display_df.style
        .map(highlight_status, subset=["Status"])
        .map(highlight_diff, subset=["Diff (DB − filed)"])
    )

    st.dataframe(styled2, width="stretch", hide_index=True)

    # Highlight key differences in natural language
    diffs = [ln for ln in result.lines if not ln.match and ln.status != "N/A" and ln.diff is not None]
    if diffs:
        with st.expander(f"⚠️ {len(diffs)} difference(s) — details", expanded=False):
            for ln in diffs:
                direction = "DB higher" if (ln.diff or 0) > 0 else "DB lower"
                st.markdown(
                    f"- **{ln.description}** (box {ln.casilla}): "
                    f"filed={_fmt(ln.filed)}, computed={_fmt(ln.computed)}, "
                    f"diff={_diff_badge(ln)} → *{direction}*"
                )


def render() -> None:
    st.title("Tax Validation")
    st.markdown(
        "Compares **gestor-filed AEAT declarations** against values our system computes "
        "from the database. Differences may indicate missing invoices, unclassified transactions, "
        "missing expense entries, or data not yet loaded into the DB."
    )

    col_info, col_btn = st.columns([5, 1])
    with col_btn:
        if st.button("↺ Refresh", help="Clear cached results and re-run all validations", key="tv_refresh"):
            _cached_validations.clear()

    _render_receipt_import()

    with st.spinner("Running validations…"):
        results = _cached_validations()

    if not results:
        st.warning(
            "No filed declarations to compare against. Import the AEAT receipt PDFs above, "
            "or add the gestor-filed values to `tmp/validation/validation.yaml` (see the "
            "README's **Tax Validation** section), then hit ↺ Refresh."
        )
        return

    by_source: dict[str, list[str]] = {}
    for r in results:
        by_source.setdefault(r.source, []).append(f"**{r.period}** (M{r.model})")
    labels = {"db": "imported AEAT receipts", "yaml": "`tmp/validation/validation.yaml`"}
    st.info(
        "Reference data loaded from "
        + "; ".join(f"{labels.get(src, src)}: {', '.join(p)}" for src, p in by_source.items())
        + "."
    )

    # Top-level summary cards
    st.markdown("### Summary")
    cols = st.columns(len(results))
    for col, result in zip(cols, results):
        with col:
            icon = "✅" if not result.has_differences else "⚠️"
            st.metric(
                label=f"Modelo {result.model} — {result.period}",
                value=f"{icon} {result.diff_count} diff(s)",
                delta=f"{result.ok_count} lines match" if result.ok_count else None,
            )

    st.markdown("---")

    # Per-model detailed sections
    for result in results:
        _render_model_section(result)
        st.markdown("---")

    # Key gaps explanation
    with st.expander("📌 Understanding the differences", expanded=False):
        st.markdown(
            """
**Why do computed values differ from filed values?**

| Gap | Likely reason |
|-----|--------------|
| Income lower in DB (M130/390) | Not all income is via Stripe (offline invoices, bank transfers) |
| Expenses = 0 in DB | `quarterly_tax_entries` not yet populated with deductible expenses |
| Retenciones = 0 in DB | No `RETENCIONES_SOPORTADAS` entries added |
| Modelo 349 mismatch | Gestor's 349 captures **EU service purchases** (Squarespace, Stripe fees); our 349 captures **EU B2B sales** |
| IVA devengado lower in DB | Only Stripe-sourced income classified; invoices not yet reconciled |
| IVA soportado = 0 in DB | No `IVA_SOPORTADO` entries in quarterly_tax_entries |

**Diff sign convention:** `DB − filed`. Positive (⬆️) means our system computes a higher value; negative (⬇️) means we compute less than the gestor filed.
            """
        )
