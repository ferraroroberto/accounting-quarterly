"""Duplicate Review tab — proposes and applies invoice exclusions (issue #92).

Runs the five detectors in `src.invoice_dedupe` over the current invoice
ledger and lets the user confirm (or reassign) the keeper per group before
writing `excluded`/`excluded_reason`. Applied exclusions are unlocked (see
`src.database.set_invoice_exclusion`), so a plain re-extract or a manual
Invoice Ledger edit can always override them; a row the user has already
locked `excluded` on is never touched by this tab.
"""
from __future__ import annotations

from datetime import date

import pandas as pd
import streamlit as st

from app.flash import flash, show_flash
from app.year_picker import year_input
from src.database import get_invoices, set_invoice_exclusion
from src.invoice_dedupe import (
    DuplicateGroup,
    apply_groups,
    find_duplicate_groups,
    find_numbering_conflicts,
    load_sweep_rows,
)
from src.logger import get_logger

log = get_logger(__name__)

_DETECTOR_LABELS = {
    "file_hash": "Same file (file_hash)",
    "invoice_number": "Same vendor + invoice number",
    "invoice_receipt_pair": "Invoice / receipt pair",
    "email_copy": "Email-folder copy",
    "out_of_period": "Outside the swept quarter",
}


def _row_label(row: dict) -> str:
    who = row.get("vendor_name") or row.get("client_name") or row.get("vendor_nif") or "?"
    total = row.get("total_eur")
    total_txt = f"{total:,.2f}" if isinstance(total, (int, float)) else "-"
    return f"{row.get('invoice_date') or 'no date'} · {who} · {total_txt} · {row.get('filename')}"


def _flash_skipped_groups(result: dict) -> None:
    if result["skipped_groups"]:
        flash("dedupe", "warning", f"Skipped {len(result['skipped_groups'])} group(s) whose kept row is excluded: "
                          "excluding the rest would leave no active invoice. Pick an active row to keep.")


def _render_group(group: DuplicateGroup, by_id: dict[str, dict], idx: int) -> None:
    member_ids = list(group.loser_ids) + ([group.keeper_id] if group.keeper_id else [])
    rows = [by_id[i] for i in member_ids if i in by_id]
    if not rows:
        return

    with st.container(border=True):
        st.markdown(f"**{_DETECTOR_LABELS.get(group.detector, group.detector)}** — {group.note}")
        st.dataframe(
            pd.DataFrame([
                {
                    "filename": r.get("filename"),
                    "invoice_date": r.get("invoice_date"),
                    "vendor/client": r.get("vendor_name") or r.get("client_name"),
                    "invoice_number": r.get("invoice_number"),
                    "total_eur": r.get("total_eur"),
                    "role": "proposed keeper" if r["id"] == group.keeper_id else "proposed exclude",
                }
                for r in rows
            ]),
            width="stretch", hide_index=True, key=f"dedupe_table_{idx}",
        )

        col_keep, col_apply, col_skip = st.columns([3, 1, 1])
        keep_id = group.keeper_id
        if keep_id is not None:
            with col_keep:
                keep_id = st.radio(
                    "Keep", [r["id"] for r in rows], index=[r["id"] for r in rows].index(group.keeper_id),
                    format_func=lambda i: _row_label(by_id[i]), key=f"dedupe_keep_{idx}", horizontal=False,
                )
        with col_apply:
            if st.button("Exclude the rest", key=f"dedupe_apply_{idx}", type="primary"):
                losers = tuple(r["id"] for r in rows if r["id"] != keep_id)
                result = apply_groups([DuplicateGroup(group.detector, group.reason, losers, keep_id, group.note)])
                if result["applied"]:
                    flash("dedupe", "success", f"Excluded {result['applied']} invoice(s) as {group.reason!r}.")
                if result["skipped_locked"]:
                    flash("dedupe", "warning", f"Skipped {result['skipped_locked']} row(s) with a locked `excluded` field.")
                _flash_skipped_groups(result)
                st.rerun()
        with col_skip:
            if st.button("Ignore", key=f"dedupe_skip_{idx}"):
                st.session_state.setdefault("dedupe_ignored", set()).add(
                    (group.detector, group.loser_ids)
                )
                st.rerun()


