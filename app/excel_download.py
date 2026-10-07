"""Generate-and-download for the Excel report, shared by Quarter Report and History."""
from __future__ import annotations

import os
import tempfile
from typing import Optional

import streamlit as st

from src.excel_exporter import create_excel_report
from src.exceptions import StaleClassificationError
from src.models import ClassifiedPayment


def render_excel_download(
    payments: list[ClassifiedPayment],
    year: int,
    quarter: Optional[int],
    filename: str,
    download_key: str,
    label: str = "",
) -> None:
    """Build the workbook and offer it as a download, or show why it can't be built.

    A stale classification (``StaleClassificationError``) is reported with
    ``st.error``; the temp file is removed on every path.
    """
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        create_excel_report(payments, tmp_path, year, quarter, label)
        with open(tmp_path, "rb") as f:
            excel_bytes = f.read()
    except StaleClassificationError as exc:
        st.error(str(exc))
        return
    finally:
        os.unlink(tmp_path)
    st.download_button(
        label=f"Download {filename}",
        data=excel_bytes,
        file_name=filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        key=download_key,
    )
