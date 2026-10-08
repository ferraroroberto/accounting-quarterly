"""Tax computation engine for Spanish autónomo obligations."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import partial
from typing import Optional

from src.logger import get_logger
from src.periods import quarter_iso_bounds
from src.tax_deadlines import calendar_deadline
from src.tax_data import (
    activity_start_note,
    clamp_start,
    get_tax_entries_total,
    get_vat_amount,
    get_vat_base,
    get_vat_treatment,
    income_invoice_eur,
    invoice_tax_treatment,
    load_classified_for_quarter,
    load_classified_ytd,
    load_expense_invoices_ytd,
    load_income_invoices_for_quarter,
    load_income_invoices_ytd,
    net_amount,
    tax_settings,
    load_app_config,
)
from src.tax_models import (
    MODELO130_BOX_FIELDS,
    AuditEntry,
    EUB2CThresholdResult,
    Modelo130Result,
    Modelo347Result,
    Modelo347Row,
    Modelo349Result,
    Modelo349Row,
    OSSCountryRow,
    OSSReturnResult,
    TaxDeadline,
)
from src.declared_reports import apply_frozen_amounts
from src.modelo_303 import compute_modelo_303, prorrata_pct  # noqa: F401  (re-exported, #174)
from src.fixed_assets import DepreciationResult, capital_asset_invoice_ids, depreciation_for_period
from src.vat_rules import (
    EU_B2C_THRESHOLD_EUR,
    EU_B2C_TREATMENTS,
    EU_B2C_WARN_RATIO,
    is_oss_registered,
    oss_rate,
)

log = get_logger(__name__)

_DUE_SOON_DAYS = 15


# ---------------------------------------------------------------------------
# Public computation functions
# ---------------------------------------------------------------------------

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
    platform_fees: float = 0.0
    fee_split_unknown: int = 0
    manual_gastos: float = 0.0
    inv_retenciones: float = 0.0
    manual_retenciones: float = 0.0
    capital_excluded: float = 0.0
    unregistered_capital_ids: list = field(default_factory=list)
    depreciation: Optional[DepreciationResult] = None
    records: dict = field(default_factory=dict)
    activity_start_note: Optional[str] = None

    @property
    def c01(self) -> float:
        return round(self.stripe_income + self.inv_income + self.exch_diff_total, 2)

    @property
    def gastos_reales(self) -> float:
        return round(self.inv_gastos + self.ss_gastos + self.platform_fees + self.depreciation.total_eur
                     + self.manual_gastos, 2)

    @property
    def c06(self) -> float:
        return round(self.inv_retenciones + self.manual_retenciones, 2)


def _collect_130_ytd(year: int, quarter: int, conn: sqlite3.Connection,
                     config: Optional[dict]) -> _Collected130:
    """Gather income, real expenses and withholdings from 1 January to the quarter end."""
    col = _Collected130()
    _, ytd_end = quarter_iso_bounds(year, quarter)
    ytd_start = f"{year}-01-01"

    # 01 — Stripe VAT bases (frozen declared amounts win, see _load_classified_range).
    rows = load_classified_ytd(year, quarter, conn, config)
    col.stripe_income = sum(get_vat_base(r, config) for r in rows)
    # 01 — issued invoices, gross of the IRPF withheld (D12); eur_received wins (#93).
    income_invs = load_income_invoices_ytd(year, quarter, conn, config)
    col.inv_income = sum(income_invoice_eur(inv) for inv in income_invs)
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
    expense_invs = load_expense_invoices_ytd(year, quarter, conn, config)
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
    # 02 — application fees a connected platform kept from the same Stripe charges
    # as box 01 (#135). Stripe's own fee is already expensed from its invoices, so
    # only the application part counts; it follows the charge's balance transaction
    # (a fee the platform did not return on a refund stays an expense). Rows with
    # an unknown split (fetched before the split was stored) are counted, not guessed.
    fee_rows = [r for r in rows if r.get("fee_application")]
    col.platform_fees = round(sum(float(r["fee_application"]) for r in fee_rows), 2)
    col.fee_split_unknown = sum(1 for r in rows if r.get("fee_application") is None)
    if col.fee_split_unknown:
        log.warning("⚠️ 130 %dQ%d: fee split unknown for %d Stripe charge(s) — their platform fees are "
                    "not in box 02; re-fetch with `stripe-fetch --backfill-fee-split`",
                    year, quarter, col.fee_split_unknown)
    col.manual_gastos = get_tax_entries_total(year, quarter, "GASTOS_DEDUCIBLES", conn, ytd=True)

    # 06 — IRPF withheld by clients on issued invoices (exact cents) + manual entries.
    col.inv_retenciones = sum((inv.get("irpf_amount") or 0.0) for inv in income_invs)
    col.manual_retenciones = get_tax_entries_total(year, quarter, "RETENCIONES_SOPORTADAS", conn, ytd=True)

    # Audit note: records dated before tax.activity_start_date that the YTD loaders
    # above left out of boxes 01/02 (issue #133).
    col.activity_start_note = activity_start_note(
        conn, config, ytd_start, {"stripe": ytd_end, "income": ytd_end, "expense": ytd_end}, "130")

    # --- Audit records ------------------------------------------------------
    stripe_agg: dict[tuple, dict] = {}
    for r in rows:
        key = (r.get("geo_region") or "UNKNOWN", r.get("activity_type") or "UNKNOWN",
               get_vat_treatment(r, config))
        agg = stripe_agg.setdefault(key, {"n": 0, "gross_eur": 0.0, "base_eur": 0.0})
        agg["n"] += 1
        agg["gross_eur"] = round(agg["gross_eur"] + net_amount(r), 2)
        agg["base_eur"] = round(agg["base_eur"] + get_vat_base(r, config), 2)

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
             "eur_used": round(income_invoice_eur(inv), 2),
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
        "platform_fees": [
            {"source": "stripe_platform_fee", "date": str(r.get("created_date") or "")[:10],
             "charge_id": r["id"], "fee_application": round(float(r["fee_application"]), 2),
             "refunded_eur": round(r.get("converted_amount_refunded") or 0.0, 2)}
            for r in fee_rows
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
    regime = tax_settings(config).get("regime", "estimacion_directa_simplificada")
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
    configured = tax_settings(config).get("previous_year_net_yield")
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
    if col.fee_split_unknown:
        notes.append(f"Fee split unknown for {col.fee_split_unknown} Stripe charge(s); re-fetch "
                     "(stripe-fetch --backfill-fee-split) — their platform fees are not in box 02.")
    if col.unregistered_capital_ids:
        notes.append(f"{len(col.unregistered_capital_ids)} capital-asset invoice(s) have no fixed asset "
                     "registered: neither expensed nor depreciated.")
    if col.activity_start_note:
        notes.append(col.activity_start_note)
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
           "02 · Gastos reales YTD — facturas recibidas + cuotas RETA + comisiones de plataforma + "
           "amortizaciones + entradas manuales",
           "SUM(subtotal_eur × deductible_pct_irpf/100) FROM invoices WHERE direction='in' AND excluded=0 "
           "AND not a capital asset YTD + SUM(amount_eur) FROM social_security_payments YTD (refunds negative) "
           "+ SUM(fee_application) FROM transactions YTD "
           "+ amortizaciones YTD + SUM(amount_eur) FROM quarterly_tax_entries WHERE entry_type='GASTOS_DEDUCIBLES'",
           r.gastos_reales, inv_gastos=round(col.inv_gastos, 2), ss_gastos=col.ss_gastos,
           platform_fees=col.platform_fees, amortizaciones=dep.total_eur, manual_gastos=round(col.manual_gastos, 2),
           ss_records=rec["social_security"], records=rec["expenses"]),
        _a("c02_platform_fees",
           "02 · Comisiones de plataforma YTD (application fees retenidas de cargos Stripe)",
           "SUM(fee_application) FROM transactions YTD (same classified charges as 01, by charge date); "
           "Stripe's own fee is expensed from the Stripe invoices; split unknown → counted, not guessed",
           col.platform_fees, fee_split_unknown=col.fee_split_unknown, records=rec["platform_fees"]),
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
    rows = load_classified_for_quarter(year, quarter, db_conn, config)

    by_country: dict[str, dict] = defaultdict(lambda: {"count": 0, "base": 0.0, "vat": 0.0})
    for row in rows:
        treatment = get_vat_treatment(row, config)
        if treatment != "OSS_EU":
            continue
        cc = (row.get("oss_country") or row.get("card_country") or "UNKNOWN").upper()
        base = get_vat_base(row, config)
        vat = get_vat_amount(row, config)
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


def compute_modelo_347(
    year: int, db_conn: sqlite3.Connection, config: Optional[dict] = None
) -> Modelo347Result:
    """Compute Modelo 347 (annual operations > €3,005.06 with Spain counterparties).

    ``config``'s ``tax.activity_start_date`` (issue #133), when set and later
    than 1 January, excludes transactions and invoices dated before it.
    """
    result = Modelo347Result(year=year)
    start = clamp_start(f"{year}-01-01", config)

    # Stripe transactions from Spanish counterparties
    rows = db_conn.execute(
        """SELECT id, email_meta, buyer_vat_id, converted_amount, converted_amount_refunded,
                  geo_region, strftime('%m', created_date) as month
           FROM transactions
           WHERE strftime('%Y', created_date) = ?
             AND created_date >= ?
             AND geo_region = 'SPAIN'
             AND activity_type IS NOT NULL AND activity_type != 'UNKNOWN'
           ORDER BY created_date""",
        (str(year), start),
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
        (start, f"{year}-12-31"),
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
            ddl = calendar_deadline(model, year, q)
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
        ddl = calendar_deadline(model, year)
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

    r347 = compute_modelo_347(year, db_conn, config)
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
    for row in load_classified_ytd(year, quarter, db_conn, config):
        if get_vat_treatment(row, config) not in EU_B2C_TREATMENTS:
            continue
        base = get_vat_base(row, config)
        ytd += base
        by_country[(row.get("card_country") or "UNKNOWN").upper()] += base
        result.n_transactions += 1
    prev = sum(
        get_vat_base(r, config)
        for r in load_classified_ytd(year - 1, 4, db_conn, config)
        if get_vat_treatment(r, config) in EU_B2C_TREATMENTS
    )
    result.ytd_base_eur = round(ytd, 2)
    result.previous_year_base_eur = round(prev, 2)
    result.by_country = {k: round(v, 2) for k, v in sorted(by_country.items())}
    if result.status != "OK":
        log.warning("⚠️ %s", result.message)
    return result
