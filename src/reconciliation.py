"""Box-by-box reconciliation of filed AEAT returns against the app's figures.

For one model (303 / 130 / 349 / 390) and period this module lines up every
box of the filed return (``src/filed_returns.py``, with ``validation.yaml`` as
fallback via ``src/tax_validator.py``) against the box the app computes, and
gives each row a status:

- ✅ ``exact``        filed and app agree within €0.01.
- 🟡 ``catalogued``   they differ, and an entry of the divergence catalogue
                      explains the difference.
- 🔴 ``uncatalogued`` they differ and nothing explains it.
- ⚪ ``missing``      one side is unavailable: no filed return for the period,
                      or the app does not compute that box.

On an imported return a blank box means 0, so a box the app computes but the
return left blank compares against 0 (not ⚪). The Modelo 349 is compared
operator by operator, keyed by the full VAT id (country prefix + number) and
the operation key (``op:<VATID>:<KEY>``).

**Divergence catalogue** — the git-ignored ``divergences.json`` at the repo
root (the repo ships ``divergences.json.example`` with fake entries). Each
entry names ``model``, ``year`` (or null = any year), ``quarter`` (or null =
any quarter), ``box`` and either an ``expected_delta`` (app − filed, matched
within ``tolerance``) or a ``rule`` (``app_gte_filed``, ``app_lte_filed``,
``any``), plus a ``category`` (``gestor_error``, ``convention``,
``app_choice``) and an ``explanation``.

**App side** — ``app_boxes(model, year, quarter, conn, config)`` calls the
engine result's ``aeat_boxes()``: the 303 (#97), 130 (#98) and 349 (#99)
quarterly engines and the Modelo 390 engine (``src/modelo_390.py``, #103),
which is built from the year's four 303 results.

No Streamlit here: the UI lives in ``app/tax_validation.py``.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from src.exceptions import StripeAutomationError
from src.filed_returns import VALIDATOR_KEYS
from src.logger import get_logger
from src.modelo_390 import MODELO390_LABELS, compute_modelo_390
from src.modelo_130 import compute_modelo_130
from src.modelo_303 import compute_modelo_303
from src.modelo_349 import compute_modelo_349
from src.tax_data import load_app_config
from src.tax_validator import find_filing, load_filings

log = get_logger(__name__)

CATALOGUE_PATH = Path(__file__).parent.parent / "divergences.json"

MODELS = ("303", "130", "349", "390")
QUARTERLY_MODELS = ("303", "130", "349")

EXACT_TOLERANCE = 0.01
DEFAULT_TOLERANCE = 0.01
_EPS = 1e-9

STATUS_EXACT = "exact"
STATUS_CATALOGUED = "catalogued"
STATUS_UNCATALOGUED = "uncatalogued"
STATUS_MISSING = "missing"
STATUSES = (STATUS_EXACT, STATUS_CATALOGUED, STATUS_UNCATALOGUED, STATUS_MISSING)
STATUS_ICONS = {
    STATUS_EXACT: "✅",
    STATUS_CATALOGUED: "🟡",
    STATUS_UNCATALOGUED: "🔴",
    STATUS_MISSING: "⚪",
}

RULES = ("app_gte_filed", "app_lte_filed", "any")
CATEGORIES = ("gestor_error", "convention", "app_choice")
CATALOGUE_FIELDS = (
    "model", "year", "quarter", "box", "expected_delta", "rule",
    "tolerance", "category", "explanation",
)

OPERATOR_PREFIX = "op:"

# Short labels for the boxes the view is likely to show. A box without a label
# still reconciles; it just shows no description.
BOX_LABELS: dict[str, dict[str, str]] = {
    "303": {
        "01": "Base 4 %", "03": "Cuota 4 %", "04": "Base 10 %", "06": "Cuota 10 %",
        "07": "Base 21 %", "09": "Cuota 21 %",
        "10": "Adquisiciones intracomunitarias — base", "11": "Adquisiciones intracomunitarias — cuota",
        "12": "Otras operaciones con inversión del sujeto pasivo — base",
        "13": "Otras operaciones con inversión del sujeto pasivo — cuota",
        "27": "Total cuota devengada",
        "28": "Deducible: interiores corrientes — base", "29": "Deducible: interiores corrientes — cuota",
        "30": "Deducible: interiores bienes de inversión — base",
        "31": "Deducible: interiores bienes de inversión — cuota",
        "36": "Deducible: adquisiciones intracomunitarias corrientes — base",
        "37": "Deducible: adquisiciones intracomunitarias corrientes — cuota",
        "44": "Regularización prorrata", "45": "Total a deducir",
        "46": "Resultado régimen general (27 − 45)",
        "59": "Entregas intracomunitarias (informativo)",
        "60": "Exportaciones y asimiladas (informativo)",
        "64": "Suma de resultados", "65": "% atribuible a la Administración del Estado",
        "66": "Atribuible a la Administración del Estado",
        "69": "Resultado", "71": "Resultado de la liquidación",
        "72": "A compensar (último periodo)", "73": "A devolver",
        "78": "Cuotas a compensar de periodos anteriores aplicadas",
        "87": "Cuotas a compensar pendientes para periodos posteriores",
        "110": "Cuotas a compensar pendientes de periodos anteriores",
        "120": "Operaciones no sujetas por reglas de localización (informativo)",
    },
    "130": {
        "01": "Ingresos computables (YTD)", "02": "Gastos fiscalmente deducibles (YTD)",
        "03": "Rendimiento neto (01 − 02)", "04": "20 % de 03",
        "05": "Pagos fraccionados de trimestres anteriores", "06": "Retenciones e ingresos a cuenta (YTD)",
        "07": "Pago fraccionado previo (04 − 05 − 06)", "11": "Actividades agrícolas (suma)",
        "12": "Suma de pagos fraccionados previos", "13": "Minoración art. 110.3.c RIRPF",
        "14": "Diferencia (12 − 13)", "15": "Resultados negativos de trimestres anteriores",
        "16": "Deducción vivienda habitual", "17": "Total (14 − 15 − 16)",
        "18": "Complementaria: resultado anterior", "19": "Resultado de la declaración",
    },
    "349": {
        "01": "Número total de operadores", "02": "Importe de las operaciones intracomunitarias",
        "03": "Número de operadores con rectificaciones", "04": "Importe de las rectificaciones",
    },
    "390": dict(MODELO390_LABELS),
}


class CatalogueError(StripeAutomationError):
    """The divergence catalogue file or an entry in it is invalid."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Divergence:
    """One catalogued, explained difference between a filed and an app box."""
    model: str
    year: Optional[int]
    quarter: Optional[int]
    box: str
    category: str
    explanation: str
    expected_delta: Optional[float] = None
    rule: Optional[str] = None
    tolerance: float = DEFAULT_TOLERANCE

    def applies_to(self, model: str, year: int, quarter: Optional[int], box: str) -> bool:
        return (
            self.model == model
            and self.box == box
            and (self.year is None or self.year == year)
            and (self.quarter is None or self.quarter == quarter)
        )

    def matches(self, filed: float, app: float) -> bool:
        """Does ``app − filed`` fit this entry's expected delta / rule?"""
        diff = app - filed
        if self.expected_delta is not None:
            return abs(diff - self.expected_delta) <= self.tolerance + _EPS
        if self.rule == "any":
            return True
        if self.rule == "app_gte_filed":
            return app >= filed - self.tolerance - _EPS
        if self.rule == "app_lte_filed":
            return app <= filed + self.tolerance + _EPS
        return False

    def to_dict(self) -> dict[str, Any]:
        return {k: getattr(self, k) for k in CATALOGUE_FIELDS}


