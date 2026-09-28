"""Vendor registry: per-vendor tax defaults applied to expense invoices (issue #91).

Most expense invoices carry no vendor VAT id after OCR, so their tax treatment
falls back to heuristics. Vendors repeat every month, so a small registry makes
classification deterministic.

**Source of truth** — the git-ignored ``vendors.json`` at the repo root (same
pattern as ``classification_rules.json``: the repo ships only
``vendors.json.example`` with fake vendors). There is no ``vendors`` table: the
file is read on demand and its defaults are *written onto invoice rows*, so the
tax engine only ever reads ``invoices``.

**Matching** (first hit wins):

1. ``folder`` — the first path component of the invoice ``filename`` (invoices
   live in one sub-folder per vendor under the configured ``invoice_in_dir``),
   compared with the vendor ``key`` / ``aliases``.
2. ``vat_id`` — the invoice's normalised vendor VAT id against ``vat_id`` /
   ``alt_vat_ids``.
3. ``name`` — the vendor name against ``key`` / ``aliases`` / ``legal_entity``
   (whole-token match; the longest alias wins).

**Applying** (``apply_vendor_registry``, also run after every OCR extraction):

- Registry defaults (``tax_treatment``, ``deductible_pct_vat``,
  ``deductible_pct_irpf``, ``activity_type``, ``asset_class``) replace the
  heuristic values on the row — except fields in the row's ``locked_fields``
  (user edits always win). A default left empty in the registry leaves the row
  untouched. Editing ``tax_treatment`` keeps the legacy ``vat_treatment`` in sync.
- ``vendor_vat_id_norm``, ``geo_region`` (when ``UNKNOWN``) and ``supply_country``
  are only filled when missing — an id read from the document itself wins.
- Registry writes never add to ``locked_fields`` and never stamp ``reviewed_at``,
  so re-applying after a registry edit keeps working.

CLI::

    python -m src.vendor_registry seed-from-xlsx <vendor-list.xlsx> [--registry PATH]
    python -m src.vendor_registry apply [--db PATH] [--registry PATH]
"""
from __future__ import annotations

import argparse
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any, Iterable, Optional

from src._json_store import JsonCache
from src.database import (
    TAX_TREATMENTS_IN,
    _EU_VAT_PREFIXES,  # the one EU-country list; shared rather than duplicated
    derive_geo_region_from_nif,
    get_connection,
    init_db,
    legacy_vat_treatment_for,
    normalize_vat_id,
    parse_locked_fields,
    upsert_invoice,
)
from src.logger import get_logger

log = get_logger(__name__)

REGISTRY_PATH = Path(__file__).parent.parent / "vendors.json"

# Business activities, in the Stripe classifier's vocabulary (src.models.ActivityType),
# and the IAE epigraph each one is registered under (P&L per activity for the Renta).
ACTIVITIES: tuple[str, ...] = ("COACHING", "NEWSLETTER", "ILLUSTRATIONS")
ACTIVITY_IAE: dict[str, str] = {"COACHING": "826", "NEWSLETTER": "751", "ILLUSTRATIONS": "861"}

# Spreadsheet activity labels → ACTIVITIES (the seed/import accepts either).
_ACTIVITY_ALIASES: dict[str, str] = {
    "coaching": "COACHING", "teaching": "COACHING", "training": "COACHING",
    "newsletter": "NEWSLETTER", "publicity": "NEWSLETTER",
    "illustration": "ILLUSTRATIONS", "illustrations": "ILLUSTRATIONS",
}

# Invoice column ← Vendor attribute. The registry value replaces the row's value
# unless the column is locked or the registry leaves it empty.
_DEFAULT_FIELDS: dict[str, str] = {
    "tax_treatment": "default_tax_treatment",
    "deductible_pct_vat": "default_deductible_pct_vat",
    "deductible_pct_irpf": "default_deductible_pct_irpf",
    "activity_type": "activity",
    "asset_class": "asset_class",
}