def _render_recent_auto_exclusions(records: list[dict]) -> None:
    from src.database import parse_locked_fields

    auto = [
        r for r in records
        if r.get("excluded") and r.get("excluded_reason")
        and "excluded" not in parse_locked_fields(r.get("locked_fields"))
    ]
    if not auto:
        return
    with st.expander(f"Auto-excluded rows ({len(auto)}) — undo available", expanded=False):
        for r in auto:
            c1, c2 = st.columns([5, 1])
            with c1:
                st.caption(f"{r['excluded_reason']} — {_row_label(r)}")
            with c2:
                if st.button("Undo", key=f"dedupe_undo_{r['id']}"):
                    set_invoice_exclusion(r["id"], False, None)
                    flash("dedupe", "info", f"Un-excluded {r.get('filename')}.")
                    st.rerun()


def render() -> None:
    """Render the Duplicate Review tab."""
    st.subheader("Duplicate Review — exclude duplicates, receipts, out-of-period invoices")
    st.caption(
        "Scans the invoice ledger for candidate duplicate/receipt/out-of-period groups. "
        "Nothing is excluded until you confirm a group below. Excluded rows are ignored by every tax "
        "computation; a row already locked in the Invoice Ledger tab is never touched here."
    )
    show_flash("dedupe")

    all_invoices = get_invoices()
    if not all_invoices:
        st.info("No invoices extracted yet — use the Invoice OCR tab first.")
        return
    by_id = {r["id"]: r for r in all_invoices}

    c_dir, c_scope, c_year, c_quarter = st.columns([2, 2, 1, 1])
    with c_dir:
        direction = st.selectbox(
            "Direction", ["Both", "in", "out"], key="dedupe_direction",
            format_func=lambda d: {"Both": "Both", "in": "Expenses (in)", "out": "Income (out)"}[d],
        )
    with c_scope:
        scope_period = st.checkbox(
            "Also check out-of-period against a swept quarter", value=False, key="dedupe_scope_period",
        )
    today = date.today()
    with c_year:
        year = year_input("dedupe_year", today.year, disabled=not scope_period)
    with c_quarter:
        quarter = st.selectbox(
            "Quarter", [1, 2, 3, 4], index=(today.month - 1) // 3, key="dedupe_quarter",
            disabled=not scope_period,
        )

    if st.button("Scan for duplicates", type="primary", key="dedupe_scan"):
        rows = all_invoices if direction == "Both" else [r for r in all_invoices if r["direction"] == direction]
        sweep_rows = None
        if scope_period:
            sweep_rows = load_sweep_rows(int(year), int(quarter))
            if direction != "Both":
                sweep_rows = [r for r in sweep_rows if r["direction"] == direction]
        groups = find_duplicate_groups(
            rows, sweep_rows=sweep_rows,
            year=int(year) if scope_period else None,
            quarter=int(quarter) if scope_period else None,
        )
        ignored = st.session_state.get("dedupe_ignored", set())
        st.session_state["dedupe_numbering"] = [c.note for c in find_numbering_conflicts(rows)]
        st.session_state["dedupe_groups"] = [
            g for g in groups if (g.detector, g.loser_ids) not in ignored
        ]

    for note in st.session_state.get("dedupe_numbering", []):
        st.warning(f"Numbering, not a duplicate (nothing excluded): {note}")
    groups: list[DuplicateGroup] = st.session_state.get("dedupe_groups", [])
    if groups:
        m1, m2 = st.columns(2)
        m1.metric("Candidate groups", len(groups))
        m2.metric("Candidate rows to exclude", sum(len(g.loser_ids) for g in groups))

        if st.button("Apply all proposed exclusions", key="dedupe_apply_all"):
            result = apply_groups(groups)
            flash("dedupe", "success",
                   f"Applied {result['applied']} exclusion(s); skipped {result['skipped_locked']} "
                   "locked row(s).")
            _flash_skipped_groups(result)
            st.session_state["dedupe_groups"] = []
            st.rerun()

        for idx, group in enumerate(groups):
            _render_group(group, by_id, idx)
    else:
        st.info("Run a scan to see candidate groups.")

    st.markdown("---")
    _render_recent_auto_exclusions(all_invoices)