@dataclass
class ReconLine:
    """One box (or one 349 operator) of the reconciliation table."""
    box: str
    description: str
    filed: Optional[float]
    app: Optional[float]
    status: str = STATUS_MISSING
    divergence: Optional[Divergence] = None

    @property
    def diff(self) -> Optional[float]:
        """App minus filed (None when a side is missing)."""
        if self.filed is None or self.app is None:
            return None
        return round(self.app - self.filed, 2)

    @property
    def tag(self) -> str:
        return self.divergence.category if self.divergence else ""

    @property
    def explanation(self) -> str:
        return self.divergence.explanation if self.divergence else ""


@dataclass
class Reconciliation:
    """Filed-vs-app comparison of one model and period."""
    model: str
    year: int
    quarter: Optional[int]
    filed_found: bool
    filed_source: str = ""         # "db" (imported receipt) or "yaml"
    filed_date: str = ""
    lines: list[ReconLine] = field(default_factory=list)
    live_audit: list[dict] = field(default_factory=list)  # audit entries of this computation

    @property
    def period(self) -> str:
        return f"{self.year} Q{self.quarter}" if self.quarter else f"{self.year} annual"

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for ln in self.lines:
            out[ln.status] += 1
        return out


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

_VAT_SEPARATORS_RE = re.compile(r"[\s.\-/_]")


