"""Modelo 390 (annual VAT summary) built from the four quarterly Modelo 303 results (#103).

The 390 is not a sum of four forms: it re-states the year's operations with a
per-rate breakdown, the "sin prorratear" bases, the annual result, the
compensation chain and the volume of operations. Every figure here is derived
from the four ``compute_modelo_303`` results of the year (boxes and their audit
records), so the 390 can never drift from the quarterly returns it summarises.

Box numbering and formulas follow the AEAT "Modelo 390. Instrucciones para
cumplimentar el modelo" (sede.agenciatributaria.gob.es, Procedimiento G412,
``instr390.pdf`` — layout valid from ejercicio 2024, when the 2 % and 7.5 %
rows were added) and the form's printed formulas (Anexo I, modelo 390):

- IVA devengado, régimen ordinario, per rate 0/2/4/5/7.5/10/21 %:
  700/701, 667/668, **01/02 (4 %)**, 702/703, 669/670, **03/04 (10 %)**, **05/06 (21 %)**.
- Adquisiciones intracomunitarias de servicios, same rate order:
  720/721, 687/688, **545/546 (4 %)**, 722/723, 689/690, **547/548 (10 %)**, **551/552 (21 %)**.
- 27/28 IVA devengado en otros supuestos de inversión del sujeto pasivo (art. 84.Uno.2º/4º LIVA).
- 33/34 total bases/cuotas; 47 = 34 + recargo de equivalencia (none here).
- IVA deducible, bases "sin prorratear", cuotas after the pro-rata, per rate 2/4/5/7.5/10/21 %:
  interiores corrientes 695/696, **190/191 (4 %)**, 724/725, 697/698, **603/604 (10 %)**,
  **605/606 (21 %)**, total **48/49**; bienes de inversión 749/750, **196/197**, 728/729,
  751/752, **611/612**, **613/614**, total **50/51**; adquisiciones intracomunitarias de
  servicios 773/774, **587/588**, 740/741, 775/776, **635/636**, **637/638**, total **597/598**.
- 63 regularización bienes de inversión; 522 regularización por prorrata definitiva.
- 64 = 49 + 513 + 51 + 521 + 53 + 55 + 57 + 59 + 598 + 61 + 661 + 62 + 652 + 63 + 522;
  65 = 47 − 64; 84 = 65 + 83 + 658; 86 = 84 + 659 − 85.
- 85 compensación de cuotas del ejercicio anterior (credit of earlier years applied via 303 box 78,
  from a FIFO walk of the quarters — credit generated inside the year never counts here);
  95 total a ingresar in the year's returns; 97 a compensar / 98 a devolver of the last return;
  662 credit generated in an earlier period of the year and not applied by year end.
- Volumen de operaciones: 99 régimen general; 103 entregas intracomunitarias de bienes y
  servicios; 104 exportaciones y otras exentas con derecho a deducción; 105 exentas sin derecho
  a deducción; 110 no sujetas por reglas de localización (services to non-EU customers — the
  303's box 120, D11); 126 no sujetas acogidas a ventanilla única (OSS);
  108 = 99 + 653 + 103 + 104 + 105 + 110 + 100 + 101 + 102 + 125 + 126 + 127 + 128 + 227 + 228 − 106 − 107.
- Prorratas (only when exempt operations exist): 115 importe total de las operaciones,
  116 operaciones con derecho a deducción, 117 tipo ("G" general), 118 % definitivo.

Rows this taxpayer never uses (other rates, intragroup, criterio de caja,
recargo de equivalencia, imports, special regimes, rectifications) are 0 and
not modelled. Reverse-charge purchases with ``INTRA_EU_RC`` are services
(Modelo 349 key I), so they go to the intra-EU *services* rows.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from src.logger import get_logger
from src.tax_engine import compute_modelo_303, load_app_config, prorrata_pct
from src.tax_models import AuditEntry, Modelo303Result

log = get_logger(__name__)

RATES: tuple[float, ...] = (4.0, 10.0, 21.0)
DEFAULT_RATE = 21.0

# (base box, cuota box) per rate, per section — AEAT instr390, section 5.
DEVENGADO_ORDINARIO: dict[float, tuple[str, str]] = {4.0: ("01", "02"), 10.0: ("03", "04"), 21.0: ("05", "06")}
DEVENGADO_AIC_SERVICIOS: dict[float, tuple[str, str]] = {
    4.0: ("545", "546"), 10.0: ("547", "548"), 21.0: ("551", "552")}
DEDUCIBLE_INTERIORES: dict[float, tuple[str, str]] = {
    4.0: ("190", "191"), 10.0: ("603", "604"), 21.0: ("605", "606")}
DEDUCIBLE_INVERSION: dict[float, tuple[str, str]] = {
    4.0: ("196", "197"), 10.0: ("611", "612"), 21.0: ("613", "614")}
DEDUCIBLE_AIC_SERVICIOS: dict[float, tuple[str, str]] = {
    4.0: ("587", "588"), 10.0: ("635", "636"), 21.0: ("637", "638")}

# Every modelled box, in form order, with its short Spanish label.
MODELO390_LABELS: dict[str, str] = {
    "01": "Régimen ordinario 4 % — base", "02": "Régimen ordinario 4 % — cuota",
    "03": "Régimen ordinario 10 % — base", "04": "Régimen ordinario 10 % — cuota",
    "05": "Régimen ordinario 21 % — base", "06": "Régimen ordinario 21 % — cuota",
    "545": "Adq. intracomunitarias de servicios 4 % — base", "546": "Adq. intracomunitarias de servicios 4 % — cuota",
    "547": "Adq. intracomunitarias de servicios 10 % — base", "548": "Adq. intracomunitarias de servicios 10 % — cuota",
    "551": "Adq. intracomunitarias de servicios 21 % — base", "552": "Adq. intracomunitarias de servicios 21 % — cuota",
    "27": "Otros supuestos de inversión del sujeto pasivo — base",
    "28": "Otros supuestos de inversión del sujeto pasivo — cuota",
    "33": "Total bases IVA devengado", "34": "Total cuotas IVA devengado",
    "47": "Total cuotas IVA y recargo de equivalencia",
    "190": "Deducible interiores corrientes 4 % — base", "191": "Deducible interiores corrientes 4 % — cuota",
    "603": "Deducible interiores corrientes 10 % — base", "604": "Deducible interiores corrientes 10 % — cuota",
    "605": "Deducible interiores corrientes 21 % — base", "606": "Deducible interiores corrientes 21 % — cuota",
    "48": "Deducible interiores corrientes — total base", "49": "Deducible interiores corrientes — total cuota",
    "196": "Deducible bienes de inversión 4 % — base", "197": "Deducible bienes de inversión 4 % — cuota",
    "611": "Deducible bienes de inversión 10 % — base", "612": "Deducible bienes de inversión 10 % — cuota",
    "613": "Deducible bienes de inversión 21 % — base", "614": "Deducible bienes de inversión 21 % — cuota",
    "50": "Deducible bienes de inversión — total base", "51": "Deducible bienes de inversión — total cuota",
    "587": "Deducible adq. intracom. servicios 4 % — base", "588": "Deducible adq. intracom. servicios 4 % — cuota",
    "635": "Deducible adq. intracom. servicios 10 % — base", "636": "Deducible adq. intracom. servicios 10 % — cuota",
    "637": "Deducible adq. intracom. servicios 21 % — base", "638": "Deducible adq. intracom. servicios 21 % — cuota",
    "597": "Deducible adq. intracom. servicios — total base",
    "598": "Deducible adq. intracom. servicios — total cuota",
    "63": "Regularización bienes de inversión", "522": "Regularización por prorrata definitiva",
    "64": "Suma de deducciones", "65": "Resultado régimen general (47 − 64)",
    "84": "Suma de resultados", "85": "Compensación de cuotas del ejercicio anterior",
    "86": "Resultado de la liquidación (84 − 85)",
    "95": "Total resultados a ingresar en las autoliquidaciones del ejercicio",
    "97": "A compensar (última autoliquidación)", "98": "A devolver (última autoliquidación)",
    "662": "Cuotas pendientes de compensación generadas en el ejercicio",
    "99": "Operaciones en régimen general", "103": "Entregas intracomunitarias de bienes y servicios",
    "104": "Exportaciones y otras operaciones exentas con derecho a deducción",
    "105": "Operaciones exentas sin derecho a deducción",
    "110": "Operaciones no sujetas por reglas de localización",
    "126": "No sujetas por localización acogidas a ventanilla única (OSS)",
    "108": "Volumen de operaciones",
    "115": "Prorrata — importe total de las operaciones",
    "116": "Prorrata — operaciones con derecho a deducción",
    "118": "Prorrata — % definitivo",
}

# Per-rate sections split from the 303 audit records: key -> (label, rows).
_SECTIONS: dict[str, tuple[str, dict[float, tuple[str, str]]]] = {
    "aic_dev": ("accrued intra-EU services (545-552)", DEVENGADO_AIC_SERVICIOS),
    "interiores": ("deductible current domestic (190-606)", DEDUCIBLE_INTERIORES),
    "inversion": ("deductible capital goods (196-614)", DEDUCIBLE_INVERSION),
    "aic_ded": ("deductible intra-EU services (587-638)", DEDUCIBLE_AIC_SERVICIOS),
}
# Box -> (section key, rate) for the audit trail.
_SECTION_OF: dict[str, tuple[str, float]] = {
    box: (key, rate) for key, (_, rows) in _SECTIONS.items() for rate, pair in rows.items() for box in pair
}


@dataclass
class Modelo390Result:
    """Modelo 390 for one year. ``boxes`` holds every modelled box keyed as printed."""
    year: int
    boxes: dict[str, float] = field(default_factory=dict)
    prorrata_applies: bool = False
    prorrata_type: str = ""                        # box 117: "G" general pro-rata
    prorrata_definitive_pct: Optional[float] = None
    quarters: list[Modelo303Result] = field(default_factory=list)
    notes: str = ""
    audit: list = field(default_factory=list)      # list[AuditEntry]

    def aeat_boxes(self) -> dict[str, float]:
        """Every modelled box keyed by its AEAT number, in form order.

        Same interface as the quarterly results' ``aeat_boxes()`` (consumed by
        the reconciliation view and the annual pack). The pro-rata boxes
        115/116/118 are present only when the pro-rata applies.
        """
        return dict(self.boxes)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nearest_rate(pct: Optional[float]) -> float:
    """The modelled Spanish rate closest to ``pct`` (21 % when unknown)."""
    if pct is None or pct <= 0:
        return DEFAULT_RATE
    return min(RATES, key=lambda r: abs(r - pct))


def _rate_of(amount_vat: Any, amount_base: Any) -> float:
    try:
        base, vat = float(amount_base or 0.0), float(amount_vat or 0.0)
    except (TypeError, ValueError):
        return DEFAULT_RATE
    return _nearest_rate(abs(vat / base) * 100.0) if base else DEFAULT_RATE


def _rate_of_record(rec: dict) -> tuple[float, bool, Optional[float]]:
    """(modelled rate, used the VAT ÷ base ratio as a fallback, raw invoice rate %).

    Prefers the invoice's own stored ``iva_rate`` (#137) over inferring the rate
    from VAT ÷ base — a blended-rate bill or OCR/rounding mismatch between
    ``iva_amount`` and ``subtotal_eur`` can point the ratio at the wrong row.
    The ratio is only a fallback for records without a stored rate (older rows,
    non-invoice entries), and that fallback is counted by the caller.
    """
    raw = rec.get("iva_rate")
    if raw is not None:
        pct = _f(raw)
        return _nearest_rate(pct), False, pct
    return _rate_of(rec.get("iva_amount"), rec.get("subtotal_eur")), True, None


def _audit_inputs(result: Modelo303Result, cell: str) -> dict[str, Any]:
    for entry in result.audit or []:
        if entry.cell == cell:
            try:
                return json.loads(entry.inputs_json or "{}")
            except ValueError:
                return {}
    return {}


def _f(v: Any) -> float:
    try:
        return float(v or 0.0)
    except (TypeError, ValueError):
        return 0.0


# (rate, base weight, cuota weight, ratio fallback used?, raw rate % if any) of
# one audit record, per section. The last two elements are read by add_quarter
# to count fallbacks and off-rate (non 4/10/21 %) records for the audit (#137);
# sections whose rate is always explicit (rate_pct/manual) omit them.
def _weights_interiores(rec: dict) -> tuple:
    pct = _f(rec.get("deductible_pct_vat", 100.0)) / 100.0
    if rec.get("source") == "manual_entry":
        return _nearest_rate(rec.get("vat_rate")), _f(rec.get("base")), _f(rec.get("cuota"))
    if rec.get("source") == "platform_fee":   # Stripe application fee, NON_EU_RC deducted at 100% (#147)
        fee, rate = _f(rec.get("fee_application")), _f(rec.get("rate_pct"))
        return _nearest_rate(rate), fee, fee * rate / 100.0
    if "rate_pct" in rec:   # NON_EU_RC self-assessed, deducted with the current domestic operations
        return _nearest_rate(_f(rec["rate_pct"])), _f(rec.get("subtotal_eur")) * pct, _f(rec.get("deductible_cuota"))
    rate, fallback, raw = _rate_of_record(rec)
    return rate, _f(rec.get("subtotal_eur")) * pct, _f(rec.get("c29_cuota")), fallback, raw


def _weights_inversion(rec: dict) -> tuple:
    if rec.get("source") == "fixed_asset":
        return (_rate_of(rec.get("vat_eur"), rec.get("base_eur")),
                _f(rec.get("box_30_base")), _f(rec.get("box_31_cuota")))
    pct = _f(rec.get("deductible_pct_vat", 100.0)) / 100.0
    rate, fallback, raw = _rate_of_record(rec)
    return rate, _f(rec.get("subtotal_eur")) * pct, _f(rec.get("c31_cuota")), fallback, raw


def _weights_aic_devengado(rec: dict) -> tuple[float, float, float]:
    return _nearest_rate(_f(rec.get("rate_pct"))), _f(rec.get("subtotal_eur")), _f(rec.get("self_assessed_cuota"))


def _weights_aic_deducible(rec: dict) -> tuple[float, float, float]:
    pct = _f(rec.get("deductible_pct_vat", 100.0)) / 100.0
    return (_nearest_rate(_f(rec.get("rate_pct"))), _f(rec.get("subtotal_eur")) * pct,
            _f(rec.get("deductible_cuota")))


@dataclass
class _RateSplit:
    """Unrounded per-rate (base, cuota) of one 390 section, summed over the quarters."""
    base: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    cuota: dict[float, float] = field(default_factory=lambda: defaultdict(float))
    total_base: float = 0.0
    total_cuota: float = 0.0
    fallback_quarters: list[int] = field(default_factory=list)
    rate_fallback_count: int = 0                             # records with no stored iva_rate (#137)
    off_rate_values: set[float] = field(default_factory=set)  # stored rates not in RATES, reported not forced

    def add_quarter(self, quarter: int, weights: Iterable[tuple],
                    total_base: float, total_cuota: float) -> None:
        """Spread one quarter's totals over the rates in proportion to its records."""
        weights = list(weights)
        self.total_base += total_base
        self.total_cuota += total_cuota
        for w in weights:
            if len(w) > 3 and w[3]:
                self.rate_fallback_count += 1
            if len(w) > 4 and w[4] is not None and w[4] not in RATES:
                self.off_rate_values.add(w[4])
        for idx, total, bucket in ((1, total_base, self.base), (2, total_cuota, self.cuota)):
            if not total:
                continue
            wsum = sum(w[idx] for w in weights)
            if abs(wsum) < 1e-9:
                bucket[DEFAULT_RATE] += total
                if quarter not in self.fallback_quarters:
                    self.fallback_quarters.append(quarter)
                continue
            for w in weights:
                bucket[w[0]] += total * w[idx] / wsum

    def rounded(self) -> tuple[dict[float, float], dict[float, float], float, float]:
        """Per-rate bases/cuotas rounded so that they add up exactly to the rounded totals."""
        tb, tc = round(self.total_base, 2), round(self.total_cuota, 2)
        return _round_to_total(self.base, tb), _round_to_total(self.cuota, tc), tb, tc


