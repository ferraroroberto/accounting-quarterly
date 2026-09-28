"""Filing sheet and immutable filed snapshots (#101).

The filing sheet lists, per quarter, exactly what to type into each AEAT form
— Modelo 303 boxes in form order, Modelo 130 boxes 01–19, the Modelo 349
summary and operator list — with the 303 credit chain, each model's result
and its deadlines. It is built from the **stored** snapshots
(``tax_computation_snapshots``, latest version per model), never from a live
recompute, so it shows what was computed (or filed) and nothing else.

``mark_filed`` freezes the latest computed snapshot as a new FILED version
(``snapshot_version`` + 1) carrying the AEAT justificante and presentation
date. FILED rows are immutable (SQLite triggers in ``src/database.py``); a
later recompute stores a new COMPUTED version, which the sheet diffs against
the filed one.

Deadlines (``filing_deadline``) follow the AEAT taxpayer calendar:

- Modelo 303 / 130 / 349, quarters 1–3: 1st–20th of the month after the
  quarter; 4th quarter: 1–30 January. A deadline on a Saturday, Sunday or
  holiday moves to the next business day.
- Direct debit (domiciliación, 303 and 130 only — the 349 has no payment):
  Orden HAC/241/2025 (BOE-A-2025-5048, amending art. 3 of Orden
  EHA/1658/2009) requires at least three business days **or** five calendar
  days between the end of the direct-debit period and the end of the filing
  period. That is the 15th for the 20th, and 27 January for 30 January 2026
  (as published in the AEAT "Plazos de presentación de autoliquidaciones con
  domiciliación bancaria", calendario del contribuyente 2026); a date that
  would fall on a non-business day is moved back to the previous business day
  (conservative — pay earlier rather than miss the window). Business days
  exclude weekends, national holidays and the holidays of Madrid (where the
  AEAT IT department sits); only the national ones plus Maundy Thursday and
  Good Friday are built in — pass ``extra_holidays`` for any other, and
  check the AEAT calendar each period.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, Iterable, Optional

from src.database import (
    SNAPSHOT_FILED,
    insert_filed_tax_snapshot_conn,
    load_tax_snapshot_versions,
    load_tax_snapshots_for_period,
    upsert_filing_status_conn,
)
from src.logger import get_logger
from src.reconciliation import normalize_box, result_boxes, result_operators
from src.tax_models import MODELO303_BOX_FIELDS
from src.tax_snapshot_codec import decode_snapshot

log = get_logger(__name__)

SHEET_MODELS = ("303", "130", "349")
PAYMENT_MODELS = ("303", "130")          # the 349 is informative: no payment, no direct debit
ZERO = 0.005

MODEL_TITLES = {
    "303": "Modelo 303 — IVA, autoliquidación trimestral",
    "130": "Modelo 130 — IRPF, pago fraccionado",
    "349": "Modelo 349 — operaciones intracomunitarias",
}

# Box -> concept, as printed on the forms.
BOX_LABELS: dict[str, dict[str, str]] = {
    "303": {
        "01": "Régimen general 4 % — base imponible", "03": "Régimen general 4 % — cuota",
        "04": "Régimen general 10 % — base imponible", "06": "Régimen general 10 % — cuota",
        "07": "Régimen general 21 % — base imponible", "09": "Régimen general 21 % — cuota",
        "10": "Adquisiciones intracomunitarias — base", "11": "Adquisiciones intracomunitarias — cuota",
        "12": "Otras operaciones con inversión del sujeto pasivo — base",
        "13": "Otras operaciones con inversión del sujeto pasivo — cuota",
        "27": "Total cuota devengada",
        "28": "Operaciones interiores corrientes — base", "29": "Operaciones interiores corrientes — cuota",
        "30": "Operaciones interiores bienes de inversión — base",
        "31": "Operaciones interiores bienes de inversión — cuota",
        "36": "Adquisiciones intracomunitarias corrientes — base",
        "37": "Adquisiciones intracomunitarias corrientes — cuota",
        "43": "Regularización bienes de inversión", "44": "Regularización por aplicación del porcentaje definitivo de prorrata",
        "45": "Total a deducir", "46": "Resultado régimen general",
        "59": "Entregas intracomunitarias de bienes y servicios", "60": "Exportaciones y operaciones asimiladas",
        "120": "Operaciones no sujetas por reglas de localización",
        "123": "Operaciones no sujetas — ventas a distancia / OSS",
        "64": "Suma de resultados", "65": "% atribuible a la Administración del Estado",
        "66": "Atribuible a la Administración del Estado",
        "110": "Cuotas a compensar pendientes de periodos anteriores",
        "78": "Cuotas a compensar de periodos anteriores aplicadas en este periodo",
        "87": "Cuotas a compensar pendientes para periodos posteriores",
        "69": "Resultado de la autoliquidación", "71": "Resultado de la liquidación",
        "72": "A compensar", "73": "A devolver",
    },
    "130": {
        "01": "Ingresos computables (desde el 1 de enero)", "02": "Gastos fiscalmente deducibles",
        "03": "Rendimiento neto (01 − 02)", "04": "20 % del importe positivo de 03",
        "05": "A deducir: pagos fraccionados de trimestres anteriores", "06": "Retenciones e ingresos a cuenta",
        "07": "Pago fraccionado previo del trimestre (04 − 05 − 06)",
        "08": "Actividades agrícolas — volumen de ingresos", "09": "Actividades agrícolas — 2 % de 08",
        "10": "Actividades agrícolas — retenciones", "11": "Actividades agrícolas — pago fraccionado previo",
        "12": "Suma de pagos fraccionados previos (07 + 11)",
        "13": "Minoración por aplicación de la deducción del art. 110.3.c) RIRPF",
        "14": "Diferencia (12 − 13)", "15": "A deducir: resultados negativos de trimestres anteriores",
        "16": "A deducir: pago de préstamos para la vivienda habitual", "17": "Total (14 − 15 − 16)",
        "18": "A deducir: resultado de la declaración anterior (complementaria)",
        "19": "Resultado de la declaración (17 − 18)",
    },
    "349": {
        "01": "Número total de operadores intracomunitarios", "02": "Importe de las operaciones intracomunitarias",
        "03": "Número total de operadores con rectificaciones", "04": "Importe de las rectificaciones",
    },
}

# Modelo 303 box -> form section, following the form order of MODELO303_BOX_FIELDS
# (page 1 liquidación; page 3 información adicional + resultado).
_303_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Page 1 — IVA devengado", ("01", "03", "04", "06", "07", "09", "10", "11", "12", "13", "27")),
    ("Page 1 — IVA deducible", ("28", "29", "30", "31", "36", "37", "43", "44", "45", "46")),
    ("Page 3 — Información adicional", ("59", "60", "120", "123")),
    ("Page 3 — Resultado", ("64", "65", "66", "110", "78", "87", "69", "71", "72", "73")),
)
_303_SECTION_OF = {box: title for title, boxes in _303_SECTIONS for box in boxes}
_303_ORDER = {box: i for i, box in enumerate(MODELO303_BOX_FIELDS)}

SOURCES = (
    "AEAT, calendario del contribuyente 2026 — \"Plazos de presentación de autoliquidaciones con "
    "domiciliación bancaria\" (sede.agenciatributaria.gob.es)",
    "Orden HAC/241/2025, de 10 de marzo (BOE-A-2025-5048), art. 3 of Orden EHA/1658/2009: at least "
    "three business days or five calendar days between the end of the direct-debit period and the "
    "end of the filing period",
)


# ---------------------------------------------------------------------------
# Deadlines
# ---------------------------------------------------------------------------

def _easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=None)
def spanish_holidays(year: int) -> frozenset[date]:
    """National holidays plus Maundy Thursday (a Madrid holiday) and Good Friday."""
    fixed = {(1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25)}
    easter = _easter_sunday(year)
    return frozenset({date(year, m, d) for m, d in fixed} | {easter - timedelta(days=3), easter - timedelta(days=2)})


def _is_business_day(d: date, extra: frozenset[date]) -> bool:
    return d.weekday() < 5 and d not in spanish_holidays(d.year) and d not in extra


@dataclass(frozen=True)
class FilingDeadline:
    """When a quarterly return must be filed (and paid by direct debit)."""
    model: str
    year: int
    quarter: int
    nominal: date                          # 20th of the next month / 30 January
    last_day: date                         # nominal moved to the next business day
    direct_debit_last_day: Optional[date]  # None when the model has no payment (349)


def filing_deadline(model: str, year: int, quarter: int,
                    extra_holidays: Iterable[date] = ()) -> FilingDeadline:
    """Deadlines of a quarterly 303 / 130 / 349 (see the module docstring for the rules)."""
    if model not in SHEET_MODELS or quarter not in (1, 2, 3, 4):
        raise ValueError(f"no quarterly deadline for Modelo {model} Q{quarter}")
    extra = frozenset(extra_holidays)
    nominal = date(year + 1, 1, 30) if quarter == 4 else date(year, 3 * quarter + 1, 20)
    last = nominal
    while not _is_business_day(last, extra):
        last += timedelta(days=1)
    debit: Optional[date] = None
    if model in PAYMENT_MODELS:
        by_calendar = last - timedelta(days=5)
        by_business, seen = last, 0          # latest day with 3 business days after it
        while seen < 3:
            if _is_business_day(by_business, extra):
                seen += 1
            by_business -= timedelta(days=1)
        # The two minimums are alternatives: the later date satisfies one of them.
        # A non-business result is moved back to a business day (conservative).
        debit = max(by_calendar, by_business)
        while not _is_business_day(debit, extra):
            debit -= timedelta(days=1)
    return FilingDeadline(model, year, quarter, nominal, last, debit)


# ---------------------------------------------------------------------------
# Sheet model
# ---------------------------------------------------------------------------

# Boxes holding a count, not an amount (typed as a plain integer).
COUNT_BOXES = {("349", "01"), ("349", "03")}


@dataclass
class SheetBox:
    box: str
    label: str
    value: float
    section: str = ""
    count: bool = False

    @property
    def shown(self) -> str:
        """The value for reading: ``1,234.56`` (or ``3`` for a count)."""
        return str(int(round(self.value))) if self.count else f"{self.value:,.2f}"

    @property
    def typed(self) -> str:
        """The value as typed into the Sede form: ``1234,56`` (or ``3`` for a count)."""
        return str(int(round(self.value))) if self.count else aeat_amount(self.value)


@dataclass
class BoxChange:
    box: str
    filed: float
    current: float


@dataclass
class ModelSheet:
    """One model's part of the filing sheet (from its latest stored snapshot)."""
    model: str
    deadline: FilingDeadline
    status: Optional[str] = None          # COMPUTED / FILED; None = no snapshot stored
    version: Optional[int] = None
    computed_at: str = ""
    justificante: str = ""
    presented_on: str = ""
    boxes: list[SheetBox] = field(default_factory=list)      # every box, form order
    operators: list[dict] = field(default_factory=list)      # 349 only
    result: str = ""
    credit_chain: list[SheetBox] = field(default_factory=list)  # 303 only
    carry_forward: Optional[float] = None                       # 303: next quarter's 110
    credit_source: str = ""
    filed_version: Optional[int] = None    # latest FILED version when a newer draft exists
    changes_since_filed: list[BoxChange] = field(default_factory=list)
    notes: str = ""

    @property
    def title(self) -> str:
        return MODEL_TITLES[self.model]

    def visible_boxes(self, include_zero: bool = False) -> list[SheetBox]:
        return [b for b in self.boxes if include_zero or abs(b.value) >= ZERO]


