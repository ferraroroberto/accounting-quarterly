"""Modelo 347 purchases side: acquisitions from Spanish vendors above €3,005.06 a year (#103).

The sales side is ``src.tax_engine.compute_modelo_347``. Rules applied here
(Reglamento General de gestión e inspección tributaria, RD 1065/2007):

- art. 33.1: declare every person or entity with whom the operations of the
  calendar year, *in aggregate*, "hayan superado la cifra de 3.005,06 euros"
  — strictly more than 3,005.06 — broken down by quarter, with acquisitions
  counted separately from supplies;
- the amount is the operation's total **IVA incluido** (AEAT Modelo 347
  instructions): invoice base + VAT, never ``total_eur`` (that nets the IRPF
  withheld, which is a withholding on payment, not a smaller operation);
- art. 33.2.i: operations already reported in another periodic information
  return are excluded — intra-EU acquisitions (``INTRA_EU_RC``, Modelo 349)
  and purchases with IRPF withheld by the taxpayer (``irpf_amount`` > 0,
  Modelos 111/190 or 115/180);
- art. 33.2.g: imports and operations with non-Spanish counterparties are
  excluded, so only vendors identified by a Spanish NIF are declared.

Invoices are keyed by invoice date; ``excluded`` invoices (duplicates,
receipts) are skipped. Vendors without a NIF cannot be declared; those whose
year total passes the threshold are listed in ``unidentified``.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from src.database import normalize_vat_id
from src.logger import get_logger
from src.tax_models import AuditEntry

log = get_logger(__name__)

THRESHOLD_EUR = 3005.06

EXCLUDED_349 = "intra-EU acquisition, declared in the Modelo 349 (art. 33.2.i RGAT)"
EXCLUDED_WITHHOLDING = "IRPF withheld, declared in the withholding returns (art. 33.2.i RGAT)"
EXCLUDED_FOREIGN = "non-Spanish vendor / services from abroad (art. 33.2.g RGAT)"


@dataclass
class Modelo347PurchaseRow:
    """One declarable vendor: Spanish NIF (without the ES prefix), name, VAT-inclusive totals."""
    nif: str
    name: str
    total: float
    quarter_breakdown: dict[int, float] = field(default_factory=dict)
    n_invoices: int = 0


@dataclass
class Modelo347PurchasesResult:
    year: int
    threshold: float = THRESHOLD_EUR
    rows: list[Modelo347PurchaseRow] = field(default_factory=list)       # declarable (> threshold)
    below_threshold: int = 0                                             # Spanish vendors not declared
    unidentified: list[Modelo347PurchaseRow] = field(default_factory=list)  # > threshold, no NIF
    excluded: list[dict] = field(default_factory=list)                   # invoices left out, with reason
    notes: str = ""
    audit: list = field(default_factory=list)                            # list[AuditEntry]

    @property
    def total(self) -> float:
        return round(sum(r.total for r in self.rows), 2)


def _quarter(date_str: str) -> int:
    return (int(str(date_str)[5:7]) - 1) // 3 + 1


def _spanish_nif(inv: dict, registry) -> tuple[Optional[str], str]:
    """(normalised ES-prefixed NIF or None, display name) of an expense invoice's vendor."""
    match = registry.match_invoice(inv) if registry is not None else None
    vendor = match.vendor if match else None
    nif = inv.get("vendor_vat_id_norm") or normalize_vat_id(inv.get("vendor_nif"))
    if not nif and vendor is not None:
        nif = normalize_vat_id(vendor.vat_id)
    name = ((vendor.legal_entity if vendor and vendor.legal_entity else None)
            or inv.get("vendor_name") or (vendor.key if vendor else "") or "")
    return nif, name


def _is_spanish(inv: dict, nif: Optional[str]) -> bool:
    if nif:
        return nif.startswith("ES")
    return (inv.get("geo_region") or "").upper() == "SPAIN"