@dataclass
class _CreditWalk:
    """The year's 303 compensation chain split into credit of earlier years and of this year."""
    carried_in: float                  # Q1 box 110: credit pending from earlier years
    carried_in_source: str             # Q1 c110_source: filed | app_chain | none
    sum_box_78: float
    applied_earlier_years: float       # 390 box 85
    pending_earlier_years: float       # credit of earlier years still pending after Q4
    steps: list[dict] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _walk_credit(qs: list[Modelo303Result]) -> _CreditWalk:
    """FIFO walk of the credit to compensate over the year's quarters (#136).

    The credit carried in from earlier years (Q1 box 110) is consumed first by
    each quarter's box 78; credit generated inside the year (a quarter's box 72)
    only feeds the later 110s and never counts in box 85. The earlier-years part
    is capped at each quarter's own 110, so when a later 110 shrinks (it chains
    from a filed return that diverges from the app's quarter) intra-year credit
    applied afterwards is not mistaken for credit of earlier years. Such chain
    breaks, and a carried-in credit taken from the app's own chain instead of a
    filed return, are flagged.
    """
    first = qs[0]
    carried_in = round(first.c110_pendiente_anteriores, 2)
    walk = _CreditWalk(carried_in=carried_in, carried_in_source=first.c110_source,
                       sum_box_78=round(sum(r.c78_aplicadas_periodo for r in qs), 2),
                       applied_earlier_years=0.0, pending_earlier_years=0.0)
    if carried_in and first.c110_source == "app_chain":
        walk.flags.append("carried_in_from_app_chain")
        walk.notes.append(
            f"Box 85: the credit carried in from {first.year - 1} (Q1 box 110 = {carried_in:.2f}) comes from "
            f"the app's own chain, not a filed return — import the {first.year - 1} 4T 303 receipt to confirm it.")
    earlier, applied, prev = carried_in, 0.0, None
    for r in qs:
        box_110 = round(r.c110_pendiente_anteriores, 2)
        if prev is not None and abs(box_110 - prev.credit_carry_forward) >= 0.005:
            walk.flags.append(f"chain_break_Q{r.quarter}")
            walk.notes.append(
                f"Box 85/662: Q{r.quarter} box 110 ({box_110:.2f}) differs from Q{prev.quarter} 87 + 72 "
                f"({prev.credit_carry_forward:.2f}) — a filed return diverges from the app; the credit of "
                "earlier years is capped at each quarter's 110. Check against the filed 303s.")
        earlier = round(max(0.0, min(earlier, box_110)), 2)
        from_earlier = round(min(earlier, max(0.0, r.c78_aplicadas_periodo)), 2)
        walk.steps.append({
            "quarter": f"Q{r.quarter}", "box_110": box_110, "earlier_years_in_110": earlier,
            "box_78": round(r.c78_aplicadas_periodo, 2), "from_earlier_years": from_earlier,
            "from_this_year": round(r.c78_aplicadas_periodo - from_earlier, 2),
            "box_72": round(r.c72_a_compensar, 2)})
        earlier = round(earlier - from_earlier, 2)
        applied += from_earlier
        prev = r
    walk.applied_earlier_years = round(applied, 2)
    walk.pending_earlier_years = earlier
    return walk


