"""Fixed assets: IRPF depreciation and VAT capital goods (#96).

Durable purchases are not expensed in the quarter they are bought:

- **IRPF (Modelo 130).** An asset whose unit base is above
  ``assets.threshold_eur`` (default €300) is depreciated with the *tabla de
  amortizaciones simplificada* of *estimación directa simplificada*. Its cost
  enters box 02 through :func:`depreciation_for_period`; the invoice it came
  from is flagged ``is_capital_asset`` so the 130 does not expense it twice.
  Items at or below the threshold are expensed in full in the quarter they are
  acquired (*bienes de escaso valor*).
- **VAT (Modelo 303).** An asset with a unit base above €3,005.06 is a *bien de
  inversión* (art. 108 LIVA): its deductible VAT goes in boxes 30/31 at the
  business-use share (art. 95.Tres LIVA) and it enters a 5-year regularisation
  register (arts. 107-109 LIVA). This module computes and exposes both; the 303
  engine reads them (boxes 30/31, and the regularisation in box 43).

Storage: ``fixed_assets`` (one row per asset unit) and ``fixed_asset_vat_usage``
(the VAT business-use % actually applied in each year of the regularisation
period). Both are created lazily by :func:`ensure_fixed_assets_schema`.
"""
from __future__ import annotations

import calendar
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Iterable, Optional

from src.periods import quarter_date_bounds
from src.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Rules and constants
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AssetClass:
    """One group of the simplified depreciation table."""
    label: str
    max_coefficient_pct: float   # coeficiente lineal máximo (% per year)
    max_years: int               # periodo máximo (years)


# Tabla de amortizaciones simplificada — Orden de 27 de marzo de 1998 (BOE
# 28/03/1998, BOE-A-1998-7200), applicable in IRPF estimación directa
# simplificada (art. 30.1 RIRPF). Verified 2026-09-28 against the AEAT IRPF 2025
# practical manual ("Especialidades fiscales de las amortizaciones en la
# modalidad simplificada"), which reproduces the table unchanged. Groups 7-10
# (livestock, fruit trees, vineyards, olive groves) are omitted as irrelevant.
ASSET_CLASSES: dict[str, AssetClass] = {
    "buildings": AssetClass("Edificios y otras construcciones", 3.0, 68),
    "installations": AssetClass("Instalaciones, mobiliario y enseres", 10.0, 20),
    "machinery": AssetClass("Maquinaria", 12.0, 18),
    "vehicles": AssetClass("Elementos de transporte", 16.0, 14),
    "it_equipment": AssetClass(
        "Equipos para tratamiento de la información y sistemas y programas informáticos", 26.0, 10
    ),
    "tools": AssetClass("Útiles y herramientas", 30.0, 8),
    "other": AssetClass("Resto del inmovilizado material", 10.0, 20),
}
DEFAULT_ASSET_CLASS = "other"

# Items at or below this unit value are expensed, not depreciated (config
# ``assets.threshold_eur``).
DEFAULT_THRESHOLD_EUR = 300.0

# ``annual_q4``: the full year's depreciation is booked in Q4 (the Q4 YTD 130
# carries it; Q1-Q3 carry none). ``quarterly``: each quarter carries its days.
POSTING_MODES: tuple[str, ...] = ("annual_q4", "quarterly")
DEFAULT_POSTING_MODE = "annual_q4"

# Art. 108 LIVA: a capital good (bien de inversión) has a unit value above this.
VAT_CAPITAL_GOOD_THRESHOLD_EUR = 3005.06
# Art. 107 LIVA: the regularisation period is the year of acquisition + 4
# (buildings: + 9 — not modelled; buildings are out of scope here).
VAT_REGULARISATION_YEARS = 5
# Art. 107 LIVA: regularise only when the % changes by more than 10 points.
VAT_REGULARISATION_MIN_POINTS = 10.0


@dataclass(frozen=True)
class AssetSettings:
    threshold_eur: float = DEFAULT_THRESHOLD_EUR
    posting_mode: str = DEFAULT_POSTING_MODE


