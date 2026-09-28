"""Tax computation engine for Spanish autónomo obligations."""
from __future__ import annotations

import calendar
import math
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import partial
from typing import Optional

from src.logger import get_logger
from src.tax_models import (
    MODELO130_BOX_FIELDS,
    AuditEntry,
    EUB2CThresholdResult,
    Modelo130Result,
    Modelo303Result,
    Modelo347Result,
    Modelo347Row,
    Modelo349Result,
    Modelo349Row,
    OSSCountryRow,
    OSSReturnResult,
    TaxDeadline,
    _tax_deadline_date,
)
from src.database import derive_tax_treatment_for_invoice
from src.declared_reports import apply_frozen_amounts
from src.fixed_assets import DepreciationResult, capital_asset_invoice_ids, depreciation_for_period
from src.vat_rules import (
    EU_B2C_THRESHOLD_EUR,
    EU_B2C_TREATMENTS,
    EU_B2C_WARN_RATIO,
    IVA_ES_RATE,
    SPANISH_21_TREATMENTS,
    is_oss_registered,
    oss_rate,
    vat_amount_on_base,
    vat_base_from_inclusive,
    vat_treatment,
)

log = get_logger(__name__)

_DUE_SOON_DAYS = 15


# ---------------------------------------------------------------------------
# App-config plumbing
# ---------------------------------------------------------------------------
# The tax engine is config-aware: several ``config.tax`` settings alter the
# computation (EU VAT-treatment overrides, IVA/OSS registration, prorrata,
# fiscal regime). The public compute functions take an optional ``config``
# dict; when it is ``None`` they fall back to an empty dict, i.e. the documented
# defaults — this keeps the engine pure and the unit tests deterministic. The
# app entry points (``compute_and_persist_tax_snapshots`` and the tax validator)
# load the real ``config.json`` once and thread it down.

def _tax_settings(config: Optional[dict]) -> dict:
    """Return the ``tax`` sub-section of the app config (empty dict if absent)."""
    return (config or {}).get("tax", {})


def load_app_config() -> dict:
    """Load ``config.json`` for the engine, returning ``{}`` if unavailable.

    Guarded so the engine never crashes on a missing/broken config file — a
    fresh checkout with no ``config.json`` simply runs with documented defaults.
    """
    try:
        from src.config import load_config

        return load_config()
    except Exception:  # pragma: no cover - config is optional for the engine
        return {}


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

def _load_classified_range(
    start: str, end: str, conn: sqlite3.Connection
) -> list[dict]:
    """Load classified transactions in ``[start, end]`` from an open connection.

    Shared loader for the quarter- and YTD-scoped wrappers below: the SELECT,
    table, ``activity_type`` filter and ordering are identical between them —
    only the ``start`` bound differs.

    Transactions that appear in a frozen (declared) Stripe report carry the
    declared EUR amounts instead of the live ones, so later FX re-conversions
    cannot move a quarter that was already sent to the gestor.
    """
    rows = conn.execute(
        """SELECT id, created_date, converted_amount, converted_amount_refunded,
                  activity_type, geo_region, card_country, email_meta,
                  vat_treatment, vat_base_eur, vat_amount_eur, oss_country, buyer_vat_id
           FROM transactions
           WHERE created_date >= ? AND created_date <= ?
             AND activity_type IS NOT NULL AND activity_type != 'UNKNOWN'
           ORDER BY created_date""",
        (start, end),
    ).fetchall()
    return apply_frozen_amounts([dict(r) for r in rows], conn)


def _classified_quarter_end(year: int, quarter: int) -> str:
    """End bound (inclusive, end-of-day) for transaction queries up to ``quarter``."""
    month_end = quarter * 3
    last_day = calendar.monthrange(year, month_end)[1]
    return f"{year}-{month_end:02d}-{last_day:02d}T23:59:59"


def _load_classified_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Load classified transactions for a specific quarter from an open connection."""
    month_start = (quarter - 1) * 3 + 1
    start = f"{year}-{month_start:02d}-01"
    end = _classified_quarter_end(year, quarter)
    return _load_classified_range(start, end, conn)


def _load_classified_ytd(year: int, quarter: int, conn: sqlite3.Connection) -> list[dict]:
    """Load classified transactions from Q1 through the given quarter."""
    end = _classified_quarter_end(year, quarter)
    return _load_classified_range(f"{year}-01-01", end, conn)


def _invoice_date_range(year: int, quarter: int) -> tuple[str, str]:
    month_start = (quarter - 1) * 3 + 1
    month_end = quarter * 3
    last_day = calendar.monthrange(year, month_end)[1]
    return (
        f"{year}-{month_start:02d}-01",
        f"{year}-{month_end:02d}-{last_day:02d}",
    )


def _load_expense_invoices_range(
    start: str, end: str, conn: sqlite3.Connection
) -> list[dict]:
    """Expense invoices (direction='in') in ``[start, end]``, keyed by ``invoice_date``.

    The accounting date is the invoice date (``supply_date`` is informational
    only), and rows marked ``excluded`` (duplicates, receipts, …) are skipped.
    Shared loader for the quarter- and YTD-scoped wrappers below: the projection,
    ``direction='in'`` filter, date keying and ordering are identical — only the
    ``start`` bound differs.
    """
    rows = conn.execute(
        """SELECT id, invoice_date AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment, tax_treatment,
                  COALESCE(deductible_pct_vat, deductible_pct, 100.0) AS deductible_pct_vat,
                  COALESCE(deductible_pct_irpf, deductible_pct, 100.0) AS deductible_pct_irpf,
                  vendor_nif, vendor_name, description
           FROM invoices
           WHERE direction = 'in'
             AND COALESCE(excluded, 0) = 0
             AND invoice_date >= ?
             AND invoice_date <= ?
           ORDER BY tx_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_expense_invoices_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Expense invoices (direction='in') for the quarter, keyed by invoice_date."""
    start, end = _invoice_date_range(year, quarter)
    return _load_expense_invoices_range(start, end, conn)


def _load_expense_invoices_ytd(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Expense invoices (direction='in') from Q1 through the given quarter (YTD)."""
    _, end = _invoice_date_range(year, quarter)
    return _load_expense_invoices_range(f"{year}-01-01", end, conn)