@dataclass
class FilingSheet:
    year: int
    quarter: int
    models: dict[str, ModelSheet]

    @property
    def missing(self) -> list[str]:
        return [m for m, s in self.models.items() if s.status is None]


def _ordered(model: str, boxes: dict[str, float]) -> list[str]:
    if model == "303":
        return sorted(boxes, key=lambda b: (_303_ORDER.get(b, len(_303_ORDER)), int(b) if b.isdigit() else 0))
    return sorted(boxes, key=lambda b: (int(b) if b.isdigit() else 10_000, b))


def _sheet_boxes(model: str, boxes: dict[str, float]) -> list[SheetBox]:
    labels = BOX_LABELS.get(model, {})
    return [SheetBox(b, labels.get(b, ""), round(boxes[b], 2),
                     _303_SECTION_OF.get(b, "") if model == "303" else "", (model, b) in COUNT_BOXES)
            for b in _ordered(model, boxes)]


def _snapshot_boxes(model: str, payload_json: str) -> tuple[Any, dict[str, float]]:
    result = decode_snapshot(model, payload_json)
    return result, {normalize_box(b): float(v) for b, v in result_boxes(model, result).items()}


def _eur(v: float) -> str:
    return f"€{v:,.2f}"


def _result_line(model: str, boxes: dict[str, float]) -> str:
    if model == "303":
        r71 = boxes.get("71", 0.0)
        if r71 >= ZERO:
            return f"To pay (box 71): {_eur(r71)}"
        if boxes.get("73", 0.0) >= ZERO:
            return f"To refund (box 73): {_eur(boxes['73'])}"
        if boxes.get("72", 0.0) >= ZERO:
            return f"To compensate in later periods (box 72): {_eur(boxes['72'])}"
        return "Zero result (box 71 = 0)"
    if model == "130":
        r19 = boxes.get("19", 0.0)
        if r19 >= ZERO:
            return f"To pay (box 19): {_eur(r19)}"
        if r19 <= -ZERO:
            return f"Negative (box 19): {_eur(r19)} — nothing to pay; deductible in later quarters (box 15)"
        return "Zero result (box 19 = 0)"
    return (f"Informative return: {int(boxes.get('01', 0))} operator(s), "
            f"total {_eur(boxes.get('02', 0.0))} (box 02)")