def normalize_box(box: Any) -> str:
    """'7' -> '07', ' 110 ' -> '110'; operator keys are upper-cased."""
    s = str(box).strip()
    if s.lower().startswith(OPERATOR_PREFIX):
        return OPERATOR_PREFIX + s[len(OPERATOR_PREFIX):].upper()
    if s.isdigit() and len(s) < 2:
        return s.zfill(2)
    return s


def operator_vat(country: Optional[str], vat_id: Optional[str]) -> str:
    """Full VAT id for matching: country prefix + number, no separators."""
    v = _VAT_SEPARATORS_RE.sub("", (vat_id or "").upper())
    c = (country or "").strip().upper()
    if c and not v.startswith(c):
        v = c + v
    return v or "UNKNOWN"


def operator_box(country: Optional[str], vat_id: Optional[str], key: Optional[str]) -> str:
    """Reconciliation key of a 349 operator: ``op:<VATID>:<KEY>``."""
    return f"{OPERATOR_PREFIX}{operator_vat(country, vat_id)}:{(key or '').strip().upper()}"


def classify(
    filed: Optional[float], app: Optional[float], candidates: Iterable[Divergence]
) -> tuple[str, Optional[Divergence]]:
    """Status of one row and the catalogue entry explaining it (if any).

    ``candidates`` are the catalogue entries that apply to the row's model,
    period and box; the first whose delta / rule fits wins.
    """
    if filed is None or app is None:
        return STATUS_MISSING, None
    if abs(round(app - filed, 2)) <= EXACT_TOLERANCE + _EPS:
        return STATUS_EXACT, None
    for entry in candidates:
        if entry.matches(filed, app):
            return STATUS_CATALOGUED, entry
    return STATUS_UNCATALOGUED, None


def apply_catalogue(rec: Reconciliation, catalogue: Iterable[Divergence]) -> Reconciliation:
    """Set every line's status against ``catalogue`` (in place; returns ``rec``)."""
    entries = list(catalogue)
    for ln in rec.lines:
        candidates = [e for e in entries if e.applies_to(rec.model, rec.year, rec.quarter, ln.box)]
        ln.status, ln.divergence = classify(ln.filed, ln.app, candidates)
    return rec


def _sort_key(box: str) -> tuple[int, int, str]:
    if box.isdigit():
        return (0, int(box), box)
    return (1, 0, box)


def _ops_by_key(ops: Optional[list[dict]]) -> dict[str, dict]:
    """349 operators keyed by ``op:<VATID>:<KEY>``, bases summed per key."""
    out: dict[str, dict] = {}
    for op in ops or []:
        k = operator_box(op.get("country"), op.get("vat_id"), op.get("key"))
        prev = out.get(k)
        out[k] = {**op, "base": round((prev["base"] if prev else 0.0) + float(op["base"]), 2)}
    return out