def asset_settings(config: Optional[dict]) -> AssetSettings:
    """Read ``config['assets']`` (threshold_eur, posting_mode), falling back to defaults."""
    cfg = (config or {}).get("assets", {}) or {}
    threshold = float(cfg.get("threshold_eur", DEFAULT_THRESHOLD_EUR))
    mode = cfg.get("posting_mode", DEFAULT_POSTING_MODE)
    if mode not in POSTING_MODES:
        log.warning("⚠️ Unknown assets.posting_mode %r — using %s", mode, DEFAULT_POSTING_MODE)
        mode = DEFAULT_POSTING_MODE
    return AssetSettings(threshold_eur=threshold, posting_mode=mode)


def class_max_coefficient(asset_class: str) -> float:
    """Maximum linear coefficient (%) of an asset class (unknown class → ``other``)."""
    return ASSET_CLASSES.get(asset_class, ASSET_CLASSES[DEFAULT_ASSET_CLASS]).max_coefficient_pct


def normalize_asset_class(value: Optional[str]) -> str:
    """Map a free-text class (e.g. from the invoice ledger) onto a table key."""
    key = (value or "").strip().lower().replace(" ", "_").replace("-", "_")
    return key if key in ASSET_CLASSES else DEFAULT_ASSET_CLASS


def is_expensed(base_eur: float, threshold_eur: float = DEFAULT_THRESHOLD_EUR) -> bool:
    """True when a unit base is at or below the threshold (expensed, not depreciated)."""
    return round(base_eur, 2) <= round(threshold_eur, 2)


def is_vat_capital_good(base_eur: float) -> bool:
    """Art. 108 LIVA: unit value above €3,005.06."""
    return round(base_eur, 2) > VAT_CAPITAL_GOOD_THRESHOLD_EUR


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------

@dataclass
class FixedAsset:
    """One asset unit. Percentages are 0-100."""
    description: str
    acquisition_date: str               # ISO date
    base_eur: float                     # unit base, ex-VAT
    business_use_pct: float = 100.0     # IRPF: share of the base that is depreciated
    asset_class: str = DEFAULT_ASSET_CLASS
    coefficient_pct: Optional[float] = None   # None → class maximum
    start_of_use: Optional[str] = None  # None → acquisition_date
    vat_eur: float = 0.0                # VAT paid on acquisition (cuota soportada)
    vat_business_pct: Optional[float] = None  # None → business_use_pct
    vat_capital_good: Optional[bool] = None   # None → base > €3,005.06
    vat_deducted_eur: Optional[float] = None  # None → vat_eur × vat_business_pct
    disposal_date: Optional[str] = None
    notes: Optional[str] = None
    invoice_id: Optional[str] = None
    id: Optional[int] = None

    def __post_init__(self) -> None:
        self.asset_class = normalize_asset_class(self.asset_class)
        if self.coefficient_pct is None:
            self.coefficient_pct = class_max_coefficient(self.asset_class)
        if self.vat_business_pct is None:
            self.vat_business_pct = self.business_use_pct
        if self.vat_capital_good is None:
            self.vat_capital_good = is_vat_capital_good(self.base_eur)
        self.vat_capital_good = bool(self.vat_capital_good)

    @property
    def in_use_from(self) -> date:
        return _d(self.start_of_use or self.acquisition_date)

    @property
    def depreciable_base(self) -> float:
        """base × business-use %: the most that can ever be depreciated."""
        return self.base_eur * self.business_use_pct / 100.0

    @property
    def annual_charge(self) -> float:
        return self.depreciable_base * float(self.coefficient_pct) / 100.0

    @property
    def vat_deductible_eur(self) -> float:
        """VAT deducted at acquisition: the stored override, else VAT × VAT business %."""
        if self.vat_deducted_eur is not None:
            return round(self.vat_deducted_eur, 2)
        return round(self.vat_eur * float(self.vat_business_pct) / 100.0, 2)


def _d(value: str | date) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _days_in_year(year: int) -> int:
    return 366 if calendar.isleap(year) else 365