def _model_sheet(conn: sqlite3.Connection, year: int, quarter: int, model: str,
                 row: Optional[sqlite3.Row]) -> ModelSheet:
    sheet = ModelSheet(model=model, deadline=filing_deadline(model, year, quarter))
    if row is None:
        return sheet
    result, boxes = _snapshot_boxes(model, row["payload_json"])
    sheet.status, sheet.version, sheet.computed_at = row["status"], row["snapshot_version"], row["computed_at"]
    sheet.justificante, sheet.presented_on = row["justificante"] or "", row["presented_on"] or ""
    sheet.boxes = _sheet_boxes(model, boxes)
    sheet.result = _result_line(model, boxes)
    sheet.notes = getattr(result, "notes", "") or ""
    if model == "349":
        sheet.operators = result_operators(result)
    if model == "303":
        by_box = {b.box: b for b in sheet.boxes}
        sheet.credit_chain = [by_box[b] for b in ("110", "78", "87", "71", "72", "73") if b in by_box]
        sheet.carry_forward = round(boxes.get("87", 0.0) + boxes.get("72", 0.0), 2)
        sheet.credit_source = getattr(result, "c110_source", "") or ""
    if sheet.status != SNAPSHOT_FILED:
        filed = [v for v in load_tax_snapshot_versions(conn, year, quarter, model) if v["status"] == SNAPSHOT_FILED]
        if filed:
            sheet.filed_version = filed[-1]["snapshot_version"]
            _, filed_boxes = _snapshot_boxes(model, filed[-1]["payload_json"])
            sheet.changes_since_filed = [
                BoxChange(b, round(filed_boxes.get(b, 0.0), 2), round(boxes.get(b, 0.0), 2))
                for b in _ordered(model, {**filed_boxes, **boxes})
                if abs(filed_boxes.get(b, 0.0) - boxes.get(b, 0.0)) >= ZERO
            ]
    return sheet