def _round_to_total(parts: dict[float, float], total: float) -> dict[float, float]:
    out = {r: round(parts.get(r, 0.0), 2) for r in RATES}
    residual = round(total - sum(out.values()), 2)
    if residual:
        largest = max(RATES, key=lambda r: (abs(out[r]), r))
        out[largest] = round(out[largest] + residual, 2)
    return out


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def compute_modelo_390(
    year: int, db_conn: sqlite3.Connection, config: Optional[dict] = None,
    *, quarters: Optional[list[Modelo303Result]] = None,
) -> Modelo390Result:
    """Compute the Modelo 390 of ``year`` from its four Modelo 303 results.

    ``quarters`` may pass the four already-computed 303 results (Q1..Q4) to
    avoid recomputing them; otherwise they are computed with ``config``
    (the app config when ``None``).
    """
    if config is None:
        config = load_app_config()
    qs = quarters if quarters is not None else [compute_modelo_303(year, q, db_conn, config) for q in range(1, 5)]
    if [r.quarter for r in qs] != [1, 2, 3, 4] or any(r.year != year for r in qs):
        raise ValueError(f"Modelo 390 {year} needs the four 303 results of {year}, Q1..Q4")

    result = Modelo390Result(year=year, quarters=list(qs))
    b: dict[str, float] = {}
    notes: list[str] = []
    per_q: dict[str, list[float]] = {}   # audit: quarterly contributions per box

    def _qsum(box: str, getter) -> float:
        vals = [round(float(getter(r)), 2) for r in qs]
        per_q[box] = vals
        return round(sum(vals), 2)

    # --- IVA devengado ------------------------------------------------------
    for rate, (fb, fc) in DEVENGADO_ORDINARIO.items():
        base_f, cuota_f = {4.0: ("c01_base", "c03_cuota"), 10.0: ("c04_base", "c06_cuota"),
                           21.0: ("c07_base", "c09_cuota")}[rate]
        b[fb] = _qsum(fb, lambda r, n=base_f: getattr(r, n))
        b[fc] = _qsum(fc, lambda r, n=cuota_f: getattr(r, n))

    sections = {key: (_RateSplit(), rows) for key, (_, rows) in _SECTIONS.items()}
    for r in qs:
        c10 = _audit_inputs(r, "c10_base").get("records", [])
        in28 = _audit_inputs(r, "c28_base")
        in30 = _audit_inputs(r, "c30_base")
        in36 = _audit_inputs(r, "c36_base")
        sections["aic_dev"][0].add_quarter(
            r.quarter, map(_weights_aic_devengado, c10), r.c10_base, r.c11_cuota)
        # Deductible bases "sin prorratear" (base_100 of the 303 audit), cuotas after the pro-rata.
        sections["interiores"][0].add_quarter(
            r.quarter, map(_weights_interiores, in28.get("records", [])),
            _f(in28.get("base_100", r.c28_base)), r.c29_cuota)
        sections["inversion"][0].add_quarter(
            r.quarter, map(_weights_inversion, in30.get("records", [])),
            _f(in30.get("base_100", r.c30_base)), r.c31_cuota)
        sections["aic_ded"][0].add_quarter(
            r.quarter, map(_weights_aic_deducible, c10),
            _f(in36.get("base_100", r.c36_base)), r.c37_cuota)

    totals: dict[str, tuple[float, float]] = {}
    for key, (split, rows) in sections.items():
        bases, cuotas, tb, tc = split.rounded()
        for rate, (fb, fc) in rows.items():
            b[fb], b[fc] = bases[rate], cuotas[rate]
        totals[key] = (tb, tc)
        if split.fallback_quarters:
            notes.append(f"{_SECTIONS[key][0]}: no per-rate records in "
                         f"Q{', Q'.join(map(str, split.fallback_quarters))} — amounts put in the 21 % row.")
        if split.rate_fallback_count:
            notes.append(f"{_SECTIONS[key][0]}: {split.rate_fallback_count} record(s) had no stored VAT "
                         "rate — the rate was inferred from VAT ÷ base instead.")
        if split.off_rate_values:
            rates_str = ", ".join(f"{r:g} %" for r in sorted(split.off_rate_values))
            notes.append(f"{_SECTIONS[key][0]}: record(s) at {rates_str} (not 4/10/21 %) were placed "
                         "in the nearest modelled row.")

    b["27"] = _qsum("27", lambda r: r.c12_base)
    b["28"] = _qsum("28", lambda r: r.c13_cuota)
    b["33"] = round(b["01"] + b["03"] + b["05"] + b["545"] + b["547"] + b["551"] + b["27"], 2)
    b["34"] = round(b["02"] + b["04"] + b["06"] + b["546"] + b["548"] + b["552"] + b["28"], 2)
    b["47"] = b["34"]   # no recargo de equivalencia

    # --- IVA deducible --------------------------------------------------------
    b["48"], b["49"] = totals["interiores"]
    b["50"], b["51"] = totals["inversion"]
    b["597"], b["598"] = totals["aic_ded"]
    b["63"] = _qsum("63", lambda r: r.c43_regularizacion_bienes_inversion)
    b["522"] = _qsum("522", lambda r: r.c44_regularizacion_prorrata)
    b["64"] = round(b["49"] + b["51"] + b["598"] + b["63"] + b["522"], 2)
    b["65"] = round(b["47"] - b["64"], 2)

    # --- Resultado de la liquidación -----------------------------------------
    # 85: credit of earlier years applied this year; 662: credit generated in the year still
    # pending after Q4 (FIFO walk over the quarters, see _walk_credit).
    walk = _walk_credit(qs)
    notes.extend(walk.notes)
    if walk.flags:
        log.warning("⚠️ Modelo 390 %s: credit chain flags %s — check boxes 85/662 against the filed 303s",
                    year, ", ".join(walk.flags))
    b["84"] = b["65"]
    b["85"] = walk.applied_earlier_years
    b["86"] = round(b["84"] - b["85"], 2)
    b["95"] = round(sum(max(0.0, r.c71_resultado_liquidacion) for r in qs), 2)
    q4 = qs[3]
    b["97"] = round(q4.c72_a_compensar, 2)
    b["98"] = round(q4.c73_a_devolver, 2)
    # 662: credit generated in an earlier period of the year and still pending after the last
    # period — Q4's 87 minus what is left of the credit carried in from earlier years.
    b["662"] = round(max(0.0, q4.c87_pendiente_posteriores - walk.pending_earlier_years), 2)

    # --- Volumen de operaciones ---------------------------------------------
    b["99"] = round(b["01"] + b["03"] + b["05"], 2)
    b["103"] = _qsum("103", lambda r: r.c59_entregas_intracom)
    b["104"] = _qsum("104", lambda r: r.c60_exportaciones)
    b["105"] = _qsum("105", lambda r: r.exempt_base)
    b["110"] = _qsum("110", lambda r: r.c120_no_sujetas_localizacion)
    b["126"] = _qsum("126", lambda r: r.oss_base)
    b["108"] = round(b["99"] + b["103"] + b["104"] + b["105"] + b["110"] + b["126"], 2)

    # --- Prorratas -------------------------------------------------------------
    with_right = sum(r.c01_base + r.c04_base + r.c07_base + r.c59_entregas_intracom + r.c60_exportaciones
                     + r.c120_no_sujetas_localizacion + r.oss_base for r in qs)
    exempt = sum(r.exempt_base for r in qs)
    if exempt > 0 and q4.prorrata_enabled:
        definitive = q4.prorrata_definitive_pct
        if definitive is None:
            definitive = prorrata_pct(with_right, exempt) or 100.0
        result.prorrata_applies = True
        result.prorrata_type = "G"
        result.prorrata_definitive_pct = float(definitive)
        b["115"] = round(with_right + exempt, 2)
        b["116"] = round(with_right, 2)
        b["118"] = float(definitive)
        notes.append(f"General pro-rata (box 117 = G): definitive {definitive:.0f}% — also the "
                     f"provisional % of {year + 1}.")
    elif exempt > 0:
        notes.append("Exempt operations without right to deduct exist but the pro-rata is disabled "
                     "(tax.prorrata.enabled = false): section 12 left blank.")

    if b["110"]:
        notes.append("Box 110 holds services to non-EU customers not subject by location rules "
                     "(the 303's box 120, D11); the external accountant used box 104 for them.")
    order = {box: i for i, box in enumerate(MODELO390_LABELS)}
    result.boxes = {k: b[k] for k in sorted(b, key=lambda k: order.get(k, len(order)))}
    result.notes = " ".join(notes)
    result.audit = _audit(result, per_q, {k: s for k, (s, _) in sections.items()}, walk)
    log.info("ℹ️ Modelo 390 %s: 33=%.2f 34=%.2f 64=%.2f 65=%.2f 86=%.2f 108=%.2f prorrata=%s",
             year, b["33"], b["34"], b["64"], b["65"], b["86"], b["108"],
             result.prorrata_definitive_pct if result.prorrata_applies else "n/a")
    return result


