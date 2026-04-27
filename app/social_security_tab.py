"""Social Security (Seguridad Social) tab — import bank export and view cuotas."""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent

from src.config import load_config
from src.logger import get_logger
from src.social_security import (
    clear_ss_payments,
    delete_ss_payment,
    get_ss_payments,
    get_ss_total_for_period,
    load_bank_export,
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
    default_sheet = ss_cfg.get("sheet_name", 0)
    default_skiprows = int(ss_cfg.get("skiprows", 0))

    col1, col2 = st.columns(2)
    with col1:
        file_path_str = st.text_input(
            "Bank export file path (.xlsx / .xls / .csv)",
            value=default_file,
            help="Path relative to the project root, or absolute. "
                 "Configure the default in config.json → social_security.bank_export_file",
        )
        date_column = st.text_input("Date column name", value=default_date_col)
        amount_column = st.text_input("Amount column name", value=default_amount_col)
    with col2:
        description_column = st.text_input(
            "Description column name (optional)", value=default_desc_col
        )
        sheet_name_input = st.text_input(
            "Sheet name or index (0-based)",
            value=str(default_sheet),
            help="Use a sheet name like 'Sheet1' or a zero-based index like '0'.",
        )
        skiprows = st.number_input("Skip rows at top of file", min_value=0, value=default_skiprows, step=1)

    # Resolve sheet name to int if numeric
    try:
        sheet_name: int | str = int(sheet_name_input)
    except ValueError:
        sheet_name = sheet_name_input.strip()

    desc_col_clean = description_column.strip() or None

    file_path = _resolve(file_path_str)

    # Preview
    if st.button("Preview file columns"):
        if not file_path.exists():
            st.error(f"File not found: `{file_path}`")
        else:
            try:
                suffix = file_path.suffix.lower()
                if suffix in (".xlsx", ".xls", ".xlsm"):
                    df_preview = pd.read_excel(file_path, sheet_name=sheet_name, nrows=5, skiprows=skiprows, dtype=str)
                else:
                    df_preview = pd.read_csv(file_path, nrows=5, skiprows=skiprows, dtype=str)
                df_preview.columns = [str(c).strip() for c in df_preview.columns]
                st.markdown(f"**Columns found:** {list(df_preview.columns)}")
                st.dataframe(df_preview, width="stretch")
            except Exception as exc:
                st.error(f"Could not read file: {exc}")

    st.markdown("---")

    col_imp, col_clear = st.columns([2, 1])
    with col_imp:
        if st.button("Import from file", type="primary"):
            if not file_path.exists():
                st.error(f"File not found: `{file_path}`")
            else:
                try:
                    rows = load_bank_export(
                        file_path=file_path,
                        date_column=date_column,
                        amount_column=amount_column,
                        description_column=desc_col_clean,
                        sheet_name=sheet_name,
                        skiprows=int(skiprows),
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
        if st.button("Clear all SS payments", type="secondary"):
            clear_ss_payments()
            st.success("All Social Security payment rows cleared.")
            st.rerun()

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

    selected_year = st.selectbox("Filter by year", options=["All"] + [str(y) for y in years_available])

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
    )

    # -------------------------------------------------------------------------
    # Delete individual row
    # -------------------------------------------------------------------------
    with st.expander("Delete a payment row"):
        st.markdown("Enter the **ID** of the row you want to delete (visible in the table above).")
        del_id = st.number_input("Row ID to delete", min_value=1, step=1)
        if st.button("Delete row", type="secondary"):
            delete_ss_payment(int(del_id))
            st.success(f"Row {del_id} deleted.")
            st.rerun()