# ---------------------------------------------------------------------------
# Depreciation (pure)
# ---------------------------------------------------------------------------

def _last_day_in_use(asset: FixedAsset) -> Optional[date]:
    """In use until the day before disposal (None = still in use)."""
    return _d(asset.disposal_date) - timedelta(days=1) if asset.disposal_date else None


def accumulated_depreciation(asset: FixedAsset, through: date,
                             threshold_eur: float = DEFAULT_THRESHOLD_EUR) -> float:
    """Depreciation accumulated from start of use through ``through`` (inclusive), unrounded.

    Linear: annual charge × Σ (days in use in year Y / days in year Y), capped
    at base × business %. Dividing by the year's own length (366 in a leap year)
    keeps a full year at exactly the coefficient; in a 365-day year it is the
    usual days/365. Items at or below the threshold are charged in full on the
    acquisition date.
    """
    if is_expensed(asset.base_eur, threshold_eur):
        return asset.depreciable_base if through >= _d(asset.acquisition_date) else 0.0
    start = asset.in_use_from
    last = _last_day_in_use(asset)
    end = min(through, last) if last else through
    if end < start:
        return 0.0
    fraction = 0.0
    for y in range(start.year, end.year + 1):
        y_start = max(start, date(y, 1, 1))
        y_end = min(end, date(y, 12, 31))
        fraction += ((y_end - y_start).days + 1) / _days_in_year(y)
    return min(asset.depreciable_base, asset.annual_charge * fraction)


def depreciation_between(asset: FixedAsset, start: date, end: date,
                         threshold_eur: float = DEFAULT_THRESHOLD_EUR) -> float:
    """Depreciation charged in ``[start, end]`` (inclusive), rounded to cents."""
    if end < start:
        return 0.0
    return round(
        accumulated_depreciation(asset, end, threshold_eur)
        - accumulated_depreciation(asset, start - timedelta(days=1), threshold_eur),
        2,
    )


def _days_in_use(asset: FixedAsset, start: date, end: date) -> int:
    lo = max(start, asset.in_use_from)
    last = _last_day_in_use(asset)
    hi = min(end, last) if last else end
    return max(0, (hi - lo).days + 1)


@dataclass
class DepreciationLine:
    """One asset's charge in a period (the audit-trail record)."""
    asset_id: Optional[int]
    invoice_id: Optional[str]
    description: str
    asset_class: str
    coefficient_pct: float
    base_eur: float
    business_use_pct: float
    period_start: str
    period_end: str
    days: int
    charge_eur: float
    accumulated_eur: float      # through period_end
    net_book_value_eur: float   # depreciable base − accumulated
    expensed: bool              # at/below threshold: charged in full on acquisition
    fully_depreciated: bool


@dataclass
class DepreciationResult:
    year: int
    quarter: int
    ytd: bool
    posting_mode: str
    threshold_eur: float
    period_start: str
    period_end: str
    total_eur: float = 0.0
    lines: list[DepreciationLine] = field(default_factory=list)

    def records(self) -> list[dict[str, Any]]:
        """Per-asset breakdown as plain dicts (for AuditEntry ``records``)."""
        return [asdict(line) for line in self.lines if line.charge_eur]


