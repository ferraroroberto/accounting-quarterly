"""Dataclasses for Spanish tax obligation computations."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Optional


FilingStatus = Literal["PENDING", "DUE", "OVERDUE", "FILED"]

TaxModel = Literal["303", "390", "130", "100", "347", "349", "OSS"]

EntryType = Literal[
    "RETENCIONES_SOPORTADAS", "GASTOS_DEDUCIBLES", "IVA_SOPORTADO", "OTHER"
]

# EU VAT rates for OSS digital services
OSS_RATES: dict[str, float] = {
    "DE": 0.19, "FR": 0.20, "IT": 0.22, "NL": 0.21,
    "BE": 0.21, "PT": 0.23, "AT": 0.20, "PL": 0.23,
    "SE": 0.25, "DK": 0.25, "FI": 0.24, "IE": 0.23,
    "DEFAULT_EU": 0.21,
}

# Deadline dates per model / quarter for status computation
def _tax_deadline_date(model: str, year: int, quarter: int) -> date:
    """Return the actual deadline date for a given tax model, year, and quarter."""
    if model in ("303", "130", "349"):
        ends = {1: date(year, 4, 20), 2: date(year, 7, 20),
                3: date(year, 10, 20), 4: date(year + 1, 1, 30)}
        return ends[quarter]
    if model == "OSS":
        ends = {1: date(year, 4, 30), 2: date(year, 7, 31),
                3: date(year, 10, 31), 4: date(year + 1, 1, 31)}
        return ends[quarter]
    if model == "390":
        return date(year + 1, 1, 30)
    if model == "347":
        return date(year + 1, 2, 28)
    return date(year, 12, 31)


@dataclass
class Modelo303Result:
    year: int
    quarter: int
    # Devengado
    box_01_base: float = 0.0          # Base imponible al 21% (IVA_ES_21)
    box_03_cuota: float = 0.0         # 21% × Box 01
    box_59_intracom_entregas: float = 0.0  # Casilla 59: Entregas intracomunitarias exentas (EU B2B sales)
    # Deducible (AEAT casillas: 28 = base, 29 = cuota — names match the form)
    box_28_base_soportado: float = 0.0   # Casilla 28: Base imponible IVA soportado interior corriente
    box_29_cuota_soportado: float = 0.0  # Casilla 29: Cuota IVA soportado deducible (invoices + quarterly_tax_entries)
    # Resultado
    box_46_diferencia: float = 0.0    # Box 03 - Box 29
    box_48_resultado: float = 0.0     # Net to pay (positive) or refund (negative)
    # Informative
    oss_base: float = 0.0
    oss_vat: float = 0.0
    export_base: float = 0.0          # IVA_EXPORT transactions base
    notes: str = ""
    audit: list = field(default_factory=list)  # list[AuditEntry]


@dataclass
class Modelo130Result:
    year: int
    quarter: int
    # Ingresos y gastos YTD
    box_01_ingresos: float = 0.0       # Ingresos computables YTD
    box_02_gastos: float = 0.0         # Gastos deducibles YTD
    box_03_rendimiento: float = 0.0    # Box 01 - Box 02 (rendimiento neto previo)
    # Gastos de difícil justificación (5% of rendimiento neto previo, capped €2,000/year)
    gastos_dificil_justificacion: float = 0.0
    rendimiento_neto: float = 0.0      # box_03 - gastos_dificil_justificacion
    # Cálculo
    box_05_base: float = 0.0           # 20% × rendimiento_neto
    box_07_retenciones: float = 0.0    # Retenciones soportadas YTD
    box_14_pagos_anteriores: float = 0.0  # Previous quarters paid
    box_16_resultado: float = 0.0      # max(0, Box 05 - Box 07 - Box 14)
    notes: str = ""
    audit: list = field(default_factory=list)  # list[AuditEntry]


@dataclass
class Modelo349Row:
    buyer_name: str
    buyer_vat_id: str
    total_amount: float


@dataclass
class Modelo349Result:
    year: int
    quarter: int
    rows: list[Modelo349Row] = field(default_factory=list)
    total: float = 0.0
    notes: str = ""
    audit: list = field(default_factory=list)  # list[AuditEntry]


@dataclass
class OSSCountryRow:
    country: str
    transactions: int
    base_eur: float
    vat_rate: float
    vat_amount_eur: float


@dataclass
class OSSReturnResult:
    year: int
    quarter: int
    rows: list[OSSCountryRow] = field(default_factory=list)
    total_base: float = 0.0
    total_vat: float = 0.0
    total_transactions: int = 0
    audit: list = field(default_factory=list)  # list[AuditEntry]


@dataclass
class Modelo347Row:
    counterparty_name: str
    counterparty_nif: str
    total_operations: float
    quarter_breakdown: dict[int, float] = field(default_factory=dict)


@dataclass
class Modelo347Result:
    year: int
    rows: list[Modelo347Row] = field(default_factory=list)
    threshold: float = 3005.06
    audit: list = field(default_factory=list)  # list[AuditEntry]


@dataclass
class TaxDeadline:
    model: TaxModel
    name: str
    year: int
    quarter: Optional[int]     # None for annual filings
    deadline: date
    status: FilingStatus
    amount_eur: Optional[float] = None  # None until computed/filed
    notes: str = ""


@dataclass
class AuditEntry:
    """One auditable calculation step within a tax model computation."""
    model: str            # "303", "130", "349", "OSS", "347"
    year: int
    quarter: int          # 0 for annual models
    cell: str             # field name, e.g. "box_01_base"
    label: str            # human-readable, e.g. "Base imponible 21% (IVA devengado)"
    formula: str          # text description of the formula/rule applied
    value: float          # computed value in EUR
    inputs_json: str = "" # JSON-serialised dict of named inputs for full traceability

    @classmethod
    def of(
        cls,
        model: str,
        year: int,
        quarter: int,
        cell: str,
        label: str,
        formula: str,
        value: float,
        **inputs: Any,
    ) -> "AuditEntry":
        """Build an entry, JSON-serialising ``inputs`` into ``inputs_json``.

        Single construction site for every ``compute_modelo_*`` function in
        ``src/tax_engine.py`` — they all built the same shape by hand before.
        """
        return cls(
            model=model, year=year, quarter=quarter,
            cell=cell, label=label, formula=formula, value=value,
            inputs_json=json.dumps(inputs),
        )
