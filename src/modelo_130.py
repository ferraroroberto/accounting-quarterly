"""Modelo 130 (quarterly IRPF instalment): AEAT box model, box 13 reduction and negative carry.

Moved verbatim out of ``src.tax_engine`` (#174). Reads its records through ``src.tax_data``;
``compute_modelo_130`` is the public entry point.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from functools import partial
from typing import Optional

from src.fixed_assets import DepreciationResult, capital_asset_invoice_ids, depreciation_for_period
from src.logger import get_logger
from src.periods import quarter_iso_bounds
from src.tax_data import (
    activity_start_note,
    get_tax_entries_total,
    get_vat_base,
    get_vat_treatment,
    income_invoice_eur,
    load_classified_ytd,
    load_expense_invoices_ytd,
    load_income_invoices_ytd,
    net_amount,
    tax_settings,
)
from src.tax_models import AuditEntry, MODELO130_BOX_FIELDS, Modelo130Result

log = get_logger(__name__)

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