def compute_depreciation(
    assets: Iterable[FixedAsset],
    year: int,
    quarter: int,
    *,
    ytd: bool = True,
    posting_mode: str = DEFAULT_POSTING_MODE,
    threshold_eur: float = DEFAULT_THRESHOLD_EUR,
) -> DepreciationResult:
    """Depreciation charged in a quarter (``ytd=False``) or from 1 January (``ytd=True``).

    - ``quarterly``: each asset is charged its days in the period.
    - ``annual_q4``: depreciable assets are charged the whole year in Q4 and
      nothing in Q1-Q3.
    Expensed items (≤ threshold) are charged in the quarter of acquisition in
    both modes.
    """
    if posting_mode not in POSTING_MODES:
        raise ValueError(f"posting_mode must be one of {POSTING_MODES}, got {posting_mode!r}")
    q_start, q_end = quarter_date_bounds(year, quarter)
    p_start = date(year, 1, 1) if ytd else q_start
    result = DepreciationResult(
        year=year, quarter=quarter, ytd=ytd, posting_mode=posting_mode,
        threshold_eur=threshold_eur, period_start=p_start.isoformat(), period_end=q_end.isoformat(),
    )
    for a in assets:
        expensed = is_expensed(a.base_eur, threshold_eur)
        if expensed or posting_mode == "quarterly":
            start, end = p_start, q_end
        elif quarter == 4:
            start, end = date(year, 1, 1), date(year, 12, 31)
        else:
            start, end = p_start, p_start - timedelta(days=1)  # empty: posted in Q4
        charge = depreciation_between(a, start, end, threshold_eur)
        accumulated = round(accumulated_depreciation(a, end, threshold_eur), 2)
        result.lines.append(DepreciationLine(
            asset_id=a.id, invoice_id=a.invoice_id, description=a.description,
            asset_class=a.asset_class, coefficient_pct=float(a.coefficient_pct),
            base_eur=round(a.base_eur, 2), business_use_pct=a.business_use_pct,
            period_start=start.isoformat(), period_end=end.isoformat(),
            days=0 if expensed else _days_in_use(a, start, end),
            charge_eur=charge, accumulated_eur=accumulated,
            net_book_value_eur=round(a.depreciable_base - accumulated, 2),
            expensed=expensed,
            fully_depreciated=accumulated >= round(a.depreciable_base, 2),
        ))
    result.total_eur = round(sum(line.charge_eur for line in result.lines), 2)
    return result


@dataclass
class ScheduleRow:
    year: int
    days: int
    charge_eur: float
    accumulated_eur: float
    net_book_value_eur: float


def depreciation_schedule(asset: FixedAsset,
                          threshold_eur: float = DEFAULT_THRESHOLD_EUR) -> list[ScheduleRow]:
    """Year-by-year schedule until fully depreciated or disposed of."""
    rows: list[ScheduleRow] = []
    cap = round(asset.depreciable_base, 2)
    first = _d(asset.acquisition_date).year if is_expensed(asset.base_eur, threshold_eur) \
        else asset.in_use_from.year
    last_day = _last_day_in_use(asset)
    for y in range(first, first + 200):
        end = date(y, 12, 31)
        charge = depreciation_between(asset, date(y, 1, 1), end, threshold_eur)
        accumulated = round(accumulated_depreciation(asset, end, threshold_eur), 2)
        rows.append(ScheduleRow(
            year=y, days=_days_in_use(asset, date(y, 1, 1), end),
            charge_eur=charge, accumulated_eur=accumulated,
            net_book_value_eur=round(cap - accumulated, 2),
        ))
        if accumulated >= cap or (last_day and last_day <= end):
            break
    return rows


# ---------------------------------------------------------------------------
# VAT capital goods (pure)
# ---------------------------------------------------------------------------

@dataclass
class CapitalGoodsVat:
    """Modelo 303 boxes 30/31 contributions for capital goods acquired in a quarter."""
    year: int
    quarter: int
    box_30_base: float = 0.0
    box_31_cuota: float = 0.0
    lines: list[dict[str, Any]] = field(default_factory=list)


def compute_capital_goods_vat(assets: Iterable[FixedAsset], year: int, quarter: int) -> CapitalGoodsVat:
    """Box 30 = base × VAT business %, box 31 = VAT deducted, for capital goods acquired in the quarter."""
    q_start, q_end = quarter_date_bounds(year, quarter)
    out = CapitalGoodsVat(year=year, quarter=quarter)
    for a in assets:
        if not a.vat_capital_good or not (q_start <= _d(a.acquisition_date) <= q_end):
            continue
        base = round(a.base_eur * float(a.vat_business_pct) / 100.0, 2)
        cuota = a.vat_deductible_eur
        out.lines.append({
            "asset_id": a.id, "invoice_id": a.invoice_id, "description": a.description,
            "acquisition_date": a.acquisition_date, "base_eur": round(a.base_eur, 2),
            "vat_eur": round(a.vat_eur, 2), "vat_business_pct": a.vat_business_pct,
            "box_30_base": base, "box_31_cuota": cuota,
        })
    out.box_30_base = round(sum(line["box_30_base"] for line in out.lines), 2)
    out.box_31_cuota = round(sum(line["box_31_cuota"] for line in out.lines), 2)
    return out


