"""Invoice Upload tab content - scaffold for accounting partner integration."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent.parent

from src.config import load_config
from src.database import get_uploaded_files, record_upload
from src.logger import get_logger

log = get_logger(__name__)


def _scan_invoices(directory: str) -> list[str]:
    """Scan a directory for PDF files."""
    dir_path = ROOT / directory
    if not dir_path.exists():
        return []
    return sorted([f.name for f in dir_path.glob("*.pdf")])


def _get_new_invoices(all_files: list[str], uploaded: list[dict]) -> list[str]:
    """Find files that haven't been uploaded yet."""
    uploaded_names = {u["filename"] for u in uploaded}
    return [f for f in all_files if f not in uploaded_names]


def render():
    """Render the Invoice Upload tab."""
    cfg = load_config()
    app_cfg = cfg.get("app", {})
    invoice_in_dir = app_cfg.get("invoice_in_dir", "data/invoices/in")
    invoice_out_dir = app_cfg.get("invoice_out_dir", "data/invoices/out")

    accounting_api = cfg.get("accounting_api", {})
    api_enabled = accounting_api.get("enabled", False)

    st.subheader("Invoice Upload to Accounting Partner")

    if not api_enabled:
        st.info(
            "The accounting API integration is not yet configured. "
            "Set `accounting_api.enabled: true` in `config.json` and provide "
            "the API base URL and key to activate this feature."
        )

    st.markdown("---")

    # --- API Connection Status ---
    st.subheader("API Connection")

    col1, col2 = st.columns(2)
    with col1:
        st.text_input(
            "API Base URL",
            accounting_api.get("base_url", ""),
            disabled=True,
            key="inv_api_url",
            help="Configure in config.json -> accounting_api -> base_url",
        )
    with col2:
        api_key_env = accounting_api.get("api_key_env", "ACCOUNTING_API_KEY")
        import os
        has_key = bool(os.getenv(api_key_env))
        if has_key:
            st.success(f"API key configured ({api_key_env})")
        else:
            st.warning(f"API key not set. Add `{api_key_env}` to your .env file.")

    if st.button("Test API Connection", key="inv_test_api", disabled=not api_enabled):
        st.info("API connection test: placeholder - will be implemented when API details are provided.")

    st.markdown("---")

    # --- Invoices Received (In) ---
    in_tab, out_tab = st.tabs(["Invoices Received (In)", "Invoices Produced (Out)"])

    with in_tab:
        st.subheader("Invoices Received")
        st.caption(f"Directory: `{invoice_in_dir}`")

        in_dir_path = ROOT / invoice_in_dir
        in_dir_path.mkdir(parents=True, exist_ok=True)

        all_in = _scan_invoices(invoice_in_dir)
        uploaded_in = get_uploaded_files("in")
        new_in = _get_new_invoices(all_in, uploaded_in)

        col_total, col_uploaded, col_new = st.columns(3)
        col_total.metric("Total PDFs", len(all_in))
        col_uploaded.metric("Already Uploaded", len(uploaded_in))
        col_new.metric("New (pending)", len(new_in))

        if new_in:
            st.markdown("**New invoices to upload:**")
            for f in new_in:
                st.markdown(f"- `{f}`")

            if st.button("Upload new invoices (In)", key="upload_in", disabled=not api_enabled):
                progress = st.progress(0, text="Uploading...")
                for i, filename in enumerate(new_in):
                    # Placeholder: actual API call would go here
                    record_upload(filename, "in", api_response="placeholder_ok")
                    progress.progress((i + 1) / len(new_in), text=f"Uploaded {filename}")
                progress.empty()
                st.success(f"Uploaded {len(new_in)} invoices")
                st.rerun()
        else:
            st.success("All invoices have been uploaded.")

        if uploaded_in:
            st.markdown("---")
            st.markdown("**Upload history:**")
            st.dataframe(
                pd.DataFrame(uploaded_in),
                use_container_width=True,
                hide_index=True,
            )

    with out_tab:
        st.subheader("Invoices Produced")
        st.caption(f"Directory: `{invoice_out_dir}`")

        out_dir_path = ROOT / invoice_out_dir
        out_dir_path.mkdir(parents=True, exist_ok=True)

        all_out = _scan_invoices(invoice_out_dir)
        uploaded_out = get_uploaded_files("out")
        new_out = _get_new_invoices(all_out, uploaded_out)

        col_total, col_uploaded, col_new = st.columns(3)
        col_total.metric("Total PDFs", len(all_out))
        col_uploaded.metric("Already Uploaded", len(uploaded_out))
        col_new.metric("New (pending)", len(new_out))

        if new_out:
            st.markdown("**New invoices to upload:**")
            for f in new_out:
                st.markdown(f"- `{f}`")

            if st.button("Upload new invoices (Out)", key="upload_out", disabled=not api_enabled):
                progress = st.progress(0, text="Uploading...")
                for i, filename in enumerate(new_out):
                    record_upload(filename, "out", api_response="placeholder_ok")
                    progress.progress((i + 1) / len(new_out), text=f"Uploaded {filename}")
                progress.empty()
                st.success(f"Uploaded {len(new_out)} invoices")
                st.rerun()
        else:
            st.success("All invoices have been uploaded.")

        if uploaded_out:
            st.markdown("---")
            st.markdown("**Upload history:**")
            st.dataframe(
                pd.DataFrame(uploaded_out),
                use_container_width=True,
                hide_index=True,
            )
