"""Modelo 303 (quarterly VAT): AEAT box model, pro-rata and credit chain.

Moved verbatim out of ``src.tax_engine`` (#174). Reads its records through ``src.tax_data`` and the
tax-code derivations in ``src.tax_codes``; ``compute_modelo_303`` is the public entry point.
"""
from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from src.logger import get_logger
from src.periods import quarter_iso_bounds
from src.tax_data import (
    activity_start_date,
    activity_start_note,
    classified_quarter_end,
    get_vat_amount,
    get_vat_base,
    get_vat_treatment,
    income_invoice_eur,
    invoice_tax_treatment,
    load_classified_for_quarter,
    load_expense_invoices_for_quarter,
    load_income_invoices_for_quarter,
    net_amount,
    oss_country_code,
    tax_settings,
)
from src.tax_models import AuditEntry, Modelo303Result
from src.vat_rules import IVA_ES_RATE, SPANISH_21_TREATMENTS

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Modelo 303 — AEAT box model, pro-rata and credit chain (#97)
# ---------------------------------------------------------------------------
# Box layout and formulas follow the AEAT Modelo 303 form as reproduced in the
# "Manual práctico IVA 2025", cap. 9 (anexo, Modelo 303):
#   rows 01/02/03 = 4%, 04/05/06 = 10%, 07/08/09 = 21%
#   27 = 152 + 167 + 03 + 155 + 06 + 09 + 11 + 13 + 15 + 158 + 170 + 18 + 21 + 24 + 26
#   45 = 29 + 31 + 33 + 35 + 37 + 39 + 41 + 42 + 43 + 44;   46 = 27 − 45
#   64 = 46 + 58 + 76;   66 = 64 × 65 %;   87 = 110 − 78
#   69 = 66 + 77 − 78 + 68 + 108;   71 = 69 − 70 + 109
#   72 = "si resulta [71] negativa, consignar el importe a compensar"; 73 = devolución
# Boxes this taxpayer never uses (0/2/5% rows, recargo de equivalencia,
# imports, rectifications, 58/76/77/68/108/70/109) are 0 and not modelled.
#
# Credit chain (boxes 110/78/87): 110 = previous period's 87 + 72; 78 is at
# most the positive 66; from 2T 2021 a return that applies 78 cannot itself be
# "a compensar". In the last period, a refund (73) may include the whole 110
# (78 = 110) — AEAT "Novedades validación cuotas a compensar de periodos
# anteriores" (Mod_303/303_NovedValidCuotasCompensPeriodosAnter.pdf) and the
# Delsol/Sage 303 guides on 4T refunds.

_RATE_ROWS: dict[float, tuple[str, str]] = {
    4.0: ("c01_base", "c03_cuota"),
    10.0: ("c04_base", "c06_cuota"),
    21.0: ("c07_base", "c09_cuota"),
}
_SPANISH_RATES_PCT: tuple[float, ...] = tuple(_RATE_ROWS)
Q4_NEGATIVE_OPTIONS: tuple[str, ...] = ("compensate", "refund")
# tax.platform_fee_vat_treatment (#147): self-assess the Stripe application fees
# as a non-EU reverse charge (default), or leave them out of the 303.
PLATFORM_FEE_VAT_TREATMENTS: tuple[str, ...] = ("NON_EU_RC", "NONE")
# Safety bound for the app-computed credit chain when no filed return stops it.
_CREDIT_CHAIN_MAX_QUARTERS = 40


@dataclass
class _Collected303:
    """Unrounded, pre-pro-rata sums of one quarter, keyed by result field name."""
    acc: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    records: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    notes: list[str] = field(default_factory=list)
    platform_fee_treatment: str = "NON_EU_RC"
    platform_fees: float = 0.0          # quarter's fee_application total self-assessed in 12/13 (#147)
    platform_fees_cuota: float = 0.0
    fee_split_unknown: int = 0

    @property
    def with_right_to_deduct(self) -> float:
        """Art. 104.Dos.1º LIVA numerator: taxed sales + art. 94.Uno.2º operations
        located abroad that would give the right to deduct (EU B2B, non-EU, OSS)."""
        a = self.acc
        return (a["c01_base"] + a["c04_base"] + a["c07_base"] + a["c59_entregas_intracom"]
                + a["c60_exportaciones"] + a["c120_no_sujetas_localizacion"] + a["oss_base"])

    @property
    def full_deductible_vat(self) -> float:
        """Deductible cuota of the quarter before pro-rata (29 + 31 + 37 at 100%)."""
        return self.acc["c29_cuota"] + self.acc["c31_cuota"] + self.acc["c37_cuota"]


def _rate_pct(raw: object) -> Optional[float]:
    """Stored VAT rate as a percent (21 and 0.21 both → 21.0); None when unset or ≤ 0."""
    try:
        r = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if r <= 0:
        return None
    return round(r * 100.0, 2) if r <= 1.0 else round(r, 2)


def _reverse_charge_rate(inv: dict) -> float:
    """Spanish rate self-assessed on a reverse-charge purchase: the invoice's rate
    when it is a Spanish one (4/10/21), otherwise the general 21%."""
    r = _rate_pct(inv.get("iva_rate"))
    return r if r in _SPANISH_RATES_PCT else IVA_ES_RATE * 100.0


def _sale_rate_row(inv: dict, base: float, vat: float) -> tuple[str, str]:
    """(base field, cuota field) of the devengado row matching an ES_21 sale's rate."""
    r = _rate_pct(inv.get("iva_rate"))
    if r is None and base and vat:
        r = abs(vat / base) * 100.0
    if r is None:   # no rate and no VAT to infer it from: the general rate
        return _RATE_ROWS[21.0]
    nearest = min(_SPANISH_RATES_PCT, key=lambda x: abs(x - r))
    return _RATE_ROWS[nearest]


def _collect_303_sales(year: int, quarter: int, conn: sqlite3.Connection,
                       config: Optional[dict], col: _Collected303) -> None:
    """Accrued VAT and informational boxes from Stripe rows and issued invoices."""
    # Stripe: aggregated per (geo, activity, treatment, OSS country) — the
    # gestor works with the quarterly summary, not individual charges.
    agg: dict[tuple, dict] = {}
    for row in load_classified_for_quarter(year, quarter, conn, config):
        treatment = get_vat_treatment(row, config)
        base = get_vat_base(row, config)
        vat = get_vat_amount(row, config)
        oss_cc = oss_country_code(row) if treatment == "OSS_EU" else ""
        key = (row.get("geo_region") or "UNKNOWN", row.get("activity_type") or "UNKNOWN",
               treatment, oss_cc)
        bucket = agg.setdefault(key, {"n": 0, "gross_eur": 0.0, "base_eur": 0.0, "vat_eur": 0.0})
        bucket["n"] += 1
        bucket["gross_eur"] += net_amount(row)
        bucket["base_eur"] += base
        bucket["vat_eur"] += vat
        if treatment in SPANISH_21_TREATMENTS:
            # IVA_ES_21 and EU_B2C_ES21 (EU consumers under the art. 73 LIVA
            # threshold, not in OSS — D7) both accrue Spanish 21%.
            col.acc["c07_base"] += base
            col.acc["c09_cuota"] += vat
        elif treatment == "IVA_EU_B2B":
            col.acc["c59_entregas_intracom"] += base
        elif treatment == "OSS_EU":
            col.acc["oss_base"] += base
            col.acc["oss_vat"] += vat
        elif treatment == "IVA_EXPORT":
            # Non-EU customers: services not subject by location rules (D11).
            col.acc["c120_no_sujetas_localizacion"] += base

    target = {"IVA_EU_B2B": "c59_entregas_intracom", "OSS_EU": "oss_base",
              "IVA_EXPORT": "c120_no_sujetas_localizacion"}
    for (geo, act, treatment, oss_cc), v in agg.items():
        box = "c07_base" if treatment in SPANISH_21_TREATMENTS else target.get(treatment)
        if box is None:
            continue
        rec = {"source": "stripe_aggregado", "geo_region": geo, "activity": act,
               "vat_treatment": treatment, "n_transactions": v["n"],
               "gross_eur": round(v["gross_eur"], 2), "base_eur": round(v["base_eur"], 2),
               "vat_eur": round(v["vat_eur"], 2)}
        if oss_cc:
            rec["oss_country"] = oss_cc
        col.records[box].append(rec)

    unclassified = 0
    for inv in load_income_invoices_for_quarter(year, quarter, conn, config):
        tt = invoice_tax_treatment("out", inv)
        base = income_invoice_eur(inv)
        vat = inv.get("iva_amount") or 0.0
        rec = {"source": "invoice_out", "id": inv.get("id"),
               "date": str(inv.get("tx_date", ""))[:10],
               "client": str(inv.get("client_name") or inv.get("client_nif") or "")[:40],
               "description": str(inv.get("description") or "")[:50],
               "base_eur": round(base, 2), "vat_eur": round(vat, 2), "tax_treatment": tt}
        if tt == "ES_21":
            base_f, cuota_f = _sale_rate_row(inv, base, vat)
        elif tt == "EU_B2C_ES21":
            base_f, cuota_f = _RATE_ROWS[21.0]
        elif tt == "EU_B2B":
            base_f, cuota_f = "c59_entregas_intracom", None
        elif tt == "NON_EU_NOT_SUBJECT":
            base_f, cuota_f = "c120_no_sujetas_localizacion", None
        elif tt == "EXEMPT_TEACHING":
            # Art. 20.1.9º LIVA: exempt without right to deduct — no 303 box,
            # only the pro-rata denominator (D2).
            base_f, cuota_f = "exempt_base", None
        else:
            unclassified += 1
            continue
        col.acc[base_f] += base
        if cuota_f:
            col.acc[cuota_f] += vat
        col.records[base_f].append(rec)
    if unclassified:
        col.notes.append(
            f"{unclassified} income invoice(s) without a tax_treatment were left out of the 303 — "
            "set one in the invoice ledger."
        )

    natural_start = quarter_iso_bounds(year, quarter)[0]
    note = activity_start_note(
        conn, config, natural_start,
        {"stripe": classified_quarter_end(year, quarter), "income": quarter_iso_bounds(year, quarter)[1]}, "303")
    if note:
        col.notes.append(note)


def _platform_fee_vat_treatment(config: Optional[dict]) -> str:
    """``tax.platform_fee_vat_treatment``: ``NON_EU_RC`` (default) or ``NONE`` (#147)."""
    option = tax_settings(config).get("platform_fee_vat_treatment", "NON_EU_RC")
    if option not in PLATFORM_FEE_VAT_TREATMENTS:
        raise ValueError(
            f"tax.platform_fee_vat_treatment must be one of {PLATFORM_FEE_VAT_TREATMENTS}, got {option!r}")
    return option


def _collect_303_platform_fees(year: int, quarter: int, conn: sqlite3.Connection,
                               config: Optional[dict], col: _Collected303) -> None:
    """Stripe application fees as a non-EU reverse charge (#147, art. 84.Uno.2º LIVA).

    The platforms that keep them are established outside the EU and charge no
    VAT, so the quarter's ``fee_application`` total — same classified charges as
    the 303 sales, by charge date — is accrued in 12/13 at 21% and deducted in
    28/29 at 100% (before the pro-rata), like a ``NON_EU_RC`` invoice (D8).
    Charges with an unknown fee split are counted in a note, never estimated.
    """
    col.platform_fee_treatment = _platform_fee_vat_treatment(config)
    if col.platform_fee_treatment == "NONE":
        return
    rows = load_classified_for_quarter(year, quarter, conn, config)
    fee_rows = [r for r in rows if r.get("fee_application")]
    col.fee_split_unknown = sum(1 for r in rows if r.get("fee_application") is None)
    rate = IVA_ES_RATE * 100.0
    col.platform_fees = round(sum(float(r["fee_application"]) for r in fee_rows), 2)
    col.platform_fees_cuota = round(col.platform_fees * rate / 100.0, 2)
    col.acc["c12_base"] += col.platform_fees
    col.acc["c13_cuota"] += col.platform_fees_cuota
    col.acc["c28_base"] += col.platform_fees
    col.acc["c29_cuota"] += col.platform_fees_cuota
    for r in fee_rows:
        rec = {"source": "platform_fee", "charge_id": r["id"], "date": str(r.get("created_date") or "")[:10],
               "activity": r.get("activity_type") or "", "fee_application": round(float(r["fee_application"]), 2),
               "rate_pct": rate}
        col.records["c12_base"].append(rec)
        col.records["c28_base"].append(rec)
    if col.fee_split_unknown:
        log.warning("⚠️ 303 %dQ%d: fee split unknown for %d Stripe charge(s) — their platform fees are "
                    "not in boxes 12/13/28/29; re-fetch with `stripe-fetch --backfill-fee-split`",
                    year, quarter, col.fee_split_unknown)
        col.notes.append(
            f"Fee split unknown for {col.fee_split_unknown} Stripe charge(s); re-fetch "
            "(stripe-fetch --backfill-fee-split) — their platform fees are not in boxes 12/13/28/29."
        )


def _collect_303_purchases(year: int, quarter: int, conn: sqlite3.Connection,
                           config: Optional[dict], col: _Collected303) -> None:
    """Reverse-charge accruals and deductible VAT (at 100%, before pro-rata)."""
    from src.fixed_assets import capital_goods_vat_for_period, load_fixed_assets

    capital_by_invoice: dict[str, list] = defaultdict(list)
    registered_by_invoice: dict[str, list] = defaultdict(list)
    for asset in load_fixed_assets(conn):
        if asset.invoice_id:
            registered_by_invoice[asset.invoice_id].append(asset)
            if asset.vat_capital_good:
                capital_by_invoice[asset.invoice_id].append(asset)

    for inv in load_expense_invoices_for_quarter(year, quarter, conn, config):
        tt = invoice_tax_treatment("in", inv)
        base = inv.get("subtotal_eur") or 0.0
        iva = inv.get("iva_amount") or 0.0
        pct = inv["deductible_pct_vat"] / 100.0
        rec = {"source": "invoice_in", "id": inv.get("id"),
               "date": str(inv.get("tx_date", ""))[:10],
               "vendor": str(inv.get("vendor_name") or inv.get("vendor_nif") or "")[:40],
               "description": str(inv.get("description") or "")[:50],
               "subtotal_eur": round(base, 2), "iva_amount": round(iva, 2),
               "deductible_pct_vat": inv["deductible_pct_vat"], "tax_treatment": tt,
               # The invoice's own stored rate (#137) — the 390 prefers this over
               # inferring the rate from VAT ÷ base, which a blended-rate bill or
               # rounding can misroute into the wrong rate row.
               "iva_rate": _rate_pct(inv.get("iva_rate"))}

        if tt in ("DOMESTIC", "DOMESTIC_CAPITAL"):
            linked = capital_by_invoice.get(inv.get("id"), [])
            if linked:
                # The capital-good share is deducted in 30/31 from the fixed-asset
                # register (at its own VAT business %); only the rest stays in 28/29.
                base_rest = max(0.0, base - sum(a.base_eur for a in linked))
                iva_rest = max(0.0, iva - sum(a.vat_eur for a in linked))
                rec["capital_goods_in_30_31"] = [a.id for a in linked]
            elif tt == "DOMESTIC_CAPITAL" and registered_by_invoice.get(inv.get("id")):
                # Registered but no linked asset is a VAT capital good (e.g. below
                # the art. 108 LIVA threshold): deduct like DOMESTIC in 28/29; the
                # asset stays registered for IRPF depreciation only.
                base_rest, iva_rest = base, iva
                rec["registered_non_capital_assets"] = [
                    a.id for a in registered_by_invoice[inv.get("id")]
                ]
                col.notes.append(
                    f"Capital-good invoice {inv.get('id')} is linked to a registered asset "
                    "that is not a VAT capital good — boxes 28/29 use the invoice; the asset "
                    "stays registered for IRPF depreciation only."
                )
            elif tt == "DOMESTIC_CAPITAL":
                # Flagged as a capital good but not in the fixed-asset register:
                # deduct it in 30/31 straight from the invoice.
                col.acc["c30_base"] += base * pct
                col.acc["c31_cuota"] += iva * pct
                col.records["c30_base"].append({**rec, "c31_cuota": round(iva * pct, 2)})
                col.notes.append(
                    f"Capital-good invoice {inv.get('id')} is not in the fixed-asset register — "
                    "boxes 30/31 use the invoice; register it for the arts. 107-109 LIVA regularisation."
                )
                continue
            else:
                base_rest, iva_rest = base, iva
            # Only a zero-VAT remainder adds nothing; a negative one is a received
            # rectificativa and must net off the quarter's 28/29 (#161).
            if iva_rest != 0:
                col.acc["c28_base"] += base_rest * pct
                col.acc["c29_cuota"] += iva_rest * pct
                col.records["c28_base"].append({**rec, "c29_cuota": round(iva_rest * pct, 2)})
        elif tt in ("INTRA_EU_RC", "NON_EU_RC"):
            rate = _reverse_charge_rate(inv)
            cuota = round(base * rate / 100.0, 2)
            accr_b, accr_c, ded_b, ded_c = (
                ("c10_base", "c11_cuota", "c36_base", "c37_cuota") if tt == "INTRA_EU_RC"
                # Other reverse charge (art. 84.Uno.2º LIVA, D8): accrued in 12/13
                # and deducted with the current domestic operations in 28/29.
                else ("c12_base", "c13_cuota", "c28_base", "c29_cuota")
            )
            col.acc[accr_b] += base
            col.acc[accr_c] += cuota
            col.acc[ded_b] += base * pct
            col.acc[ded_c] += cuota * pct
            rc = {**rec, "rate_pct": rate, "self_assessed_cuota": cuota,
                  "deductible_cuota": round(cuota * pct, 2)}
            col.records[accr_b].append(rc)
            col.records[ded_b].append(rc)
        # NO_VAT and NOT_DEDUCTIBLE carry no deductible VAT.

    _collect_303_platform_fees(year, quarter, conn, config, col)

    cg = capital_goods_vat_for_period(year, quarter, conn)
    col.acc["c30_base"] += cg.box_30_base
    col.acc["c31_cuota"] += cg.box_31_cuota
    col.records["c30_base"].extend({"source": "fixed_asset", **line} for line in cg.lines)

    # Manual IVA_SOPORTADO entries carry the cuota; the base needs their explicit
    # rate (no more assumed 21%). Entries without a rate keep counting in 29 but
    # add nothing to 28 and are flagged.
    try:
        manual = conn.execute(
            """SELECT id, amount_eur, vat_rate, description FROM quarterly_tax_entries
               WHERE year = ? AND quarter = ? AND entry_type = 'IVA_SOPORTADO'""",
            (year, quarter),
        ).fetchall()
    except sqlite3.OperationalError:  # DB not migrated yet (no vat_rate column)
        manual = [dict(r, vat_rate=None) for r in conn.execute(
            """SELECT id, amount_eur, description FROM quarterly_tax_entries
               WHERE year = ? AND quarter = ? AND entry_type = 'IVA_SOPORTADO'""",
            (year, quarter),
        ).fetchall()]
    no_rate = []
    for m in manual:
        cuota = float(m["amount_eur"] or 0.0)
        rate = _rate_pct(m["vat_rate"])
        m_base = round(cuota / (rate / 100.0), 2) if rate else 0.0
        col.acc["c28_base"] += m_base
        col.acc["c29_cuota"] += cuota
        col.records["c28_base"].append({
            "source": "manual_entry", "id": m["id"], "description": m["description"],
            "cuota": round(cuota, 2), "vat_rate": rate, "base": m_base,
        })
        if not rate:
            no_rate.append(m["id"])
    if no_rate:
        col.notes.append(
            f"Manual IVA_SOPORTADO entries {no_rate} have no VAT rate: their cuota is in box 29 "
            "but their base is missing from box 28 — re-enter them with the rate."
        )

    natural_start, end = quarter_iso_bounds(year, quarter)
    note = activity_start_note(conn, config, natural_start, {"expense": end}, "303")
    if note:
        col.notes.append(note)


def _collect_303_quarter(year: int, quarter: int, conn: sqlite3.Connection,
                         config: Optional[dict], *, sales_only: bool = False) -> _Collected303:
    col = _Collected303()
    _collect_303_sales(year, quarter, conn, config, col)
    if not sales_only:
        _collect_303_purchases(year, quarter, conn, config, col)
    return col


def prorrata_pct(with_right_eur: float, exempt_eur: float) -> Optional[float]:
    """Art. 104.Dos LIVA pro-rata %: operations with the right to deduct over all
    operations, rounded UP to the unit. ``None`` when there were no operations."""
    with_right = max(0.0, with_right_eur)
    exempt = max(0.0, exempt_eur)
    total = with_right + exempt
    if total <= 0:
        return None
    if exempt <= 0:
        return 100.0
    # round(…, 6) first so float noise (83.0000000001) does not round up a whole point.
    return float(min(100, math.ceil(round(with_right / total * 100.0, 6))))


def _year_prorrata_operations(year: int, conn: sqlite3.Connection,
                              config: Optional[dict]) -> tuple[float, float]:
    """(operations with right to deduct, exempt operations) of a whole year."""
    cols = [_collect_303_quarter(year, q, conn, config, sales_only=True) for q in range(1, 5)]
    return (sum(c.with_right_to_deduct for c in cols), sum(c.acc["exempt_base"] for c in cols))


def _prorrata_provisional(year: int, conn: sqlite3.Connection,
                          config: Optional[dict]) -> tuple[float, str]:
    """Provisional pro-rata % for ``year`` = the previous year's definitive % (art. 105 LIVA).

    Resolution: ``tax.prorrata.definitive_pct_by_year[year-1]`` (the stored,
    filed figure) → the legacy flat ``tax.vat_proration_percentage`` when set
    to something other than 100 → the previous year's definitive % computed from
    the app's own data → 100 when the previous year has no operations.
    """
    tax_cfg = tax_settings(config)
    by_year = (tax_cfg.get("prorrata") or {}).get("definitive_pct_by_year") or {}
    prev = year - 1
    for key in (str(prev), prev):
        if key in by_year:
            return float(by_year[key]), f"config tax.prorrata.definitive_pct_by_year[{prev}]"
    legacy = tax_cfg.get("vat_proration_percentage")
    if legacy is not None and float(legacy) != 100.0:
        return float(legacy), "config tax.vat_proration_percentage (legacy flat %)"
    pct = prorrata_pct(*_year_prorrata_operations(prev, conn, config))
    if pct is None:
        return 100.0, f"default 100% (no operations in {prev})"
    return pct, f"definitive % of {prev} computed from the app data (art. 104 LIVA)"


def _q4_negative_option(config: Optional[dict], override: Optional[str]) -> str:
    option = override or tax_settings(config).get("modelo303_q4_negative_result", "compensate")
    if option not in Q4_NEGATIVE_OPTIONS:
        raise ValueError(f"Q4 negative-result option must be one of {Q4_NEGATIVE_OPTIONS}, got {option!r}")
    return option


def _previous_period(year: int, quarter: int) -> tuple[int, int]:
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def _earliest_303_activity(conn: sqlite3.Connection, config: Optional[dict] = None) -> Optional[str]:
    """ISO date of the first row that can feed a 303 (None on an empty DB).

    Clamped up to ``tax.activity_start_date`` (issue #133) when set: a period
    ending before the activity start has no data to chain from, even if a stray
    row in the DB predates it.
    """
    dates = [
        conn.execute("SELECT MIN(created_date) FROM transactions WHERE activity_type IS NOT NULL "
                     "AND activity_type != 'UNKNOWN'").fetchone()[0],
        conn.execute("SELECT MIN(invoice_date) FROM invoices WHERE COALESCE(excluded, 0) = 0").fetchone()[0],
    ]
    row = conn.execute("SELECT MIN(year * 10 + quarter) FROM quarterly_tax_entries").fetchone()[0]
    if row:
        dates.append(f"{row // 10}-{(row % 10 - 1) * 3 + 1:02d}-01")
    dates = [str(d)[:10] for d in dates if d]
    earliest = min(dates) if dates else None
    floor = activity_start_date(config)
    if floor and (earliest is None or floor > earliest):
        return floor
    return earliest


def _previous_303_credit(year: int, quarter: int, conn: sqlite3.Connection,
                         config: Optional[dict], depth: int) -> tuple[float, str, dict]:
    """Box 110: credit pending from the previous period, as (amount, source, detail).

    Chains from the **filed** previous return (its 87 + 72) when one was
    imported — the legally operative figure (decision of 2026-09-28). Otherwise
    falls back to the app's own computation of the previous period, which in
    turn chains further back, until a filed return or the first period with data.
    """
    from src.filed_returns import load_filed_boxes

    py, pq = _previous_period(year, quarter)
    filed = load_filed_boxes(conn, "303", py, f"{pq}T")
    if filed:
        c87, c72 = filed.get("87", 0.0), filed.get("72", 0.0)
        return round(c87 + c72, 2), "filed", {"period": f"{py}-{pq}T", "filed_87": c87, "filed_72": c72}
    earliest = _earliest_303_activity(conn, config)
    _, prev_end = quarter_iso_bounds(py, pq)
    if earliest is None or prev_end < earliest:
        return 0.0, "none", {"period": f"{py}-{pq}T", "reason": "no data before this period"}
    if depth >= _CREDIT_CHAIN_MAX_QUARTERS:
        return 0.0, "none", {"period": f"{py}-{pq}T", "reason": "chain depth limit reached"}
    prev = compute_modelo_303(py, pq, conn, config, _chain_depth=depth + 1)
    return prev.credit_carry_forward, "app_chain", {
        "period": f"{py}-{pq}T", "app_87": prev.c87_pendiente_posteriores,
        "app_72": prev.c72_a_compensar, "app_110_source": prev.c110_source,
    }


def compute_modelo_303(
    year: int,
    quarter: int,
    db_conn: sqlite3.Connection,
    config: Optional[dict] = None,
    *,
    q4_negative_result: Optional[str] = None,
    _chain_depth: int = 0,
) -> Modelo303Result:
    """Compute Modelo 303 (quarterly VAT return) box by box for the given quarter.

    ``config`` (the app config dict) drives the EU VAT-treatment overrides,
    ``tax.vat_registered`` (unregistered → no deductible VAT), the pro-rata
    (``tax.prorrata.enabled``, default on; ``tax.prorrata.definitive_pct_by_year``)
    and ``tax.modelo303_q4_negative_result`` (``compensate`` → box 72,
    ``refund`` → box 73, Q4 only; ``q4_negative_result`` overrides it).

    Pro-rata (arts. 102–106 LIVA): during the year the provisional % (the
    previous year's definitive %) scales every deductible box; Q4 computes the
    year's definitive % and regularises the difference over the year's whole
    deductible VAT in box 44. ``c46_sin_prorrata`` is 46 at 100% deduction
    ("gestor mode", for the reconciliation).
    """
    tax_cfg = tax_settings(config)
    vat_registered = tax_cfg.get("vat_registered", True) is not False
    prorrata_enabled = (tax_cfg.get("prorrata") or {}).get("enabled", True) is not False
    option = _q4_negative_option(config, q4_negative_result)
    result = Modelo303Result(year=year, quarter=quarter, q4_negative_result=option,
                             prorrata_enabled=prorrata_enabled)
    col = _collect_303_quarter(year, quarter, db_conn, config)
    acc = col.acc
    notes = list(col.notes)

    # --- Accrued + informational boxes ------------------------------------
    for name in ("c01_base", "c03_cuota", "c04_base", "c06_cuota", "c07_base", "c09_cuota",
                 "c10_base", "c11_cuota", "c12_base", "c13_cuota", "c59_entregas_intracom",
                 "c60_exportaciones", "c120_no_sujetas_localizacion", "oss_base", "oss_vat",
                 "exempt_base"):
        setattr(result, name, round(acc[name], 2))
    result.c27_total_devengado = round(
        result.c03_cuota + result.c06_cuota + result.c09_cuota + result.c11_cuota + result.c13_cuota, 2)

    # --- Pro-rata ----------------------------------------------------------
    if prorrata_enabled:
        provisional, prov_source = _prorrata_provisional(year, db_conn, config)
    else:
        provisional, prov_source = 100.0, "pro-rata disabled (tax.prorrata.enabled = false)"
    result.prorrata_provisional_pct = provisional
    result.prorrata_provisional_source = prov_source
    p = provisional / 100.0 if vat_registered else 0.0
    if not vat_registered:
        notes.append("Not IVA-registered (tax.vat_registered = false): no deductible VAT.")

    # --- Deductible boxes (provisional % applied to cuota and base) ---------
    for base_f, cuota_f in (("c28_base", "c29_cuota"), ("c30_base", "c31_cuota"),
                            ("c36_base", "c37_cuota")):
        setattr(result, base_f, round(acc[base_f] * p, 2))
        setattr(result, cuota_f, round(acc[cuota_f] * p, 2))

    year_full_deductible = 0.0
    regularisation_rows: list[dict] = []
    if quarter == 4 and vat_registered:
        from src.fixed_assets import vat_regularisation_for_year

        # Box 43: arts. 107-109 LIVA capital-goods regularisation (years 2-5).
        regularisation_rows = [r for r in vat_regularisation_for_year(db_conn, year) if r["applies"]]
        result.c43_regularizacion_bienes_inversion = round(
            sum(r["adjustment_eur"] for r in regularisation_rows), 2)
        if prorrata_enabled:
            earlier = [_collect_303_quarter(year, q, db_conn, config) for q in (1, 2, 3)]
            year_cols = earlier + [col]
            with_right = sum(c.with_right_to_deduct for c in year_cols)
            exempt = sum(c.acc["exempt_base"] for c in year_cols)
            definitive = prorrata_pct(with_right, exempt)
            result.prorrata_definitive_pct = definitive if definitive is not None else 100.0
            year_full_deductible = sum(c.full_deductible_vat for c in year_cols)
            # Box 44 (art. 105 LIVA): (definitive − provisional) × the year's deductible VAT.
            result.c44_regularizacion_prorrata = round(
                year_full_deductible * (result.prorrata_definitive_pct - provisional) / 100.0, 2)

    result.c45_total_deducir = round(
        result.c29_cuota + result.c31_cuota + result.c37_cuota
        + result.c43_regularizacion_bienes_inversion + result.c44_regularizacion_prorrata, 2)
    result.c46_resultado_regimen_general = round(result.c27_total_devengado - result.c45_total_deducir, 2)
    full_deductible_q = col.full_deductible_vat if vat_registered else 0.0
    result.c46_sin_prorrata = round(
        result.c27_total_devengado - full_deductible_q - result.c43_regularizacion_bienes_inversion, 2)

    # --- Result and credit chain --------------------------------------------
    result.c64_suma_resultados = result.c46_resultado_regimen_general   # 58 and 76 do not apply
    result.c65_pct_atribuible_estado = 100.0                            # territorio común
    result.c66_atribuible_estado = round(
        result.c64_suma_resultados * result.c65_pct_atribuible_estado / 100.0, 2)
    c110, c110_source, c110_detail = _previous_303_credit(year, quarter, db_conn, config, _chain_depth)
    result.c110_pendiente_anteriores = c110
    result.c110_source = c110_source
    q4_refund = quarter == 4 and option == "refund"
    if q4_refund:
        # Last period with a refund request: the whole pending credit is applied.
        result.c78_aplicadas_periodo = c110
    else:
        result.c78_aplicadas_periodo = round(min(c110, max(0.0, result.c66_atribuible_estado)), 2)
    result.c87_pendiente_posteriores = round(c110 - result.c78_aplicadas_periodo, 2)
    result.c69_resultado_autoliquidacion = round(
        result.c66_atribuible_estado - result.c78_aplicadas_periodo, 2)    # 77, 68, 108 = 0
    result.c71_resultado_liquidacion = result.c69_resultado_autoliquidacion  # 70, 109 = 0
    if result.c71_resultado_liquidacion < 0:
        if q4_refund:
            result.c73_a_devolver = -result.c71_resultado_liquidacion
        else:
            result.c72_a_compensar = -result.c71_resultado_liquidacion

    if result.c120_no_sujetas_localizacion:
        notes.append("Box 120 holds non-EU sales not subject by location rules (D11); "
                     "the external accountant reported non-EU service invoices in box 60 instead.")
    if quarter == 4 and prorrata_enabled and result.prorrata_definitive_pct is not None:
        notes.append(f"Definitive pro-rata {year}: {result.prorrata_definitive_pct:.0f}% — store it as "
                     f"tax.prorrata.definitive_pct_by_year[{year}] once filed (provisional % of {year + 1}).")
    result.notes = " ".join(notes)
    log.info("ℹ️ Modelo 303 %s Q%d: 27=%.2f 45=%.2f 46=%.2f 110=%.2f (%s) 71=%.2f",
             year, quarter, result.c27_total_devengado, result.c45_total_deducir,
             result.c46_resultado_regimen_general, c110, c110_source, result.c71_resultado_liquidacion)

    result.audit = _modelo303_audit(result, col, provisional, prov_source, vat_registered,
                                    year_full_deductible, regularisation_rows, c110_detail)
    return result


def _modelo303_audit(r: Modelo303Result, col: _Collected303, provisional: float, prov_source: str,
                     vat_registered: bool, year_full_deductible: float,
                     regularisation_rows: list[dict], c110_detail: dict) -> list[AuditEntry]:
    """One audit entry per box (records attached to the base / informational boxes)."""
    _a = partial(AuditEntry.of, "303", r.year, r.quarter)
    rec = col.records
    pro = {"prorrata_provisional_pct": provisional, "prorrata_source": prov_source,
           "vat_registered": vat_registered}
    fees = {"platform_fee_vat_treatment": col.platform_fee_treatment, "platform_fees": col.platform_fees,
            "platform_fees_cuota": col.platform_fees_cuota, "fee_split_unknown": col.fee_split_unknown}
    return [
        _a("c01_base", "01 Base imponible 4%", "SUM(base) income invoices ES_21 at 4%", r.c01_base,
           records=rec["c01_base"]),
        _a("c03_cuota", "03 Cuota 4%", "SUM(iva_amount) of the 01 invoices", r.c03_cuota),
        _a("c04_base", "04 Base imponible 10%", "SUM(base) income invoices ES_21 at 10%", r.c04_base,
           records=rec["c04_base"]),
        _a("c06_cuota", "06 Cuota 10%", "SUM(iva_amount) of the 04 invoices", r.c06_cuota),
        _a("c07_base", "07 Base imponible 21% (Stripe España + UE B2C art. 73 + facturas ES_21/EU_B2C_ES21)",
           "SUM(base) WHERE treatment IN (IVA_ES_21, EU_B2C_ES21) [transactions] "
           "+ income invoices ES_21 at 21% / EU_B2C_ES21", r.c07_base, records=rec["c07_base"]),
        _a("c09_cuota", "09 Cuota 21%", "SUM(vat) of the 07 rows", r.c09_cuota, c07_base=r.c07_base),
        _a("c10_base", "10 Adquisiciones intracomunitarias — base",
           "SUM(subtotal_eur) expense invoices INTRA_EU_RC", r.c10_base, records=rec["c10_base"]),
        _a("c11_cuota", "11 Adquisiciones intracomunitarias — cuota (autoliquidada)",
           "SUM(round(base × rate, 2)) of the 10 invoices, rate 21% unless a Spanish rate is stated",
           r.c11_cuota),
        _a("c12_base", "12 Otras operaciones con ISP — base",
           "SUM(subtotal_eur) expense invoices NON_EU_RC (D8, art. 84.Uno.2º LIVA) "
           "+ SUM(fee_application) FROM transactions in Q (same classified charges as the sales, by charge "
           "date) when tax.platform_fee_vat_treatment = NON_EU_RC", r.c12_base,
           records=rec["c12_base"], **fees),
        _a("c13_cuota", "13 Otras operaciones con ISP — cuota",
           "SUM(round(base × rate, 2)) of the 12 invoices + round(platform fees × 21%, 2)",
           r.c13_cuota, **fees),
        _a("c27_total_devengado", "27 Total cuota devengada", "03 + 06 + 09 + 11 + 13", r.c27_total_devengado,
           c03=r.c03_cuota, c06=r.c06_cuota, c09=r.c09_cuota, c11=r.c11_cuota, c13=r.c13_cuota),
        _a("c28_base", "28 Base — operaciones interiores corrientes",
           "(SUM(base × deductible_pct_vat) DOMESTIC excl. capital goods + NON_EU_RC + platform fees at 100% "
           "+ manual entries (cuota / rate)) × provisional pro-rata", r.c28_base,
           base_100=round(col.acc["c28_base"], 2), records=rec["c28_base"], **pro, **fees),
        _a("c29_cuota", "29 Cuota — operaciones interiores corrientes",
           "(SUM(iva × deductible_pct_vat) DOMESTIC excl. capital goods + NON_EU_RC self-assessed "
           "+ platform fees self-assessed at 100% + manual IVA_SOPORTADO) × provisional pro-rata", r.c29_cuota,
           cuota_100=round(col.acc["c29_cuota"], 2), **pro, **fees),
        _a("c30_base", "30 Base — bienes de inversión",
           "(fixed-asset register capital goods acquired in Q: base × vat_business_pct "
           "+ unregistered DOMESTIC_CAPITAL invoices) × provisional pro-rata", r.c30_base,
           base_100=round(col.acc["c30_base"], 2), records=rec["c30_base"], **pro),
        _a("c31_cuota", "31 Cuota — bienes de inversión", "VAT deducted on those capital goods × provisional pro-rata",
           r.c31_cuota, cuota_100=round(col.acc["c31_cuota"], 2), **pro),
        _a("c36_base", "36 Base — adquisiciones intracomunitarias corrientes",
           "SUM(base × deductible_pct_vat) INTRA_EU_RC × provisional pro-rata", r.c36_base,
           base_100=round(col.acc["c36_base"], 2), **pro),
        _a("c37_cuota", "37 Cuota — adquisiciones intracomunitarias corrientes",
           "SUM(self-assessed cuota × deductible_pct_vat) INTRA_EU_RC × provisional pro-rata", r.c37_cuota,
           cuota_100=round(col.acc["c37_cuota"], 2), **pro),
        _a("c43_regularizacion_bienes_inversion", "43 Regularización bienes de inversión (Q4)",
           "SUM(adjustment_eur) of the arts. 107-109 LIVA register rows that apply this year",
           r.c43_regularizacion_bienes_inversion, records=regularisation_rows),
        _a("c44_regularizacion_prorrata", "44 Regularización por prorrata definitiva (Q4)",
           "(definitive % − provisional %) × deductible VAT of the year at 100% (Q1–Q4 29 + 31 + 37)",
           r.c44_regularizacion_prorrata, definitive_pct=r.prorrata_definitive_pct,
           year_deductible_vat_100=round(year_full_deductible, 2), **pro),
        _a("c45_total_deducir", "45 Total a deducir", "29 + 31 + 37 + 43 + 44", r.c45_total_deducir),
        _a("c46_resultado_regimen_general", "46 Resultado régimen general", "27 − 45",
           r.c46_resultado_regimen_general, c46_sin_prorrata=r.c46_sin_prorrata),
        _a("c59_entregas_intracom", "59 Entregas intracomunitarias de bienes y servicios",
           "SUM(base) Stripe IVA_EU_B2B + income invoices EU_B2B", r.c59_entregas_intracom,
           records=rec["c59_entregas_intracom"]),
        _a("c60_exportaciones", "60 Exportaciones y operaciones asimiladas",
           "Exports of goods — no treatment maps here (services go to 120)", r.c60_exportaciones),
        _a("c120_no_sujetas_localizacion", "120 Operaciones no sujetas por reglas de localización",
           "SUM(base) Stripe IVA_EXPORT + income invoices NON_EU_NOT_SUBJECT (D11; the external "
           "accountant used box 60 for these invoices)", r.c120_no_sujetas_localizacion,
           records=rec["c120_no_sujetas_localizacion"]),
        _a("oss_base", "123 / OSS — ventas UE B2C por ventanilla única",
           "SUM(base) WHERE treatment = OSS_EU [declared in the OSS return, informational here]",
           r.oss_base, oss_vat=r.oss_vat, records=rec["oss_base"]),
        _a("exempt_base", "Operaciones exentas sin derecho a deducción (no box — pro-rata denominator)",
           "SUM(base) income invoices EXEMPT_TEACHING (art. 20.1.9º LIVA)", r.exempt_base,
           records=rec["exempt_base"]),
        _a("c64_suma_resultados", "64 Suma de resultados", "46 + 58 + 76 (58/76 not applicable)",
           r.c64_suma_resultados),
        _a("c65_pct_atribuible_estado", "65 % atribuible a la Administración del Estado",
           "100 (territorio común)", r.c65_pct_atribuible_estado),
        _a("c66_atribuible_estado", "66 Atribuible a la Administración del Estado", "64 × 65 %",
           r.c66_atribuible_estado),
        _a("c110_pendiente_anteriores", "110 Cuotas a compensar pendientes de periodos anteriores",
           "previous period's filed 87 + 72; else the app's own previous 87 + 72", r.c110_pendiente_anteriores,
           source=r.c110_source, **c110_detail),
        _a("c78_aplicadas_periodo", "78 Cuotas a compensar aplicadas en este periodo",
           "min(110, max(0, 66)); Q4 refund → 110", r.c78_aplicadas_periodo),
        _a("c87_pendiente_posteriores", "87 Cuotas pendientes para periodos posteriores", "110 − 78",
           r.c87_pendiente_posteriores),
        _a("c69_resultado_autoliquidacion", "69 Resultado de la autoliquidación",
           "66 + 77 − 78 + 68 + 108 (77/68/108 = 0)", r.c69_resultado_autoliquidacion),
        _a("c71_resultado_liquidacion", "71 Resultado", "69 − 70 + 109 (70/109 = 0)", r.c71_resultado_liquidacion),
        _a("c72_a_compensar", "72 A compensar", "−71 when 71 < 0 (unless Q4 refund)", r.c72_a_compensar,
           credit_carry_forward=r.credit_carry_forward),
        _a("c73_a_devolver", "73 A devolver", "−71 when 71 < 0 in Q4 with the refund option", r.c73_a_devolver,
           q4_negative_result=r.q4_negative_result),
    ]
