"""P&L per IAE activity for the annual income-tax return (Renta), #103.

Income and expenses are the year's Modelo 130 inputs (Q4 year-to-date) — the
same loaders and the same rules — split by economic activity:

- ``COACHING``      → IAE 826 (coaching / teaching)
- ``ILLUSTRATIONS`` → IAE 861 (illustration licences)
- ``NEWSLETTER``    → IAE 751 (newsletter / publicity)

Income: Stripe VAT bases by the transaction's ``activity_type``; issued
invoices by the invoice ``activity_type`` (an ``EXEMPT_TEACHING`` invoice
without one is teaching → COACHING); exchange differences by the activity of
the invoice they come from. Expenses: deductible expense invoices by the
invoice ``activity_type``, else the vendor registry's ``activity``; Stripe
platform (application) fees by the activity of the charge they came from.

What has no activity is allocated by a configurable rule,
``tax.pl_allocation`` in ``config.json``::

    {"reta": "COACHING", "depreciation": "COACHING", "unallocated": "COACHING"}

Each value is an activity, or ``BY_INCOME`` (split in proportion to the
income attributed directly to each activity). The default sends everything
to COACHING (IAE 826), as the external accountant does. ``unallocated``
covers expense and income lines without an activity and manual
``GASTOS_DEDUCIBLES`` entries.

Totals tie to the Q4 Modelo 130: income = box 01, expenses = the real
expenses inside box 02. The 5 % gastos de difícil justificación (also in box
02) is a single allowance on the whole net yield and is shown separately.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Optional

from src.fixed_assets import capital_asset_invoice_ids, depreciation_for_period
from src.logger import get_logger
from src.tax_engine import (
    _get_tax_entries_total,
    _get_vat_base,
    _income_invoice_eur,
    _load_classified_ytd,
    _load_expense_invoices_ytd,
    _load_income_invoices_ytd,
    compute_modelo_130,
    load_app_config,
)
from src.vendor_registry import ACTIVITIES, ACTIVITY_IAE

log = get_logger(__name__)

BY_INCOME = "BY_INCOME"
ALLOCATION_KEYS: tuple[str, ...] = ("reta", "depreciation", "unallocated")
DEFAULT_ACTIVITY = "COACHING"   # IAE 826 — where the external accountant puts shared items
TIE_TOLERANCE = 0.01

ACTIVITY_LABELS: dict[str, str] = {
    "COACHING": "Coaching / teaching", "ILLUSTRATIONS": "Illustration", "NEWSLETTER": "Newsletter / publicity",
}


@dataclass
class ActivityPL:
    """One activity's column of the P&L."""
    activity: str
    iae: str
    income: float = 0.0
    expenses_invoices: float = 0.0
    reta: float = 0.0
    depreciation: float = 0.0
    other_expenses: float = 0.0       # platform fees, manual entries + expenses allocated from "unallocated"

    @property
    def total_expenses(self) -> float:
        return round(self.expenses_invoices + self.reta + self.depreciation + self.other_expenses, 2)

    @property
    def net(self) -> float:
        return round(self.income - self.total_expenses, 2)


@dataclass
class PLByActivity:
    year: int
    allocation: dict[str, str] = field(default_factory=dict)
    activities: list[ActivityPL] = field(default_factory=list)
    lines: list[dict] = field(default_factory=list)   # every income / expense line with its activity
    total_income: float = 0.0
    total_expenses: float = 0.0
    gastos_dificil_justificacion: float = 0.0          # the 5 % inside 130 box 02, not split
    m130_c01: float = 0.0
    m130_gastos_reales: float = 0.0
    m130_c02: float = 0.0
    notes: str = ""

    @property
    def net(self) -> float:
        return round(self.total_income - self.total_expenses, 2)

    @property
    def ties_to_130(self) -> bool:
        """Income = 130 Q4 box 01 and expenses = the real expenses of 130 Q4 box 02."""
        return (abs(self.total_income - self.m130_c01) <= TIE_TOLERANCE
                and abs(self.total_expenses - self.m130_gastos_reales) <= TIE_TOLERANCE)


def allocation_rules(config: Optional[dict]) -> tuple[dict[str, str], list[str]]:
    """``tax.pl_allocation`` validated, with defaults; returns (rules, warnings)."""
    raw = ((config or {}).get("tax") or {}).get("pl_allocation") or {}
    rules, warnings = {}, []
    for key in ALLOCATION_KEYS:
        value = str(raw.get(key) or DEFAULT_ACTIVITY).strip().upper()
        if value not in ACTIVITIES and value != BY_INCOME:
            warnings.append(f"tax.pl_allocation.{key} = {raw.get(key)!r} is not one of "
                            f"{', '.join(ACTIVITIES + (BY_INCOME,))} — using {DEFAULT_ACTIVITY}.")
            value = DEFAULT_ACTIVITY
        rules[key] = value
    return rules, warnings


