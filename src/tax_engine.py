"""Tax computation engine for Spanish autónomo obligations."""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date, datetime
from functools import partial
from typing import Optional

from src.logger import get_logger
from src.tax_deadlines import calendar_deadline
from src.tax_data import (
    clamp_start,
    get_vat_amount,
    get_vat_base,
    get_vat_treatment,
    load_classified_for_quarter,
    load_classified_ytd,
    load_app_config,
)
from src.tax_models import (
    AuditEntry,
    EUB2CThresholdResult,
    Modelo347Result,
    Modelo347Row,
    OSSCountryRow,
    OSSReturnResult,
    TaxDeadline,
)
from src.declared_reports import apply_frozen_amounts
from src.modelo_349 import compute_modelo_349
from src.modelo_130 import compute_modelo_130, minoracion_art_110_3_c  # noqa: F401  (re-exported, #174)
from src.modelo_303 import compute_modelo_303, prorrata_pct  # noqa: F401  (re-exported, #174)
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