@dataclass
class RegularisationRow:
    year: int
    pct_used: float
    recorded: bool          # False → assumed unchanged from the year of acquisition
    delta_points: float     # pct_used − initial pct
    applies: bool           # |delta| > 10 points, in years 2..5
    adjustment_eur: float   # + extra deduction / − VAT to repay (303 box 43 at Q4)


def vat_regularisation_register(asset: FixedAsset,
                                usage_by_year: Optional[dict[int, float]] = None) -> list[RegularisationRow]:
    """Arts. 107-109 LIVA register for one capital good: year of acquisition + 4.

    Annual adjustment = total VAT borne (cuota soportada) / 5 × (pct of the
    year − pct of the year of acquisition), only when the difference exceeds 10
    points. The base of the formula is the whole VAT borne, not the amount
    deducted: the percentages are shares of that whole. Years after the
    disposal year are not listed (the art. 110 one-off disposal adjustment is
    not computed).
    """
    if not asset.vat_capital_good:
        return []
    usage = usage_by_year or {}
    initial = float(asset.vat_business_pct)
    acq_year = _d(asset.acquisition_date).year
    disposal_year = _d(asset.disposal_date).year if asset.disposal_date else None
    rows: list[RegularisationRow] = []
    for y in range(acq_year, acq_year + VAT_REGULARISATION_YEARS):
        if disposal_year is not None and y > disposal_year:
            break
        recorded = y in usage and y != acq_year
        pct = float(usage[y]) if recorded else initial
        delta = round(pct - initial, 2)
        applies = y != acq_year and abs(delta) > VAT_REGULARISATION_MIN_POINTS
        adj = round(asset.vat_eur / VAT_REGULARISATION_YEARS * delta / 100.0, 2) if applies else 0.0
        rows.append(RegularisationRow(y, pct, recorded, delta, applies, adj))
    return rows


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_COLUMNS: tuple[str, ...] = (
    "invoice_id", "description", "acquisition_date", "start_of_use", "base_eur",
    "business_use_pct", "asset_class", "coefficient_pct", "vat_eur", "vat_business_pct",
    "vat_capital_good", "vat_deducted_eur", "disposal_date", "notes",
)
EDITABLE_FIELDS: tuple[str, ...] = tuple(c for c in _COLUMNS if c != "invoice_id")


def ensure_fixed_assets_schema(conn: sqlite3.Connection) -> None:
    """Create ``fixed_assets`` and ``fixed_asset_vat_usage`` if missing (idempotent)."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fixed_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            invoice_id TEXT,                       -- invoices.id it came from (NULL = manual)
            description TEXT NOT NULL,
            acquisition_date TEXT NOT NULL,
            start_of_use TEXT,                     -- NULL = acquisition_date
            base_eur REAL NOT NULL,                -- unit base, ex-VAT
            business_use_pct REAL NOT NULL DEFAULT 100,
            asset_class TEXT NOT NULL DEFAULT 'other',
            coefficient_pct REAL NOT NULL,
            vat_eur REAL NOT NULL DEFAULT 0,
            vat_business_pct REAL NOT NULL DEFAULT 100,
            vat_capital_good INTEGER NOT NULL DEFAULT 0,
            vat_deducted_eur REAL,                 -- NULL = vat_eur × vat_business_pct
            disposal_date TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_fixed_assets_invoice ON fixed_assets(invoice_id);
        CREATE TABLE IF NOT EXISTS fixed_asset_vat_usage (
            asset_id INTEGER NOT NULL REFERENCES fixed_assets(id) ON DELETE CASCADE,
            year INTEGER NOT NULL,
            pct_used REAL NOT NULL,
            PRIMARY KEY (asset_id, year)
        );
    """)


