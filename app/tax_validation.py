"""Reconciliation tab — filed AEAT returns vs the app's figures, box by box.

Pick a model and period; every box of the filed return is lined up against
the app's value with a status (✅ exact, 🟡 catalogued divergence, 🔴
unexplained, ⚪ one side missing). The divergence catalogue
(``divergences.json``) is edited at the bottom of the tab. All logic lives in
``src/reconciliation.py``; this module only renders.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

import pandas as pd
import streamlit as st

from app.tax_audit import _render_audit_table  # same per-cell drill-down as the Tax Audit tab
from src.database import get_connection
from src.filed_returns import import_pdf
from src.logger import get_logger
from src.reconciliation import (
    CATALOGUE_FIELDS,
    CATALOGUE_PATH,
    CATEGORIES,
    MODELS,
    RULES,
    STATUS_EXACT,
    STATUS_ICONS,
    STATUSES,
    CatalogueError,
    Divergence,
    Reconciliation,
    apply_catalogue,
    audit_entries_for_box,
    list_filed_periods,
    load_catalogue,
    load_logged_audit,
    reconcile,
    save_catalogue,
    to_markdown,
)

log = get_logger(__name__)

_MODEL_LABELS = {
    "303": "Modelo 303 — IVA trimestral",
    "130": "Modelo 130 — pago fraccionado IRPF",
    "349": "Modelo 349 — operaciones intracomunitarias",
    "390": "Modelo 390 — resumen anual IVA",
}
_NUMERIC_CATALOGUE_FIELDS = ("year", "quarter", "expected_delta", "tolerance")


# ---------------------------------------------------------------------------
# Cached data access (each opens and closes its own connection so the result
# is picklable; tests replace these functions)
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300, show_spinner=False)
def _cached_filed_periods() -> list[tuple[str, int, Optional[int]]]:
    conn = get_connection()
    try:
        return list_filed_periods(conn)
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def _cached_reconciliation(model: str, year: int, quarter: Optional[int]) -> Reconciliation:
    """Filed-vs-app lines without catalogue statuses (applied at render time)."""
    conn = get_connection()
    try:
        return reconcile(model, year, quarter, conn)
    finally:
        conn.close()


@st.cache_data(ttl=300, show_spinner=False)
def _cached_logged_audit(model: str, year: int, quarter: int) -> list[dict]:
    conn = get_connection()
    try:
        return load_logged_audit(conn, model, year, quarter)
    finally:
        conn.close()


def _clear_caches() -> None:
    for fn in (_cached_filed_periods, _cached_reconciliation, _cached_logged_audit):
        clear = getattr(fn, "clear", None)
        if clear:
            clear()


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------

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
            _clear_caches()


def _render_period_picker(periods: list[tuple[str, int, Optional[int]]]) -> tuple[str, int, Optional[int]]:
    """Model / year / quarter selectors; defaults to the latest filed period."""
    latest = max(
        (p for p in periods if p[2] is not None),
        key=lambda p: (p[1], p[2]), default=None,
    )
    today = date.today()
    default_year = latest[1] if latest else today.year
    default_quarter = latest[2] if latest else (today.month - 1) // 3 + 1

    col_model, col_year, col_quarter = st.columns([2, 1, 1])
    with col_model:
        model = st.selectbox(
            "Model", options=list(MODELS), format_func=lambda m: _MODEL_LABELS[m], key="rc_model",
        )
    with col_year:
        year = int(st.number_input(
            "Year", min_value=2020, max_value=2035, value=int(default_year), step=1, key="rc_year",
        ))
    with col_quarter:
        quarter: Optional[int] = st.selectbox(
            "Quarter", options=[1, 2, 3, 4], index=int(default_quarter) - 1,
            format_func=lambda q: f"Q{q}", key="rc_quarter", disabled=model == "390",
        )
    if model == "390":
        quarter = None

    filed = [p for p in periods if p[0] == model]
    if filed:
        st.caption(
            "Filed periods on record for this model: "
            + ", ".join(f"{y} Q{q}" if q else f"{y} annual" for _, y, q in filed)
        )
    return model, year, quarter


def _load_catalogue_safe() -> tuple[list[Divergence], Optional[str]]:
    try:
        return load_catalogue(), None
    except CatalogueError as exc:
        return [], str(exc)


def _lines_df(rec: Reconciliation, hide_exact: bool) -> pd.DataFrame:
    rows = []
    for ln in rec.lines:
        if hide_exact and ln.status == STATUS_EXACT:
            continue
        rows.append({
            "Box": ln.box,
            "Description": ln.description,
            "Filed": ln.filed,
            "App": ln.app,
            "Diff (app − filed)": ln.diff,
            "Status": f"{STATUS_ICONS[ln.status]} {ln.status}",
            "Tag": ln.tag,
            "Explanation": ln.explanation,
        })
    return pd.DataFrame(rows, columns=["Box", "Description", "Filed", "App", "Diff (app − filed)",
                                       "Status", "Tag", "Explanation"])


def _render_summary(rec: Reconciliation) -> None:
    counts = rec.counts()
    cols = st.columns(len(STATUSES))
    for col, status in zip(cols, STATUSES):
        with col:
            st.metric(f"{STATUS_ICONS[status]} {status}", counts[status])


def _render_table(rec: Reconciliation) -> None:
    hide_exact = st.checkbox("Hide exact matches", value=False, key="rc_hide_exact")
    money = st.column_config.NumberColumn(format="%.2f")
    st.dataframe(
        _lines_df(rec, hide_exact),
        width="stretch",
        hide_index=True,
        column_config={"Filed": money, "App": money, "Diff (app − filed)": money},
    )
    caveats = [ln for ln in rec.lines if ln.note]
    if caveats:
        with st.expander(f"ℹ️ Legacy engine mapping caveats ({len(caveats)})", expanded=False):
            st.caption(
                "The app's engine still uses its pre-AEAT field names; these boxes are mapped "
                "from them and do not carry the form's exact meaning yet (#97/#98/#99)."
            )
            for ln in caveats:
                st.markdown(f"- **{ln.box}** — {ln.note}")


def _render_drilldown(rec: Reconciliation) -> None:
    st.markdown("#### Drill-down: audit records behind an app value")
    boxes = [ln for ln in rec.lines if ln.app is not None]
    if not boxes:
        st.caption("The app computes no box for this period.")
        return
    labels = {ln.box: f"{ln.box} — {ln.description}" if ln.description else ln.box for ln in boxes}
    box = st.selectbox("Box", options=list(labels), format_func=labels.get, key="rc_drill_box")

    logged = _cached_logged_audit(rec.model, rec.year, rec.quarter) if rec.quarter is not None else []
    if rec.quarter is None:
        # Modelo 390: computed live from the four quarterly 303s, never logged.
        source = rec.live_audit
        st.caption("Live Modelo 390 computation (built from the year's four 303 results — drill into "
                   "a 303 quarter for the invoice-level records).")
    elif logged:
        source = logged
        st.caption(
            f"From `tax_audit_log`, run `{logged[0]['computed_at']}`. It can be older than the "
            "live figures above — re-run **Calculate tax** in Tax Obligations to refresh it."
        )
    else:
        source = rec.live_audit
        st.caption(
            "No stored `tax_audit_log` run for this period — showing the live computation's "
            "audit trail (click **Calculate tax** in Tax Obligations to persist it)."
        )
    entries = audit_entries_for_box(source, rec.model, box, rec.engine)
    if not entries:
        st.info("No audit cell is linked to this box.")
        return
    _render_audit_table(entries)


def _render_export(rec: Reconciliation) -> None:
    md = to_markdown(rec)
    q = f"Q{rec.quarter}" if rec.quarter else "annual"
    st.download_button(
        "⬇️ Download table as markdown",
        data=md,
        file_name=f"reconciliation_{rec.model}_{rec.year}_{q}.md",
        mime="text/markdown",
        key="rc_md_download",
    )
    with st.expander("Markdown preview", expanded=False):
        st.code(md, language="markdown")


def _catalogue_df(entries: list[Divergence]) -> pd.DataFrame:
    df = pd.DataFrame([e.to_dict() for e in entries], columns=list(CATALOGUE_FIELDS))
    for col in _NUMERIC_CATALOGUE_FIELDS:
        df[col] = pd.to_numeric(df[col], errors="coerce").astype("float64")
    for col in (c for c in CATALOGUE_FIELDS if c not in _NUMERIC_CATALOGUE_FIELDS):
        df[col] = df[col].astype("object")
    return df


def _render_catalogue_editor(catalogue: list[Divergence], load_error: Optional[str]) -> None:
    with st.expander(f"📒 Divergence catalogue — `{CATALOGUE_PATH.name}` ({len(catalogue)} entries)",
                     expanded=False):
        saved = st.session_state.pop("rc_catalogue_saved", None)
        if saved is not None:
            st.success(f"Saved {saved} catalogue entr{'y' if saved == 1 else 'ies'}.")
        if load_error:
            st.error(f"`{CATALOGUE_PATH.name}` could not be loaded — fix or re-save it:\n\n{load_error}")
        st.caption(
            "One row per explained difference. `year` / `quarter` empty = any. Give **either** "
            "`expected_delta` (app − filed, matched within `tolerance`) **or** a `rule`: "
            "`app_gte_filed`, `app_lte_filed`, `any`. For a 349 operator use the table's box "
            "key, e.g. `op:IE1234567X:I`. The file is git-ignored; see `divergences.json.example`."
        )
        edited = st.data_editor(
            _catalogue_df(catalogue),
            num_rows="dynamic",
            width="stretch",
            hide_index=True,
            key="rc_catalogue_editor",
            column_config={
                "model": st.column_config.SelectboxColumn("model", options=list(MODELS)),
                "year": st.column_config.NumberColumn("year", step=1, format="%d"),
                "quarter": st.column_config.NumberColumn("quarter", min_value=1, max_value=4, step=1, format="%d"),
                "box": st.column_config.TextColumn("box"),
                "expected_delta": st.column_config.NumberColumn("expected_delta", format="%.2f"),
                "rule": st.column_config.SelectboxColumn("rule", options=list(RULES)),
                "tolerance": st.column_config.NumberColumn("tolerance", min_value=0.0, format="%.2f"),
                "category": st.column_config.SelectboxColumn("category", options=list(CATEGORIES)),
                "explanation": st.column_config.TextColumn("explanation", width="large"),
            },
        )
        if st.button("💾 Save catalogue", key="rc_catalogue_save"):
            records = [
                r for r in edited.to_dict("records")
                if any(not pd.isna(v) and v != "" for v in r.values())
            ]
            try:
                parsed = save_catalogue(records)
            except CatalogueError as exc:
                st.error(f"Not saved — fix these rows:\n\n{exc}")
            else:
                st.session_state["rc_catalogue_saved"] = len(parsed)
                st.rerun()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def render() -> None:
    st.title("Reconciliation")
    st.markdown(
        "Every box of a **filed AEAT return** next to the value the **app** computes. "
        "✅ exact (±€0.01) · 🟡 difference explained by the divergence catalogue · "
        "🔴 unexplained difference · ⚪ one side missing. Diff = app − filed."
    )
    _, col_btn = st.columns([5, 1])
    with col_btn:
        if st.button("↺ Refresh", help="Clear cached results and recompute", key="tv_refresh"):
            _clear_caches()

    _render_receipt_import()

    periods = _cached_filed_periods()
    model, year, quarter = _render_period_picker(periods)
    catalogue, catalogue_error = _load_catalogue_safe()
    if catalogue_error:
        st.error(f"Divergence catalogue ignored — `{CATALOGUE_PATH.name}` is invalid (see the editor below).")

    try:
        with st.spinner("Reconciling…"):
            rec = _cached_reconciliation(model, year, quarter)
    except Exception as exc:  # engine/DB failure: show it, don't blank the tab
        log.exception("❌ Reconciliation failed for Modelo %s %s Q%s", model, year, quarter)
        st.error(f"Could not compute the reconciliation: {exc}")
        _render_catalogue_editor(catalogue, catalogue_error)
        return
    rec = apply_catalogue(rec, catalogue)

    period = f"{year} Q{quarter}" if quarter else f"{year} (annual)"
    st.subheader(f"{_MODEL_LABELS[model]} — {period}")
    if rec.filed_found:
        source = {"db": "imported AEAT receipt", "yaml": "`tmp/validation/validation.yaml`"}
        st.caption(
            f"Filed {rec.filed_date or '—'} · source: {source.get(rec.filed_source, rec.filed_source)} "
            f"· app engine: {rec.engine}"
        )
        _render_summary(rec)
    else:
        st.warning(
            f"No filed Modelo {model} return for {period}. Import the AEAT receipt PDF with "
            "**📥 Import filed AEAT receipts** above (or add the filed values to "
            "`tmp/validation/validation.yaml`), then hit ↺ Refresh. The app's own figures are "
            "shown below with ⚪ on the filed side."
        )

    if rec.lines:
        _render_table(rec)
        _render_drilldown(rec)
        _render_export(rec)
    else:
        st.info("Nothing to compare: the app computes no box and there is no filed return.")

    st.markdown("---")
    _render_catalogue_editor(catalogue, catalogue_error)
