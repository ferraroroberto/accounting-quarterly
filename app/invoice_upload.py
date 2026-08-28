"""Invoice Upload tab content - IntegraLOOP/BILOOP document integration."""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from src.config import load_config
from src.database import get_uploaded_files, record_upload
from src.invoice_scanner import resolve_invoice_dir, scan_invoice_pdfs
from src.accounting_api_client import (
    AccountingAPIClient,
    AccountingAPIError,
    load_accounting_api_config,
)
from src.logger import get_logger

log = get_logger(__name__)


def _get_new_invoices(all_files: list[str], uploaded: list[dict]) -> list[str]:
    """Find files that haven't been uploaded yet."""
    uploaded_names = {u["filename"] for u in uploaded}
    return [f for f in all_files if f not in uploaded_names]


def _render_upload_panel(
    direction: str,
    doc_type: str,
    invoice_dir: str,
    client: "AccountingAPIClient | None",
    api_enabled: bool,
) -> None:
    """Render one invoice-upload tab (scan → metrics → upload → history).

    ``direction`` is ``"in"`` (received) or ``"out"`` (produced); ``doc_type`` is
    the accounting-partner document type (``"buy_invoices"`` / ``"sell_invoices"``).
    """
    label = "Invoices Received" if direction == "in" else "Invoices Produced"
    st.subheader(label)
    st.caption(f"Directory: `{invoice_dir}`")

    dir_path = resolve_invoice_dir(direction)
    dir_path.mkdir(parents=True, exist_ok=True)

    all_files = [str(p.relative_to(dir_path)) for p in scan_invoice_pdfs(direction)]
    uploaded = get_uploaded_files(direction)
    new_files = _get_new_invoices(all_files, uploaded)

    col_total, col_uploaded, col_new = st.columns(3)
    col_total.metric("Total PDFs", len(all_files))
    col_uploaded.metric("Already Uploaded", len(uploaded))
    col_new.metric("New (pending)", len(new_files))

    if new_files:
        st.markdown("**New invoices to upload:**")
        for f in new_files:
            st.markdown(f"- `{f}`")

        if st.button(
            f"Upload new invoices ({direction.capitalize()})",
            key=f"upload_{direction}",
            disabled=not api_enabled,
        ):
            if not client:
                st.error("Accounting API client not configured.")
            else:
                token = st.session_state.get("inv_api_token") or (os.getenv("ACCOUNTING_TOKEN") or "")
                if not token:
                    st.error("No token available. Set ACCOUNTING_TOKEN or fetch token.")
                else:
                    progress = st.progress(0, text="Uploading...")
                    uploaded_count = 0
                    for i, filename in enumerate(new_files):
                        try:
                            path = str((dir_path / filename).resolve())
                            res = client.upload_document(
                                token=token,
                                doc_type=doc_type,
                                file_path=path,
                                date=datetime.now(),
                                year=datetime.now().year,
                                overwrite=0,
                            )
                            record_upload(filename, direction, api_response=str(res)[:2000])
                            uploaded_count += 1
                            progress.progress((i + 1) / len(new_files), text=f"Uploaded {filename}")
                        except AccountingAPIError as exc:
                            log.error("Invoice upload failed for %s: %s", filename, exc)
                            record_upload(filename, direction, api_response=f"ERROR: {exc}")
                            st.error(f"{filename}: {exc}")
                    progress.empty()
                    st.success(f"Uploaded {uploaded_count}/{len(new_files)} invoices")
                    st.rerun()
    else:
        st.success("All invoices have been uploaded.")

    if uploaded:
        st.markdown("---")
        st.markdown("**Upload history:**")
        st.dataframe(
            pd.DataFrame(uploaded),
            width="stretch",
            hide_index=True,
        )