def _load_income_invoices_range(
    start: str, end: str, conn: sqlite3.Connection
) -> list[dict]:
    """Income invoices (direction='out') in ``[start, end]``, keyed by ``invoice_date``.

    These are manually-issued invoices (bank transfer, etc.) NOT processed through
    Stripe — Stripe income already lives in the ``transactions`` table.

    Shared loader for the quarter- and YTD-scoped wrappers below: the projection,
    ``direction='out'`` filter, invoice-date keying, ``excluded`` filter and
    ordering are identical — only the ``start`` bound differs. Kept separate from
    the expense loader because the projection differs (client_nif/client_name vs
    vendor_nif/vendor_name).
    """
    rows = conn.execute(
        """SELECT id, invoice_date AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment, tax_treatment,
                  client_nif, client_name, description, eur_received
           FROM invoices
           WHERE direction = 'out'
             AND COALESCE(excluded, 0) = 0
             AND invoice_date >= ?
             AND invoice_date <= ?
           ORDER BY tx_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_income_invoices_ytd(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Income invoices (direction='out') from Q1 through the given quarter (YTD)."""
    _, end = _invoice_date_range(year, quarter)
    return _load_income_invoices_range(f"{year}-01-01", end, conn)


def _load_income_invoices_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Income invoices (direction='out') for the quarter only."""
    start, end = _invoice_date_range(year, quarter)
    return _load_income_invoices_range(start, end, conn)


def _income_invoice_eur(inv: dict) -> float:
    """Effective EUR value of an income (direction='out') invoice (issue #93).

    ``eur_received`` wins when set — the money was actually converted on
    receipt. Otherwise the stored ``subtotal_eur`` already holds the ECB rate
    at the invoice date (resolved at OCR time by
    ``src.fx_rates.resolve_invoice_amounts``), which is final per decision D5
    (art. 79.Once LIVA) for income kept in a foreign-currency account — not a
    provisional figure to be revisited later.
    """
    eur_received = inv.get("eur_received")
    return float(eur_received) if eur_received is not None else (inv.get("subtotal_eur") or 0.0)


def _net_amount(row: dict) -> float:
    # Row-dict counterpart of Payment.net_amount (src/models.py) — same formula,
    # different input shape (SQL row dict vs. Pydantic model); kept in sync by hand.
    return round(row["converted_amount"] - row["converted_amount_refunded"], 2)


def _get_vat_treatment(row: dict, config: Optional[dict] = None) -> str:
    """Return stored vat_treatment, or derive it on-the-fly if missing.

    The fallback derivation delegates to the shared treatment matrix in
    ``src.vat_rules`` so the classifier and the engine cannot diverge. The
    ``config`` dict is threaded through so the EU VAT-treatment overrides
    (``tax.default_vat_treatment_eu_*``) and the ``tax.vat_registered`` flag
    are honoured. Rows carrying an explicit stored treatment (manual override)
    win over the derivation.
    """
    stored = row.get("vat_treatment")
    if stored and stored != "UNKNOWN":
        return stored
    # Derive from activity × geo (fallback for rows not yet VAT-classified).
    # buyer_vat_id (accounting-quarterly#113) decides the EU B2B/B2C split.
    return vat_treatment(
        row.get("activity_type"), row.get("geo_region"), config=config,
        buyer_vat_id=row.get("buyer_vat_id"),
    )


def _oss_country_code(row: dict) -> str:
    return (row.get("oss_country") or row.get("card_country") or "").upper()


def _get_vat_base(row: dict, config: Optional[dict] = None) -> float:
    """Return the ex-VAT taxable base for a transaction row.

    Stripe amounts are VAT-inclusive (the customer paid the gross amount).
    For Spain (IVA_ES_21) and EU B2C (OSS_EU) we extract the base by
    dividing by (1 + rate).  For exports and EU B2B (ISP) the full net
    amount is the income base — no VAT was charged.
    """
    if row.get("vat_base_eur") is not None:
        return row["vat_base_eur"]
    return vat_base_from_inclusive(
        _net_amount(row), _get_vat_treatment(row, config), _oss_country_code(row)
    )


def _get_vat_amount(row: dict, config: Optional[dict] = None) -> float:
    if row.get("vat_amount_eur") is not None:
        return row["vat_amount_eur"]
    return vat_amount_on_base(
        _get_vat_base(row, config), _get_vat_treatment(row, config), _oss_country_code(row)
    )


def _get_tax_entries_total(
    year: int, quarter: int, entry_type: str, conn: sqlite3.Connection, ytd: bool = False
) -> float:
    if ytd:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount_eur), 0) AS total
               FROM quarterly_tax_entries
               WHERE year = ? AND quarter <= ? AND entry_type = ?""",
            (year, quarter, entry_type),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount_eur), 0) AS total
               FROM quarterly_tax_entries
               WHERE year = ? AND quarter = ? AND entry_type = ?""",
            (year, quarter, entry_type),
        ).fetchone()
    return float(row["total"]) if row else 0.0


# ---------------------------------------------------------------------------
# Public computation functions
# ---------------------------------------------------------------------------

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
# Safety bound for the app-computed credit chain when no filed return stops it.
_CREDIT_CHAIN_MAX_QUARTERS = 40


@dataclass
class _Collected303:
    """Unrounded, pre-pro-rata sums of one quarter, keyed by result field name."""
    acc: dict[str, float] = field(default_factory=lambda: defaultdict(float))
    records: dict[str, list[dict]] = field(default_factory=lambda: defaultdict(list))
    notes: list[str] = field(default_factory=list)

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


def _invoice_tax_treatment(direction: str, inv: dict) -> Optional[str]:
    """The ledger ``tax_treatment``, derived from the legacy columns when unset."""
    return inv.get("tax_treatment") or derive_tax_treatment_for_invoice(
        direction, inv.get("vat_treatment"), inv.get("geo_region"), inv.get("iva_amount")
    )


def _collect_303_sales(year: int, quarter: int, conn: sqlite3.Connection,
                       config: Optional[dict], col: _Collected303) -> None:
    """Accrued VAT and informational boxes from Stripe rows and issued invoices."""
    # Stripe: aggregated per (geo, activity, treatment, OSS country) — the
    # gestor works with the quarterly summary, not individual charges.
    agg: dict[tuple, dict] = {}
    for row in _load_classified_for_quarter(year, quarter, conn):
        treatment = _get_vat_treatment(row, config)
        base = _get_vat_base(row, config)
        vat = _get_vat_amount(row, config)
        oss_cc = _oss_country_code(row) if treatment == "OSS_EU" else ""
        key = (row.get("geo_region") or "UNKNOWN", row.get("activity_type") or "UNKNOWN",
               treatment, oss_cc)
        bucket = agg.setdefault(key, {"n": 0, "gross_eur": 0.0, "base_eur": 0.0, "vat_eur": 0.0})
        bucket["n"] += 1
        bucket["gross_eur"] += _net_amount(row)
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
    for inv in _load_income_invoices_for_quarter(year, quarter, conn):
        tt = _invoice_tax_treatment("out", inv)
        base = _income_invoice_eur(inv)
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


def _collect_303_purchases(year: int, quarter: int, conn: sqlite3.Connection,
                           col: _Collected303) -> None:
    """Reverse-charge accruals and deductible VAT (at 100%, before pro-rata)."""
    from src.fixed_assets import capital_goods_vat_for_period, load_fixed_assets

    capital_by_invoice: dict[str, list] = defaultdict(list)
    for asset in load_fixed_assets(conn):
        if asset.vat_capital_good and asset.invoice_id:
            capital_by_invoice[asset.invoice_id].append(asset)

    for inv in _load_expense_invoices_for_quarter(year, quarter, conn):
        tt = _invoice_tax_treatment("in", inv)
        base = inv.get("subtotal_eur") or 0.0
        iva = inv.get("iva_amount") or 0.0
        pct = inv["deductible_pct_vat"] / 100.0
        rec = {"source": "invoice_in", "id": inv.get("id"),
               "date": str(inv.get("tx_date", ""))[:10],
               "vendor": str(inv.get("vendor_name") or inv.get("vendor_nif") or "")[:40],
               "description": str(inv.get("description") or "")[:50],
               "subtotal_eur": round(base, 2), "iva_amount": round(iva, 2),
               "deductible_pct_vat": inv["deductible_pct_vat"], "tax_treatment": tt}

        if tt in ("DOMESTIC", "DOMESTIC_CAPITAL"):
            linked = capital_by_invoice.get(inv.get("id"), [])
            if linked:
                # The capital-good share is deducted in 30/31 from the fixed-asset
                # register (at its own VAT business %); only the rest stays in 28/29.
                base_rest = max(0.0, base - sum(a.base_eur for a in linked))
                iva_rest = max(0.0, iva - sum(a.vat_eur for a in linked))
                rec["capital_goods_in_30_31"] = [a.id for a in linked]
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
            if iva_rest > 0:
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


def _collect_303_quarter(year: int, quarter: int, conn: sqlite3.Connection,
                         config: Optional[dict], *, sales_only: bool = False) -> _Collected303:
    col = _Collected303()
    _collect_303_sales(year, quarter, conn, config, col)
    if not sales_only:
        _collect_303_purchases(year, quarter, conn, col)
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
    tax_cfg = _tax_settings(config)
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
    option = override or _tax_settings(config).get("modelo303_q4_negative_result", "compensate")
    if option not in Q4_NEGATIVE_OPTIONS:
        raise ValueError(f"Q4 negative-result option must be one of {Q4_NEGATIVE_OPTIONS}, got {option!r}")
    return option


def _previous_period(year: int, quarter: int) -> tuple[int, int]:
    return (year, quarter - 1) if quarter > 1 else (year - 1, 4)


def _earliest_303_activity(conn: sqlite3.Connection) -> Optional[str]:
    """ISO date of the first row that can feed a 303 (None on an empty DB)."""
    dates = [
        conn.execute("SELECT MIN(created_date) FROM transactions WHERE activity_type IS NOT NULL "
                     "AND activity_type != 'UNKNOWN'").fetchone()[0],
        conn.execute("SELECT MIN(invoice_date) FROM invoices WHERE COALESCE(excluded, 0) = 0").fetchone()[0],
    ]
    row = conn.execute("SELECT MIN(year * 10 + quarter) FROM quarterly_tax_entries").fetchone()[0]
    if row:
        dates.append(f"{row // 10}-{(row % 10 - 1) * 3 + 1:02d}-01")
    dates = [str(d)[:10] for d in dates if d]
    return min(dates) if dates else None


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
    earliest = _earliest_303_activity(conn)
    _, prev_end = _invoice_date_range(py, pq)
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
    tax_cfg = _tax_settings(config)
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
           "SUM(subtotal_eur) expense invoices NON_EU_RC (D8, art. 84.Uno.2º LIVA)", r.c12_base,
           records=rec["c12_base"]),
        _a("c13_cuota", "13 Otras operaciones con ISP — cuota", "SUM(round(base × rate, 2)) of the 12 invoices",
           r.c13_cuota),
        _a("c27_total_devengado", "27 Total cuota devengada", "03 + 06 + 09 + 11 + 13", r.c27_total_devengado,
           c03=r.c03_cuota, c06=r.c06_cuota, c09=r.c09_cuota, c11=r.c11_cuota, c13=r.c13_cuota),
        _a("c28_base", "28 Base — operaciones interiores corrientes",
           "(SUM(base × deductible_pct_vat) DOMESTIC excl. capital goods + NON_EU_RC + manual entries "
           "(cuota / rate)) × provisional pro-rata", r.c28_base,
           base_100=round(col.acc["c28_base"], 2), records=rec["c28_base"], **pro),
        _a("c29_cuota", "29 Cuota — operaciones interiores corrientes",
           "(SUM(iva × deductible_pct_vat) DOMESTIC excl. capital goods + NON_EU_RC self-assessed "
           "+ manual IVA_SOPORTADO) × provisional pro-rata", r.c29_cuota,
           cuota_100=round(col.acc["c29_cuota"], 2), **pro),
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


# ---------------------------------------------------------------------------
# Modelo 130 — AEAT box model, box 13 reduction and negative carry (#98)
# ---------------------------------------------------------------------------
# Box layout and formulas follow the AEAT Sede "Modelo 130 — Instrucciones"
# (sede.agenciatributaria.gob.es, IRPF > Modelo 130 > Instrucciones):
#   02 (simplificada) includes amortizaciones, provisiones and the gastos de
#      difícil justificación (art. 30.2.4ª LIRPF: 5 %, max €2,000 a year);
#   03 = 01 − 02, "si negativa, se consigna con signo menos";
#   04 = 20 % of the positive 03 (0 when 03 is negative);
#   05 = Σ positive 07 of the earlier returns of the year − Σ their 16;
#   07 = 04 − 05 − 06, may be negative;   12 = 07 + 11, 0 when negative;
#   13 = art. 110.3.c RIRPF reduction by the previous year's net yield of
#        economic activities (≤ 9,000 → 100; ≤ 10,000 → 75; ≤ 11,000 → 50;
#        ≤ 12,000 → 25; no activity the previous year counts as 0);
#   14 = 12 − 13, may be negative;
#   15 = negative 19s of earlier returns of the year not yet deducted, only
#        when 14 is positive and never more than the positive 14;
#   17 = 14 − 15 − 16, may be negative;   19 = 17 − 18, may be negative.
# Filed returns confirm 13 applies even when 12 = 0, leaving a negative 14/19
# that a later quarter deducts in 15.

GDJ_RATE = 0.05
GDJ_CAP_EUR = 2000.0
_PAGO_FRACCIONADO_RATE = 0.20
# art. 110.3.c RIRPF: (previous-year net yield upper bound, quarterly reduction).
_MINORACION_SCALE: tuple[tuple[float, float], ...] = (
    (9000.0, 100.0), (10000.0, 75.0), (11000.0, 50.0), (12000.0, 25.0),
)


def minoracion_art_110_3_c(previous_year_net_yield: float) -> float:
    """Box 13 of the Modelo 130 for a given previous-year net yield (EUR)."""
    net = round(previous_year_net_yield, 2)
    for upper, reduction in _MINORACION_SCALE:
        if net <= upper:
            return reduction
    return 0.0


@dataclass
class _Collected130:
    """Year-to-date inputs of boxes 01, 02 (real expenses) and 06, with audit records."""
    stripe_income: float = 0.0
    inv_income: float = 0.0
    exch_diff_total: float = 0.0
    inv_gastos: float = 0.0
    ss_gastos: float = 0.0
    manual_gastos: float = 0.0
    inv_retenciones: float = 0.0
    manual_retenciones: float = 0.0
    capital_excluded: float = 0.0
    unregistered_capital_ids: list = field(default_factory=list)
    depreciation: Optional[DepreciationResult] = None
    records: dict = field(default_factory=dict)

    @property
    def c01(self) -> float:
        return round(self.stripe_income + self.inv_income + self.exch_diff_total, 2)

    @property
    def gastos_reales(self) -> float:
        return round(self.inv_gastos + self.ss_gastos + self.depreciation.total_eur + self.manual_gastos, 2)

    @property
    def c06(self) -> float:
        return round(self.inv_retenciones + self.manual_retenciones, 2)


def _collect_130_ytd(year: int, quarter: int, conn: sqlite3.Connection,
                     config: Optional[dict]) -> _Collected130:
    """Gather income, real expenses and withholdings from 1 January to the quarter end."""
    col = _Collected130()
    _, ytd_end = _invoice_date_range(year, quarter)
    ytd_start = f"{year}-01-01"

    # 01 — Stripe VAT bases (frozen declared amounts win, see _load_classified_range).
    rows = _load_classified_ytd(year, quarter, conn)
    col.stripe_income = sum(_get_vat_base(r, config) for r in rows)
    # 01 — issued invoices, gross of the IRPF withheld (D12); eur_received wins (#93).
    income_invs = _load_income_invoices_ytd(year, quarter, conn)
    col.inv_income = sum(_income_invoice_eur(inv) for inv in income_invs)
    # 01 — exchange differences (#93 / D5): converting a foreign-currency balance
    # realises a gain/loss against the EUR booked; income of the conversion period.
    exch_rows = conn.execute(
        """SELECT id, invoice_id, conversion_date, currency, foreign_amount,
                  eur_obtained, booked_eur, gain_loss_eur, notes
           FROM fx_exchange_differences
           WHERE conversion_date >= ? AND conversion_date <= ?
           ORDER BY conversion_date""",
        (ytd_start, ytd_end),
    ).fetchall()
    col.exch_diff_total = round(sum(float(r["gain_loss_eur"]) for r in exch_rows), 2)

    # 02 — expense invoices × deductible_pct_irpf; excluded rows are filtered by
    # the loader, capital assets enter through depreciation instead (#96).
    expense_invs = _load_expense_invoices_ytd(year, quarter, conn)
    capital_ids = capital_asset_invoice_ids(conn)
    capital_invs = [inv for inv in expense_invs if inv["id"] in capital_ids]
    expense_invs = [inv for inv in expense_invs if inv["id"] not in capital_ids]
    col.depreciation = depreciation_for_period(year, quarter, conn, ytd=True, config=config)
    registered_ids = {line.invoice_id for line in col.depreciation.lines if line.invoice_id}
    col.unregistered_capital_ids = [inv["id"] for inv in capital_invs if inv["id"] not in registered_ids]
    if col.unregistered_capital_ids:
        log.warning("⚠️ 130 %dQ%d: %d capital-asset invoice(s) have no fixed asset registered — "
                    "their cost is neither expensed nor depreciated: %s",
                    year, quarter, len(col.unregistered_capital_ids), ", ".join(col.unregistered_capital_ids))

    def _deductible(inv: dict) -> float:
        return (inv.get("subtotal_eur") or 0.0) * inv["deductible_pct_irpf"] / 100.0

    col.inv_gastos = sum(_deductible(inv) for inv in expense_invs)
    col.capital_excluded = round(sum(_deductible(inv) for inv in capital_invs), 2)
    # 02 — RETA cuotas as paid, net of refunds (stored negative), art. 30 LIRPF.
    ss_rows = conn.execute(
        """SELECT id, payment_date, amount_eur, description
           FROM social_security_payments
           WHERE payment_date >= ? AND payment_date <= ?
           ORDER BY payment_date""",
        (ytd_start, ytd_end),
    ).fetchall()
    col.ss_gastos = round(sum(float(r["amount_eur"]) for r in ss_rows), 2)
    col.manual_gastos = _get_tax_entries_total(year, quarter, "GASTOS_DEDUCIBLES", conn, ytd=True)

    # 06 — IRPF withheld by clients on issued invoices (exact cents) + manual entries.
    col.inv_retenciones = sum((inv.get("irpf_amount") or 0.0) for inv in income_invs)
    col.manual_retenciones = _get_tax_entries_total(year, quarter, "RETENCIONES_SOPORTADAS", conn, ytd=True)

    # --- Audit records ------------------------------------------------------
    stripe_agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("geo_region") or "UNKNOWN", r.get("activity_type") or "UNKNOWN",
               _get_vat_treatment(r, config))
        agg = stripe_agg.setdefault(key, {"n": 0, "gross_eur": 0.0, "base_eur": 0.0})
        agg["n"] += 1
        agg["gross_eur"] = round(agg["gross_eur"] + _net_amount(r), 2)
        agg["base_eur"] = round(agg["base_eur"] + _get_vat_base(r, config), 2)

    def _client(inv: dict) -> str:
        return str(inv.get("client_name") or inv.get("client_nif") or "")[:40]

    def _vendor(inv: dict) -> str:
        return str(inv.get("vendor_name") or inv.get("vendor_nif") or "")[:40]

    col.records = {
        "income": [
            {"source": "stripe_agregado", "geo_region": k[0], "activity": k[1], "vat_treatment": k[2],
             "n_transactions": v["n"], "gross_eur": v["gross_eur"], "base_eur_irpf": v["base_eur"]}
            for k, v in stripe_agg.items()
        ] + [
            {"source": "invoice_out", "date": inv.get("tx_date", "")[:10], "client": _client(inv),
             "description": str(inv.get("description") or "")[:50],
             "subtotal_eur": round(inv.get("subtotal_eur") or 0.0, 2),
             "eur_received": (round(inv["eur_received"], 2) if inv.get("eur_received") is not None else None),
             "eur_used": round(_income_invoice_eur(inv), 2),
             "irpf_amount": round(inv.get("irpf_amount") or 0.0, 2),
             "vat_treatment": inv.get("vat_treatment") or ""}
            for inv in income_invs
        ] + [
            {"source": "fx_exchange_difference", "date": r["conversion_date"], "currency": r["currency"],
             "foreign_amount": round(float(r["foreign_amount"]), 2),
             "eur_obtained": round(float(r["eur_obtained"]), 2),
             "booked_eur": round(float(r["booked_eur"]), 2),
             "gain_loss_eur": round(float(r["gain_loss_eur"]), 2), "notes": r["notes"] or ""}
            for r in exch_rows
        ],
        "expenses": [
            {"source": "invoice_in", "date": inv.get("tx_date", "")[:10], "vendor": _vendor(inv),
             "description": str(inv.get("description") or "")[:50],
             "subtotal_eur": round(inv.get("subtotal_eur") or 0.0, 2),
             "deductible_pct": inv["deductible_pct_irpf"], "deductible_amount": round(_deductible(inv), 2),
             "geo_region": inv.get("geo_region") or ""}
            for inv in expense_invs
        ],
        "social_security": [
            {"source": "social_security", "date": r["payment_date"],
             "description": r["description"] or "Cuota Seguridad Social",
             "amount_eur": round(float(r["amount_eur"]), 2)}
            for r in ss_rows
        ],
        "capital": [
            {"source": "invoice_in_capital", "date": inv.get("tx_date", "")[:10], "vendor": _vendor(inv),
             "description": str(inv.get("description") or "")[:50],
             "subtotal_eur": round(inv.get("subtotal_eur") or 0.0, 2),
             "fixed_asset_registered": inv["id"] in registered_ids}
            for inv in capital_invs
        ],
        "withholdings": [
            {"source": "invoice_out", "date": inv.get("tx_date", "")[:10], "client": _client(inv),
             "description": str(inv.get("description") or "")[:50],
             "subtotal_eur": round(inv.get("subtotal_eur") or 0.0, 2),
             "irpf_amount": round(inv.get("irpf_amount") or 0.0, 2)}
            for inv in income_invs if inv.get("irpf_amount")
        ],
    }
    return col


def _gastos_dificil_justificacion(c01: float, gastos_reales: float, eligible: bool) -> tuple[float, float]:
    """(allowance, raw 5 %) — 5 % of the positive (01 − real expenses), capped at €2,000.

    Art. 30.2.4ª LIRPF, estimación directa simplificada only. The cap is annual;
    the 130 is cumulative, so the YTD figure is capped directly.
    """
    base = round(c01 - gastos_reales, 2)
    if not eligible or base <= 0:
        return 0.0, 0.0
    raw = base * GDJ_RATE
    return round(min(raw, GDJ_CAP_EUR), 2), raw


def _gdj_eligible(config: Optional[dict]) -> tuple[str, bool]:
    regime = _tax_settings(config).get("regime", "estimacion_directa_simplificada")
    return regime, regime == "estimacion_directa_simplificada"


def _previous_year_net_yield(year: int, conn: sqlite3.Connection,
                             config: Optional[dict]) -> tuple[float, str, dict]:
    """Net yield of economic activities of ``year − 1`` for box 13, as (amount, source, detail).

    Order: the filed Q4 130 of the previous year (its box 03, cumulative for
    the whole year); then config ``tax.previous_year_net_yield`` (a number, or
    ``{"<year>": amount}``); then the app's own previous-year Q4 box 03 — with
    no data that is 0, which is also the AEAT rule when there was no activity.
    """
    from src.filed_returns import load_filed_boxes

    prev = year - 1
    filed = load_filed_boxes(conn, "130", prev, "4T")
    if filed:
        return round(filed.get("03", 0.0), 2), "filed", {"period": f"{prev}-4T", "filed_03": filed.get("03", 0.0)}
    configured = _tax_settings(config).get("previous_year_net_yield")
    if isinstance(configured, dict):
        configured = configured.get(str(prev), configured.get(prev))
    if configured is not None:
        return round(float(configured), 2), "config", {"previous_year": prev, "config_value": configured}
    col = _collect_130_ytd(prev, 4, conn, config)
    _, eligible = _gdj_eligible(config)
    gdj, _ = _gastos_dificil_justificacion(col.c01, col.gastos_reales, eligible)
    net = round(col.c01 - col.gastos_reales - gdj, 2)
    return net, "app", {"previous_year": prev, "app_01": col.c01, "app_02": round(col.gastos_reales + gdj, 2)}


@dataclass
class _Chain130:
    """Boxes 05 and 15 inputs accumulated over the earlier quarters of the year."""
    positive_07: float = 0.0
    sum_16: float = 0.0
    negative_19: float = 0.0
    used_15: float = 0.0
    periods: list = field(default_factory=list)

    @property
    def c05(self) -> float:
        return round(self.positive_07 - self.sum_16, 2)

    @property
    def negatives_pending(self) -> float:
        return round(self.negative_19 - self.used_15, 2)

    def add(self, period: str, source: str, c07: float, c15: float, c16: float, c19: float) -> None:
        self.positive_07 += max(0.0, c07)
        self.sum_16 += c16
        self.negative_19 += max(0.0, -c19)
        self.used_15 += c15
        self.periods.append({"period": period, "source": source, "c07": c07, "c15": c15,
                             "c16": c16, "c19": c19})

    @property
    def source(self) -> str:
        sources = {p["source"] for p in self.periods}
        if not sources:
            return "none"
        return sources.pop() if len(sources) == 1 else "mixed"


def compute_modelo_130(
    year: int, quarter: int, db_conn: sqlite3.Connection, config: Optional[dict] = None
) -> Modelo130Result:
    """Compute Modelo 130 (quarterly IRPF advance) box by box for the given quarter.

    ``config`` drives the EU VAT-treatment overrides (via the shared
    derivation), the ``tax.regime`` gate on the 5 % gastos de difícil
    justificación (only *estimación directa simplificada*), the fixed-assets
    posting mode and ``tax.previous_year_net_yield`` (box 13 fallback).

    Boxes 05 and 15 chain through the earlier quarters of the year: each
    quarter's filed return is used when imported (the legally operative
    figures), otherwise the app's own computation of that quarter.
    """
    from src.filed_returns import load_filed_boxes

    prev_net = _previous_year_net_yield(year, db_conn, config)
    chain = _Chain130()
    for p in range(1, quarter):
        filed = load_filed_boxes(db_conn, "130", year, f"{p}T")
        if filed:
            # A blank box on a filed return is 0.
            chain.add(f"{year}-{p}T", "filed", filed.get("07", 0.0), filed.get("15", 0.0),
                      filed.get("16", 0.0), filed.get("19", 0.0))
        else:
            r = _build_modelo_130(year, p, db_conn, config, prev_net, chain)
            chain.add(f"{year}-{p}T", "app_chain", r.c07_pago_fraccionado, r.c15_negativos_anteriores,
                      r.c16_deduccion_vivienda, r.c19_resultado)
    return _build_modelo_130(year, quarter, db_conn, config, prev_net, chain)


def _build_modelo_130(year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict],
                      prev_net: tuple[float, str, dict], chain: _Chain130) -> Modelo130Result:
    """One quarter's boxes, given the previous-year net yield and the earlier-quarter chain."""
    r = Modelo130Result(year=year, quarter=quarter)
    col = _collect_130_ytd(year, quarter, conn, config)
    regime, eligible = _gdj_eligible(config)

    # I. Actividades económicas (estimación directa)
    r.c01_ingresos = col.c01
    r.gastos_reales = col.gastos_reales
    r.gastos_dificil_justificacion, raw_gdj = _gastos_dificil_justificacion(r.c01_ingresos, r.gastos_reales,
                                                                            eligible)
    r.c02_gastos = round(r.gastos_reales + r.gastos_dificil_justificacion, 2)
    r.c03_rendimiento_neto = round(r.c01_ingresos - r.c02_gastos, 2)
    r.c04_veinte_pct = round(max(0.0, r.c03_rendimiento_neto) * _PAGO_FRACCIONADO_RATE, 2)
    r.c05_pagos_anteriores = chain.c05
    r.c05_source = chain.source
    r.c06_retenciones = col.c06
    r.c07_pago_fraccionado = round(r.c04_veinte_pct - r.c05_pagos_anteriores - r.c06_retenciones, 2)
    # II. Actividades agrícolas: not applicable, 08–11 stay 0.
    # III. Total liquidación
    r.c12_suma_pagos = round(max(0.0, r.c07_pago_fraccionado + r.c11_pago_fraccionado_agricolas), 2)
    r.previous_year_net_yield, r.previous_year_net_source, prev_detail = prev_net
    r.c13_minoracion = minoracion_art_110_3_c(r.previous_year_net_yield)
    r.c14_diferencia = round(r.c12_suma_pagos - r.c13_minoracion, 2)
    r.negativos_pendientes_anteriores = chain.negatives_pending
    r.c15_negativos_anteriores = (
        round(min(r.c14_diferencia, r.negativos_pendientes_anteriores), 2) if r.c14_diferencia > 0 else 0.0)
    r.c17_total = round(r.c14_diferencia - r.c15_negativos_anteriores - r.c16_deduccion_vivienda, 2)
    r.c19_resultado = round(r.c17_total - r.c18_complementaria, 2)
    r.negativos_pendientes_posteriores = round(
        r.negativos_pendientes_anteriores - r.c15_negativos_anteriores + max(0.0, -r.c19_resultado), 2)

    notes = []
    if r.c19_resultado < 0:
        notes.append(f"Negative result: {-r.c19_resultado:,.2f} can be deducted in box 15 of later "
                     f"quarters of {year} (pending after this quarter: {r.negativos_pendientes_posteriores:,.2f}).")
    if col.unregistered_capital_ids:
        notes.append(f"{len(col.unregistered_capital_ids)} capital-asset invoice(s) have no fixed asset "
                     "registered: neither expensed nor depreciated.")
    r.notes = " ".join(notes)
    log.info("ℹ️ Modelo 130 %s Q%d: 01=%.2f 02=%.2f 03=%.2f 05=%.2f (%s) 06=%.2f 07=%.2f 13=%.2f (%s) "
             "15=%.2f 19=%.2f", year, quarter, r.c01_ingresos, r.c02_gastos, r.c03_rendimiento_neto,
             r.c05_pagos_anteriores, r.c05_source, r.c06_retenciones, r.c07_pago_fraccionado,
             r.c13_minoracion, r.previous_year_net_source, r.c15_negativos_anteriores, r.c19_resultado)

    r.audit = _modelo130_audit(r, col, regime, eligible, raw_gdj, prev_detail, chain)
    return r


