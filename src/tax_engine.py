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
    if row.get("vat_base_eur") is not None:
        return row["vat_base_eur"]
    return _net_amount(row)


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
    result = Modelo303Result(year=year, quarter=quarter)
    rows = _load_classified_for_quarter(year, quarter, db_conn)

    for row in rows:
        treatment = _get_vat_treatment(row)
        base = _get_vat_base(row)
        vat = _get_vat_amount(row)

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

    # Deductible IVA from manual entries (current quarter only)
    result.box_28_iva_soportado = _get_tax_entries_total(
        year, quarter, "IVA_SOPORTADO", db_conn, ytd=False
    )
    # Derive base from cuota assuming general 21% rate (approximate when mixed rates exist)
    result.box_29_base_soportado = round(result.box_28_iva_soportado / 0.21, 2) if result.box_28_iva_soportado else 0.0

    # Round accumulations
    result.box_01_base = round(result.box_01_base, 2)
    result.box_03_cuota = round(result.box_03_cuota, 2)
    result.box_59_intracom_entregas = round(result.box_59_intracom_entregas, 2)
    result.oss_base = round(result.oss_base, 2)
    result.oss_vat = round(result.oss_vat, 2)
    result.export_base = round(result.export_base, 2)

    result.box_46_diferencia = round(result.box_03_cuota - result.box_28_iva_soportado, 2)
    result.box_48_resultado = result.box_46_diferencia  # simplified (100% proration)

    return result


def compute_modelo_130(year: int, quarter: int, db_conn: sqlite3.Connection) -> Modelo130Result:
    """Compute Modelo 130 (quarterly IRPF advance) for the given quarter."""
    result = Modelo130Result(year=year, quarter=quarter)
    rows = _load_classified_ytd(year, quarter, db_conn)

    total_income = sum(_get_vat_base(r) for r in rows)
    result.box_01_ingresos = round(total_income, 2)

    result.box_02_gastos = round(
        _get_tax_entries_total(year, quarter, "GASTOS_DEDUCIBLES", db_conn, ytd=True), 2
    )

    result.box_03_rendimiento = round(result.box_01_ingresos - result.box_02_gastos, 2)
    result.box_05_base = round(max(0.0, result.box_03_rendimiento) * 0.20, 2)

    result.box_07_retenciones = round(
        _get_tax_entries_total(year, quarter, "RETENCIONES_SOPORTADAS", db_conn, ytd=True), 2
    )

    result.box_14_pagos_anteriores = round(
        _previous_modelo130_payments(year, quarter, db_conn), 2
    )

    result.box_16_resultado = round(
        max(0.0, result.box_05_base - result.box_07_retenciones - result.box_14_pagos_anteriores),
        2,
    )

    return result


def compute_modelo_349(year: int, quarter: int, db_conn: sqlite3.Connection) -> Modelo349Result:
    """Compute Modelo 349 (intra-EU operations summary) for the given quarter."""
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

    for info in by_vat_id.values():
        result.rows.append(Modelo349Row(
            buyer_name=info["name"],
            buyer_vat_id=info["vat_id"],
            total_amount=round(info["total"], 2),
        ))
    result.total = round(sum(r.total_amount for r in result.rows), 2)
    return result


def compute_oss_return(year: int, quarter: int, db_conn: sqlite3.Connection) -> OSSReturnResult:
    """Compute OSS quarterly return (B2C digital services to EU non-Spain customers)."""
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
    return result


def compute_modelo_347(year: int, db_conn: sqlite3.Connection) -> Modelo347Result:
    """Compute Modelo 347 (annual operations > €3,005.06 with Spain counterparties)."""
    result = Modelo347Result(year=year)

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

    by_email: dict[str, dict] = {}
    for row in rows:
        email = row["email_meta"] or "UNKNOWN"
        net = row["converted_amount"] - row["converted_amount_refunded"]
        month = int(row["month"])
        q = (month - 1) // 3 + 1
        if email not in by_email:
            by_email[email] = {"total": 0.0, "quarters": defaultdict(float)}
        by_email[email]["total"] += net
        by_email[email]["quarters"][q] += net

    for email, info in by_email.items():
        total = round(info["total"], 2)
        if total >= result.threshold:
            result.rows.append(Modelo347Row(
                counterparty_name=email,
                counterparty_nif="",  # must be entered manually
                total_operations=total,
                quarter_breakdown={q: round(v, 2) for q, v in info["quarters"].items()},
            ))

    result.rows.sort(key=lambda r: r.total_operations, reverse=True)
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