_MIN_ALIAS_LEN = 3  # shorter aliases would match inside unrelated vendor names
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_vendor_name(raw: Optional[str]) -> str:
    """Matching form of a vendor name / folder: accents stripped, lower-case,
    punctuation collapsed to single spaces. ``"Acme-Tools, S.L."`` → ``"acme tools s l"``."""
    if not raw:
        return ""
    text = unicodedata.normalize("NFKD", str(raw)).encode("ascii", "ignore").decode("ascii")
    return _NON_ALNUM_RE.sub(" ", text.lower()).strip()


def invoice_folder(filename: Optional[str]) -> Optional[str]:
    """First path component of an invoice ``filename`` (``"acme/2025-01.pdf"`` → ``"acme"``).

    ``filename`` is stored relative to the invoice directory, with either separator.
    Returns ``None`` for a file at the directory root.
    """
    if not filename:
        return None
    parts = [p for p in re.split(r"[\\/]", filename) if p]
    return parts[0] if len(parts) > 1 else None


def _opt_str(value: Any) -> Optional[str]:
    if value is None or (isinstance(value, float) and value != value):  # None / NaN
        return None
    text = str(value).strip()
    return text or None


def _opt_pct(value: Any) -> Optional[float]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, float) and value != value:  # NaN from a pandas editor
        return None
    return float(value)


def _str_list(value: Any) -> tuple[str, ...]:
    """Accept a list or a comma-separated string; drop blanks and duplicates, keep order."""
    if value is None or (isinstance(value, float) and value != value):
        return ()
    items = value.split(",") if isinstance(value, str) else list(value)
    out: list[str] = []
    for item in items:
        text = _opt_str(item)
        if text and text not in out:
            out.append(text)
    return tuple(out)