def _validate(asset: FixedAsset) -> None:
    if not (asset.description or "").strip():
        raise ValueError("description is required")
    if not asset.acquisition_date:
        raise ValueError("acquisition_date is required")
    for name in ("acquisition_date", "start_of_use", "disposal_date"):
        value = getattr(asset, name)
        if value:
            try:
                _d(value)
            except ValueError as exc:
                raise ValueError(f"{name} must be an ISO date (YYYY-MM-DD), got {value!r}") from exc
    if asset.base_eur is None or asset.base_eur <= 0:
        raise ValueError(f"base_eur must be positive, got {asset.base_eur}")
    for name in ("business_use_pct", "vat_business_pct"):
        pct = getattr(asset, name)
        if not 0.0 <= float(pct) <= 100.0:
            raise ValueError(f"{name} must be between 0 and 100, got {pct}")
    max_coef = class_max_coefficient(asset.asset_class)
    if not 0.0 < float(asset.coefficient_pct) <= max_coef:
        raise ValueError(
            f"coefficient_pct {asset.coefficient_pct} must be > 0 and ≤ {max_coef} "
            f"(maximum for class {asset.asset_class!r})"
        )
    if asset.disposal_date and _d(asset.disposal_date) < _d(asset.acquisition_date):
        raise ValueError("disposal_date is before acquisition_date")


def _to_row(asset: FixedAsset) -> dict[str, Any]:
    row = {c: getattr(asset, c) for c in _COLUMNS}
    row["vat_capital_good"] = 1 if asset.vat_capital_good else 0
    for c in ("acquisition_date", "start_of_use", "disposal_date"):
        row[c] = _d(row[c]).isoformat() if row[c] else None
    return row


def _from_row(row: sqlite3.Row | dict) -> FixedAsset:
    r = dict(row)
    return FixedAsset(
        id=r["id"], invoice_id=r["invoice_id"], description=r["description"],
        acquisition_date=r["acquisition_date"], start_of_use=r["start_of_use"],
        base_eur=float(r["base_eur"]), business_use_pct=float(r["business_use_pct"]),
        asset_class=r["asset_class"], coefficient_pct=float(r["coefficient_pct"]),
        vat_eur=float(r["vat_eur"] or 0.0), vat_business_pct=float(r["vat_business_pct"]),
        vat_capital_good=bool(r["vat_capital_good"]), vat_deducted_eur=r["vat_deducted_eur"],
        disposal_date=r["disposal_date"], notes=r["notes"],
    )


def add_fixed_asset(conn: sqlite3.Connection, asset: FixedAsset) -> int:
    """Validate and insert an asset; returns its id. Does not touch the invoice."""
    ensure_fixed_assets_schema(conn)
    _validate(asset)
    row = _to_row(asset)
    cols = ", ".join(row)
    cur = conn.execute(
        f"INSERT INTO fixed_assets ({cols}) VALUES ({', '.join(':' + c for c in row)})", row
    )
    conn.commit()
    asset_id = int(cur.lastrowid)
    log.info("ℹ️ Fixed asset %d registered: %s (%s, base %.2f)", asset_id, asset.description,
             asset.asset_class, asset.base_eur)
    return asset_id


def asset_from_invoice(conn: sqlite3.Connection, invoice_id: str) -> FixedAsset:
    """Prefill an asset from an expense invoice (base, VAT, dates, business-use %, class)."""
    inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if inv is None:
        raise KeyError(f"No invoice with id {invoice_id!r}")
    inv = dict(inv)
    if inv["direction"] != "in":
        raise ValueError("Only expense invoices (direction 'in') can become fixed assets")
    irpf_pct = inv.get("deductible_pct_irpf")
    vat_pct = inv.get("deductible_pct_vat")
    return FixedAsset(
        invoice_id=invoice_id,
        description=(inv.get("description") or inv.get("vendor_name") or inv["filename"])[:200],
        acquisition_date=(inv.get("invoice_date") or "")[:10],
        base_eur=float(inv.get("subtotal_eur") or 0.0),
        business_use_pct=float(irpf_pct if irpf_pct is not None else 100.0),
        vat_business_pct=float(vat_pct if vat_pct is not None else 100.0),
        asset_class=normalize_asset_class(inv.get("asset_class")),
        vat_eur=float(inv.get("iva_amount") or 0.0),
    )