def _modelo130_audit(r: Modelo130Result, col: _Collected130, regime: str, eligible: bool, raw_gdj: float,
                     prev_detail: dict, chain: _Chain130) -> list[AuditEntry]:
    """One audit entry per box, plus the box 02 split (cells named c02_*)."""
    _a = partial(AuditEntry.of, "130", r.year, r.quarter)
    dep = col.depreciation
    rec = col.records
    return [
        _a("c01_ingresos",
           "01 Ingresos computables YTD — Stripe + facturas emitidas + diferencias de cambio",
           "SUM(vat_base_eur) FROM transactions YTD (frozen declared amounts win) "
           "+ SUM(COALESCE(eur_received, subtotal_eur)) FROM invoices WHERE direction='out' YTD "
           "+ SUM(gain_loss_eur) FROM fx_exchange_differences YTD",
           r.c01_ingresos, stripe_income=round(col.stripe_income, 2), inv_income=round(col.inv_income, 2),
           exchange_diff_total=col.exch_diff_total, ytd_through_quarter=r.quarter, records=rec["income"]),
        _a("c02_gastos", "02 Gastos fiscalmente deducibles YTD (gastos reales + 5% difícil justificación)",
           "gastos_reales + gastos_dificil_justificacion", r.c02_gastos,
           gastos_reales=r.gastos_reales, gastos_dificil_justificacion=r.gastos_dificil_justificacion),
        _a("c02_gastos_reales",
           "02 · Gastos reales YTD — facturas recibidas + cuotas RETA + amortizaciones + entradas manuales",
           "SUM(subtotal_eur × deductible_pct_irpf/100) FROM invoices WHERE direction='in' AND excluded=0 "
           "AND not a capital asset YTD + SUM(amount_eur) FROM social_security_payments YTD (refunds negative) "
           "+ amortizaciones YTD + SUM(amount_eur) FROM quarterly_tax_entries WHERE entry_type='GASTOS_DEDUCIBLES'",
           r.gastos_reales, inv_gastos=round(col.inv_gastos, 2), ss_gastos=col.ss_gastos,
           amortizaciones=dep.total_eur, manual_gastos=round(col.manual_gastos, 2),
           ss_records=rec["social_security"], records=rec["expenses"]),
        _a("c02_gastos_dificil_justificacion", "02 · Gastos de difícil justificación (5%, máx. €2.000/año)",
           "min(5% × max(0, 01 − gastos_reales), 2000) if regime = estimacion_directa_simplificada else 0 "
           "[art. 30.2.4ª LIRPF]",
           r.gastos_dificil_justificacion, base=round(r.c01_ingresos - r.gastos_reales, 2), rate=GDJ_RATE,
           cap_eur=GDJ_CAP_EUR, raw_5pct=round(raw_gdj, 2), cap_applied=raw_gdj > GDJ_CAP_EUR,
           regime=regime, gdj_eligible=eligible),
        _a("c02_amortizaciones", f"02 · Amortizaciones YTD (tabla simplificada, contabilización {dep.posting_mode})",
           "SUM(base × business% × coeficiente × días/días_año), tope base × business%; "
           f"bienes ≤ {dep.threshold_eur:.2f} € se gastan en el trimestre de adquisición "
           "[Orden 27/03/1998; art. 30 RIRPF]",
           dep.total_eur, posting_mode=dep.posting_mode, threshold_eur=dep.threshold_eur,
           period_start=dep.period_start, period_end=dep.period_end, records=dep.records()),
        _a("c02_capital_assets_excluded", "02 · Facturas de inmovilizado excluidas de gastos (entran vía amortización)",
           "SUM(subtotal_eur × deductible_pct_irpf/100) of capital-asset invoices YTD — informative, not in 02",
           col.capital_excluded, unregistered_invoice_ids=col.unregistered_capital_ids, records=rec["capital"]),
        _a("c03_rendimiento_neto", "03 Rendimiento neto (01 − 02)", "01 − 02 (negative allowed)",
           r.c03_rendimiento_neto, c01=r.c01_ingresos, c02=r.c02_gastos),
        _a("c04_veinte_pct", "04 20% del importe positivo de 03", "20% × max(0, 03)", r.c04_veinte_pct,
           c03=r.c03_rendimiento_neto, rate=_PAGO_FRACCIONADO_RATE),
        _a("c05_pagos_anteriores", "05 Pagos fraccionados de trimestres anteriores",
           "Σ positive 07 − Σ 16 of the earlier quarters of the year (filed return when imported, "
           "else the app's own quarter)", r.c05_pagos_anteriores, source=r.c05_source, periods=chain.periods),
        _a("c06_retenciones", "06 Retenciones e ingresos a cuenta YTD",
           "SUM(irpf_amount) FROM invoices WHERE direction='out' YTD (gross-booked, D12) "
           "+ SUM(amount_eur) FROM quarterly_tax_entries WHERE entry_type='RETENCIONES_SOPORTADAS'",
           r.c06_retenciones, inv_retenciones=round(col.inv_retenciones, 2),
           manual_retenciones=round(col.manual_retenciones, 2), records=rec["withholdings"]),
        _a("c07_pago_fraccionado", "07 Pago fraccionado previo (04 − 05 − 06)", "04 − 05 − 06 (negative allowed)",
           r.c07_pago_fraccionado, c04=r.c04_veinte_pct, c05=r.c05_pagos_anteriores, c06=r.c06_retenciones),
        *[_a(MODELO130_BOX_FIELDS[box], f"{box} {label} (actividades agrícolas, ganaderas, forestales y pesqueras)",
             "not applicable — 0", getattr(r, MODELO130_BOX_FIELDS[box]))
          for box, label in (("08", "Volumen de ingresos"), ("09", "2% de 08"),
                             ("10", "Retenciones e ingresos a cuenta"), ("11", "Pago fraccionado previo (09 − 10)"))],
        _a("c12_suma_pagos", "12 Suma de pagos fraccionados previos (07 + 11)", "max(0, 07 + 11)",
           r.c12_suma_pagos, c07=r.c07_pago_fraccionado, c11=r.c11_pago_fraccionado_agricolas),
        _a("c13_minoracion", "13 Minoración art. 110.3.c RIRPF",
           "previous-year net yield ≤ 9,000 → 100; ≤ 10,000 → 75; ≤ 11,000 → 50; ≤ 12,000 → 25; else 0 "
           "(filed Q4 130 box 03, else tax.previous_year_net_yield, else the app's previous-year Q4)",
           r.c13_minoracion, previous_year_net_yield=r.previous_year_net_yield,
           source=r.previous_year_net_source, **prev_detail),
        _a("c14_diferencia", "14 Diferencia (12 − 13)", "12 − 13 (negative allowed)", r.c14_diferencia,
           c12=r.c12_suma_pagos, c13=r.c13_minoracion),
        _a("c15_negativos_anteriores", "15 Resultados negativos de trimestres anteriores",
           "min(14, unused negative 19s of the earlier quarters of the year) when 14 > 0, else 0",
           r.c15_negativos_anteriores, pending_before=r.negativos_pendientes_anteriores,
           pending_after=r.negativos_pendientes_posteriores, periods=chain.periods),
        _a("c16_deduccion_vivienda", "16 Deducción por préstamo vivienda habitual", "not applicable — 0",
           r.c16_deduccion_vivienda),
        _a("c17_total", "17 Total (14 − 15 − 16)", "14 − 15 − 16 (negative allowed)", r.c17_total),
        _a("c18_complementaria", "18 Resultado a ingresar de la autoliquidación anterior (complementaria)",
           "0 — not a complementary return", r.c18_complementaria),
        _a("c19_resultado", "19 Resultado de la autoliquidación (17 − 18)",
           "17 − 18 (negative: deductible in box 15 of later quarters of the year)", r.c19_resultado,
           negativos_pendientes_posteriores=r.negativos_pendientes_posteriores),
    ]


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
    from src.database import _EU_VAT_PREFIXES, normalize_vat_id
    from src.vendor_registry import load_registry

    result = Modelo349Result(year=year, quarter=quarter)
    ops: dict[tuple[str, str], dict] = {}   # (key, VAT id or "?name") -> bucket

    def _add(key: str, vat: Optional[str], name: str, base: float, rec: dict) -> None:
        b = ops.setdefault((key, vat or f"?{name}"), {"vat": vat or "", "name": name,
                                                      "base": 0.0, "records": []})
        b["name"] = b["name"] or name
        b["base"] += base
        b["records"].append({**rec, "base_eur": round(base, 2)})

    for row in _load_classified_for_quarter(year, quarter, db_conn):
        if _get_vat_treatment(row, config) != "IVA_EU_B2B":
            continue
        _add("S", normalize_vat_id(row.get("buyer_vat_id")), row.get("email_meta") or "",
             _get_vat_base(row, config),
             {"source": "stripe", "id": row["id"], "date": str(row["created_date"])[:10]})

    for inv in _load_income_invoices_for_quarter(year, quarter, db_conn):
        if _invoice_tax_treatment("out", inv) != "EU_B2B":
            continue
        _add("S", normalize_vat_id(inv.get("client_nif")), inv.get("client_name") or "",
             _income_invoice_eur(inv),
             {"source": "invoice_out", "id": inv["id"], "date": str(inv["tx_date"])[:10]})

    registry = load_registry()
    start, end = _invoice_date_range(year, quarter)
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
        if _invoice_tax_treatment("in", inv) != "INTRA_EU_RC":
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
    eu_prefixes = _EU_VAT_PREFIXES | {"EL"}   # Greece's VAT ids use EL, not GR
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
            if country not in eu_prefixes:
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