def _invoice_meta(conn: sqlite3.Connection, year: int) -> dict[str, dict]:
    """id → the fields used to find an invoice's activity (both directions, the whole year)."""
    rows = conn.execute(
        """SELECT id, direction, activity_type, tax_treatment, filename, vendor_name, vendor_nif,
                  vendor_vat_id_norm
           FROM invoices WHERE invoice_date >= ? AND invoice_date <= ?""",
        (f"{year}-01-01", f"{year}-12-31"),
    ).fetchall()
    return {r["id"]: dict(r) for r in rows}


def _norm_activity(value: Any) -> Optional[str]:
    v = str(value or "").strip().upper()
    return v if v in ACTIVITIES else None


def compute_pl_by_activity(
    year: int, db_conn: sqlite3.Connection, config: Optional[dict] = None, registry=None,
) -> PLByActivity:
    """P&L of ``year`` per IAE activity, tied to the Q4 Modelo 130 (see module doc)."""
    if config is None:
        config = load_app_config()
    if registry is None:
        from src.vendor_registry import load_registry
        registry = load_registry()
    rules, notes = allocation_rules(config)
    meta = _invoice_meta(db_conn, year)
    lines: list[dict] = []

    def _line(kind: str, source: str, amount: float, activity: Optional[str], rule_key: str,
              **extra: Any) -> None:
        lines.append({"kind": kind, "source": source, "amount_eur": amount, "activity": activity,
                      "rule": None if activity else rule_key, **extra})

    # --- Income (130 box 01) ----------------------------------------------------
    stripe_rows = _load_classified_ytd(year, 4, db_conn)
    for r in stripe_rows:
        _line("income", "stripe", _get_vat_base(r, config), _norm_activity(r.get("activity_type")), "unallocated",
              id=r["id"], date=str(r.get("created_date", ""))[:10])
    for inv in _load_income_invoices_ytd(year, 4, db_conn):
        m = meta.get(inv["id"], {})
        act = _norm_activity(m.get("activity_type"))
        if act is None and (inv.get("tax_treatment") or m.get("tax_treatment")) == "EXEMPT_TEACHING":
            act = "COACHING"
        _line("income", "invoice_out", _income_invoice_eur(inv), act, "unallocated",
              id=inv["id"], date=str(inv.get("tx_date", ""))[:10],
              description=str(inv.get("client_name") or inv.get("description") or "")[:50])
    for r in db_conn.execute(
        """SELECT id, invoice_id, conversion_date, gain_loss_eur FROM fx_exchange_differences
           WHERE conversion_date >= ? AND conversion_date <= ? ORDER BY conversion_date""",
        (f"{year}-01-01", f"{year}-12-31"),
    ).fetchall():
        linked = meta.get(r["invoice_id"]) if r["invoice_id"] else None
        _line("income", "fx_exchange_difference", float(r["gain_loss_eur"]),
              _norm_activity(linked.get("activity_type")) if linked else None, "unallocated",
              id=r["id"], date=r["conversion_date"], invoice_id=r["invoice_id"])

    # --- Expenses (real expenses of 130 box 02) -------------------------------------
    capital_ids = capital_asset_invoice_ids(db_conn)
    for inv in _load_expense_invoices_ytd(year, 4, db_conn):
        if inv["id"] in capital_ids:
            continue   # enters through depreciation, as in the 130
        m = meta.get(inv["id"], {})
        act = _norm_activity(m.get("activity_type"))
        if act is None and registry is not None:
            match = registry.match_invoice(m or inv)
            act = _norm_activity(match.vendor.activity) if match else None
        amount = (inv.get("subtotal_eur") or 0.0) * inv["deductible_pct_irpf"] / 100.0
        _line("expense", "invoice_in", amount, act, "unallocated", id=inv["id"],
              date=str(inv.get("tx_date", ""))[:10],
              description=str(inv.get("vendor_name") or inv.get("description") or "")[:50])
    for r in db_conn.execute(
        """SELECT id, payment_date, amount_eur FROM social_security_payments
           WHERE payment_date >= ? AND payment_date <= ? ORDER BY payment_date""",
        (f"{year}-01-01", f"{year}-12-31"),
    ).fetchall():
        _line("expense", "reta", float(r["amount_eur"]), None, "reta", id=r["id"], date=r["payment_date"])
    for r in stripe_rows:
        if r.get("fee_application"):
            _line("expense", "platform_fee", float(r["fee_application"]), _norm_activity(r.get("activity_type")),
                  "unallocated", id=r["id"], date=str(r.get("created_date", ""))[:10])
    dep = depreciation_for_period(year, 4, db_conn, ytd=True, config=config)
    for d in dep.lines:
        if d.charge_eur:
            _line("expense", "depreciation", d.charge_eur, None, "depreciation", id=d.asset_id,
                  description=d.description[:50])
    manual = _get_tax_entries_total(year, 4, "GASTOS_DEDUCIBLES", db_conn, ytd=True)
    if manual:
        _line("expense", "manual_entry", manual, None, "unallocated", description="GASTOS_DEDUCIBLES")

    # --- Allocation ---------------------------------------------------------------
    direct_income: dict[str, float] = defaultdict(float)
    for ln in lines:
        if ln["kind"] == "income" and ln["activity"]:
            direct_income[ln["activity"]] += ln["amount_eur"]
    income_total_direct = sum(v for v in direct_income.values() if v > 0)
    shares = ({a: max(0.0, direct_income[a]) / income_total_direct for a in ACTIVITIES}
              if income_total_direct > 0 else {DEFAULT_ACTIVITY: 1.0})

    allocated: list[dict] = []
    for ln in lines:
        if ln["activity"]:
            allocated.append({**ln, "allocated_by": "direct"})
            continue
        target = rules[ln["rule"]]
        if target == BY_INCOME:
            for act, share in shares.items():
                if share:
                    allocated.append({**ln, "activity": act, "amount_eur": ln["amount_eur"] * share,
                                      "allocated_by": f"{ln['rule']}:{BY_INCOME} {share:.4f}"})
        else:
            allocated.append({**ln, "activity": target, "allocated_by": f"{ln['rule']}:{target}"})

    by_act = {a: ActivityPL(activity=a, iae=ACTIVITY_IAE[a]) for a in ACTIVITIES}
    sums: dict[tuple[str, str], float] = defaultdict(float)
    for ln in allocated:
        col = ("income" if ln["kind"] == "income" else
               {"invoice_in": "expenses_invoices", "reta": "reta", "depreciation": "depreciation"}
               .get(ln["source"], "other_expenses"))
        if ln["source"] == "invoice_in" and ln["allocated_by"] != "direct":
            col = "other_expenses"
        sums[(ln["activity"], col)] += ln["amount_eur"]
    for (act, col), v in sums.items():
        setattr(by_act[act], col, round(v, 2))

    for kind in ("income", "expense"):
        unallocated = [ln for ln in lines if not ln["activity"] and ln["rule"] == "unallocated" and ln["kind"] == kind]
        if unallocated:
            notes.append(f"{len(unallocated)} {kind} line(s) without an activity (€"
                         f"{sum(ln['amount_eur'] for ln in unallocated):,.2f}) allocated by "
                         f"tax.pl_allocation.unallocated = {rules['unallocated']}; set activity_type on the "
                         "invoice or the vendor registry to attribute them directly.")

    m130 = compute_modelo_130(year, 4, db_conn, config)
    result = PLByActivity(
        year=year, allocation=rules, activities=[by_act[a] for a in ACTIVITIES],
        lines=[{**ln, "amount_eur": round(ln["amount_eur"], 2),
                "iae": ACTIVITY_IAE.get(ln["activity"], "")} for ln in allocated],
        total_income=round(sum(ln["amount_eur"] for ln in lines if ln["kind"] == "income"), 2),
        total_expenses=round(sum(ln["amount_eur"] for ln in lines if ln["kind"] == "expense"), 2),
        gastos_dificil_justificacion=m130.gastos_dificil_justificacion,
        m130_c01=m130.c01_ingresos, m130_gastos_reales=m130.gastos_reales, m130_c02=m130.c02_gastos,
    )
    if not result.ties_to_130:
        notes.append(f"⚠️ P&L does not tie to the Q4 Modelo 130: income {result.total_income:,.2f} vs box 01 "
                     f"{result.m130_c01:,.2f}; expenses {result.total_expenses:,.2f} vs real expenses "
                     f"{result.m130_gastos_reales:,.2f}.")
        log.warning("⚠️ P&L %s does not tie to the Q4 130 (income %.2f vs %.2f, expenses %.2f vs %.2f)",
                    year, result.total_income, result.m130_c01, result.total_expenses, result.m130_gastos_reales)
    result.notes = " ".join(notes)
    log.info("ℹ️ P&L %s by activity: income %.2f, expenses %.2f, GDJ %.2f, ties to 130: %s",
             year, result.total_income, result.total_expenses, result.gastos_dificil_justificacion,
             result.ties_to_130)
    return result