def register_asset_from_invoice(conn: sqlite3.Connection, asset: FixedAsset) -> int:
    """Insert ``asset`` and flag its invoice ``is_capital_asset`` (+ ``asset_class``).

    The flag removes the invoice from the Modelo 130 expenses; its cost enters
    through depreciation instead. Both ledger columns survive re-extraction
    (they are ledger-only in ``upsert_invoice``).
    """
    if not asset.invoice_id:
        raise ValueError("asset.invoice_id is required")
    if conn.execute("SELECT 1 FROM invoices WHERE id = ?", (asset.invoice_id,)).fetchone() is None:
        raise KeyError(f"No invoice with id {asset.invoice_id!r}")
    asset_id = add_fixed_asset(conn, asset)
    conn.execute(
        "UPDATE invoices SET is_capital_asset = 1, asset_class = ? WHERE id = ?",
        (asset.asset_class, asset.invoice_id),
    )
    conn.commit()
    return asset_id


def update_fixed_asset(conn: sqlite3.Connection, asset_id: int, changes: dict[str, Any]) -> list[str]:
    """Apply edits; returns the fields that changed.

    Changing ``asset_class`` without an explicit ``coefficient_pct`` resets the
    coefficient to the new class maximum.
    """
    unknown = [f for f in changes if f not in EDITABLE_FIELDS]
    if unknown:
        raise ValueError(f"Not editable: {', '.join(unknown)}")
    current = get_fixed_asset(conn, asset_id)
    if current is None:
        raise KeyError(f"No fixed asset with id {asset_id}")
    data = asdict(current)
    clean = {k: (None if isinstance(v, str) and not v.strip() else v) for k, v in changes.items()}
    if "asset_class" in clean:
        clean["asset_class"] = normalize_asset_class(clean["asset_class"])
        if clean["asset_class"] != current.asset_class and "coefficient_pct" not in clean:
            clean["coefficient_pct"] = None
    data.update(clean)
    updated = FixedAsset(**data)
    _validate(updated)
    new_row, old_row = _to_row(updated), _to_row(current)
    changed = [c for c in EDITABLE_FIELDS if new_row[c] != old_row[c]]
    if changed:
        sets = ", ".join(f"{c} = :{c}" for c in changed)
        conn.execute(
            f"UPDATE fixed_assets SET {sets}, updated_at = datetime('now') WHERE id = :_id",
            {**{c: new_row[c] for c in changed}, "_id": asset_id},
        )
        if "asset_class" in changed and current.invoice_id:
            conn.execute("UPDATE invoices SET asset_class = ? WHERE id = ?",
                         (updated.asset_class, current.invoice_id))
        conn.commit()
        log.info("ℹ️ Fixed asset %d edited: %s", asset_id, ", ".join(changed))
    return changed


def delete_fixed_asset(conn: sqlite3.Connection, asset_id: int) -> None:
    """Delete an asset; unflag its invoice when no other asset comes from it."""
    asset = get_fixed_asset(conn, asset_id)
    if asset is None:
        raise KeyError(f"No fixed asset with id {asset_id}")
    conn.execute("DELETE FROM fixed_asset_vat_usage WHERE asset_id = ?", (asset_id,))
    conn.execute("DELETE FROM fixed_assets WHERE id = ?", (asset_id,))
    if asset.invoice_id and not conn.execute(
        "SELECT 1 FROM fixed_assets WHERE invoice_id = ?", (asset.invoice_id,)
    ).fetchone():
        conn.execute("UPDATE invoices SET is_capital_asset = 0 WHERE id = ?", (asset.invoice_id,))
        log.info("ℹ️ Invoice %s no longer a capital asset (its last fixed asset was deleted)",
                 asset.invoice_id)
    conn.commit()
    log.info("ℹ️ Fixed asset %d deleted: %s", asset_id, asset.description)