def build_lines(
    model: str,
    filed_boxes: Optional[dict[str, float]],
    app: dict[str, float],
    *,
    filed_complete: bool = True,
    filed_operators: Optional[list[dict]] = None,
    app_ops: Optional[list[dict]] = None,
) -> list[ReconLine]:
    """Rows for the union of filed and app boxes (plus 349 operators).

    ``filed_boxes`` is None when there is no filed return for the period.
    With ``filed_complete`` (an imported receipt) a blank box counts as 0;
    a hand-written YAML filing only knows the boxes it lists.
    """
    labels = BOX_LABELS.get(model, {})
    lines: list[ReconLine] = []
    boxes = set(app) | set(filed_boxes or {})
    for box in sorted(boxes, key=_sort_key):
        if filed_boxes is None:
            filed = None
        elif box in filed_boxes:
            filed = filed_boxes[box]
        else:
            filed = 0.0 if filed_complete else None
        lines.append(ReconLine(box, labels.get(box, ""), filed, app.get(box)))

    if model == "349":
        filed_by, app_by = _ops_by_key(filed_operators), _ops_by_key(app_ops)
        for k in sorted(set(filed_by) | set(app_by)):
            f_op, a_op = filed_by.get(k), app_by.get(k)
            name = (f_op or {}).get("name") or (a_op or {}).get("name") or ""
            key = k.rsplit(":", 1)[1]
            filed = f_op["base"] if f_op else (None if filed_boxes is None else 0.0)
            app_val = a_op["base"] if a_op else 0.0
            lines.append(ReconLine(k, f"{name} (key {key})".strip(), filed, app_val))
    return lines


def _fmt_amount(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:,.2f}"


def _md_cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def to_markdown(rec: Reconciliation) -> str:
    """The reconciliation table as GitHub-flavoured markdown."""
    counts = rec.counts()
    source = {"db": "imported AEAT receipt", "yaml": "validation.yaml"}.get(rec.filed_source, "")
    head = [f"## Modelo {rec.model} — {rec.period} — filed vs app", ""]
    if rec.filed_found:
        head.append(f"Filed: {rec.filed_date or '—'} ({source}).")
    else:
        head.append("No filed return for this period.")
    head.append("")
    head.append(" · ".join(f"{STATUS_ICONS[s]} {s}: {counts[s]}" for s in STATUSES))
    head.append("")
    rows = [
        "| Box | Description | Filed | App | Diff (app − filed) | Status | Tag | Explanation |",
        "|---|---|--:|--:|--:|---|---|---|",
    ]
    for ln in rec.lines:
        diff = "—" if ln.diff is None else f"{ln.diff:+,.2f}"
        rows.append(
            f"| {_md_cell(ln.box)} | {_md_cell(ln.description)} | {_fmt_amount(ln.filed)} "
            f"| {_fmt_amount(ln.app)} | {diff} | {STATUS_ICONS[ln.status]} {ln.status} "
            f"| {_md_cell(ln.tag)} | {_md_cell(ln.explanation)} |"
        )
    return "\n".join(head + rows) + "\n"


# ---------------------------------------------------------------------------
# Divergence catalogue (divergences.json)
# ---------------------------------------------------------------------------

def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v)) or (isinstance(v, str) and not v.strip())


def parse_entry(raw: dict[str, Any]) -> Divergence:
    """Validate one catalogue entry (dict) and return it; raises CatalogueError."""
    errors: list[str] = []
    model = "" if _blank(raw.get("model")) else str(raw.get("model")).strip()
    if model not in MODELS:
        errors.append(f"model must be one of {', '.join(MODELS)}")

    def _int_or_none(name: str, lo: int, hi: int) -> Optional[int]:
        v = raw.get(name)
        if _blank(v):
            return None
        try:
            f = float(v)
        except (TypeError, ValueError):
            errors.append(f"{name} must be a number or empty")
            return None
        if f != int(f) or not lo <= int(f) <= hi:
            errors.append(f"{name} must be a whole number between {lo} and {hi}, or empty")
            return None
        return int(f)

    year = _int_or_none("year", 2000, 2100)
    quarter = _int_or_none("quarter", 1, 4)
    if model == "390" and quarter is not None:
        errors.append("Modelo 390 is annual: leave quarter empty")

    box = "" if _blank(raw.get("box")) else normalize_box(raw.get("box"))
    if not box:
        errors.append("box is required")

    expected_delta: Optional[float] = None
    if not _blank(raw.get("expected_delta")):
        try:
            expected_delta = float(raw["expected_delta"])
        except (TypeError, ValueError):
            errors.append("expected_delta must be a number")
    rule = None if _blank(raw.get("rule")) else str(raw.get("rule")).strip()
    if rule is not None and rule not in RULES:
        errors.append(f"rule must be one of {', '.join(RULES)}")
    if (expected_delta is None) == (rule is None):
        errors.append("give exactly one of expected_delta or rule")

    tolerance = DEFAULT_TOLERANCE
    if not _blank(raw.get("tolerance")):
        try:
            tolerance = float(raw["tolerance"])
        except (TypeError, ValueError):
            errors.append("tolerance must be a number")
        else:
            if tolerance < 0:
                errors.append("tolerance must be ≥ 0")

    category = "" if _blank(raw.get("category")) else str(raw.get("category")).strip()
    if category not in CATEGORIES:
        errors.append(f"category must be one of {', '.join(CATEGORIES)}")
    explanation = "" if _blank(raw.get("explanation")) else str(raw.get("explanation")).strip()
    if not explanation:
        errors.append("explanation is required")

    if errors:
        raise CatalogueError("; ".join(errors))
    return Divergence(model=model, year=year, quarter=quarter, box=box, category=category,
                      explanation=explanation, expected_delta=expected_delta, rule=rule,
                      tolerance=tolerance)


