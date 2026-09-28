"""Dataclasses for Spanish tax obligation computations."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal, Optional


# COMPUTED / FILED are also the ``tax_computation_snapshots.status`` values (#101).
FilingStatus = Literal["PENDING", "DUE", "OVERDUE", "COMPUTED", "FILED"]

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


Q4NegativeResult = Literal["compensate", "refund"]

# AEAT Modelo 303 box number -> Modelo303Result field, in the order the boxes
# are printed on the form (page 1 liquidación, page 3 información adicional +
# resultado, then the compensación / devolución boxes). Source: the AEAT form
# as reproduced in the "Manual práctico IVA 2025", cap. 9 (Modelo 303 anexo).
# Rows 01/02/03 = 4%, 04/05/06 = 10%, 07/08/09 = 21% (the 0%, 2% and 5% rows
# 150/165/153 are not used by this taxpayer and are not modelled).
MODELO303_BOX_FIELDS: dict[str, str] = {
    "01": "c01_base", "03": "c03_cuota",
    "04": "c04_base", "06": "c06_cuota",
    "07": "c07_base", "09": "c09_cuota",
    "10": "c10_base", "11": "c11_cuota",
    "12": "c12_base", "13": "c13_cuota",
    "27": "c27_total_devengado",
    "28": "c28_base", "29": "c29_cuota",
    "30": "c30_base", "31": "c31_cuota",
    "36": "c36_base", "37": "c37_cuota",
    "43": "c43_regularizacion_bienes_inversion",
    "44": "c44_regularizacion_prorrata",
    "45": "c45_total_deducir",
    "46": "c46_resultado_regimen_general",
    "59": "c59_entregas_intracom",
    "60": "c60_exportaciones",
    "120": "c120_no_sujetas_localizacion",
    "123": "oss_base",
    "64": "c64_suma_resultados",
    "65": "c65_pct_atribuible_estado",
    "66": "c66_atribuible_estado",
    "110": "c110_pendiente_anteriores",
    "78": "c78_aplicadas_periodo",
    "87": "c87_pendiente_posteriores",
    "69": "c69_resultado_autoliquidacion",
    "71": "c71_resultado_liquidacion",
    "72": "c72_a_compensar",
    "73": "c73_a_devolver",
}


@dataclass
class Modelo303Result:
    """Modelo 303 (quarterly VAT return) with fields named after the AEAT boxes.

    ``cNN_*`` fields hold the value to type into box NN; ``aeat_boxes()`` returns
    them keyed by the box number exactly as printed on the form. The remaining
    fields are context for the reconciliation / filing sheet, not boxes.
    """
    year: int
    quarter: int
    # --- IVA devengado (régimen general) ---
    c01_base: float = 0.0                 # 4% row
    c03_cuota: float = 0.0
    c04_base: float = 0.0                 # 10% row
    c06_cuota: float = 0.0
    c07_base: float = 0.0                 # 21% row (ES_21, EU_B2C_ES21, Stripe Spain/EU B2C)
    c09_cuota: float = 0.0
    c10_base: float = 0.0                 # adquisiciones intracomunitarias (INTRA_EU_RC)
    c11_cuota: float = 0.0
    c12_base: float = 0.0                 # otras operaciones con ISP (NON_EU_RC, D8)
    c13_cuota: float = 0.0
    c27_total_devengado: float = 0.0      # 03 + 06 + 09 + 11 + 13
    # --- IVA deducible ---
    c28_base: float = 0.0                 # operaciones interiores corrientes (+ NON_EU_RC)
    c29_cuota: float = 0.0
    c30_base: float = 0.0                 # operaciones interiores con bienes de inversión
    c31_cuota: float = 0.0
    c36_base: float = 0.0                 # adquisiciones intracomunitarias corrientes
    c37_cuota: float = 0.0
    c43_regularizacion_bienes_inversion: float = 0.0  # arts. 107-109 LIVA, Q4 only
    c44_regularizacion_prorrata: float = 0.0          # art. 105 LIVA, Q4 only
    c45_total_deducir: float = 0.0        # 29 + 31 + 37 + 43 + 44
    c46_resultado_regimen_general: float = 0.0  # 27 − 45
    # --- Información adicional ---
    c59_entregas_intracom: float = 0.0    # EU B2B sales
    c60_exportaciones: float = 0.0        # exports of goods (none today, see notes)
    c120_no_sujetas_localizacion: float = 0.0  # non-EU sales not subject (D11)
    # --- Resultado ---
    c64_suma_resultados: float = 0.0      # 46 + 58 + 76 (58/76 not applicable) = 46
    c65_pct_atribuible_estado: float = 100.0
    c66_atribuible_estado: float = 0.0    # 64 × 65 %
    c110_pendiente_anteriores: float = 0.0
    c78_aplicadas_periodo: float = 0.0
    c87_pendiente_posteriores: float = 0.0  # 110 − 78
    c69_resultado_autoliquidacion: float = 0.0  # 66 + 77 − 78 + 68 + 108 (77/68/108 = 0)
    c71_resultado_liquidacion: float = 0.0      # 69 − 70 + 109 (70/109 = 0)
    c72_a_compensar: float = 0.0          # −71 when 71 < 0 and not refunded
    c73_a_devolver: float = 0.0           # −71 when 71 < 0, Q4 refund option
    # --- Context (not boxes) ---
    oss_base: float = 0.0                 # also box 123 (informational) when OSS-registered
    oss_vat: float = 0.0
    exempt_base: float = 0.0              # EXEMPT_TEACHING sales (art. 20.1.9º) — pro-rata denominator
    prorrata_enabled: bool = True
    prorrata_provisional_pct: float = 100.0
    prorrata_provisional_source: str = ""
    prorrata_definitive_pct: Optional[float] = None   # Q4 only
    c46_sin_prorrata: float = 0.0         # "gestor mode": 46 with 100% deduction, no box 44
    c110_source: str = ""                 # filed | app_chain | none
    q4_negative_result: str = "compensate"
    notes: str = ""
    audit: list = field(default_factory=list)  # list[AuditEntry]

    def aeat_boxes(self) -> dict[str, float]:
        """Every modelled box keyed by its AEAT number ("01", "110", …), in form order.

        Interface consumed by the reconciliation view (#100) and the filing
        sheet (#101). Box 123 is the OSS base; it is 0 unless OSS-registered.
        """
        return {box: float(getattr(self, name)) for box, name in MODELO303_BOX_FIELDS.items()}

    @property
    def credit_carry_forward(self) -> float:
        """Credit pending for the next period: 87 + 72 (what next period's 110 will be)."""
        return round(self.c87_pendiente_posteriores + self.c72_a_compensar, 2)

    # Read-only aliases for the pre-#97 field names. No production code reads
    # them any more; only older test modules do (test_invoice_ledger,
    # test_invoice_dedupe, test_invoice_fx, test_stripe_eu_b2c_reclassify,
    # test_database). Remove once those assert the cNN_* fields. The old "box_01" was the 21% row (07/09)
    # and the old "export_base" held the non-EU sales now reported in box 120.
    @property
    def box_01_base(self) -> float:
        return self.c07_base

    @property
    def box_03_cuota(self) -> float:
        return self.c09_cuota

    @property
    def box_59_intracom_entregas(self) -> float:
        return self.c59_entregas_intracom

    @property
    def box_28_base_soportado(self) -> float:
        return self.c28_base

    @property
    def box_29_cuota_soportado(self) -> float:
        return self.c29_cuota

    @property
    def box_46_diferencia(self) -> float:
        return self.c46_resultado_regimen_general

    @property
    def export_base(self) -> float:
        return self.c120_no_sujetas_localizacion


# AEAT Modelo 130 box number -> Modelo130Result field, in form order. Source:
# the AEAT Sede "Modelo 130 — Instrucciones" (section I estimación directa
# 01–07, section II agrícolas 08–11, section III total liquidación 12–19).
MODELO130_BOX_FIELDS: dict[str, str] = {
    "01": "c01_ingresos",
    "02": "c02_gastos",
    "03": "c03_rendimiento_neto",
    "04": "c04_veinte_pct",
    "05": "c05_pagos_anteriores",
    "06": "c06_retenciones",
    "07": "c07_pago_fraccionado",
    "08": "c08_ingresos_agricolas",
    "09": "c09_dos_pct_agricolas",
    "10": "c10_retenciones_agricolas",
    "11": "c11_pago_fraccionado_agricolas",
    "12": "c12_suma_pagos",
    "13": "c13_minoracion",
    "14": "c14_diferencia",
    "15": "c15_negativos_anteriores",
    "16": "c16_deduccion_vivienda",
    "17": "c17_total",
    "18": "c18_complementaria",
    "19": "c19_resultado",
}


@dataclass
class Modelo130Result:
    """Modelo 130 (quarterly IRPF advance) with fields named after the AEAT boxes.

    ``cNN_*`` fields hold the value to type into box NN (year-to-date where the
    form says so); ``aeat_boxes()`` returns them keyed by the printed box
    number. Only 04 and 12 are floored at 0 — the form lets 03, 07, 14, 17 and
    19 be negative. The remaining fields are context, not boxes.
    """
    year: int
    quarter: int
    # --- I. Actividades económicas en estimación directa (YTD) ---
    c01_ingresos: float = 0.0             # ingresos computables YTD
    c02_gastos: float = 0.0               # gastos reales + 5% difícil justificación
    c03_rendimiento_neto: float = 0.0     # 01 − 02 (negative allowed)
    c04_veinte_pct: float = 0.0           # 20% × max(0, 03)
    c05_pagos_anteriores: float = 0.0     # Σ positive 07 − Σ 16 of earlier quarters of the year
    c06_retenciones: float = 0.0          # retenciones e ingresos a cuenta YTD
    c07_pago_fraccionado: float = 0.0     # 04 − 05 − 06 (negative allowed)
    # --- II. Actividades agrícolas, ganaderas, forestales y pesqueras (not used) ---
    c08_ingresos_agricolas: float = 0.0
    c09_dos_pct_agricolas: float = 0.0
    c10_retenciones_agricolas: float = 0.0
    c11_pago_fraccionado_agricolas: float = 0.0
    # --- III. Total liquidación ---
    c12_suma_pagos: float = 0.0           # max(0, 07 + 11)
    c13_minoracion: float = 0.0           # art. 110.3.c RIRPF, by the previous year's net yield
    c14_diferencia: float = 0.0           # 12 − 13 (negative allowed)
    c15_negativos_anteriores: float = 0.0  # unused negative 19s of the year, ≤ positive 14
    c16_deduccion_vivienda: float = 0.0   # housing-loan deduction (not applicable)
    c17_total: float = 0.0                # 14 − 15 − 16 (negative allowed)
    c18_complementaria: float = 0.0       # complementary return only
    c19_resultado: float = 0.0            # 17 − 18 (negative allowed; carried into 15 later)
    # --- Context (not boxes) ---
    gastos_reales: float = 0.0            # 02 without the 5% allowance
    gastos_dificil_justificacion: float = 0.0  # the 5% allowance included in 02
    previous_year_net_yield: Optional[float] = None
    previous_year_net_source: str = ""    # filed | config | app
    c05_source: str = ""                  # filed | app_chain | mixed | none
    negativos_pendientes_anteriores: float = 0.0   # unused negative 19s before this quarter
    negativos_pendientes_posteriores: float = 0.0  # left for the next quarters of the year
    notes: str = ""
    audit: list = field(default_factory=list)  # list[AuditEntry]

    def aeat_boxes(self) -> dict[str, float]:
        """Boxes "01".."19" keyed as printed on the AEAT form, in form order.

        Interface consumed by the reconciliation view (#100) and the filing
        sheet (#101). Boxes 08–11, 16 and 18 are always 0 for this taxpayer.
        """
        return {box: float(getattr(self, name)) for box, name in MODELO130_BOX_FIELDS.items()}


@dataclass
class Modelo349Row:
    """One 349 operator line: an EU VAT id and operation key with its summed base (EUR)."""
    key: str             # clave de operación: "I" acquisitions of services, "S" services supplied
    country: str         # VAT id country prefix ("IE", "SE", "EL", …)
    vat_id: str          # full normalised VAT id, country prefix included ("" when unknown)
    name: str
    base: float
    n_records: int = 0   # invoices / Stripe charges summed into this line


@dataclass
class Modelo349Result:
    """Modelo 349 (intra-EU operations, quarterly) — keys I and S, no rectifications.

    ``rows`` are the declarable operator lines; ``excluded`` holds operators whose
    quarter total is zero or negative and ``unidentified`` the lines without a VAT
    id — neither can be declared, both are listed so they can be fixed.
    """
    year: int
    quarter: int
    rows: list[Modelo349Row] = field(default_factory=list)
    total: float = 0.0                    # box 02
    excluded: list[Modelo349Row] = field(default_factory=list)
    unidentified: list[Modelo349Row] = field(default_factory=list)
    notes: str = ""
    audit: list = field(default_factory=list)  # list[AuditEntry]

    def aeat_boxes(self) -> dict[str, float]:
        """Summary boxes of the form: 01/02 operators and amount, 03/04 rectifications
        (always 0 — rectification lines are out of scope)."""
        return {"01": float(len(self.rows)), "02": round(float(self.total), 2), "03": 0.0, "04": 0.0}

    def operators(self) -> list[dict]:
        """Declarable operator lines as the form lists them: ``vat_id`` is the number
        without the ``country`` prefix (same shape as the filed 349 operators)."""
        return [{"country": r.country, "vat_id": r.vat_id[len(r.country):], "name": r.name,
                 "key": r.key, "base": r.base} for r in self.rows]


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
class EUB2CThresholdResult:
    """Year-to-date EU B2C distance sales vs the art. 73 LIVA threshold.

    ``ytd_base_eur`` / ``previous_year_base_eur`` are ex-VAT bases of rows whose
    VAT treatment is EU B2C (``EU_B2C_ES21`` or ``OSS_EU``). The threshold is
    exceeded when either the current or the previous calendar year passes
    ``limit_eur``; from then on EU consumers must be charged destination VAT.
    """
    year: int
    quarter: int
    limit_eur: float
    warn_ratio: float
    ytd_base_eur: float = 0.0
    previous_year_base_eur: float = 0.0
    n_transactions: int = 0
    by_country: dict[str, float] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return self.ytd_base_eur / self.limit_eur if self.limit_eur else 0.0

    @property
    def status(self) -> Literal["OK", "WARNING", "EXCEEDED"]:
        if self.ytd_base_eur > self.limit_eur or self.previous_year_base_eur > self.limit_eur:
            return "EXCEEDED"
        if self.ratio >= self.warn_ratio:
            return "WARNING"
        return "OK"

    @property
    def message(self) -> str:
        head = (
            f"EU B2C sales {self.year} YTD (to Q{self.quarter}): €{self.ytd_base_eur:,.2f} "
            f"of €{self.limit_eur:,.0f} ({self.ratio:.0%})"
        )
        if self.status == "EXCEEDED":
            return (
                f"{head} — threshold exceeded (previous year €{self.previous_year_base_eur:,.2f}). "
                f"EU consumers must now be charged destination-country VAT (OSS or local registration)."
            )
        if self.status == "WARNING":
            return f"{head} — at or above {self.warn_ratio:.0%} of the art. 73 LIVA threshold."
        return head


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
    cell: str             # field name, e.g. "c07_base"
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
