"""Messages that survive ``st.rerun()``.

A ``st.error`` / ``st.success`` written just before ``st.rerun()`` is wiped by
the rerun and the user never sees it. ``flash`` queues the message in
``st.session_state`` instead, and ``show_flash`` renders and clears it on the
next run. Each tab uses its own ``scope`` so its messages show in that tab (all
tabs render on every run) — the Invoice Ledger scope is also fed by the Fixed
Assets form it embeds.
"""
from __future__ import annotations

import streamlit as st

FLASH_KINDS = ("success", "info", "warning", "error")


def _key(scope: str) -> str:
    return f"{scope}_flash"


def flash(scope: str, kind: str, message: str) -> None:
    """Queue ``message`` (``kind`` is a ``st.<kind>`` name) for the next ``show_flash(scope)``."""
    if kind not in FLASH_KINDS:
        raise ValueError(f"Unknown flash kind {kind!r}; use one of {FLASH_KINDS}")
    st.session_state.setdefault(_key(scope), []).append((kind, message))


def show_flash(scope: str) -> None:
    """Render and clear every message queued for ``scope``."""
    for kind, message in st.session_state.pop(_key(scope), []):
        getattr(st, kind)(message)