def parse_catalogue(entries: Iterable[dict[str, Any]]) -> list[Divergence]:
    """Validate every entry; raises CatalogueError listing each bad row (1-based)."""
    out: list[Divergence] = []
    errors: list[str] = []
    for i, raw in enumerate(entries, 1):
        if not isinstance(raw, dict):
            errors.append(f"row {i}: not an object")
            continue
        try:
            out.append(parse_entry(raw))
        except CatalogueError as exc:
            errors.append(f"row {i}: {exc}")
    if errors:
        raise CatalogueError("\n".join(errors))
    return out


def load_catalogue(path: Optional[str | Path] = None) -> list[Divergence]:
    """Read the catalogue ([] when the file does not exist); raises CatalogueError."""
    target = Path(path) if path is not None else CATALOGUE_PATH
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CatalogueError(f"{target.name} is not valid JSON: {exc}") from exc
    entries = data.get("divergences") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise CatalogueError(f'{target.name} must be an object with a "divergences" list')
    return parse_catalogue(entries)


def save_catalogue(entries: Iterable[dict[str, Any]], path: Optional[str | Path] = None) -> list[Divergence]:
    """Validate and write the catalogue; nothing is written if any entry is invalid."""
    parsed = parse_catalogue(entries)
    target = Path(path) if path is not None else CATALOGUE_PATH
    payload = {"divergences": [d.to_dict() for d in parsed]}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(target)
    log.info("ℹ️ Saved %d divergence catalogue entr%s to %s",
             len(parsed), "y" if len(parsed) == 1 else "ies", target.name)
    return parsed


# ---------------------------------------------------------------------------
# App side (engine adapter)
# ---------------------------------------------------------------------------

def _compute(model: str, year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict]) -> Any:
    engines = {"303": compute_modelo_303, "130": compute_modelo_130, "349": compute_modelo_349}
    return engines[model](year, quarter, conn, config)


def result_boxes(model: str, result: Any) -> dict[str, float]:
    """AEAT box → value of one engine result's ``aeat_boxes()``."""
    return {normalize_box(k): float(v) for k, v in result.aeat_boxes().items() if v is not None}


def result_operators(result: Any) -> list[dict]:
    """The 349 operator rows of one engine result (``operators()``; empty when absent)."""
    return list(result.operators()) if callable(getattr(result, "operators", None)) else []


def app_boxes(
    model: str, year: int, quarter: Optional[int], conn: sqlite3.Connection, config: Optional[dict] = None
) -> dict[str, float]:
    """The app's value for every AEAT box it computes, keyed as printed on the form.

    Uses the engine result's ``aeat_boxes()`` (#97/#98/#99); the Modelo 390
    engine (#103) builds its boxes from the year's four 303 results.
    """
    if config is None:
        config = load_app_config()
    if model == "390":
        return result_boxes(model, compute_modelo_390(year, conn, config))
    if model not in QUARTERLY_MODELS or quarter is None:
        raise ValueError(f"unsupported model/period: {model} {year} Q{quarter}")
    return result_boxes(model, _compute(model, year, quarter, conn, config))


# ---------------------------------------------------------------------------
# Filed side + entry point
# ---------------------------------------------------------------------------

