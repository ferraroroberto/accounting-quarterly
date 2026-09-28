"""Modelo 390 aggregation and the reconciliation adapter's legacy fallbacks.

TEMPORARY. The quarterly engine results have their own ``aeat_boxes()``
keyed by the printed box number (Modelo 303 #97, 130 #98, 349 #99, the 349
also ``operators()``), so ``src/reconciliation`` no longer needs a legacy
field map for them: ``legacy_boxes`` / ``legacy_audit_cells`` only remain as
the adapter's fallback for a result without ``aeat_boxes()``.

What is left is the Modelo 390 aggregation of the four quarterly 303s (used
by the reconciliation and ``src/tax_validator.py``) with its caveat note,
until the annual pack (#103) gives the 390 its own engine.
"""
from __future__ import annotations

import sqlite3
from typing import Any, Optional

from src.tax_engine import compute_modelo_303


def legacy_boxes(model: str, result: Any) -> dict[str, float]:
    """AEAT-numbered boxes of a result without ``aeat_boxes()`` — none are left ({})."""
    return {}


def legacy_notes(model: str) -> dict[str, str]:
    """Box -> caveat for the legacy mapping of ``model`` (only boxes with a caveat)."""
    if model == "390":
        return {"33": LEGACY_390_NOTE}
    return {}


def legacy_audit_cells(model: str, box: str) -> tuple[str, ...]:
    """Legacy ``tax_audit_log`` cells behind an AEAT box (empty when unknown)."""
    return ()


# ---------------------------------------------------------------------------
# Modelo 390 (annual) — aggregated from the four quarterly 303 results
# ---------------------------------------------------------------------------

LEGACY_390_NOTE = (
    "Annual figures are the sum of the four 303 quarters (21 % row, 28/29, 59, 120, OSS only — "
    "reverse charge, capital goods and pro-rata are not aggregated yet); box 33 adds the "
    "intra-EU deliveries (59) to the 21 % base, and 108 adds the OSS base."
)


def legacy_390_boxes(year: int, conn: sqlite3.Connection, config: Optional[dict] = None) -> dict[str, float]:
    """Modelo 390 boxes aggregated from the four quarterly 303 results.

    Same arithmetic the Tax Validation 390 lines always used; returned keyed
    by the 390 box number. "export" is the 303's box 120 (non-EU services not
    subject by location), which is what the 390's 104 has held so far.
    """
    agg = dict(base_21=0.0, cuota_21=0.0, intracom=0.0, export=0.0,
               oss=0.0, soportado_base=0.0, soportado_cuota=0.0)
    for q in range(1, 5):
        m = compute_modelo_303(year, q, conn, config)
        agg["base_21"] += m.c07_base
        agg["cuota_21"] += m.c09_cuota
        agg["intracom"] += m.c59_entregas_intracom
        agg["export"] += m.c120_no_sujetas_localizacion
        agg["oss"] += m.oss_base
        agg["soportado_base"] += m.c28_base
        agg["soportado_cuota"] += m.c29_cuota
    agg = {k: round(v, 2) for k, v in agg.items()}
    resultado = round(agg["cuota_21"] - agg["soportado_cuota"], 2)
    return {
        "05": agg["base_21"],
        "06": agg["cuota_21"],
        "33": round(agg["base_21"] + agg["intracom"], 2),
        "34": agg["cuota_21"],
        "48": agg["soportado_base"],
        "49": agg["soportado_cuota"],
        "64": agg["soportado_cuota"],
        "65": resultado,
        "86": resultado,
        "99": round(agg["base_21"] + agg["intracom"] + agg["export"], 2),
        "103": agg["intracom"],
        "104": agg["export"],
        "108": round(agg["base_21"] + agg["intracom"] + agg["export"] + agg["oss"], 2),
    }