def compute_modelo_347_purchases(year: int, db_conn: sqlite3.Connection,
                                 registry=None) -> Modelo347PurchasesResult:
    """Spanish vendors whose VAT-inclusive purchases of ``year`` exceed €3,005.06.

    ``registry`` is the vendor registry used to fill a missing NIF / name
    (``src.vendor_registry.load_registry()`` when ``None``).
    """
    if registry is None:
        from src.vendor_registry import load_registry
        registry = load_registry()
    result = Modelo347PurchasesResult(year=year)
    rows = db_conn.execute(
        """SELECT id, filename, invoice_date, subtotal_eur, iva_amount, irpf_amount, geo_region,
                  vat_treatment, tax_treatment, vendor_nif, vendor_vat_id_norm, vendor_name
           FROM invoices
           WHERE direction = 'in' AND COALESCE(excluded, 0) = 0
             AND invoice_date >= ? AND invoice_date <= ?
           ORDER BY invoice_date, id""",
        (f"{year}-01-01", f"{year}-12-31"),
    ).fetchall()

    buckets: dict[str, dict] = {}
    for inv in map(dict, rows):
        amount = round((inv.get("subtotal_eur") or 0.0) + (inv.get("iva_amount") or 0.0), 2)
        nif, name = _spanish_nif(inv, registry)
        treatment = inv.get("tax_treatment") or ""
        reason = None
        if treatment == "INTRA_EU_RC":
            reason = EXCLUDED_349
        elif treatment == "NON_EU_RC" or not _is_spanish(inv, nif):
            reason = EXCLUDED_FOREIGN
        elif (inv.get("irpf_amount") or 0.0) > 0:
            reason = EXCLUDED_WITHHOLDING
        if reason:
            # Foreign vendors are the bulk and routine; keep the list to the reasons worth a look.
            if reason != EXCLUDED_FOREIGN:
                result.excluded.append({"id": inv["id"], "date": inv["invoice_date"], "vendor": name,
                                        "nif": nif or "", "amount_eur": amount, "reason": reason})
            continue
        key = nif or f"?{name.strip().lower()}"
        b = buckets.setdefault(key, {"nif": nif or "", "name": name, "total": 0.0,
                                     "quarters": defaultdict(float), "ids": []})
        b["name"] = b["name"] or name
        b["total"] += amount
        b["quarters"][_quarter(inv["invoice_date"])] += amount
        b["ids"].append(inv["id"])

    for b in sorted(buckets.values(), key=lambda v: -v["total"]):
        total = round(b["total"], 2)
        nif = b["nif"][2:] if b["nif"].startswith("ES") else b["nif"]
        row = Modelo347PurchaseRow(nif=nif, name=b["name"], total=total,
                                   quarter_breakdown={q: round(v, 2) for q, v in sorted(b["quarters"].items())},
                                   n_invoices=len(b["ids"]))
        b["row"] = row
        if total <= THRESHOLD_EUR:
            result.below_threshold += 1
        elif not nif:
            result.unidentified.append(row)
        else:
            result.rows.append(row)

    notes = []
    for row in result.unidentified:
        notes.append(f"{row.name or 'Unnamed vendor'} totals €{row.total:,.2f} (> €{THRESHOLD_EUR:,.2f}) but has "
                     "no NIF — add it to the invoices or the vendor registry to declare it.")
    if result.excluded:
        notes.append(f"{len(result.excluded)} Spanish-side invoice(s) excluded (349 / withholding).")
    result.notes = " ".join(notes)

    _a = partial(AuditEntry.of, "347", year, 0)
    audit = [
        _a(f"purchase_{b['row'].nif or b['row'].name[:30]}", f"Adquisiciones a {b['row'].name}",
           f"SUM(subtotal_eur + iva_amount) of the year's expense invoices from this vendor — declared when "
           f"> €{THRESHOLD_EUR:,.2f}", b["row"].total, nif=b["row"].nif,
           quarter_breakdown=b["row"].quarter_breakdown, invoice_ids=b["ids"])
        for b in buckets.values() if b["row"].total > THRESHOLD_EUR
    ]
    audit.append(_a("purchases_summary", "Resumen Modelo 347 — adquisiciones",
                    f"Spanish vendors > €{THRESHOLD_EUR:,.2f}", float(len(result.rows)),
                    declared=len(result.rows), below_threshold=result.below_threshold,
                    unidentified=[r.name for r in result.unidentified],
                    excluded=[{"id": e["id"], "reason": e["reason"]} for e in result.excluded]))
    result.audit = audit
    log.info("ℹ️ Modelo 347 %s purchases: %d declarable vendor(s), %d below threshold, %d unidentified, "
             "%d excluded invoice(s)", year, len(result.rows), result.below_threshold,
             len(result.unidentified), len(result.excluded))
    return result