def _filed_side(filing: dict, model: str) -> tuple[dict[str, float], bool, list[dict]]:
    """(boxes, complete, operators) of a filing dict from the validator loader."""
    if filing.get("source") == "db" and "boxes" in filing:
        boxes = {normalize_box(k): float(v) for k, v in filing["boxes"].items()}
        complete = True
    else:
        # YAML fallback: map the validator's value keys ("07_base_21pct") back to boxes.
        reverse = {key: box for box, key in VALIDATOR_KEYS.get(model, {}).items()}
        boxes = {reverse[k]: float(v) for k, v in (filing.get("values") or {}).items()
                 if k in reverse and v is not None}
        complete = False
    ops = [
        {"country": o.get("country"), "vat_id": o.get("vat_id"), "name": o.get("name"),
         "key": o.get("clave") or o.get("key"), "base": float(o.get("amount", o.get("base", 0.0)))}
        for o in filing.get("operators") or []
    ]
    return boxes, complete, ops


def list_filed_periods(conn: Optional[sqlite3.Connection]) -> list[tuple[str, int, Optional[int]]]:
    """(model, year, quarter) of every filed return known to the validator loader."""
    out = []
    for f in load_filings(conn):
        key = (str(f.get("model")), int(f.get("year", 0)), f.get("quarter"))
        if key[0] in MODELS and key not in out:
            out.append(key)
    return out


def _audit_dicts(entries: Iterable[Any]) -> list[dict]:
    return [{**asdict(e), "computed_at": "live"} for e in entries]


def reconcile(
    model: str,
    year: int,
    quarter: Optional[int],
    conn: sqlite3.Connection,
    config: Optional[dict] = None,
    catalogue: Optional[Iterable[Divergence]] = None,
) -> Reconciliation:
    """Compare the filed return of (model, year, quarter) with the app's boxes."""
    if model not in MODELS:
        raise ValueError(f"unsupported model {model!r}")
    if model == "390":
        quarter = None
    elif quarter is None:
        raise ValueError(f"Modelo {model} needs a quarter")
    if config is None:
        config = load_app_config()

    filing = find_filing(load_filings(conn), model, year, quarter)
    filed_boxes: Optional[dict[str, float]] = None
    filed_complete, filed_ops = True, []
    if filing is not None:
        filed_boxes, filed_complete, filed_ops = _filed_side(filing, model)

    app_ops: list[dict] = []
    if model == "390":
        result = compute_modelo_390(year, conn, config)
    else:
        result = _compute(model, year, quarter, conn, config)
    app = result_boxes(model, result)
    if model == "349":
        app_ops = result_operators(result)
    live_audit = _audit_dicts(getattr(result, "audit", []) or [])

    rec = Reconciliation(
        model=model, year=year, quarter=quarter, filed_found=filing is not None,
        filed_source=(filing or {}).get("source", ""), filed_date=(filing or {}).get("filed_date", ""),
        lines=build_lines(model, filed_boxes, app, filed_complete=filed_complete,
                          filed_operators=filed_ops, app_ops=app_ops),
        live_audit=live_audit,
    )
    apply_catalogue(rec, catalogue or [])
    log.info("ℹ️ Reconciled Modelo %s %s (filed=%s): %s",
             model, rec.period, rec.filed_found, rec.counts())
    return rec


# ---------------------------------------------------------------------------
# Drill-down: audit records behind an app box
# ---------------------------------------------------------------------------

def audit_entries_for_box(entries: Iterable[dict], model: str, box: str) -> list[dict]:
    """The audit entries (``tax_audit_log`` row dicts) that produced ``box``.

    AEAT-numbered results match cells named after the box (``c07_base``,
    ``box_07``, ``07``). A 349 operator row matches the cells that name its VAT id.
    """
    entries = list(entries)
    if box.startswith(OPERATOR_PREFIX):
        vat = box[len(OPERATOR_PREFIX):].rsplit(":", 1)[0]
        return [e for e in entries if vat in _VAT_SEPARATORS_RE.sub("", str(e.get("cell", "")).upper())]
    pattern = re.compile(rf"^(?:c|box_?)?{re.escape(box)}(?:_|$)", re.IGNORECASE)
    return [e for e in entries if pattern.match(str(e.get("cell", "")))]