@dataclass(frozen=True)
class Vendor:
    """One registry entry. Empty defaults mean "leave the invoice's value alone"."""

    key: str
    aliases: tuple[str, ...] = ()
    legal_entity: Optional[str] = None
    country: Optional[str] = None
    vat_id: Optional[str] = None
    alt_vat_ids: tuple[str, ...] = ()
    default_tax_treatment: Optional[str] = None
    default_deductible_pct_vat: Optional[float] = None
    default_deductible_pct_irpf: Optional[float] = None
    activity: Optional[str] = None
    asset_class: Optional[str] = None
    recurrence: Optional[str] = None
    notes: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Vendor":
        """Build and validate an entry; raises ``ValueError`` on invalid values."""
        key = normalize_vendor_name(data.get("key"))
        if not key:
            raise ValueError(f"Vendor entry without a key: {data!r}")
        country = _opt_str(data.get("country"))
        if country is not None:
            country = country.upper()
            if not re.fullmatch(r"[A-Z]{2}", country):
                raise ValueError(f"Vendor {key!r}: country must be an ISO-2 code, got {country!r}")
        treatment = _opt_str(data.get("default_tax_treatment"))
        if treatment is not None and treatment not in TAX_TREATMENTS_IN:
            raise ValueError(
                f"Vendor {key!r}: default_tax_treatment {treatment!r} is not one of "
                f"{', '.join(TAX_TREATMENTS_IN)}"
            )
        pcts = {}
        for name in ("default_deductible_pct_vat", "default_deductible_pct_irpf"):
            pct = _opt_pct(data.get(name))
            if pct is not None and not 0.0 <= pct <= 100.0:
                raise ValueError(f"Vendor {key!r}: {name} must be between 0 and 100, got {pct}")
            pcts[name] = pct
        activity = _opt_str(data.get("activity"))
        if activity is not None:
            activity = _ACTIVITY_ALIASES.get(activity.lower(), activity.upper())
            if activity not in ACTIVITIES:
                raise ValueError(
                    f"Vendor {key!r}: activity {activity!r} is not one of {', '.join(ACTIVITIES)}"
                )
        return cls(
            key=key,
            aliases=_str_list(data.get("aliases")),
            legal_entity=_opt_str(data.get("legal_entity")),
            country=country,
            vat_id=_opt_str(data.get("vat_id")),
            alt_vat_ids=_str_list(data.get("alt_vat_ids")),
            default_tax_treatment=treatment,
            default_deductible_pct_vat=pcts["default_deductible_pct_vat"],
            default_deductible_pct_irpf=pcts["default_deductible_pct_irpf"],
            activity=activity,
            asset_class=_opt_str(data.get("asset_class")),
            recurrence=_opt_str(data.get("recurrence")),
            notes=_opt_str(data.get("notes")),
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["aliases"] = list(self.aliases)
        data["alt_vat_ids"] = list(self.alt_vat_ids)
        return data

    def geo_region(self) -> Optional[str]:
        """Region implied by the vendor's country (VAT id as fallback), or ``None``."""
        if self.country:
            if self.country == "ES":
                return "SPAIN"
            return "EU_NOT_SPAIN" if self.country in _EU_VAT_PREFIXES else "OUTSIDE_EU"
        return derive_geo_region_from_nif(self.vat_id) if self.vat_id else None


@dataclass(frozen=True)
class VendorMatch:
    vendor: Vendor
    signal: str  # "folder" | "vat_id" | "name"


@dataclass
class VendorRegistry:
    """Validated, indexed set of vendors."""

    vendors: list[Vendor] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._by_alias: dict[str, Vendor] = {}
        self._by_vat: dict[str, Vendor] = {}
        self._names: list[tuple[tuple[str, ...], Vendor]] = []
        seen: set[str] = set()
        for vendor in self.vendors:
            if vendor.key in seen:
                raise ValueError(f"Duplicate vendor key {vendor.key!r}")
            seen.add(vendor.key)
            for alias in (vendor.key, *vendor.aliases):
                self._by_alias.setdefault(normalize_vendor_name(alias), vendor)
            for vat in (vendor.vat_id, *vendor.alt_vat_ids):
                norm = normalize_vat_id(vat)
                if norm:
                    self._by_vat.setdefault(norm, vendor)
            for alias in (vendor.key, *vendor.aliases, vendor.legal_entity):
                norm = normalize_vendor_name(alias)
                if len(norm) >= _MIN_ALIAS_LEN:
                    self._names.append((tuple(norm.split()), vendor))
        # Longest alias first, so "google cloud" beats "google".
        self._names.sort(key=lambda item: -len(" ".join(item[0])))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "VendorRegistry":
        return cls([Vendor.from_dict(v) for v in data.get("vendors", [])])

    def to_dict(self) -> dict[str, Any]:
        return {"vendors": [v.to_dict() for v in self.vendors]}

    def lookup(self, name: Optional[str]) -> Optional[Vendor]:
        """Entry whose key or alias equals ``name`` once normalised (exact, not fuzzy)."""
        return self._by_alias.get(normalize_vendor_name(name))

    def match(
        self,
        filename: Optional[str] = None,
        vendor_nif: Optional[str] = None,
        vendor_name: Optional[str] = None,
    ) -> Optional[VendorMatch]:
        """Registry entry for an invoice, by folder → VAT id → name (see module doc)."""
        folder = normalize_vendor_name(invoice_folder(filename))
        if folder and folder in self._by_alias:
            return VendorMatch(self._by_alias[folder], "folder")
        vat = normalize_vat_id(vendor_nif)
        if vat and vat in self._by_vat:
            return VendorMatch(self._by_vat[vat], "vat_id")
        tokens = normalize_vendor_name(vendor_name).split()
        if tokens:
            for alias, vendor in self._names:
                n = len(alias)
                if any(tuple(tokens[i:i + n]) == alias for i in range(len(tokens) - n + 1)):
                    return VendorMatch(vendor, "name")
        return None

    def match_invoice(self, row: dict[str, Any]) -> Optional[VendorMatch]:
        """``match`` over an ``invoices`` row (uses ``vendor_vat_id_norm`` when ``vendor_nif`` is empty)."""
        return self.match(
            row.get("filename"),
            row.get("vendor_nif") or row.get("vendor_vat_id_norm"),
            row.get("vendor_name"),
        )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

_warned_missing: set[Path] = set()


def _missing_registry(path: Path) -> dict[str, Any]:
    if path in _warned_missing:
        return {"vendors": []}
    _warned_missing.add(path)
    log.warning("⚠️ Vendor registry %s not found — every expense invoice will be flagged as an "
                "unknown vendor. Copy vendors.json.example or run seed-from-xlsx.", path.name)
    return {"vendors": []}


_cache = JsonCache(REGISTRY_PATH, on_missing=_missing_registry)


def _read_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _missing_registry(path)
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def registry_path() -> Path:
    """The default registry file (``vendors.json`` at the repo root)."""
    return REGISTRY_PATH


def load_registry(path: Optional[str | Path] = None) -> VendorRegistry:
    """Load the registry. The default file is cached; an explicit ``path`` is read fresh."""
    if path is not None and Path(path) != REGISTRY_PATH:
        return VendorRegistry.from_dict(_read_file(Path(path)))
    return VendorRegistry.from_dict(_cache.load())


def save_registry(registry: VendorRegistry, path: Optional[str | Path] = None) -> Path:
    """Write the registry (sorted by key). Returns the file written."""
    data = VendorRegistry(sorted(registry.vendors, key=lambda v: v.key)).to_dict()
    target = Path(path) if path is not None else REGISTRY_PATH
    if target == REGISTRY_PATH:
        _cache.save(data)
    else:
        target.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log.info("ℹ️ Vendor registry saved to %s (%d vendors)", target, len(registry.vendors))
    return target


# ---------------------------------------------------------------------------
# Applying the registry to invoice rows
# ---------------------------------------------------------------------------

@dataclass
class ApplyResult:
    scanned: int = 0
    matched: int = 0
    unmatched: int = 0
    rows_updated: int = 0
    by_signal: dict[str, int] = field(default_factory=dict)
    field_updates: dict[str, int] = field(default_factory=dict)


def registry_updates_for(row: dict[str, Any], vendor: Vendor) -> dict[str, Any]:
    """Column → value changes the registry implies for one ``invoices`` row.

    Locked fields are never touched; empty registry defaults change nothing.
    """
    locked = set(parse_locked_fields(row.get("locked_fields")))
    updates: dict[str, Any] = {}
    for column, attr in _DEFAULT_FIELDS.items():
        value = getattr(vendor, attr)
        if value is None or column in locked:
            continue
        current = row.get(column)
        if isinstance(value, float) and current is not None:
            if abs(float(current) - value) < 1e-9:
                continue
        elif current == value:
            continue
        updates[column] = value
    if "tax_treatment" in updates:
        legacy = legacy_vat_treatment_for(updates["tax_treatment"])
        if legacy and legacy != row.get("vat_treatment"):
            updates["vat_treatment"] = legacy

    if not row.get("vendor_vat_id_norm"):
        norm = normalize_vat_id(vendor.vat_id)
        if norm:
            updates["vendor_vat_id_norm"] = norm
    if row.get("geo_region") in (None, "", "UNKNOWN"):
        geo = vendor.geo_region()
        if geo:
            updates["geo_region"] = geo
    if not row.get("supply_country") and vendor.country:
        updates["supply_country"] = vendor.country
    return updates


def _load_expense_rows(conn, invoice_ids: Optional[Iterable[str]]) -> list[dict[str, Any]]:
    if invoice_ids is None:
        rows = conn.execute("SELECT * FROM invoices WHERE direction = 'in'").fetchall()
    else:
        ids = list(invoice_ids)
        if not ids:
            return []
        marks = ", ".join("?" for _ in ids)
        rows = conn.execute(
            f"SELECT * FROM invoices WHERE direction = 'in' AND id IN ({marks})", ids
        ).fetchall()
    return [dict(r) for r in rows]


def apply_vendor_registry(
    registry: Optional[VendorRegistry] = None,
    db_path: Optional[str | Path] = None,
    invoice_ids: Optional[Iterable[str]] = None,
) -> ApplyResult:
    """Write registry defaults onto stored expense invoices (all, or ``invoice_ids``).

    Idempotent. See the module docstring for which columns are replaced vs filled.
    """
    registry = registry if registry is not None else load_registry()
    result = ApplyResult()
    conn = get_connection(db_path)
    try:
        for row in _load_expense_rows(conn, invoice_ids):
            result.scanned += 1
            match = registry.match_invoice(row)
            if match is None:
                result.unmatched += 1
                continue
            result.matched += 1
            result.by_signal[match.signal] = result.by_signal.get(match.signal, 0) + 1
            updates = registry_updates_for(row, match.vendor)
            if not updates:
                continue
            sets = ", ".join(f"{c} = :{c}" for c in updates)
            conn.execute(f"UPDATE invoices SET {sets} WHERE id = :_id", {**updates, "_id": row["id"]})
            result.rows_updated += 1
            for column in updates:
                result.field_updates[column] = result.field_updates.get(column, 0) + 1
        conn.commit()
    finally:
        conn.close()
    log.info(
        "ℹ️ Vendor registry applied: %d scanned, %d matched %s, %d unknown vendor(s), %d row(s) updated %s",
        result.scanned, result.matched, result.by_signal, result.unmatched,
        result.rows_updated, result.field_updates,
    )
    return result


def upsert_invoice_with_registry(
    data: dict,
    registry: Optional[VendorRegistry] = None,
    db_path: Optional[str | Path] = None,
) -> str:
    """``upsert_invoice`` then apply the registry to that row. The OCR entry point."""
    record_id = upsert_invoice(data, db_path=db_path)
    if data.get("direction", "in") == "in":
        apply_vendor_registry(registry, db_path=db_path, invoice_ids=[record_id])
    return record_id


def find_unmatched_invoices(
    registry: Optional[VendorRegistry] = None,
    db_path: Optional[str | Path] = None,
    include_excluded: bool = False,
) -> list[dict[str, Any]]:
    """Expense invoices no registry entry matches (the "unknown vendor" review list).

    Each row carries ``suggested_key``: the invoice folder, else the normalised vendor name.
    """
    registry = registry if registry is not None else load_registry()
    conn = get_connection(db_path)
    try:
        rows = _load_expense_rows(conn, None)
    finally:
        conn.close()
    unmatched = []
    for row in rows:
        if row.get("excluded") and not include_excluded:
            continue
        if registry.match_invoice(row) is None:
            row["suggested_key"] = (normalize_vendor_name(invoice_folder(row.get("filename")))
                                    or normalize_vendor_name(row.get("vendor_name")) or None)
            unmatched.append(row)
    unmatched.sort(key=lambda r: (r.get("suggested_key") or "", r.get("invoice_date") or ""))
    return unmatched


# ---------------------------------------------------------------------------
# Spreadsheet import (vendor, activity, recurrence)
# ---------------------------------------------------------------------------

_XLSX_COLUMNS: dict[str, tuple[str, ...]] = {
    "name": ("vendor", "item", "name", "key"),
    "activity": ("activity", "business"),
    "recurrence": ("recurrence", "recurrency"),
}


@dataclass
class SeedResult:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)