def compute_oss_return(
    year: int, quarter: int, db_conn: sqlite3.Connection, config: Optional[dict] = None
) -> OSSReturnResult:
    """Compute OSS quarterly return (B2C digital services to EU non-Spain customers).

    Unless ``tax.oss_registered`` is explicitly true (default **false**) the
    taxpayer is not enrolled in the One Stop Shop, so no OSS return is produced
    (an audit note records why) and EU B2C sales are taxed in Spain
    (``EU_B2C_ES21``, Modelo 303). Otherwise ``config`` is threaded through the
    VAT-treatment derivation.
    """
    result = OSSReturnResult(year=year, quarter=quarter)
    _a = partial(AuditEntry.of, "OSS", year, quarter)
    if not is_oss_registered(config):
        result.audit = [_a(
            "oss_not_registered",
            "OSS no aplicable — no registrado en el régimen One Stop Shop",
            "tax.oss_registered != true → no OSS return generated (EU B2C → EU_B2C_ES21 in 303)",
            0.0,
            oss_registered=False,
        )]
        return result
    rows = _load_classified_for_quarter(year, quarter, db_conn)

    by_country: dict[str, dict] = defaultdict(lambda: {"count": 0, "base": 0.0, "vat": 0.0})
    for row in rows:
        treatment = _get_vat_treatment(row, config)
        if treatment != "OSS_EU":
            continue
        cc = (row.get("oss_country") or row.get("card_country") or "UNKNOWN").upper()
        base = _get_vat_base(row, config)
        vat = _get_vat_amount(row, config)
        by_country[cc]["count"] += 1
        by_country[cc]["base"] += base
        by_country[cc]["vat"] += vat

    for country, data in sorted(by_country.items()):
        rate = oss_rate(country)
        result.rows.append(OSSCountryRow(
            country=country,
            transactions=data["count"],
            base_eur=round(data["base"], 2),
            vat_rate=rate,
            vat_amount_eur=round(data["vat"], 2),
        ))
        result.total_base += data["base"]
        result.total_vat += data["vat"]
        result.total_transactions += data["count"]

    result.total_base = round(result.total_base, 2)
    result.total_vat = round(result.total_vat, 2)

    # --- Audit trail ---
    audit = []
    for r in result.rows:
        audit.append(_a(
            f"country_{r.country}_base",
            f"OSS base — {r.country} ({int(r.vat_rate * 100)}%)",
            "SUM(vat_base_eur) WHERE vat_treatment='OSS_EU' AND country=CC",
            r.base_eur,
            country=r.country, vat_rate=r.vat_rate, transactions=r.transactions,
        ))
        audit.append(_a(
            f"country_{r.country}_vat",
            f"OSS cuota — {r.country} ({int(r.vat_rate * 100)}%)",
            f"base_eur × {r.vat_rate}  [tasa país destino — OSS Reglamento (UE) 904/2010]",
            r.vat_amount_eur,
            country=r.country, base_eur=r.base_eur, vat_rate=r.vat_rate,
        ))
    audit.append(_a(
        "total_base",
        "Base total OSS (todos los países)",
        "SUM(base_eur) across all countries",
        result.total_base,
        countries=len(result.rows), transactions=result.total_transactions,
    ))
    audit.append(_a(
        "total_vat",
        "Cuota total OSS (todos los países)",
        "SUM(vat_amount_eur) across all countries",
        result.total_vat,
        total_base=result.total_base,
    ))
    result.audit = audit
    return result