def build_filing_sheet(year: int, quarter: int, conn: sqlite3.Connection) -> FilingSheet:
    """The filing sheet of one quarter from the latest stored snapshot of each model."""
    rows = {r["model"]: r for r in load_tax_snapshots_for_period(year, quarter, conn)}
    return FilingSheet(year, quarter, {m: _model_sheet(conn, year, quarter, m, rows.get(m)) for m in SHEET_MODELS})


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def aeat_amount(value: float) -> str:
    """An amount as typed into the AEAT Sede forms: no thousands separator, decimal comma."""
    return f"{value:.2f}".replace(".", ",")


def _md(text: Any) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _status_line(s: ModelSheet) -> str:
    parts = [f"snapshot version {s.version}", s.status or ""]
    if s.status == SNAPSHOT_FILED:
        parts += [f"justificante {s.justificante}", f"presented {s.presented_on}"]
    parts.append(f"computed {s.computed_at}")
    return " · ".join(parts)


def _box_table(boxes: list[SheetBox]) -> list[str]:
    return (["| Box | Concept | Value (EUR) | Type in form |", "|---|---|--:|---|"]
            + [f"| {b.box} | {_md(b.label)} | {b.shown} | `{b.typed}` |" for b in boxes])


def _deadline_lines(sheet: FilingSheet) -> list[str]:
    out = ["## Deadlines", "", "| Model | Direct debit until | File (and pay by other means) until |",
           "|---|---|---|"]
    for s in sheet.models.values():
        d = s.deadline
        moved = f" (moved from {d.nominal.isoformat()})" if d.last_day != d.nominal else ""
        debit = d.direct_debit_last_day.isoformat() if d.direct_debit_last_day else "— (no payment)"
        out.append(f"| {s.model} | {debit} | {d.last_day.isoformat()}{moved} |")
    out += ["", "Sources: " + "; ".join(SOURCES) + ". Holidays other than the national ones, Maundy "
            "Thursday and Good Friday are not modelled — check the AEAT calendar.", ""]
    return out


