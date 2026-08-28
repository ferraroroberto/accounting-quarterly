"""Single owner of invoice-PDF directory scanning.

`app/invoice_upload.py`, `app/invoice_ocr_tab.py`, and `scripts/close_quarter.py`
each used to scan `config["app"]["invoice_in_dir"]` / `invoice_out_dir` for PDFs
with their own copy of the logic; the three copies disagreed on recursion and on
whether an absolute configured path was honoured. This module is the one place
that owns both the directory resolution and the scan.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from src.config import load_config

ROOT = Path(__file__).parent.parent

_DIRECTION_CONFIG_KEYS = {
    "in": "invoice_in_dir",
    "out": "invoice_out_dir",
}
_DEFAULT_DIRS = {
    "in": "data/invoices/in",
    "out": "data/invoices/out",
}


def resolve_invoice_dir(direction: str, config: Optional[dict[str, Any]] = None) -> Path:
    """Resolve the configured invoice directory for `direction` ("in"/"out").

    Honours an absolute path in config; anchors a relative one to the project
    root, so the result doesn't depend on the caller's current working directory.
    """
    if direction not in _DIRECTION_CONFIG_KEYS:
        raise ValueError(f"Unknown invoice direction: {direction!r}")
    cfg = config if config is not None else load_config()
    app_cfg = cfg.get("app", {})
    raw = app_cfg.get(_DIRECTION_CONFIG_KEYS[direction], _DEFAULT_DIRS[direction])
    path = Path(raw)
    return path if path.is_absolute() else ROOT / path


def scan_invoice_pdfs(direction: str, config: Optional[dict[str, Any]] = None) -> list[Path]:
    """Recursively scan the configured `direction` invoice directory for PDFs.

    Returns sorted absolute paths (empty list if the directory doesn't exist yet).
    """
    dir_path = resolve_invoice_dir(direction, config)
    if not dir_path.exists():
        return []
    return sorted(dir_path.rglob("*.pdf"))