def compute_modelo_347(year: int, db_conn: sqlite3.Connection) -> Modelo347Result:
    """Compute Modelo 347 (annual operations > €3,005.06 with Spain counterparties)."""
    result = Modelo347Result(year=year)

    # Stripe transactions from Spanish counterparties
    rows = db_conn.execute(
        """SELECT id, email_meta, buyer_vat_id, converted_amount, converted_amount_refunded,
                  geo_region, strftime('%m', created_date) as month
           FROM transactions
           WHERE strftime('%Y', created_date) = ?
             AND geo_region = 'SPAIN'
             AND activity_type IS NOT NULL AND activity_type != 'UNKNOWN'
           ORDER BY created_date""",
        (str(year),),
    ).fetchall()
    rows = apply_frozen_amounts([dict(r) for r in rows], db_conn)

    # Income invoices issued to Spanish clients.
    # `iva_amount` is selected alongside the base because Modelo 347 reports the
    # importe *IVA incluido* — see the gross-basis note on the accumulation loop.
    inv_rows = db_conn.execute(
        """SELECT COALESCE(client_name, client_nif, 'UNKNOWN') AS counterparty,
                  client_nif,
                  subtotal_eur,
                  iva_amount,
                  strftime('%m', invoice_date) AS month
           FROM invoices
           WHERE direction = 'out'
             AND geo_region = 'SPAIN'
             AND COALESCE(excluded, 0) = 0
             AND invoice_date >= ?
             AND invoice_date <= ?
             AND subtotal_eur IS NOT NULL""",
        (f"{year}-01-01", f"{year}-12-31"),
    ).fetchall()

    # Both loops key by a normalised identity — NIF/VAT-ID first (the actual tax
    # ID AEAT uses to identify a counterparty), falling back to a
    # source-appropriate secondary identifier (email for Stripe, name for
    # invoices) only when no NIF is on record. Mirrors compute_modelo_349's
    # buyer_vat_id / client_nif keying so a counterparty known by the same NIF
    # on both sides (a Stripe payment plus a manually-issued invoice)
    # aggregates into one row instead of two separate below-threshold ones.
    # Both loops must accumulate on the SAME basis, or the single threshold
    # below compares a mixture of two. Modelo 347 declares the importe total de
    # las operaciones **IVA incluido** (Art. 33 RD 1065/2007), so ``gross`` is
    # VAT-inclusive on both sides: Stripe rows are already VAT-inclusive (see
    # README, "VAT-inclusive pricing"), invoices contribute base + cuota.
    # Deliberately NOT ``total_eur`` — that column is subtotal + IVA − IRPF
    # (``src/invoice_ocr.py``), and the IRPF retención is a withholding on
    # payment, not a reduction of the operation's amount.
    by_counterparty: dict[str, dict] = {}
    for row in rows:
        nif = row["buyer_vat_id"] or ""
        email = row["email_meta"] or "UNKNOWN"
        key = nif or email
        gross = row["converted_amount"] - row["converted_amount_refunded"]
        month = int(row["month"])
        q = (month - 1) // 3 + 1
        if key not in by_counterparty:
            by_counterparty[key] = {"total": 0.0, "quarters": defaultdict(float), "nif": nif, "name": email}
        by_counterparty[key]["total"] += gross
        by_counterparty[key]["quarters"][q] += gross
        if not by_counterparty[key]["nif"] and nif:
            by_counterparty[key]["nif"] = nif

    for row in inv_rows:
        nif = row["client_nif"] or ""
        name = row["counterparty"]
        key = nif or name
        gross = (row["subtotal_eur"] or 0.0) + (row["iva_amount"] or 0.0)
        month_str = row["month"]
        if not month_str:
            continue
        month = int(month_str)
        q = (month - 1) // 3 + 1
        if key not in by_counterparty:
            by_counterparty[key] = {"total": 0.0, "quarters": defaultdict(float), "nif": nif, "name": name}
        by_counterparty[key]["total"] += gross
        by_counterparty[key]["quarters"][q] += gross
        if not by_counterparty[key]["nif"] and nif:
            by_counterparty[key]["nif"] = nif
        if by_counterparty[key]["name"] in ("", "UNKNOWN") and name not in ("", "UNKNOWN"):
            by_counterparty[key]["name"] = name

    total_counterparties = len(by_counterparty)
    below_threshold = 0
    for key, info in by_counterparty.items():
        total = round(info["total"], 2)
        if total > result.threshold:  # art. 33.1 RD 1065/2007: "hayan superado"
            result.rows.append(Modelo347Row(
                counterparty_name=info["name"] or key,
                counterparty_nif=info.get("nif", ""),
                total_operations=total,
                quarter_breakdown={q: round(v, 2) for q, v in info["quarters"].items()},
            ))
        else:
            below_threshold += 1

    result.rows.sort(key=lambda r: r.total_operations, reverse=True)

    # --- Audit trail ---
    _a = partial(AuditEntry.of, "347", year, 0)
    audit = []
    for r in result.rows:
        identity = r.counterparty_nif or r.counterparty_name
        audit.append(_a(
            f"counterparty_{r.counterparty_name[:30]}",
            f"Operaciones con {r.counterparty_name}",
            f"SUM(importe IVA incluido) WHERE geo_region='SPAIN' AND "
            f"counterparty(nif||email/name)='{identity}' — basis: transactions "
            f"(converted_amount − converted_amount_refunded) + invoices "
            f"(subtotal_eur + iva_amount) — threshold > €{result.threshold:,.2f}",
            r.total_operations,
            counterparty=r.counterparty_name,
            counterparty_nif=r.counterparty_nif,
            quarter_breakdown=r.quarter_breakdown,
        ))
    audit.append(_a(
        "summary",
        "Resumen Modelo 347",
        f"Counterparties > €{result.threshold:,.2f} threshold",
        float(len(result.rows)),
        total_counterparties_spain=total_counterparties,
        above_threshold=len(result.rows),
        below_threshold=below_threshold,
        threshold_eur=result.threshold,
    ))
    result.audit = audit
    return result


