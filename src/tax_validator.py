"""Validation of computed tax figures vs gestor-filed AEAT declarations.

Reference data is loaded from tmp/validation/validation.yaml (gitignored).
Each entry in that file describes one filed period; this module compares the
filed values against what our database computes.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from src.tax_engine import (
    compute_modelo_130,
    compute_modelo_303,
    compute_modelo_349,
)

_YAML_PATH = Path(__file__).parent.parent / "tmp" / "validation" / "validation.yaml"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ValidationLine:
    """Single comparison row between a filed value and our computed value."""
    casilla: str
    description: str
    filed: Optional[float]    # Value on the gestor's AEAT submission
    computed: Optional[float] # Value our system computes
    tolerance: float = 0.02   # Acceptable rounding gap in EUR

    @property
    def diff(self) -> Optional[float]:
        """Computed minus filed. Positive = DB computes more than gestor filed."""
        if self.filed is None or self.computed is None:
            return None
        return round(self.computed - self.filed, 2)

    @property
    def match(self) -> bool:
        d = self.diff
        return d is not None and abs(d) <= self.tolerance

    @property
    def status(self) -> str:
        d = self.diff
        if d is None:
            return "N/A"
        return "OK" if self.match else ("DB_HIGH" if d > 0 else "DB_LOW")


@dataclass
class ModelValidationResult:
    model: str       # "130", "303", "349", "390"
    period: str      # e.g. "2025 Q4" or "2025 Annual"
    filed_date: str
    lines: list[ValidationLine] = field(default_factory=list)

    @property
    def has_differences(self) -> bool:
        return any(not ln.match and ln.status != "N/A" for ln in self.lines)

    @property
    def diff_count(self) -> int:
        return sum(1 for ln in self.lines if not ln.match and ln.status != "N/A")

    @property
    def ok_count(self) -> int:
        return sum(1 for ln in self.lines if ln.match)


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------

def _load_filings() -> list[dict]:
    """Load all filed declarations from the YAML reference file."""
    if not _YAML_PATH.exists():
        return []
    with _YAML_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return data.get("filings", []) if data else []


def _find_filing(filings: list[dict], model: str, year: int, quarter: int | None) -> dict | None:
    for f in filings:
        if (str(f.get("model")) == model
                and int(f.get("year", 0)) == year
                and f.get("quarter") == quarter):
            return f
    return None


# ---------------------------------------------------------------------------
# Validation builders
# ---------------------------------------------------------------------------

def validate_modelo_130(
    year: int, quarter: int, conn: sqlite3.Connection, filings: list[dict]
) -> ModelValidationResult:
    filing = _find_filing(filings, "130", year, quarter)
    computed = compute_modelo_130(year, quarter, conn)
    period = f"{year} Q{quarter}"

    if filing is None:
        return ModelValidationResult(
            model="130", period=period, filed_date="—",
            lines=[ValidationLine("—", "No filed data in validation.yaml for this period", None, None)],
        )

    v = filing.get("values", {})
    result = ModelValidationResult(
        model="130", period=period, filed_date=filing["filed_date"]
    )
    # box_05_base in code = casilla 04 in PDF (20% of rendimiento)
    # box_07_retenciones in code = casilla 06 in PDF
    # box_14_pagos_anteriores in code = casilla 05 in PDF
    result.lines = [
        ValidationLine("01", "Ingresos computables YTD",
                        v.get("01_ingresos_ytd"), computed.box_01_ingresos),
        ValidationLine("02", "Gastos fiscalmente deducibles YTD",
                        v.get("02_gastos_ytd"), computed.box_02_gastos),
        ValidationLine("03", "Rendimiento neto (01 - 02)",
                        v.get("03_rendimiento_neto"), computed.box_03_rendimiento),
        ValidationLine("04", "20% del rendimiento (base pago fraccionado)",
                        v.get("04_veinte_pct"), computed.box_05_base),
        ValidationLine("05", "Trimestres anteriores (pagos previos imputables)",
                        v.get("05_trimestres_anteriores"), computed.box_14_pagos_anteriores),
        ValidationLine("06", "Retenciones soportadas YTD",
                        v.get("06_retenciones_ytd"), computed.box_07_retenciones),
        ValidationLine("07", "Pago fraccionado previo (04 - 05 - 06)",
                        v.get("07_pago_fraccionado"),
                        round(computed.box_05_base - computed.box_14_pagos_anteriores - computed.box_07_retenciones, 2)),
        ValidationLine("19", "Resultado final (negativa / a ingresar)",
                        v.get("19_result"), computed.box_16_resultado),
    ]
    return result


def validate_modelo_303(
    year: int, quarter: int, conn: sqlite3.Connection, filings: list[dict]
) -> ModelValidationResult:
    filing = _find_filing(filings, "303", year, quarter)
    computed = compute_modelo_303(year, quarter, conn)
    period = f"{year} Q{quarter}"

    if filing is None:
        return ModelValidationResult(
            model="303", period=period, filed_date="—",
            lines=[ValidationLine("—", "No filed data in validation.yaml for this period", None, None)],
        )

    v = filing.get("values", {})
    result = ModelValidationResult(
        model="303", period=period, filed_date=filing["filed_date"]
    )
    result.lines = [
        ValidationLine("07/08", "Base imponible régimen general @ 21%",
                        v.get("07_base_21pct"), computed.box_01_base),
        ValidationLine("09", "Cuota devengada @ 21%",
                        v.get("09_cuota_21pct"), computed.box_03_cuota),
        ValidationLine("27", "Total cuota IVA devengada",
                        v.get("27_total_cuota_devengada"), computed.box_03_cuota),
        ValidationLine("28", "Base IVA soportado interior corrientes",
                        v.get("28_base_soportado"), computed.box_29_base_soportado),
        ValidationLine("29", "Cuota IVA soportado interior corrientes",
                        v.get("29_cuota_soportado"), computed.box_28_iva_soportado),
        ValidationLine("46", "Resultado régimen general (devengado - deducible)",
                        v.get("46_resultado"), computed.box_46_diferencia),
        ValidationLine("59", "Entregas intracomunitarias de bienes y servicios",
                        v.get("59_entregas_intracom"), computed.box_59_intracom_entregas),
        ValidationLine("60", "Exportaciones y operaciones exentas (informativo)",
                        v.get("60_exportaciones"), computed.export_base),
    ]
    return result


def validate_modelo_349(
    year: int, quarter: int, conn: sqlite3.Connection, filings: list[dict]
) -> ModelValidationResult:
    filing = _find_filing(filings, "349", year, quarter)
    computed = compute_modelo_349(year, quarter, conn)
    period = f"{year} Q{quarter}"

    if filing is None:
        return ModelValidationResult(
            model="349", period=period, filed_date="—",
            lines=[ValidationLine("—", "No filed data in validation.yaml for this period", None, None)],
        )

    v = filing.get("values", {})
    result = ModelValidationResult(
        model="349", period=period, filed_date=filing["filed_date"]
    )
    lines = [
        ValidationLine("01", "Número total de operadores intracomunitarios",
                        float(v.get("01_total_operators", 0)), float(len(computed.rows))),
        ValidationLine("02", "Importe total operaciones intracomunitarias",
                        v.get("02_total_amount"), computed.total),
    ]
    for i, op in enumerate(filing.get("operators", []), 1):
        lines.append(ValidationLine(
            f"op_{i}",
            f"Filed: {op['name']} ({op['country']} {op['vat_id']}) clave={op['clave']}",
            op["amount"], None,
        ))
    for row in computed.rows:
        lines.append(ValidationLine(
            "op_db",
            f"DB: {row.buyer_name or row.buyer_vat_id}",
            None, row.total_amount,
        ))
    result.lines = lines
    return result


def validate_modelo_390(
    year: int, conn: sqlite3.Connection, filings: list[dict]
) -> ModelValidationResult:
    filing = _find_filing(filings, "390", year, None)
    period = f"{year} Annual"

    # Aggregate all 4 quarters
    agg = dict(base_21=0.0, cuota_21=0.0, intracom=0.0, export=0.0,
               oss=0.0, soportado_base=0.0, soportado_cuota=0.0)
    for q in range(1, 5):
        m = compute_modelo_303(year, q, conn)
        agg["base_21"]         += m.box_01_base
        agg["cuota_21"]        += m.box_03_cuota
        agg["intracom"]        += m.box_59_intracom_entregas
        agg["export"]          += m.export_base
        agg["oss"]             += m.oss_base
        agg["soportado_base"]  += m.box_29_base_soportado
        agg["soportado_cuota"] += m.box_28_iva_soportado
    agg = {k: round(v, 2) for k, v in agg.items()}

    agg_resultado   = round(agg["cuota_21"] - agg["soportado_cuota"], 2)
    agg_vol_total   = round(agg["base_21"] + agg["intracom"] + agg["export"] + agg["oss"], 2)
    m130 = compute_modelo_130(year, 4, conn)

    if filing is None:
        return ModelValidationResult(
            model="390", period=period, filed_date="—",
            lines=[ValidationLine("—", "No filed data in validation.yaml for this period", None, None)],
        )

    v = filing.get("values", {})
    result = ModelValidationResult(
        model="390", period=period, filed_date=filing["filed_date"]
    )
    result.lines = [
        ValidationLine("05",     "Base régimen ordinario @ 21%",
                        v.get("05_base_ord_21"),     agg["base_21"]),
        ValidationLine("06",     "Cuota ordinaria @ 21%",
                        v.get("06_cuota_ord_21"),    agg["cuota_21"]),
        ValidationLine("33",     "Total bases IVA devengado",
                        v.get("33_total_bases"),
                        round(agg["base_21"] + agg["intracom"], 2)),
        ValidationLine("34",     "Total cuotas IVA devengado",
                        v.get("34_total_cuotas"),    agg["cuota_21"]),
        ValidationLine("48/49",  "Total base/cuota IVA deducible interior",
                        v.get("48_base_interior"),   agg["soportado_base"]),
        ValidationLine("64",     "Suma total deducciones",
                        v.get("64_suma_deducciones"), agg["soportado_cuota"]),
        ValidationLine("65",     "Resultado régimen general (47 - 64)",
                        v.get("65_resultado"),       agg_resultado),
        ValidationLine("86",     "Resultado liquidación anual",
                        v.get("86_resultado_liquidacion"), agg_resultado),
        ValidationLine("99",     "Volumen operaciones régimen general",
                        v.get("99_regimen_general"),
                        round(agg["base_21"] + agg["intracom"] + agg["export"], 2)),
        ValidationLine("103",    "Entregas intracomunitarias (informativo)",
                        v.get("103_entregas_intracom"), agg["intracom"]),
        ValidationLine("104",    "Exportaciones y exentas con derecho a deducción",
                        v.get("104_exportaciones"),  agg["export"]),
        ValidationLine("108",    "Total volumen de operaciones",
                        v.get("108_total_volumen"),  agg_vol_total),
        ValidationLine("130/01", "Ingresos anuales computables (M130 cross-check)",
                        v.get("108_total_volumen"),  m130.box_01_ingresos),
    ]
    return result


# ---------------------------------------------------------------------------
# Master entry point
# ---------------------------------------------------------------------------

def run_all_validations(conn: sqlite3.Connection) -> list[ModelValidationResult]:
    """Run all validations defined in validation.yaml, in filing order."""
    filings = _load_filings()
    results = []
    seen = set()

    for f in filings:
        model   = str(f.get("model", ""))
        year    = int(f.get("year", 0))
        quarter = f.get("quarter")  # int or None
        key     = (model, year, quarter)
        if key in seen:
            continue
        seen.add(key)

        if model == "130" and quarter is not None:
            results.append(validate_modelo_130(year, int(quarter), conn, filings))
        elif model == "303" and quarter is not None:
            results.append(validate_modelo_303(year, int(quarter), conn, filings))
        elif model == "349" and quarter is not None:
            results.append(validate_modelo_349(year, int(quarter), conn, filings))
        elif model == "390" and quarter is None:
            results.append(validate_modelo_390(year, conn, filings))

    return results