def render() -> None:
    """Render the Invoice Upload tab."""
    cfg = load_config()
    app_cfg = cfg.get("app", {})
    invoice_in_dir = app_cfg.get("invoice_in_dir", "data/invoices/in")
    invoice_out_dir = app_cfg.get("invoice_out_dir", "data/invoices/out")

    accounting_api = cfg.get("accounting_api", {})
    api_enabled = accounting_api.get("enabled", False)
    default_company_id = accounting_api.get("company_id", "") or ""

    st.subheader("Invoice Upload to Accounting Partner")

    if not api_enabled:
        st.info(
            "The accounting API integration is not yet configured. "
            "Set `accounting_api.enabled: true` in `config.json` and provide "
            "the API base URL to activate this feature."
        )

    st.markdown("---")

    # --- API Connection Status ---
    st.subheader("API Connection")

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        st.text_input(
            "API Base URL",
            os.getenv("ACCOUNTING_BASE_URL", ""),
            disabled=True,
            key="inv_api_url",
            help="Configure in .env: ACCOUNTING_BASE_URL",
        )
    with col2:
        has_sub = bool(os.getenv("ACCOUNTING_SUBSCRIPTION_KEY"))
        if has_sub:
            st.success("SUBSCRIPTION_KEY configured (ACCOUNTING_SUBSCRIPTION_KEY)")
        else:
            st.warning("Missing `ACCOUNTING_SUBSCRIPTION_KEY` in .env")
    with col3:
        has_token = bool(os.getenv("ACCOUNTING_TOKEN"))
        has_userpass = bool(os.getenv("ACCOUNTING_USER")) and bool(os.getenv("ACCOUNTING_PASSWORD"))
        if has_token:
            st.success("Token configured (ACCOUNTING_TOKEN)")
        elif has_userpass:
            st.info("Token will be fetched via /token")
        else:
            st.warning("No token or user/password")

    company_id = st.text_input(
        "Company ID (companyId/company_id)",
        value=default_company_id,
        key="inv_company_id",
        disabled=not api_enabled,
        help="Required for listing documents via /documents/getDirectory.",
    )

    if "inv_api_token" not in st.session_state:
        st.session_state["inv_api_token"] = None

    col_a, col_b, col_c = st.columns([1, 1, 2])
    with col_a:
        fetch_token = st.button("Fetch token (2h)", key="inv_fetch_token", disabled=not api_enabled)
    with col_b:
        test_api = st.button("Test API Connection", key="inv_test_api", disabled=not api_enabled)
    with col_c:
        st.caption("Tip: set ACCOUNTING_TOKEN, or set ACCOUNTING_USER/PASSWORD to auto-fetch.")

    client: AccountingAPIClient | None = None
    client_err: str | None = None
    if api_enabled:
        try:
            client = AccountingAPIClient(load_accounting_api_config())
        except Exception as exc:
            client_err = str(exc)

    if client_err:
        st.error(client_err)

    if fetch_token and client:
        try:
            token = client.get_token()
            st.session_state["inv_api_token"] = token
            st.success("Token fetched and stored for this session.")
        except Exception as exc:
            st.error(str(exc))

    if test_api and client:
        token = st.session_state.get("inv_api_token") or None
        ok, msg = client.test_connection(token=token)
        (st.success if ok else st.error)(msg)

    st.markdown("---")

    # --- Platform directory (optional) ---
    st.subheader("Platform State (optional)")
    col_d1, col_d2, col_d3 = st.columns([1, 1, 1])
    with col_d1:
        dir_type = st.selectbox(
            "Document type",
            options=["sell_invoices", "buy_invoices", "others", ""],
            index=0,
            disabled=not api_enabled,
            key="inv_dir_type",
            help="Filter the platform directory by document type.",
        )
        dir_type = dir_type or None
    with col_d2:
        start_default = date.today() - timedelta(days=90)
        start_d = st.date_input("Start date", value=start_default, disabled=not api_enabled, key="inv_dir_start")
    with col_d3:
        end_d = st.date_input("End date", value=date.today(), disabled=not api_enabled, key="inv_dir_end")

    if st.button("Load directory", key="inv_load_dir", disabled=not api_enabled):
        if not client:
            st.error("Accounting API client not configured.")
        elif not company_id.strip():
            st.error("Company ID is required.")
        else:
            token = st.session_state.get("inv_api_token") or (os.getenv("ACCOUNTING_TOKEN") or "")
            if not token:
                st.error("No token available. Set ACCOUNTING_TOKEN or fetch token.")
            else:
                try:
                    data = client.list_directory(
                        token=token,
                        company_id=company_id.strip(),
                        doc_type=dir_type,
                        start_date=start_d.strftime("%Y-%m-%d"),
                        end_date=end_d.strftime("%Y-%m-%d"),
                    )
                    st.session_state["inv_directory_data"] = data
                    st.success("Directory loaded.")
                except Exception as exc:
                    st.error(str(exc))

    dir_data = st.session_state.get("inv_directory_data")
    if dir_data is not None:
        st.write(dir_data)

    st.markdown("---")

    # --- Download (optional) ---
    st.subheader("Download by document_id (optional)")
    dl_id = st.text_input("document_id (or comma-separated list)", value="", key="inv_dl_id", disabled=not api_enabled)
    if st.button("Download", key="inv_dl_btn", disabled=not api_enabled):
        if not client:
            st.error("Accounting API client not configured.")
        else:
            token = st.session_state.get("inv_api_token") or (os.getenv("ACCOUNTING_TOKEN") or "")
            if not token:
                st.error("No token available. Set ACCOUNTING_TOKEN or fetch token.")
            elif not dl_id.strip():
                st.error("Please provide a document_id.")
            else:
                try:
                    content = client.download_document(token=token, document_id=dl_id.strip())
                    st.session_state["inv_dl_bytes"] = content
                    st.success(f"Downloaded {len(content)} bytes.")
                except Exception as exc:
                    st.error(str(exc))

    if st.session_state.get("inv_dl_bytes"):
        st.download_button(
            "Save downloaded file",
            data=st.session_state["inv_dl_bytes"],
            file_name=f"document_{(dl_id or 'download')}.bin",
            key="inv_dl_save",
            disabled=not api_enabled,
        )

    st.markdown("---")

    # --- Invoices Received (In) ---
    in_tab, out_tab = st.tabs(["Invoices Received (In)", "Invoices Produced (Out)"])

    with in_tab:
        _render_upload_panel("in", "buy_invoices", invoice_in_dir, client, api_enabled)

    with out_tab:
        _render_upload_panel("out", "sell_invoices", invoice_out_dir, client, api_enabled)