def get_tax_calendar(year: int, db_conn: Optional[sqlite3.Connection] = None) -> list[TaxDeadline]:
    """Return all quarterly and annual tax deadlines for the year with their status."""
    today = date.today()
    deadlines: list[TaxDeadline] = []

    model_names = {
        "303": "Declaración IVA Trimestral",
        "130": "Pago Fraccionado IRPF",
        "349": "Operaciones Intracomunitarias",
        "OSS": "One Stop Shop (IVA digital services)",
        "390": "Resumen Anual IVA",
        "347": "Operaciones con Terceros",
    }

    # Fetch filed statuses from DB if connection provided
    filed_lookup: dict[str, dict] = {}
    if db_conn:
        rows = db_conn.execute(
            "SELECT model, quarter, status, amount_eur FROM tax_filing_status WHERE year = ?",
            (year,),
        ).fetchall()
        for r in rows:
            key = f"{r['model']}_{r['quarter'] or 'annual'}"
            filed_lookup[key] = dict(r)

    def _status(ddl: date, key: str) -> str:
        rec = filed_lookup.get(key, {})
        if rec.get("status") == "FILED":
            return "FILED"
        if ddl < today:
            return "OVERDUE"
        if (ddl - today).days <= _DUE_SOON_DAYS:
            return "DUE"
        return "PENDING"

    # Quarterly models
    for model in ("303", "130", "349", "OSS"):
        for q in range(1, 5):
            ddl = _tax_deadline_date(model, year, q)
            key = f"{model}_{q}"
            rec = filed_lookup.get(key, {})
            deadlines.append(TaxDeadline(
                model=model,  # type: ignore[arg-type]
                name=model_names[model],
                year=year,
                quarter=q,
                deadline=ddl,
                status=_status(ddl, key),  # type: ignore[arg-type]
                amount_eur=rec.get("amount_eur"),
            ))

    # Annual models
    for model in ("390", "347"):
        ddl = _tax_deadline_date(model, year, 1)
        key = f"{model}_annual"
        rec = filed_lookup.get(key, {})
        deadlines.append(TaxDeadline(
            model=model,  # type: ignore[arg-type]
            name=model_names[model],
            year=year,
            quarter=None,
            deadline=ddl,
            status=_status(ddl, key),  # type: ignore[arg-type]
            amount_eur=rec.get("amount_eur"),
        ))

    deadlines.sort(key=lambda d: d.deadline)
    return deadlines