def seed_from_xlsx(
    source: str | Path | IO[bytes],
    registry: Optional[VendorRegistry] = None,
) -> tuple[VendorRegistry, SeedResult]:
    """Merge a simple vendor spreadsheet into ``registry`` (empty when ``None``).

    The first sheet needs a header row with a vendor column (``vendor``/``item``/
    ``name``) and optionally ``activity``/``business`` and ``recurrence``/``recurrency``.
    New vendors are added; existing ones (matched by key or alias) only get an
    empty ``activity`` / ``recurrence`` filled — hand-edited values are kept.
    """
    import openpyxl

    wb = openpyxl.load_workbook(source, read_only=True, data_only=True)
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    if not rows:
        return registry or VendorRegistry(), SeedResult()
    header = [normalize_vendor_name(h) for h in rows[0]]
    cols: dict[str, Optional[int]] = {}
    for logical, names in _XLSX_COLUMNS.items():
        cols[logical] = next((header.index(n) for n in names if n in header), None)
    if cols["name"] is None:
        raise ValueError(f"No vendor column found in the spreadsheet header: {rows[0]!r}")

    vendors = list(registry.vendors) if registry else []
    result = SeedResult()
    for raw in rows[1:]:
        def cell(logical: str) -> Optional[str]:
            idx = cols[logical]
            return _opt_str(raw[idx]) if idx is not None and idx < len(raw) else None

        name = cell("name")
        if not name:
            continue
        activity_raw = cell("activity")
        activity = _ACTIVITY_ALIASES.get(activity_raw.lower()) if activity_raw else None
        if activity_raw and activity is None:
            log.warning("⚠️ Vendor %r: unknown activity %r ignored", name, activity_raw)
        recurrence = cell("recurrence")

        existing = VendorRegistry(vendors).lookup(name)
        if existing is None:
            key = normalize_vendor_name(name)
            if not key:
                result.skipped.append(name)
                continue
            vendors.append(Vendor(key=key, activity=activity, recurrence=recurrence))
            result.added.append(key)
            continue
        changes: dict[str, Any] = {}
        if existing.activity is None and activity:
            changes["activity"] = activity
        if existing.recurrence is None and recurrence:
            changes["recurrence"] = recurrence
        if changes:
            merged = Vendor.from_dict({**existing.to_dict(), **changes})
            vendors[vendors.index(existing)] = merged
            result.updated.append(existing.key)
        else:
            result.unchanged.append(existing.key)
    return VendorRegistry(vendors), result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cmd_seed(args: argparse.Namespace) -> None:
    target = Path(args.registry) if args.registry else REGISTRY_PATH
    registry, res = seed_from_xlsx(Path(args.xlsx), load_registry(target))
    save_registry(registry, target)
    log.info("✅ Seeded %s: %d added, %d updated, %d unchanged, %d skipped → %d vendors",
             target, len(res.added), len(res.updated), len(res.unchanged), len(res.skipped),
             len(registry.vendors))


