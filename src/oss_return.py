"""OSS quarterly return and the EU B2C distance-selling threshold watch.

Moved verbatim out of ``src.tax_engine`` (#174). Reads its records through ``src.tax_data``;
``compute_oss_return`` and ``compute_eu_b2c_threshold`` are the public entry points.
"""
from __future__ import annotations

import sqlite3
from collections import defaultdict
from functools import partial
from typing import Optional

from src.logger import get_logger
from src.tax_data import (
    get_vat_amount,
    get_vat_base,
    get_vat_treatment,
    load_classified_for_quarter,
    load_classified_ytd,
)
from src.tax_models import AuditEntry, EUB2CThresholdResult, OSSCountryRow, OSSReturnResult
from src.vat_rules import (
    EU_B2C_THRESHOLD_EUR,
    EU_B2C_TREATMENTS,
    EU_B2C_WARN_RATIO,
    is_oss_registered,
    oss_rate,
)

log = get_logger(__name__)


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
