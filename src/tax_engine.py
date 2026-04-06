"""Tax computation engine for Spanish autónomo obligations."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from typing import Optional

from src.logger import get_logger
from src.models import ClassifiedPayment
from src.tax_models import (
    OSS_RATES,
    AuditEntry,
    Modelo130Result,
    Modelo303Result,
    Modelo347Result,
    Modelo347Row,
    Modelo349Result,
    Modelo349Row,
    OSSCountryRow,
    OSSReturnResult,
    TaxDeadline,
    VATTreatment,
    _tax_deadline_date,
)

log = get_logger(__name__)

_DUE_SOON_DAYS = 15


# ---------------------------------------------------------------------------
# VAT treatment helpers
# ---------------------------------------------------------------------------

def compute_vat_treatment(payment: ClassifiedPayment, config: dict) -> VATTreatment:
    """Compute VAT treatment for a single payment."""
    from src.classifier import classify_vat
    updated = classify_vat(payment, config)
    return VATTreatment(
        treatment=updated.vat_treatment or "UNKNOWN",
        vat_base_eur=updated.vat_base_eur or 0.0,
        vat_amount_eur=updated.vat_amount_eur or 0.0,
        oss_country=updated.oss_country,
    )


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

def _load_classified_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Load classified transactions for a specific quarter from an open connection."""
    month_start = (quarter - 1) * 3 + 1
    month_end = quarter * 3
    start = f"{year}-{month_start:02d}-01"
    # End of last month of quarter
    import calendar
    last_day = calendar.monthrange(year, month_end)[1]
    end = f"{year}-{month_end:02d}-{last_day:02d}T23:59:59"
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
    return [dict(r) for r in rows]


def _load_classified_ytd(year: int, quarter: int, conn: sqlite3.Connection) -> list[dict]:
    """Load classified transactions from Q1 through the given quarter."""
    month_end = quarter * 3
    import calendar
    last_day = calendar.monthrange(year, month_end)[1]
    end = f"{year}-{month_end:02d}-{last_day:02d}T23:59:59"
    rows = conn.execute(
        """SELECT id, created_date, converted_amount, converted_amount_refunded,
                  activity_type, geo_region, card_country, email_meta,
                  vat_treatment, vat_base_eur, vat_amount_eur, oss_country, buyer_vat_id
           FROM transactions
           WHERE created_date >= ? AND created_date <= ?
             AND activity_type IS NOT NULL AND activity_type != 'UNKNOWN'
           ORDER BY created_date""",
        (f"{year}-01-01", end),
    ).fetchall()
    return [dict(r) for r in rows]


def _invoice_date_range(year: int, quarter: int) -> tuple[str, str]:
    import calendar
    month_start = (quarter - 1) * 3 + 1
    month_end = quarter * 3
    last_day = calendar.monthrange(year, month_end)[1]
    return (
        f"{year}-{month_start:02d}-01",
        f"{year}-{month_end:02d}-{last_day:02d}",
    )