_FORMULAS: dict[str, str] = {
    "33": "01 + 03 + 05 + 545 + 547 + 551 + 27", "34": "02 + 04 + 06 + 546 + 548 + 552 + 28",
    "47": "34 (no recargo de equivalencia)",
    "48": "Σ quarterly 303 box 28 base before pro-rata (base_100)", "49": "Σ quarterly 303 box 29",
    "50": "Σ quarterly 303 box 30 base before pro-rata (base_100)", "51": "Σ quarterly 303 box 31",
    "597": "Σ quarterly 303 box 36 base before pro-rata (base_100)", "598": "Σ quarterly 303 box 37",
    "63": "Σ quarterly 303 box 43", "522": "Σ quarterly 303 box 44 (Q4 pro-rata regularisation)",
    "64": "49 + 51 + 598 + 63 + 522", "65": "47 − 64", "84": "65 (+ 83 + 658, not applicable)",
    "85": "FIFO walk Q1..Q4: each 303 box 78 consumes first the credit of earlier years (Q1 box 110, "
          "capped at each quarter's box 110); Σ of that part",
    "86": "84 − 85", "95": "Σ max(0, 303 box 71)", "97": "Q4 303 box 72", "98": "Q4 303 box 73",
    "662": "max(0, Q4 303 box 87 − credit of earlier years still pending after Q4 (FIFO walk))",
    "99": "01 + 03 + 05 (taxed sales bases)", "103": "Σ 303 box 59", "104": "Σ 303 box 60",
    "105": "Σ EXEMPT_TEACHING sales (art. 20.1.9º LIVA)", "110": "Σ 303 box 120",
    "126": "Σ OSS base", "108": "99 + 103 + 104 + 105 + 110 + 126",
    "115": "Σ operations with right to deduct + exempt operations", "116": "Σ operations with right to deduct",
    "118": "art. 104 LIVA definitive % (Q4 303), rounded up to the unit",
}


