"""Modelo 349 (intra-EU operations recapitulative statement).

Reads its records through ``src.tax_data``; ``compute_modelo_349`` is the public entry point.
"""
from __future__ import annotations

import sqlite3
from functools import partial
from typing import Optional

from src.periods import quarter_iso_bounds
from src.tax_data import (
    clamp_start,
    get_vat_base,
    get_vat_treatment,
    income_invoice_eur,
    invoice_tax_treatment,
    load_classified_for_quarter,
    load_income_invoices_for_quarter,
)
from src.tax_models import AuditEntry, Modelo349Result, Modelo349Row


def compute_modelo_349(
    year: int, quarter: int, db_conn: sqlite3.Connection, config: Optional[dict] = None
) -> Modelo349Result:
    """Compute Modelo 349 (intra-EU operations) for the given quarter (#99).

    - Key ``I``: ``INTRA_EU_RC`` expense invoices (services acquired from EU
      businesses), grouped by the vendor VAT id — the invoice's normalised id,
      else the vendor registry's ``vat_id``; the name is the registry's
      ``legal_entity``, else the invoice vendor name.
    - Key ``S``: EU B2B sales — Stripe charges treated ``IVA_EU_B2B`` (customer
      ``buyer_vat_id``) plus issued invoices with ``tax_treatment`` ``EU_B2B``
      (``client_nif``), grouped by the normalised VAT id.

    Invoices are keyed by invoice date, bases are the stored EUR values (ECB
    rate resolved at OCR time; ``eur_received`` for income), ``excluded``
    invoices are skipped. An operator whose quarter total is zero or negative
    is left out with a warning (rectification lines are out of scope); lines
    without a VAT id cannot be declared and are listed in ``unidentified``.
    ``config`` drives the Stripe VAT-treatment derivation.
    """
    from src.tax_codes import EU_VAT_PREFIXES, normalize_vat_id
    from src.vendor_registry import load_registry

    result = Modelo349Result(year=year, quarter=quarter)
    ops: dict[tuple[str, str], dict] = {}   # (key, VAT id or "?name") -> bucket

    def _add(key: str, vat: Optional[str], name: str, base: float, rec: dict) -> None:
        b = ops.setdefault((key, vat or f"?{name}"), {"vat": vat or "", "name": name,
                                                      "base": 0.0, "records": []})
        b["name"] = b["name"] or name
        b["base"] += base
        b["records"].append({**rec, "base_eur": round(base, 2)})

    for row in load_classified_for_quarter(year, quarter, db_conn, config):
        if get_vat_treatment(row, config) != "IVA_EU_B2B":
            continue
        _add("S", normalize_vat_id(row.get("buyer_vat_id")), row.get("email_meta") or "",
             get_vat_base(row, config),
             {"source": "stripe", "id": row["id"], "date": str(row["created_date"])[:10]})

    for inv in load_income_invoices_for_quarter(year, quarter, db_conn, config):
        if invoice_tax_treatment("out", inv) != "EU_B2B":
            continue
        _add("S", normalize_vat_id(inv.get("client_nif")), inv.get("client_name") or "",
             income_invoice_eur(inv),
             {"source": "invoice_out", "id": inv["id"], "date": str(inv["tx_date"])[:10]})

    registry = load_registry()
    # tax.activity_start_date (issue #133): purchases dated before it don't
    # belong to this business either, so the same lower bound applies here.
    start, end = quarter_iso_bounds(year, quarter)
    start = clamp_start(start, config)
    purchases = db_conn.execute(
        """SELECT id, filename, invoice_date AS tx_date, subtotal_eur, iva_amount,
                  geo_region, vat_treatment, tax_treatment, vendor_nif, vendor_vat_id_norm, vendor_name
           FROM invoices
           WHERE direction = 'in' AND COALESCE(excluded, 0) = 0
             AND invoice_date >= ? AND invoice_date <= ?
           ORDER BY tx_date""",
        (start, end),
    ).fetchall()
    for inv in map(dict, purchases):
        if invoice_tax_treatment("in", inv) != "INTRA_EU_RC":
            continue
        match = registry.match_invoice(inv)
        vendor = match.vendor if match else None
        vat = (inv.get("vendor_vat_id_norm") or normalize_vat_id(inv.get("vendor_nif"))
               or normalize_vat_id(vendor.vat_id if vendor else None))
        name = (vendor.legal_entity if vendor and vendor.legal_entity else None) \
            or inv.get("vendor_name") or (vendor.key if vendor else "")
        _add("I", vat, name, inv.get("subtotal_eur") or 0.0,
             {"source": "invoice_in", "id": inv["id"], "date": str(inv["tx_date"])[:10],
              "vendor": str(inv.get("vendor_name") or "")[:40]})

    warnings: list[str] = []
    buckets = sorted(ops.items(), key=lambda kv: (kv[0][0], kv[1]["name"].lower(), kv[1]["vat"]))
    for (key, _), b in buckets:
        vat = b["vat"]
        country = vat[:2] if vat[:2].isalpha() else ""
        row = Modelo349Row(key=key, country=country, vat_id=vat, name=b["name"],
                           base=round(b["base"], 2), n_records=len(b["records"]))
        b["row"] = row
        if not vat:
            result.unidentified.append(row)
            warnings.append(f"{row.name or 'Unnamed operator'} (key {key}, €{row.base:,.2f}) has no "
                            "VAT id and cannot be declared — add it to the invoice or the vendor registry.")
        elif row.base <= 0:
            result.excluded.append(row)
            warnings.append(f"{vat} (key {key}) totals €{row.base:,.2f} this quarter and is left out — "
                            "the 349 takes no zero/negative lines; rectify the original period instead.")
        else:
            result.rows.append(row)
            if country not in EU_VAT_PREFIXES:
                warnings.append(f"{vat} (key {key}) does not start with an EU country prefix — check it.")
    result.total = round(sum((r.base for r in result.rows), 0.0), 2)
    result.notes = " ".join(warnings)

    # --- Audit trail: one cell per operator line (named after its VAT id), then 01/02 ---
    _a = partial(AuditEntry.of, "349", year, quarter)
    labels = {"I": "Adquisiciones intracomunitarias de servicios",
              "S": "Prestaciones intracomunitarias de servicios"}
    audit = []
    for n, (_, b) in enumerate(buckets, 1):
        row = b["row"]
        state = ("unidentified" if not row.vat_id else "excluded" if row.base <= 0 else "op")
        audit.append(_a(
            f"{state}_{row.key}_{row.vat_id or n}",
            f"{labels[row.key]} (clave {row.key}) — {row.name or '?'} {row.vat_id}".rstrip(),
            "SUM(base EUR) of the quarter's records for this VAT id and key"
            + ("" if state == "op" else f" — not declared ({state})"),
            row.base, records=b["records"],
        ))
    audit.append(_a("c01_operadores", "Número total de operadores",
                    "COUNT(operator lines with a VAT id and a positive total)", float(len(result.rows)),
                    excluded=[r.vat_id for r in result.excluded],
                    unidentified=[r.name for r in result.unidentified]))
    audit.append(_a("c02_importe", "Importe de las operaciones intracomunitarias",
                    "SUM(base) of the declared operator lines", result.total))
    result.audit = audit
    return result