def _cmd_apply(args: argparse.Namespace) -> None:
    registry = load_registry(Path(args.registry) if args.registry else None)
    init_db(args.db)  # same startup migrations the app runs, so ledger columns exist
    apply_vendor_registry(registry, db_path=args.db)
    unmatched = find_unmatched_invoices(registry, db_path=args.db)
    folders: dict[str, int] = {}
    for row in unmatched:
        folders[row["suggested_key"] or "?"] = folders.get(row["suggested_key"] or "?", 0) + 1
    log.info("⚠️ %d unknown-vendor invoice(s) (not excluded), by suggested key: %s",
             len(unmatched), dict(sorted(folders.items(), key=lambda kv: -kv[1])))


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m src.vendor_registry", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p_seed = sub.add_parser("seed-from-xlsx", help="Merge a vendor spreadsheet into the registry")
    p_seed.add_argument("xlsx")
    p_seed.add_argument("--registry", help=f"Registry file (default: {REGISTRY_PATH.name})")
    p_seed.set_defaults(func=_cmd_seed)
    p_apply = sub.add_parser("apply", help="Apply the registry to stored expense invoices")
    p_apply.add_argument("--db", help="SQLite file (default: data/accounting.db)")
    p_apply.add_argument("--registry", help=f"Registry file (default: {REGISTRY_PATH.name})")
    p_apply.set_defaults(func=_cmd_apply)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