def _audit(r: Modelo390Result, per_q: dict[str, list[float]], splits: dict[str, _RateSplit],
           walk: _CreditWalk) -> list[AuditEntry]:
    """One audit entry per box (cell ``cNN``, so the reconciliation drill-down finds it)."""
    out = []
    for box, value in r.boxes.items():
        inputs: dict[str, Any] = {}
        if box in per_q:
            inputs["quarters"] = dict(zip(("Q1", "Q2", "Q3", "Q4"), per_q[box]))
            formula = f"Σ of the four quarterly 303 results ({box})"
        elif box in _SECTION_OF:
            section, rate = _SECTION_OF[box]
            formula = (f"{rate:g} % share of the section total, split by the rate of each 303 audit record "
                       "and rounded so the rates add up to the total")
            inputs["section"] = section
            split = splits.get(section)
            if split is not None:
                if split.rate_fallback_count:
                    inputs["rate_fallbacks"] = split.rate_fallback_count
                if split.off_rate_values:
                    inputs["off_rates_pct"] = sorted(split.off_rate_values)
        else:
            formula = _FORMULAS.get(box, "")
        if box in ("85", "662"):
            inputs.update(q1_box_110=walk.carried_in, carried_in_source=walk.carried_in_source,
                          sum_box_78=walk.sum_box_78,
                          earlier_years_pending_after_q4=walk.pending_earlier_years,
                          walk=walk.steps, flags=walk.flags)
        if box in ("97", "98", "662"):
            inputs["q4_box_87"] = r.quarters[3].c87_pendiente_posteriores
        out.append(AuditEntry.of("390", r.year, 0, f"c{box}", f"{box} {MODELO390_LABELS.get(box, '')}".strip(),
                                 _FORMULAS.get(box, formula), value, **inputs))
    return out