def get_fixed_asset(conn: sqlite3.Connection, asset_id: int) -> Optional[FixedAsset]:
    ensure_fixed_assets_schema(conn)
    row = conn.execute("SELECT * FROM fixed_assets WHERE id = ?", (asset_id,)).fetchone()
    return _from_row(row) if row else None


def load_fixed_assets(conn: sqlite3.Connection) -> list[FixedAsset]:
    ensure_fixed_assets_schema(conn)
    rows = conn.execute("SELECT * FROM fixed_assets ORDER BY acquisition_date, id").fetchall()
    return [_from_row(r) for r in rows]


def assets_for_invoice(conn: sqlite3.Connection, invoice_id: str) -> list[FixedAsset]:
    ensure_fixed_assets_schema(conn)
    rows = conn.execute(
        "SELECT * FROM fixed_assets WHERE invoice_id = ? ORDER BY id", (invoice_id,)
    ).fetchall()
    return [_from_row(r) for r in rows]


def capital_asset_invoice_ids(conn: sqlite3.Connection) -> set[str]:
    """Ids of expense invoices flagged ``is_capital_asset`` (not expensed in the 130)."""
    rows = conn.execute(
        "SELECT id FROM invoices WHERE direction = 'in' AND COALESCE(is_capital_asset, 0) = 1"
    ).fetchall()
    return {r[0] for r in rows}


def set_vat_usage(conn: sqlite3.Connection, asset_id: int, year: int, pct_used: float) -> None:
    """Record the VAT business-use % applied to a capital good in ``year``."""
    if not 0.0 <= float(pct_used) <= 100.0:
        raise ValueError(f"pct_used must be between 0 and 100, got {pct_used}")
    ensure_fixed_assets_schema(conn)
    conn.execute(
        """INSERT INTO fixed_asset_vat_usage (asset_id, year, pct_used) VALUES (?, ?, ?)
           ON CONFLICT(asset_id, year) DO UPDATE SET pct_used = excluded.pct_used""",
        (asset_id, int(year), float(pct_used)),
    )
    conn.commit()


def load_vat_usage(conn: sqlite3.Connection, asset_id: int) -> dict[int, float]:
    ensure_fixed_assets_schema(conn)
    rows = conn.execute(
        "SELECT year, pct_used FROM fixed_asset_vat_usage WHERE asset_id = ?", (asset_id,)
    ).fetchall()
    return {int(r[0]): float(r[1]) for r in rows}


# ---------------------------------------------------------------------------
# DB-backed entry points (consumed by the tax engine: 130 and 303)
# ---------------------------------------------------------------------------

def depreciation_for_period(
    year: int,
    quarter: int,
    conn: sqlite3.Connection,
    *,
    ytd: bool = True,
    config: Optional[dict] = None,
) -> DepreciationResult:
    """Depreciation of every stored asset for a quarter or YTD, per ``config['assets']``."""
    settings = asset_settings(config)
    return compute_depreciation(
        load_fixed_assets(conn), year, quarter, ytd=ytd,
        posting_mode=settings.posting_mode, threshold_eur=settings.threshold_eur,
    )


def capital_goods_vat_for_period(year: int, quarter: int, conn: sqlite3.Connection) -> CapitalGoodsVat:
    """303 boxes 30/31 contributions of the capital goods acquired in the quarter."""
    return compute_capital_goods_vat(load_fixed_assets(conn), year, quarter)


def vat_regularisation_for_year(conn: sqlite3.Connection, year: int) -> list[dict[str, Any]]:
    """Every capital good's regularisation row for ``year`` (feeds 303 box 43 at Q4)."""
    out = []
    for a in load_fixed_assets(conn):
        for row in vat_regularisation_register(a, load_vat_usage(conn, a.id)):
            if row.year == year:
                out.append({"asset_id": a.id, "description": a.description, **asdict(row)})
    return out