def compute_and_persist_tax_snapshots(
    year: int, quarter: int, db_conn: sqlite3.Connection, config: Optional[dict] = None
) -> str:
    """Run all obligation engines for the selected period and persist JSON snapshots.

    Quarterly models (303, 130, OSS, 349) use ``quarter``; Modelo 347 is annual and is
    stored with ``quarter`` = ``TAX_SNAPSHOT_QUARTER_ANNUAL`` (0).

    The app config is loaded once here and threaded through every engine so the
    ``config.tax`` settings (EU VAT overrides, IVA/OSS registration, prorrata,
    regime) drive the computation. Callers may pass an explicit ``config`` dict.

    Returns the shared ISO ``computed_at`` timestamp written on every snapshot row.
    """
    from src.database import (
        TAX_SNAPSHOT_QUARTER_ANNUAL,
        upsert_audit_entries_conn,
        upsert_tax_snapshot_conn,
    )
    from src.tax_snapshot_codec import encode_snapshot

    if config is None:
        config = load_app_config()

    computed_at = datetime.now().isoformat(timespec="seconds")

    r303 = compute_modelo_303(year, quarter, db_conn, config)
    upsert_tax_snapshot_conn(db_conn, year, quarter, "303", encode_snapshot("303", r303), computed_at)
    upsert_audit_entries_conn(db_conn, r303.audit, computed_at)

    r130 = compute_modelo_130(year, quarter, db_conn, config)
    upsert_tax_snapshot_conn(db_conn, year, quarter, "130", encode_snapshot("130", r130), computed_at)
    upsert_audit_entries_conn(db_conn, r130.audit, computed_at)

    r_oss = compute_oss_return(year, quarter, db_conn, config)
    upsert_tax_snapshot_conn(db_conn, year, quarter, "OSS", encode_snapshot("OSS", r_oss), computed_at)
    upsert_audit_entries_conn(db_conn, r_oss.audit, computed_at)

    r349 = compute_modelo_349(year, quarter, db_conn, config)
    upsert_tax_snapshot_conn(db_conn, year, quarter, "349", encode_snapshot("349", r349), computed_at)
    upsert_audit_entries_conn(db_conn, r349.audit, computed_at)

    r347 = compute_modelo_347(year, db_conn)
    upsert_tax_snapshot_conn(
        db_conn, year, TAX_SNAPSHOT_QUARTER_ANNUAL, "347",
        encode_snapshot("347", r347), computed_at,
    )
    upsert_audit_entries_conn(db_conn, r347.audit, computed_at)

    db_conn.commit()
    return computed_at