def _load_expense_invoices_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Expense invoices (direction='in') for the quarter, keyed by supply_date || invoice_date."""
    start, end = _invoice_date_range(year, quarter)
    rows = conn.execute(
        """SELECT id, COALESCE(supply_date, invoice_date) AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment,
                  COALESCE(deductible_pct, 100.0) AS deductible_pct,
                  vendor_nif, vendor_name, description
           FROM invoices
           WHERE direction = 'in'
             AND COALESCE(supply_date, invoice_date) >= ?
             AND COALESCE(supply_date, invoice_date) <= ?
           ORDER BY tx_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_expense_invoices_ytd(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Expense invoices (direction='in') from Q1 through the given quarter (YTD)."""
    import calendar
    month_end = quarter * 3
    last_day = calendar.monthrange(year, month_end)[1]
    rows = conn.execute(
        """SELECT id, COALESCE(supply_date, invoice_date) AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment,
                  COALESCE(deductible_pct, 100.0) AS deductible_pct,
                  vendor_nif, vendor_name, description
           FROM invoices
           WHERE direction = 'in'
             AND COALESCE(supply_date, invoice_date) >= ?
             AND COALESCE(supply_date, invoice_date) <= ?
           ORDER BY tx_date""",
        (f"{year}-01-01", f"{year}-{month_end:02d}-{last_day:02d}"),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_income_invoices_ytd(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Income invoices (direction='out') from Q1 through the given quarter (YTD).

    These are manually-issued invoices (bank transfer, etc.) NOT processed through
    Stripe — Stripe income already lives in the ``transactions`` table.
    """
    import calendar
    month_end = quarter * 3
    last_day = calendar.monthrange(year, month_end)[1]
    rows = conn.execute(
        """SELECT id, COALESCE(supply_date, invoice_date) AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment,
                  client_nif, client_name, description
           FROM invoices
           WHERE direction = 'out'
             AND COALESCE(supply_date, invoice_date) >= ?
             AND COALESCE(supply_date, invoice_date) <= ?
           ORDER BY tx_date""",
        (f"{year}-01-01", f"{year}-{month_end:02d}-{last_day:02d}"),
    ).fetchall()
    return [dict(r) for r in rows]


def _load_income_invoices_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection
) -> list[dict]:
    """Income invoices (direction='out') for the quarter only."""
    start, end = _invoice_date_range(year, quarter)
    rows = conn.execute(
        """SELECT id, COALESCE(supply_date, invoice_date) AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment,
                  client_nif, client_name, description
           FROM invoices
           WHERE direction = 'out'
             AND COALESCE(supply_date, invoice_date) >= ?
             AND COALESCE(supply_date, invoice_date) <= ?
           ORDER BY tx_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def _net_amount(row: dict) -> float:
    return round(row["converted_amount"] - row["converted_amount_refunded"], 2)


def _get_vat_treatment(row: dict) -> str:
    """Return stored vat_treatment, or derive it on-the-fly if missing."""
    stored = row.get("vat_treatment")
    if stored and stored != "UNKNOWN":
        return stored
    # Derive from activity × geo (fallback for rows not yet VAT-classified)
    geo = row.get("geo_region") or "UNKNOWN"
    activity = row.get("activity_type") or "UNKNOWN"
    if geo == "OUTSIDE_EU":
        return "IVA_EXPORT"
    if geo == "SPAIN":
        return "IVA_ES_21"
    if geo == "EU_NOT_SPAIN":
        if activity == "NEWSLETTER":
            return "OSS_EU"
        return "IVA_EU_B2B"
    return "UNKNOWN"


def _get_vat_base(row: dict) -> float:
    """Return the ex-VAT taxable base for a transaction row.

    Stripe amounts are VAT-inclusive (the customer paid the gross amount).
    For Spain (IVA_ES_21) and EU B2C (OSS_EU) we extract the base by
    dividing by (1 + rate).  For exports and EU B2B (ISP) the full net
    amount is the income base — no VAT was charged.
    """
    if row.get("vat_base_eur") is not None:
        return row["vat_base_eur"]
    net = _net_amount(row)
    treatment = _get_vat_treatment(row)
    if treatment == "IVA_ES_21":
        return round(net / 1.21, 2)
    if treatment == "OSS_EU":
        cc = (row.get("oss_country") or row.get("card_country") or "").upper()
        rate = OSS_RATES.get(cc, OSS_RATES["DEFAULT_EU"])
        return round(net / (1 + rate), 2)
    # IVA_EXPORT, IVA_EU_B2B, EXEMPT, UNKNOWN — full net amount is income base
    return net


def _get_vat_amount(row: dict) -> float:
    if row.get("vat_amount_eur") is not None:
        return row["vat_amount_eur"]
    treatment = _get_vat_treatment(row)
    base = _get_vat_base(row)
    if treatment == "IVA_ES_21":
        return round(base * 0.21, 2)
    if treatment == "OSS_EU":
        cc = (row.get("oss_country") or row.get("card_country") or "").upper()
        rate = OSS_RATES.get(cc, OSS_RATES["DEFAULT_EU"])
        return round(base * rate, 2)
    return 0.0


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


def _previous_modelo130_payments(year: int, quarter: int, conn: sqlite3.Connection) -> float:
    """Sum of Box 16 amounts paid in Modelo 130 for earlier quarters of the same year."""
    rows = conn.execute(
        """SELECT COALESCE(SUM(amount_eur), 0) AS total
           FROM tax_filing_status
           WHERE year = ? AND model = '130' AND quarter < ? AND status IN ('FILED', 'COMPUTED')""",
        (year, quarter),
    ).fetchone()
    return float(rows["total"]) if rows and rows["total"] else 0.0


# ---------------------------------------------------------------------------
# Public computation functions
# ---------------------------------------------------------------------------

def compute_modelo_303(year: int, quarter: int, db_conn: sqlite3.Connection) -> Modelo303Result:
    """Compute Modelo 303 (quarterly VAT return) for the given quarter."""
    import json as _json
    result = Modelo303Result(year=year, quarter=quarter)
    rows = _load_classified_for_quarter(year, quarter, db_conn)

    # Aggregate Stripe transactions by (geo_region, activity_type, treatment).
    # The gestor works with the quarterly summary (one line per geo/activity),
    # not individual transaction rows.
    # Key: (geo_region, activity_type, treatment, oss_country_or_empty)
    _agg: dict[tuple, dict] = {}

    for row in rows:
        treatment = _get_vat_treatment(row)
        base = _get_vat_base(row)   # ex-VAT base (VAT-inclusive extraction applied)
        vat = _get_vat_amount(row)
        geo = row.get("geo_region") or "UNKNOWN"
        act = row.get("activity_type") or "UNKNOWN"
        oss_cc = (row.get("oss_country") or row.get("card_country") or "").upper() if treatment == "OSS_EU" else ""
        key = (geo, act, treatment, oss_cc)
        if key not in _agg:
            _agg[key] = {"n": 0, "gross_eur": 0.0, "base_eur": 0.0, "vat_eur": 0.0}
        _agg[key]["n"] += 1
        _agg[key]["gross_eur"] = round(_agg[key]["gross_eur"] + _net_amount(row), 2)
        _agg[key]["base_eur"] = round(_agg[key]["base_eur"] + base, 2)
        _agg[key]["vat_eur"] = round(_agg[key]["vat_eur"] + vat, 2)

        if treatment == "IVA_ES_21":
            result.box_01_base += base
            result.box_03_cuota += vat
        elif treatment == "IVA_EU_B2B":
            result.box_59_intracom_entregas += base
        elif treatment == "OSS_EU":
            result.oss_base += base
            result.oss_vat += vat
        elif treatment == "IVA_EXPORT":
            result.export_base += base

    # Build aggregated audit records (one entry per geo/activity bucket)
    def _agg_rec(k: tuple, v: dict) -> dict:
        geo, act, treatment, oss_cc = k
        rec = {
            "source": "stripe_aggregado",
            "geo_region": geo,
            "activity": act,
            "vat_treatment": treatment,
            "n_transactions": v["n"],
            "gross_eur": v["gross_eur"],
            "base_eur": v["base_eur"],
            "vat_eur": v["vat_eur"],
        }
        if oss_cc:
            rec["oss_country"] = oss_cc
        return rec

    recs_es21  = [_agg_rec(k, v) for k, v in _agg.items() if k[2] == "IVA_ES_21"]
    recs_eu_b2b = [_agg_rec(k, v) for k, v in _agg.items() if k[2] == "IVA_EU_B2B"]
    recs_oss   = [_agg_rec(k, v) for k, v in _agg.items() if k[2] == "OSS_EU"]
    recs_export = [_agg_rec(k, v) for k, v in _agg.items() if k[2] == "IVA_EXPORT"]
    n_es21, n_eu_b2b, n_oss, n_export = len(recs_es21), len(recs_eu_b2b), len(recs_oss), len(recs_export)

    # Deductible IVA: sum from expense invoices for the quarter
    expense_invs = _load_expense_invoices_for_quarter(year, quarter, db_conn)
    inv_iva_soportado = 0.0
    inv_base_soportado = 0.0
    n_expense_inv = 0
    recs_soportado: list[dict] = []
    for inv in expense_invs:
        iva = inv.get("iva_amount") or 0.0
        base = inv.get("subtotal_eur") or 0.0
        ded_pct = (inv.get("deductible_pct") or 100.0) / 100.0
        _rec = {
            "source": "invoice_in",
            "date": str(inv.get("tx_date", ""))[:10],
            "vendor": str(inv.get("vendor_name") or inv.get("vendor_nif") or "")[:40],
            "description": str(inv.get("description") or "")[:50],
            "subtotal_eur": round(base, 2),
            "iva_amount": round(iva, 2),
            "deductible_pct": inv.get("deductible_pct") or 100.0,
            "iva_deductible": round(iva * ded_pct, 2),
            "geo_region": inv.get("geo_region") or "",
        }
        recs_soportado.append(_rec)
        if iva > 0:
            inv_iva_soportado += iva * ded_pct
            inv_base_soportado += base * ded_pct
            n_expense_inv += 1

    # Also include income (outgoing) invoices into devengado boxes
    income_invs_q = _load_income_invoices_for_quarter(year, quarter, db_conn)
    n_inv_es21 = n_inv_eu_b2b = n_inv_export = 0
    for inv in income_invs_q:
        treatment = inv.get("vat_treatment") or "IVA_EXEMPT"
        base = inv.get("subtotal_eur") or 0.0
        vat = inv.get("iva_amount") or 0.0
        _rec = {
            "source": "invoice_out",
            "date": str(inv.get("tx_date", ""))[:10],
            "client": str(inv.get("client_name") or inv.get("client_nif") or "")[:40],
            "description": str(inv.get("description") or "")[:50],
            "base_eur": round(base, 2),
            "vat_eur": round(vat, 2),
            "vat_treatment": treatment,
        }
        if treatment == "IVA_ES_21":
            result.box_01_base += base
            result.box_03_cuota += vat
            n_inv_es21 += 1
            recs_es21.append(_rec)
        elif treatment == "IVA_EU_B2B":
            result.box_59_intracom_entregas += base
            n_inv_eu_b2b += 1
            recs_eu_b2b.append(_rec)
        elif treatment == "IVA_EXPORT":
            result.export_base += base
            n_inv_export += 1
            recs_export.append(_rec)

    # Deductible IVA from manual entries (current quarter only) + invoices
    result.box_28_iva_soportado = round(
        inv_iva_soportado
        + _get_tax_entries_total(year, quarter, "IVA_SOPORTADO", db_conn, ytd=False),
        2,
    )
    result.box_29_base_soportado = round(
        inv_base_soportado
        + (result.box_28_iva_soportado - inv_iva_soportado) / 0.21
        if (result.box_28_iva_soportado - inv_iva_soportado) > 0
        else inv_base_soportado,
        2,
    )

    # Round accumulations
    result.box_01_base = round(result.box_01_base, 2)
    result.box_03_cuota = round(result.box_03_cuota, 2)
    result.box_59_intracom_entregas = round(result.box_59_intracom_entregas, 2)
    result.oss_base = round(result.oss_base, 2)
    result.oss_vat = round(result.oss_vat, 2)
    result.export_base = round(result.export_base, 2)

    result.box_46_diferencia = round(result.box_03_cuota - result.box_28_iva_soportado, 2)
    result.box_48_resultado = result.box_46_diferencia  # simplified (100% proration)

    # --- Audit trail ---
    def _a(cell, label, formula, value, **inputs):
        return AuditEntry(
            model="303", year=year, quarter=quarter,
            cell=cell, label=label, formula=formula, value=value,
            inputs_json=_json.dumps(inputs),
        )
    result.audit = [
        _a("box_01_base",
           "Base imponible 21% (IVA devengado — Stripe + facturas emitidas España)",
           "SUM(vat_base_eur) WHERE vat_treatment='IVA_ES_21' (transactions + invoices out)",
           result.box_01_base,
           records=recs_es21),
        _a("box_03_cuota",
           "Cuota IVA devengado 21%",
           "SUM(vat_amount_eur) WHERE vat_treatment = 'IVA_ES_21'  [= 21% × box_01_base]",
           result.box_03_cuota,
           box_01_base=result.box_01_base, rate=0.21),
        _a("box_59_intracom_entregas",
           "Entregas intracomunitarias exentas (casilla 59)",
           "SUM(vat_base_eur) WHERE vat_treatment='IVA_EU_B2B'  [Art. 25 LIVA — ISP applies]",
           result.box_59_intracom_entregas,
           records=recs_eu_b2b),
        _a("box_28_iva_soportado",
           "IVA soportado deducible — todas las facturas recibidas del trimestre",
           "SUM(iva_amount * deductible_pct/100) FROM invoices WHERE direction='in' AND quarter=Q "
           "+ SUM(amount_eur) FROM quarterly_tax_entries WHERE entry_type='IVA_SOPORTADO' AND quarter=Q",
           result.box_28_iva_soportado,
           inv_iva_deductible=round(inv_iva_soportado, 2),
           records=recs_soportado),
        _a("box_29_base_soportado",
           "Base IVA soportado (facturas + estimación 21% para entradas manuales)",
           "SUM(subtotal_eur * deductible_pct/100) FROM invoices + manual_IVA / 0.21",
           result.box_29_base_soportado,
           inv_base_sum=round(inv_base_soportado, 2)),
        _a("box_46_diferencia",
           "Diferencia (IVA devengado − IVA deducible)",
           "box_03_cuota − box_28_iva_soportado",
           result.box_46_diferencia,
           box_03_cuota=result.box_03_cuota, box_28_iva_soportado=result.box_28_iva_soportado),
        _a("box_48_resultado",
           "Resultado a ingresar / devolver",
           "= box_46_diferencia  [100% prorrata; no prior-period compensation applied]",
           result.box_48_resultado,
           box_46_diferencia=result.box_46_diferencia),
        _a("oss_base",
           "Base OSS — servicios digitales B2C UE",
           "SUM(vat_base_eur) WHERE vat_treatment = 'OSS_EU'  [declarar por OSS, no en 303]",
           result.oss_base,
           records=recs_oss),
        _a("oss_vat",
           "Cuota OSS (tipo del país del cliente)",
           "SUM(vat_amount_eur) WHERE vat_treatment = 'OSS_EU'",
           result.oss_vat,
           oss_base=result.oss_base),
        _a("export_base",
           "Base exportaciones (exentas IVA — Art. 21 LIVA)",
           "SUM(vat_base_eur) WHERE vat_treatment = 'IVA_EXPORT'",
           result.export_base,
           records=recs_export),
    ]
    return result


def compute_modelo_130(year: int, quarter: int, db_conn: sqlite3.Connection) -> Modelo130Result:
    """Compute Modelo 130 (quarterly IRPF advance) for the given quarter."""
    import calendar as _calendar
    import json as _json
    result = Modelo130Result(year=year, quarter=quarter)
    # Stripe income (transactions table)
    rows = _load_classified_ytd(year, quarter, db_conn)
    n_stripe_rows = len(rows)
    stripe_income = sum(_get_vat_base(r) for r in rows)

    # Non-Stripe income: manually-issued invoices (direction='out')
    income_invs = _load_income_invoices_ytd(year, quarter, db_conn)
    inv_income = sum((inv.get("subtotal_eur") or 0.0) for inv in income_invs)
    n_income_invs = len(income_invs)

    result.box_01_ingresos = round(stripe_income + inv_income, 2)

    # Expenses from invoices (direction='in'), YTD
    expense_invs_ytd = _load_expense_invoices_ytd(year, quarter, db_conn)
    inv_gastos = sum(
        (inv.get("subtotal_eur") or 0.0) * (inv.get("deductible_pct") or 100.0) / 100.0
        for inv in expense_invs_ytd
    )
    n_expense_invs = len(expense_invs_ytd)

    # Social Security cuotas paid via bank account, YTD — fully deductible (Art. 30 LIRPF)
    month_end = quarter * 3
    last_day = _calendar.monthrange(year, month_end)[1]
    ss_start = f"{year}-01-01"
    ss_end = f"{year}-{month_end:02d}-{last_day:02d}"
    _ss_rows = db_conn.execute(
        """SELECT id, payment_date, amount_eur, description
           FROM social_security_payments
           WHERE payment_date >= ? AND payment_date <= ?
           ORDER BY payment_date""",
        (ss_start, ss_end),
    ).fetchall()
    ss_gastos = round(sum(float(r["amount_eur"]) for r in _ss_rows), 2)
    ss_records = [
        {
            "source": "social_security",
            "date": r["payment_date"],
            "description": r["description"] or "Cuota Seguridad Social",
            "amount_eur": round(float(r["amount_eur"]), 2),
        }
        for r in _ss_rows
    ]

    result.box_02_gastos = round(
        inv_gastos
        + ss_gastos
        + _get_tax_entries_total(year, quarter, "GASTOS_DEDUCIBLES", db_conn, ytd=True),
        2,
    )

    # IRPF retenciones soportadas: from outgoing invoices (client withholds from us)
    inv_retenciones = sum((inv.get("irpf_amount") or 0.0) for inv in income_invs)

    result.box_03_rendimiento = round(result.box_01_ingresos - result.box_02_gastos, 2)

    # Gastos de difícil justificación: 5% of rendimiento neto previo, capped at €2,000/year
    # Art. 30.2.4ª LIRPF — estimación directa simplificada
    if result.box_03_rendimiento > 0:
        raw_gdj = result.box_03_rendimiento * 0.05
        result.gastos_dificil_justificacion = round(min(raw_gdj, 2000.0), 2)
        gdj_capped = raw_gdj > 2000.0
    else:
        raw_gdj = 0.0
        result.gastos_dificil_justificacion = 0.0
        gdj_capped = False

    result.rendimiento_neto = round(
        result.box_03_rendimiento - result.gastos_dificil_justificacion, 2
    )

    result.box_05_base = round(max(0.0, result.rendimiento_neto) * 0.20, 2)

    result.box_07_retenciones = round(
        inv_retenciones
        + _get_tax_entries_total(year, quarter, "RETENCIONES_SOPORTADAS", db_conn, ytd=True),
        2,
    )

    result.box_14_pagos_anteriores = round(
        _previous_modelo130_payments(year, quarter, db_conn), 2
    )

    result.box_16_resultado = round(
        max(0.0, result.box_05_base - result.box_07_retenciones - result.box_14_pagos_anteriores),
        2,
    )

    # --- Audit trail ---
    def _a(cell, label, formula, value, **inputs):
        return AuditEntry(
            model="130", year=year, quarter=quarter,
            cell=cell, label=label, formula=formula, value=value,
            inputs_json=_json.dumps(inputs),
        )
    # Build aggregated Stripe income records for audit trail (one per geo/activity bucket).
    # Mirrors the Quarter Report view the gestor uses.
    _stripe_agg: dict[tuple, dict] = {}
    for r in rows:
        treatment = _get_vat_treatment(r)
        geo = r.get("geo_region") or "UNKNOWN"
        act = r.get("activity_type") or "UNKNOWN"
        key = (geo, act, treatment)
        if key not in _stripe_agg:
            _stripe_agg[key] = {"n": 0, "gross_eur": 0.0, "base_eur": 0.0}
        _stripe_agg[key]["n"] += 1
        _stripe_agg[key]["gross_eur"] = round(_stripe_agg[key]["gross_eur"] + _net_amount(r), 2)
        _stripe_agg[key]["base_eur"] = round(_stripe_agg[key]["base_eur"] + _get_vat_base(r), 2)

    stripe_income_records = [
        {
            "source": "stripe_agregado",
            "geo_region": k[0],
            "activity": k[1],
            "vat_treatment": k[2],
            "n_transactions": v["n"],
            "gross_eur": v["gross_eur"],
            "base_eur_irpf": v["base_eur"],
        }
        for k, v in _stripe_agg.items()
    ]
    income_inv_records = [
        {
            "source": "invoice_out",
            "date": inv.get("tx_date", "")[:10],
            "client": str(inv.get("client_name") or inv.get("client_nif") or "")[:40],
            "description": str(inv.get("description") or "")[:50],
            "subtotal_eur": round(inv.get("subtotal_eur") or 0.0, 2),
            "irpf_amount": round(inv.get("irpf_amount") or 0.0, 2),
            "vat_treatment": inv.get("vat_treatment") or "",
        }
        for inv in income_invs
    ]
    expense_inv_records = [
        {
            "source": "invoice_in",
            "date": inv.get("tx_date", "")[:10],
            "vendor": str(inv.get("vendor_name") or inv.get("vendor_nif") or "")[:40],
            "description": str(inv.get("description") or "")[:50],
            "subtotal_eur": round(inv.get("subtotal_eur") or 0.0, 2),
            "deductible_pct": inv.get("deductible_pct") or 100.0,
            "deductible_amount": round(
                (inv.get("subtotal_eur") or 0.0) * (inv.get("deductible_pct") or 100.0) / 100.0, 2
            ),
            "geo_region": inv.get("geo_region") or "",
        }
        for inv in expense_invs_ytd
    ]

    result.audit = [
        _a("box_01_ingresos",
           "Ingresos computables acumulados (Q1–Qn) — Stripe + facturas emitidas",
           f"SUM(vat_base_eur) FROM transactions YTD + SUM(subtotal_eur) FROM invoices WHERE direction='out' YTD",
           result.box_01_ingresos,
           stripe_income=round(stripe_income, 2),
           inv_income=round(inv_income, 2),
           ytd_through_quarter=quarter,
           records=stripe_income_records + income_inv_records),
        _a("box_02_gastos",
           "Gastos deducibles acumulados (Q1–Qn) — facturas recibidas + SS cuotas + entradas manuales",
           f"SUM(subtotal_eur * deductible_pct/100) FROM invoices WHERE direction='in' YTD "
           f"+ SUM(amount_eur) FROM social_security_payments YTD "
           f"+ SUM(amount_eur) FROM quarterly_tax_entries WHERE entry_type='GASTOS_DEDUCIBLES' AND quarter<=Q{quarter}",
           result.box_02_gastos,
           inv_gastos=round(inv_gastos, 2),
           ss_gastos=ss_gastos,
           ss_records=ss_records,
           records=expense_inv_records),
        _a("box_03_rendimiento",
           "Rendimiento neto previo (antes de difícil justificación)",
           "box_01_ingresos − box_02_gastos",
           result.box_03_rendimiento,
           box_01_ingresos=result.box_01_ingresos, box_02_gastos=result.box_02_gastos),
        _a("gastos_dificil_justificacion",
           "Gastos de difícil justificación (5%, máx €2.000/año)",
           "min(box_03_rendimiento × 5%, 2000)  [Art. 30.2.4ª LIRPF — estimación directa simplificada]",
           result.gastos_dificil_justificacion,
           box_03_rendimiento=result.box_03_rendimiento, rate=0.05, cap_eur=2000.0,
           raw_5pct=round(raw_gdj, 2), cap_applied=gdj_capped),
        _a("rendimiento_neto",
           "Rendimiento neto (base de cálculo IRPF)",
           "box_03_rendimiento − gastos_dificil_justificacion",
           result.rendimiento_neto,
           box_03_rendimiento=result.box_03_rendimiento,
           gastos_dificil=result.gastos_dificil_justificacion),
        _a("box_05_base",
           "Cuota IRPF (20% del rendimiento neto)",
           "max(0, rendimiento_neto) × 20%",
           result.box_05_base,
           rendimiento_neto=result.rendimiento_neto, rate=0.20),
        _a("box_07_retenciones",
           "Retenciones e ingresos a cuenta soportados YTD — facturas + entradas manuales",
           f"SUM(irpf_amount) FROM invoices WHERE direction='out' YTD "
           f"+ SUM(amount_eur) FROM quarterly_tax_entries WHERE entry_type='RETENCIONES_SOPORTADAS' AND quarter<=Q{quarter}",
           result.box_07_retenciones,
           inv_retenciones=round(inv_retenciones, 2),
           records=[
               {
                   "source": "invoice_out",
                   "date": inv.get("tx_date", "")[:10],
                   "client": str(inv.get("client_name") or inv.get("client_nif") or "")[:40],
                   "description": str(inv.get("description") or "")[:50],
                   "irpf_amount": round(inv.get("irpf_amount") or 0.0, 2),
               }
               for inv in income_invs if (inv.get("irpf_amount") or 0.0) > 0
           ]),
        _a("box_14_pagos_anteriores",
           "Pagos fraccionados ingresados en trimestres anteriores",
           "SUM(amount_eur) FROM tax_filing_status WHERE model='130' AND quarter < current AND status IN (FILED, COMPUTED)",
           result.box_14_pagos_anteriores,
           quarters_considered=list(range(1, quarter))),
        _a("box_16_resultado",
           "Resultado a ingresar",
           "max(0, box_05_base − box_07_retenciones − box_14_pagos_anteriores)",
           result.box_16_resultado,
           box_05_base=result.box_05_base,
           box_07_retenciones=result.box_07_retenciones,
           box_14_pagos_anteriores=result.box_14_pagos_anteriores),
    ]
    return result


def compute_modelo_349(year: int, quarter: int, db_conn: sqlite3.Connection) -> Modelo349Result:
    """Compute Modelo 349 (intra-EU operations summary) for the given quarter."""
    import json as _json
    result = Modelo349Result(year=year, quarter=quarter)
    rows = _load_classified_for_quarter(year, quarter, db_conn)

    by_vat_id: dict[str, dict] = {}
    for row in rows:
        treatment = _get_vat_treatment(row)
        if treatment != "IVA_EU_B2B":
            continue
        vat_id = row.get("buyer_vat_id") or "UNKNOWN"
        email = row.get("email_meta") or ""
        key = vat_id
        if key not in by_vat_id:
            by_vat_id[key] = {"name": email, "vat_id": vat_id, "total": 0.0}
        by_vat_id[key]["total"] += _net_amount(row)

    # Add EU B2B income invoices (direction='out', vat_treatment='IVA_EU_B2B')
    inv_eu_b2b = _load_income_invoices_for_quarter(year, quarter, db_conn)
    for inv in inv_eu_b2b:
        if (inv.get("vat_treatment") or "") != "IVA_EU_B2B":
            continue
        vat_id = inv.get("client_nif") or "UNKNOWN"
        name = inv.get("client_name") or ""
        amount = inv.get("subtotal_eur") or 0.0
        if vat_id not in by_vat_id:
            by_vat_id[vat_id] = {"name": name, "vat_id": vat_id, "total": 0.0}
        by_vat_id[vat_id]["total"] += amount

    warnings: list[str] = []
    negative_excluded: list[str] = []
    for info in by_vat_id.values():
        total = round(info["total"], 2)
        if total < 0:
            warnings.append(
                f"Negative total {total}€ for VAT ID {info['vat_id']} — "
                f"Model 349 does not accept negative amounts. "
                f"Corrective invoices must modify the original declaration period."
            )
            negative_excluded.append(info["vat_id"])
            continue  # Exclude negative totals from the submission rows
        result.rows.append(Modelo349Row(
            buyer_name=info["name"],
            buyer_vat_id=info["vat_id"],
            total_amount=total,
        ))
    result.total = round(sum(r.total_amount for r in result.rows), 2)
    if warnings:
        result.notes = "; ".join(warnings)

    # --- Audit trail ---
    audit = []
    for r in result.rows:
        audit.append(AuditEntry(
            model="349", year=year, quarter=quarter,
            cell=f"operator_{r.buyer_vat_id}",
            label=f"Entregas intracomunitarias — {r.buyer_vat_id}",
            formula="SUM(net_amount) for IVA_EU_B2B transactions grouped by buyer_vat_id",
            value=r.total_amount,
            inputs_json=_json.dumps({"buyer_vat_id": r.buyer_vat_id, "buyer_name": r.buyer_name}),
        ))
    audit.append(AuditEntry(
        model="349", year=year, quarter=quarter,
        cell="total",
        label="Total entregas intracomunitarias",
        formula="SUM(total_amount) across all operators",
        value=result.total,
        inputs_json=_json.dumps({
            "operator_count": len(result.rows),
            "negative_excluded": negative_excluded,
        }),
    ))
    result.audit = audit
    return result


def compute_oss_return(year: int, quarter: int, db_conn: sqlite3.Connection) -> OSSReturnResult:
    """Compute OSS quarterly return (B2C digital services to EU non-Spain customers)."""
    import json as _json
    result = OSSReturnResult(year=year, quarter=quarter)
    rows = _load_classified_for_quarter(year, quarter, db_conn)

    by_country: dict[str, dict] = defaultdict(lambda: {"count": 0, "base": 0.0, "vat": 0.0})
    for row in rows:
        treatment = _get_vat_treatment(row)
        if treatment != "OSS_EU":
            continue
        cc = (row.get("oss_country") or row.get("card_country") or "UNKNOWN").upper()
        base = _get_vat_base(row)
        vat = _get_vat_amount(row)
        by_country[cc]["count"] += 1
        by_country[cc]["base"] += base
        by_country[cc]["vat"] += vat

    for country, data in sorted(by_country.items()):
        rate = OSS_RATES.get(country, OSS_RATES["DEFAULT_EU"])
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
        audit.append(AuditEntry(
            model="OSS", year=year, quarter=quarter,
            cell=f"country_{r.country}_base",
            label=f"OSS base — {r.country} ({int(r.vat_rate * 100)}%)",
            formula="SUM(vat_base_eur) WHERE vat_treatment='OSS_EU' AND country=CC",
            value=r.base_eur,
            inputs_json=_json.dumps({
                "country": r.country, "vat_rate": r.vat_rate, "transactions": r.transactions,
            }),
        ))
        audit.append(AuditEntry(
            model="OSS", year=year, quarter=quarter,
            cell=f"country_{r.country}_vat",
            label=f"OSS cuota — {r.country} ({int(r.vat_rate * 100)}%)",
            formula=f"base_eur × {r.vat_rate}  [tasa país destino — OSS Reglamento (UE) 904/2010]",
            value=r.vat_amount_eur,
            inputs_json=_json.dumps({
                "country": r.country, "base_eur": r.base_eur, "vat_rate": r.vat_rate,
            }),
        ))
    audit.append(AuditEntry(
        model="OSS", year=year, quarter=quarter,
        cell="total_base",
        label="Base total OSS (todos los países)",
        formula="SUM(base_eur) across all countries",
        value=result.total_base,
        inputs_json=_json.dumps({"countries": len(result.rows), "transactions": result.total_transactions}),
    ))
    audit.append(AuditEntry(
        model="OSS", year=year, quarter=quarter,
        cell="total_vat",
        label="Cuota total OSS (todos los países)",
        formula="SUM(vat_amount_eur) across all countries",
        value=result.total_vat,
        inputs_json=_json.dumps({"total_base": result.total_base}),
    ))
    result.audit = audit
    return result


def compute_modelo_347(year: int, db_conn: sqlite3.Connection) -> Modelo347Result:
    """Compute Modelo 347 (annual operations > €3,005.06 with Spain counterparties)."""
    import json as _json
    result = Modelo347Result(year=year)

    # Stripe transactions from Spanish counterparties
    rows = db_conn.execute(
        """SELECT email_meta, converted_amount, converted_amount_refunded, geo_region,
                  strftime('%m', created_date) as month
           FROM transactions
           WHERE strftime('%Y', created_date) = ?
             AND geo_region = 'SPAIN'
             AND activity_type IS NOT NULL AND activity_type != 'UNKNOWN'
           ORDER BY created_date""",
        (str(year),),
    ).fetchall()

    # Income invoices issued to Spanish clients
    inv_rows = db_conn.execute(
        """SELECT COALESCE(client_name, client_nif, 'UNKNOWN') AS counterparty,
                  client_nif,
                  subtotal_eur,
                  strftime('%m', COALESCE(supply_date, invoice_date)) AS month
           FROM invoices
           WHERE direction = 'out'
             AND geo_region = 'SPAIN'
             AND COALESCE(supply_date, invoice_date) >= ?
             AND COALESCE(supply_date, invoice_date) <= ?
             AND subtotal_eur IS NOT NULL""",
        (f"{year}-01-01", f"{year}-12-31"),
    ).fetchall()

    by_email: dict[str, dict] = {}
    for row in rows:
        email = row["email_meta"] or "UNKNOWN"
        net = row["converted_amount"] - row["converted_amount_refunded"]
        month = int(row["month"])
        q = (month - 1) // 3 + 1
        if email not in by_email:
            by_email[email] = {"total": 0.0, "quarters": defaultdict(float), "nif": ""}
        by_email[email]["total"] += net
        by_email[email]["quarters"][q] += net

    for row in inv_rows:
        counterparty = row["counterparty"]
        net = row["subtotal_eur"] or 0.0
        month_str = row["month"]
        if not month_str:
            continue
        month = int(month_str)
        q = (month - 1) // 3 + 1
        if counterparty not in by_email:
            by_email[counterparty] = {"total": 0.0, "quarters": defaultdict(float), "nif": row["client_nif"] or ""}
        by_email[counterparty]["total"] += net
        by_email[counterparty]["quarters"][q] += net
        if not by_email[counterparty]["nif"] and row["client_nif"]:
            by_email[counterparty]["nif"] = row["client_nif"]

    total_counterparties = len(by_email)
    below_threshold = 0
    for email, info in by_email.items():
        total = round(info["total"], 2)
        if total >= result.threshold:
            result.rows.append(Modelo347Row(
                counterparty_name=email,
                counterparty_nif=info.get("nif", ""),
                total_operations=total,
                quarter_breakdown={q: round(v, 2) for q, v in info["quarters"].items()},
            ))
        else:
            below_threshold += 1

    result.rows.sort(key=lambda r: r.total_operations, reverse=True)

    # --- Audit trail ---
    audit = []
    for r in result.rows:
        audit.append(AuditEntry(
            model="347", year=year, quarter=0,
            cell=f"counterparty_{r.counterparty_name[:30]}",
            label=f"Operaciones con {r.counterparty_name}",
            formula=f"SUM(net_amount) WHERE geo_region='SPAIN' AND email_meta='{r.counterparty_name}' — threshold ≥ €{result.threshold:,.2f}",
            value=r.total_operations,
            inputs_json=_json.dumps({
                "counterparty": r.counterparty_name,
                "quarter_breakdown": r.quarter_breakdown,
            }),
        ))
    audit.append(AuditEntry(
        model="347", year=year, quarter=0,
        cell="summary",
        label="Resumen Modelo 347",
        formula=f"Counterparties >= €{result.threshold:,.2f} threshold",
        value=float(len(result.rows)),
        inputs_json=_json.dumps({
            "total_counterparties_spain": total_counterparties,
            "above_threshold": len(result.rows),
            "below_threshold": below_threshold,
            "threshold_eur": result.threshold,
        }),
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
    year: int, quarter: int, db_conn: sqlite3.Connection,
) -> str:
    """Run all obligation engines for the selected period and persist JSON snapshots.

    Quarterly models (303, 130, OSS, 349) use ``quarter``; Modelo 347 is annual and is
    stored with ``quarter`` = ``TAX_SNAPSHOT_QUARTER_ANNUAL`` (0).

    Returns the shared ISO ``computed_at`` timestamp written on every snapshot row.
    """
    from datetime import datetime

    from src.database import (
        TAX_SNAPSHOT_QUARTER_ANNUAL,
        upsert_audit_entries_conn,
        upsert_tax_snapshot_conn,
    )
    from src.tax_snapshot_codec import encode_snapshot

    computed_at = datetime.now().isoformat(timespec="seconds")

    r303 = compute_modelo_303(year, quarter, db_conn)
    upsert_tax_snapshot_conn(db_conn, year, quarter, "303", encode_snapshot("303", r303), computed_at)
    upsert_audit_entries_conn(db_conn, r303.audit, computed_at)

    r130 = compute_modelo_130(year, quarter, db_conn)
    upsert_tax_snapshot_conn(db_conn, year, quarter, "130", encode_snapshot("130", r130), computed_at)
    upsert_audit_entries_conn(db_conn, r130.audit, computed_at)

    r_oss = compute_oss_return(year, quarter, db_conn)
    upsert_tax_snapshot_conn(db_conn, year, quarter, "OSS", encode_snapshot("OSS", r_oss), computed_at)
    upsert_audit_entries_conn(db_conn, r_oss.audit, computed_at)

    r349 = compute_modelo_349(year, quarter, db_conn)
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
