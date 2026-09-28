"""Legacy engine field -> AEAT box mapping, used by the reconciliation adapter.

TEMPORARY. The Modelo 130 / 349 results (``src/tax_models.py``) still use
internal field names that do not follow the AEAT form numbering (e.g.
``Modelo130Result.box_05_base`` is printed in box 04). The box-model rework
(#98 130, #99 349) gives each engine result an ``aeat_boxes() -> dict[str,
float]`` method keyed by the printed box number (and the 349 an
``operators()`` method), as the Modelo 303 already has (#97).
``src/reconciliation.app_boxes`` calls those when present and falls back to
this module otherwise.

Delete the 130/349 part (and its import in ``src/reconciliation.py``) once
#98 and #99 have shipped. The Modelo 390 aggregation below stays until the
annual pack (#87 step 16) replaces it; ``src/tax_validator.py`` uses it too.

Each mapping row sums signed legacy fields into one AEAT box and carries a
``note`` wherever the legacy field does not have the AEAT box's exact meaning;
the reconciliation view shows those notes so a divergence caused by the
legacy semantics is not mistaken for a data problem.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from src.tax_engine import compute_modelo_303


@dataclass(frozen=True)
class LegacyBox:
    """One AEAT box computed from signed legacy result fields."""
    box: str
    terms: tuple[tuple[str, int], ...]   # (legacy field, +1 / -1)
    note: str = ""

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(f for f, _ in self.terms)


def _one(box: str, field: str, note: str = "") -> LegacyBox:
    return LegacyBox(box, ((field, 1),), note)


LEGACY_130: tuple[LegacyBox, ...] = (
    _one("01", "box_01_ingresos"),
    LegacyBox("02", (("box_02_gastos", 1), ("gastos_dificil_justificacion", 1)),
              "Legacy keeps the 5 % hard-to-justify allowance outside `box_02_gastos`; the adapter "
              "adds it back because the form's box 02 includes it."),
    _one("03", "rendimiento_neto",
         "AEAT 03 = 01 − 02 after the 5 % allowance = legacy `rendimiento_neto` "
         "(legacy `box_03_rendimiento` is before the allowance)."),
    _one("04", "box_05_base", "Legacy `box_05_base` is AEAT 04 (20 % × max(0, 03))."),
    _one("05", "box_14_pagos_anteriores",
         "Legacy `box_14_pagos_anteriores` sums amounts from tax_filing_status; AEAT 05 is "
         "Σ positive 07 of earlier quarters − Σ 16, from the filed returns (#98)."),
    _one("06", "box_07_retenciones", "Legacy `box_07_retenciones` is AEAT 06."),
    LegacyBox("07", (("box_05_base", 1), ("box_14_pagos_anteriores", -1), ("box_07_retenciones", -1)),
              "Derived as 04 − 05 − 06 (negative allowed, as on the form)."),
    _one("19", "box_16_resultado",
         "Legacy `box_16_resultado` is clamped at 0 and skips 12–18 (no art. 110.3.c reduction "
         "in 13, no negative-quarter carry in 15), so it matches 19 only in the simple case."),
)

LEGACY_349_NOTE = (
    "The legacy 349 lists only EU B2B sales (reported under key S); intra-EU acquisitions "
    "(key I) arrive with #99."
)

LEGACY_BOXES: dict[str, tuple[LegacyBox, ...]] = {"130": LEGACY_130}

# Extra audit cells worth showing behind a box (on top of the mapped fields).
_EXTRA_AUDIT_CELLS: dict[tuple[str, str], tuple[str, ...]] = {
    ("130", "02"): ("amortizaciones", "capital_assets_excluded"),
    ("130", "03"): ("box_03_rendimiento", "gastos_dificil_justificacion"),
}


def legacy_boxes(model: str, result: Any) -> dict[str, float]:
    """Map a legacy 130/349 engine result to AEAT-numbered boxes ({} for other models)."""
    if model == "349":
        return {"01": float(len(result.rows)), "02": round(result.total, 2)}
    return {
        m.box: round(sum(sign * float(getattr(result, f)) for f, sign in m.terms), 2)
        for m in LEGACY_BOXES.get(model, ())
    }


def legacy_operators(result: Any) -> list[dict]:
    """Legacy 349 rows in the ``operators()`` contract shape (all key S)."""
    out = []
    for row in result.rows:
        vat = (row.buyer_vat_id or "").strip()
        country = vat[:2] if vat[:2].isalpha() else ""
        out.append({"country": country, "vat_id": vat, "name": row.buyer_name or "",
                    "key": "S", "base": round(row.total_amount, 2)})
    return out


def legacy_notes(model: str) -> dict[str, str]:
    """Box -> caveat for the legacy mapping of ``model`` (only boxes with a caveat)."""
    if model == "349":
        return {"02": LEGACY_349_NOTE}
    if model == "390":
        return {"33": LEGACY_390_NOTE}
    return {m.box: m.note for m in LEGACY_BOXES.get(model, ()) if m.note}


def legacy_audit_cells(model: str, box: str) -> tuple[str, ...]:
    """Legacy ``tax_audit_log`` cells behind an AEAT box (empty when unknown)."""
    if model == "349":
        return ("total",) if box in ("01", "02") else ()
    cells: list[str] = []
    for m in LEGACY_BOXES.get(model, ()):
        if m.box == box:
            cells.extend(m.fields)
    cells.extend(_EXTRA_AUDIT_CELLS.get((model, box), ()))
    return tuple(dict.fromkeys(cells))


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