def compute_eu_b2c_threshold(
    year: int, quarter: int, db_conn: sqlite3.Connection, config: Optional[dict] = None
) -> EUB2CThresholdResult:
    """Track year-to-date EU B2C distance sales against the €10,000 art. 73 LIVA limit.

    Counts Stripe rows whose derived VAT treatment is EU B2C (``EU_B2C_ES21`` or
    ``OSS_EU``) from 1 January through the end of ``quarter``, on their ex-VAT
    base, plus the full previous year (exceeding the limit in either year ends
    the Spanish-VAT option). Declared-report amounts win over live ones, like
    every other engine figure.
    """
    result = EUB2CThresholdResult(
        year=year, quarter=quarter,
        limit_eur=EU_B2C_THRESHOLD_EUR, warn_ratio=EU_B2C_WARN_RATIO,
    )
    by_country: dict[str, float] = defaultdict(float)
    ytd = 0.0
    for row in _load_classified_ytd(year, quarter, db_conn):
        if _get_vat_treatment(row, config) not in EU_B2C_TREATMENTS:
            continue
        base = _get_vat_base(row, config)
        ytd += base
        by_country[(row.get("card_country") or "UNKNOWN").upper()] += base
        result.n_transactions += 1
    prev = sum(
        _get_vat_base(r, config)
        for r in _load_classified_ytd(year - 1, 4, db_conn)
        if _get_vat_treatment(r, config) in EU_B2C_TREATMENTS
    )
    result.ytd_base_eur = round(ytd, 2)
    result.previous_year_base_eur = round(prev, 2)
    result.by_country = {k: round(v, 2) for k, v in sorted(by_country.items())}
    if result.status != "OK":
        log.warning("⚠️ %s", result.message)
    return result
