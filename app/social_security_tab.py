"""Social Security (Seguridad Social) tab — import bank export and view cuotas."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent

from src.config import load_config
from src.logger import get_logger
from src.social_security import (
    DEFAULT_CONCEPT_PATTERNS,
    add_manual_ss_entry,
    clear_ss_payments,
    delete_ss_payment,
    detect_header_row,
    get_ss_payments,
    load_bank_export,
    read_raw_preview,
    upsert_ss_payments,
)

log = get_logger(__name__)


def _resolve(path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / path


def render() -> None:
    st.header("Seguridad Social — Cuotas de autónomo")
    st.markdown(
        "Import Social Security (Seguridad Social) monthly quota payments from a bank "
        "account export. Imported amounts are automatically included as deductible expenses "
        "in **Modelo 130** (box 02 — gastos deducibles YTD)."
    )

    cfg = load_config()
    ss_cfg = cfg.get("social_security", {})

    # -------------------------------------------------------------------------
    # Import section
    # -------------------------------------------------------------------------
    st.subheader("Import from bank export")

    default_file = ss_cfg.get("bank_export_file", "tmp/social_security_bank_export.xlsx")
    default_date_col = ss_cfg.get("date_column", "Fecha")
    default_amount_col = ss_cfg.get("amount_column", "Importe")
    default_desc_col = ss_cfg.get("description_column", "")
    default_concept_col = ss_cfg.get("concept_column", "")
    default_concept_patterns = ss_cfg.get("concept_patterns", DEFAULT_CONCEPT_PATTERNS)
    default_sheet = ss_cfg.get("sheet_name", 0)
    default_skiprows = ss_cfg.get("skiprows")

    col1, col2 = st.columns(2)
    with col1:
        file_path_str = st.text_input(
            "Bank export file path (.xlsx / .xls / .csv)",
            value=default_file,
            key="ss_file_path",
            help="Path relative to the project root, or absolute. "
                 "Configure the default in config.json → social_security.bank_export_file",
        )
        date_column = st.text_input("Date column name", value=default_date_col, key="ss_date_col")
        amount_column = st.text_input("Amount column name", value=default_amount_col, key="ss_amount_col")
        description_column = st.text_input(
            "Description column name (optional)", value=default_desc_col, key="ss_desc_col"
        )
    with col2:
        sheet_name_input = st.text_input(
            "Sheet name or index (0-based)",
            value=str(default_sheet),
            key="ss_sheet_name",
            help="Use a sheet name like 'Sheet1' or a zero-based index like '0'.",
        )
        auto_detect_header = st.checkbox(
            "Auto-detect header row",
            value=default_skiprows is None,
            key="ss_auto_header",
            help="Scans the first rows for the one containing both the date and amount "
                 "column names — handles exports with title rows above the header. "
                 "Uncheck to specify the header row index manually.",
        )
        skiprows: Optional[int] = None
        if not auto_detect_header:
            skiprows = st.number_input(
                "Header row index (0-based)",
                min_value=0,
                value=int(default_skiprows) if default_skiprows is not None else 0,
                step=1,
                key="ss_skiprows",
            )
        concept_column = st.text_input(
            "Concept/movement column name (optional filter)",
            value=default_concept_col,
            key="ss_concept_col",
            help="When set, only rows whose value in this column matches one of the "
                 "concept patterns below are imported (e.g. TGSS contribution rows).",
        )

    concept_patterns_str = st.text_input(
        "Concept match patterns (comma-separated, only used with the concept column above)",
        value=", ".join(default_concept_patterns),
        key="ss_concept_patterns",
    )

    # Resolve sheet name to int if numeric
    try:
        sheet_name: int | str = int(sheet_name_input)
    except ValueError:
        sheet_name = sheet_name_input.strip()

    desc_col_clean = description_column.strip() or None
    concept_col_clean = concept_column.strip() or None
    concept_patterns = [p.strip() for p in concept_patterns_str.split(",") if p.strip()]

    file_path = _resolve(file_path_str)

    # Preview
    if st.button("Preview file columns", key="ss_preview"):
        if not file_path.exists():
            st.error(f"File not found: `{file_path}`")
        else:
            try:
                df_scan = read_raw_preview(file_path, sheet_name=sheet_name, nrows=25)
                st.markdown("**Raw rows (no header applied):**")
                st.dataframe(df_scan.head(10), width="stretch")
                try:
                    header_idx = skiprows if skiprows is not None else detect_header_row(
                        df_scan, date_column, amount_column
                    )
                    st.success(f"Header row detected at index **{header_idx}**.")
                except ValueError as exc:
                    st.warning(str(exc))
            except Exception as exc:
                st.error(f"Could not read file: {exc}")

    st.markdown("---")

    col_imp, col_clear = st.columns([2, 1])
    with col_imp:
        if st.button("Import from file", type="primary", key="ss_import"):
            if not file_path.exists():
                st.error(f"File not found: `{file_path}`")
            else:
                try:
                    rows = load_bank_export(
                        file_path=file_path,
                        date_column=date_column,
                        amount_column=amount_column,
                        description_column=desc_col_clean,
                        concept_column=concept_col_clean,
                        concept_patterns=concept_patterns or None,
                        sheet_name=sheet_name,
                        skiprows=skiprows,
                    )
                    if not rows:
                        st.warning("No valid rows found in the file. Check the column names and date/amount format.")
                    else:
                        inserted, skipped = upsert_ss_payments(rows, source_file=str(file_path))
                        st.success(
                            f"Import complete: **{inserted} new rows** imported, "
                            f"{skipped} duplicate(s) skipped."
                        )
                        st.rerun()
                except Exception as exc:
                    st.error(f"Import failed: {exc}")
                    log.exception("SS import error")

    with col_clear:
        if st.button("Clear all SS payments", type="secondary", key="ss_clear"):
            clear_ss_payments()
            st.success("All Social Security payment rows cleared.")
            st.rerun()

    # -------------------------------------------------------------------------
    # Manual entry fallback
    # -------------------------------------------------------------------------
    with st.expander("Add a manual entry (month not covered by a bank export)"):
        st.markdown(
            "Use this for a month missing from the bank export, or to record a refund "
            "(e.g. *pluriactividad* excess-contribution refund) as a **negative** amount."
        )
        col_m1, col_m2, col_m3 = st.columns(3)
        with col_m1:
            manual_date = st.date_input("Payment date", value=date.today(), key="ss_manual_date")
        with col_m2:
            manual_amount = st.number_input(
                "Amount (€) — positive for a contribution, negative for a refund",
                value=0.0,
                step=0.01,
                format="%.2f",
                key="ss_manual_amount",
            )
        with col_m3:
            manual_description = st.text_input(
                "Description (optional)", value="", key="ss_manual_description"
            )
        if st.button("Add manual entry", type="secondary", key="ss_manual_add"):
            if manual_amount == 0.0:
                st.error("Amount cannot be zero.")
            else:
                inserted = add_manual_ss_entry(
                    payment_date=manual_date.strftime("%Y-%m-%d"),
                    amount_eur=manual_amount,
                    description=manual_description.strip(),
                )
                if inserted:
                    st.success("Manual entry added.")
                    st.rerun()
                else:
                    st.warning("An identical entry (same date, amount and description) already exists.")

    # -------------------------------------------------------------------------
    # Summary by year
    # -------------------------------------------------------------------------
    st.subheader("Summary by year")

    all_rows = get_ss_payments()
    if not all_rows:
        st.info("No Social Security payments stored yet. Import a bank export above.")
        return

    df_all = pd.DataFrame(all_rows)
    df_all["payment_date"] = pd.to_datetime(df_all["payment_date"])
    df_all["year"] = df_all["payment_date"].dt.year
    df_all["month"] = df_all["payment_date"].dt.month

    years_available = sorted(df_all["year"].unique(), reverse=True)

    summary_data = []
    for yr in years_available:
        df_yr = df_all[df_all["year"] == yr]
        total = df_yr["amount_eur"].sum()
        count = len(df_yr)
        summary_data.append({"Year": yr, "Payments": count, "Total (€)": round(total, 2)})

    st.dataframe(pd.DataFrame(summary_data), width="stretch", hide_index=True)

    # -------------------------------------------------------------------------
    # Detail table with optional year filter
    # -------------------------------------------------------------------------
    st.subheader("Payment detail")

    selected_year = st.selectbox("Filter by year", options=["All"] + [str(y) for y in years_available], key="ss_filter_year")

    if selected_year == "All":
        df_view = df_all.copy()
    else:
        df_view = df_all[df_all["year"] == int(selected_year)].copy()

    # Quarterly breakdown when a year is selected
    if selected_year != "All":
        st.markdown("**Quarterly breakdown**")
        q_data = []
        for q in range(1, 5):
            months = list(range((q - 1) * 3 + 1, q * 3 + 1))
            df_q = df_view[df_view["month"].isin(months)]
            q_data.append({
                "Quarter": f"Q{q}",
                "Months": f"{months[0]}–{months[-1]}",
                "Payments": len(df_q),
                "Total (€)": round(df_q["amount_eur"].sum(), 2),
            })
        st.dataframe(pd.DataFrame(q_data), width="stretch", hide_index=True)

    # Full detail table
    display_cols = ["id", "payment_date", "amount_eur", "description", "source_file", "imported_at"]
    available = [c for c in display_cols if c in df_view.columns]
    df_display = df_view[available].rename(columns={
        "id": "ID",
        "payment_date": "Date",
        "amount_eur": "Amount (€)",
        "description": "Description",
        "source_file": "Source file",
        "imported_at": "Imported at",
    }).sort_values("Date", ascending=False)

    st.dataframe(df_display, width="stretch", hide_index=True)

    # CSV export
    csv_bytes = df_display.to_csv(index=False).encode()
    st.download_button(
        "Download as CSV",
        data=csv_bytes,
        file_name="social_security_payments.csv",
        mime="text/csv",
        key="ss_download_csv",
    )

    # -------------------------------------------------------------------------
    # Delete individual row
    # -------------------------------------------------------------------------
    with st.expander("Delete a payment row"):
        st.markdown("Enter the **ID** of the row you want to delete (visible in the table above).")
        del_id = st.number_input("Row ID to delete", min_value=1, step=1, key="ss_del_id")
        if st.button("Delete row", type="secondary", key="ss_delete_row"):
            delete_ss_payment(int(del_id))
            st.success(f"Row {del_id} deleted.")
            st.rerun()