def render_markdown(sheet: FilingSheet, include_zero: bool = False) -> str:
    """The filing sheet as markdown (deterministic: no 'generated at' timestamp)."""
    out = [f"# Filing sheet — {sheet.year} Q{sheet.quarter}", "",
           "> What to type into each AEAT form, from the stored snapshots (`compute`). "
           + ("All modelled boxes are listed." if include_zero else "Only non-zero boxes are listed.")
           + " Amounts in EUR; the last column is the value as typed in the Sede form.", ""]
    out += _deadline_lines(sheet)
    for s in sheet.models.values():
        out += [f"## {s.title}", ""]
        if s.status is None:
            out += ["_No stored snapshot — run `compute` (or **Calculate tax**) first._", ""]
            continue
        out += [f"_{_status_line(s)}_", "", f"**Result:** {s.result}", ""]
        if s.changes_since_filed:
            out += [f"> ⚠️ Recomputed after filing: differs from filed version {s.filed_version} in "
                    f"{len(s.changes_since_filed)} box(es).", "",
                    "| Box | Filed | Current draft | Diff |", "|---|--:|--:|--:|"]
            out += [f"| {c.box} | {c.filed:,.2f} | {c.current:,.2f} | {c.current - c.filed:,.2f} |"
                    for c in s.changes_since_filed]
            out.append("")
        boxes = s.visible_boxes(include_zero)
        if not boxes:
            out += ["_All boxes are zero._", ""]
        elif s.model == "303":
            for title, _ in _303_SECTIONS:
                section = [b for b in boxes if b.section == title]
                if section:
                    out += [f"### {title}", ""] + _box_table(section) + [""]
            other = [b for b in boxes if not b.section]
            if other:
                out += ["### Other boxes", ""] + _box_table(other) + [""]
        else:
            out += _box_table(boxes) + [""]
        if s.model == "303":
            out += ["### Credit chain", "",
                    "| Box | Concept | Value (EUR) |", "|---|---|--:|"]
            out += [f"| {b.box} | {_md(b.label)} | {b.shown} |" for b in s.credit_chain]
            out += ["", f"Carried to next quarter's box 110 (87 + 72): {_eur(s.carry_forward or 0.0)}"
                    + (f" · box 110 source: {s.credit_source}" if s.credit_source else ""), ""]
        if s.model == "349":
            out += ["### Operators", ""]
            if s.operators:
                out += ["| Country | VAT id | Name | Key | Base (EUR) | Type in form |",
                        "|---|---|---|---|--:|---|"]
                out += [f"| {_md(o.get('country') or '')} | {_md(o.get('vat_id') or '')} | "
                        f"{_md(o.get('name') or '')} | {_md(o.get('key') or '')} | "
                        f"{float(o.get('base') or 0):,.2f} | `{aeat_amount(float(o.get('base') or 0))}` |"
                        for o in s.operators]
            else:
                out.append("_No declarable operators._")
            out.append("")
        if s.notes:
            out += [f"Notes: {_md(s.notes)}", ""]
    return "\n".join(out)


def render_filing_sheet(year: int, quarter: int, conn: sqlite3.Connection, config: dict) -> str:
    """``close_pipeline.filing_sheet_renderer``: the non-zero boxes of the stored snapshots."""
    return render_markdown(build_filing_sheet(year, quarter, conn))


# ---------------------------------------------------------------------------
# Mark filed
# ---------------------------------------------------------------------------

# Box holding the amount paid / returned, stored on tax_filing_status like the
# calendar's old "Mark Filed" did (the legacy 130 engine reads it for box 05).
_AMOUNT_BOX = {"303": "71", "130": "19"}


def mark_filed(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    model: str,
    justificante: str,
    presented_on: date,
) -> int:
    """Freeze the latest computed snapshot as a new immutable FILED version.

    Stores the AEAT justificante and presentation date on the new version and
    flags the period FILED in ``tax_filing_status``. Returns the new
    ``snapshot_version``. Raises ``ValueError`` on an empty justificante, a
    missing snapshot or a period whose latest version is already filed.
    Import the official receipt PDF afterwards (Reconciliation tab or
    ``python -m src.filed_returns import``) so the reconciliation can check it.
    """
    if model not in SHEET_MODELS:
        raise ValueError(f"Modelo {model} is not filed from the filing sheet")
    justificante = (justificante or "").strip()
    if not justificante:
        raise ValueError("the justificante (receipt number) is required")
    filed_at = datetime.now().isoformat(timespec="seconds")
    try:
        version, payload = insert_filed_tax_snapshot_conn(
            conn, year, quarter, model, justificante, presented_on.isoformat(), filed_at,
        )
        amount_box = _AMOUNT_BOX.get(model)
        amount = _snapshot_boxes(model, payload)[1].get(amount_box) if amount_box else None
        upsert_filing_status_conn(conn, year, model, quarter, SNAPSHOT_FILED, amount,
                                  notes=f"justificante {justificante}", filed_at=presented_on.isoformat())
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    log.info("✅ Modelo %s %s Q%s marked filed: snapshot version %d, presented %s",
             model, year, quarter, version, presented_on.isoformat())
    return version
