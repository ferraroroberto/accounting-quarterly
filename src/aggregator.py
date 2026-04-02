from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Optional

from src.logger import get_logger
from src.models import ClassifiedPayment, GeoRegion, MonthlyAggregation

log = get_logger(__name__)

GEO_REGIONS: list[GeoRegion] = ["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU"]

MONTH_NAMES = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def aggregate_by_month(
    payments: list[ClassifiedPayment],
    geo_filter: Optional[GeoRegion] = None,
) -> list[MonthlyAggregation]:
    """Aggregate classified payments by (year, month, geo_region)."""
    buckets: dict[tuple[int, int, str], MonthlyAggregation] = {}

    for p in payments:
        if p.activity_type == "UNKNOWN":
            continue
        region = p.geo_region
        if geo_filter and region != geo_filter:
            continue
        if region == "UNKNOWN":
            continue

        key = (p.year, p.month, region)
        if key not in buckets:
            buckets[key] = MonthlyAggregation(year=p.year, month=p.month, geo_region=region)

        agg = buckets[key]
        net = p.net_amount

        if p.activity_type == "COACHING":
            agg.coaching_income = round(agg.coaching_income + net, 2)
            agg.coaching_fee = round(agg.coaching_fee + p.fee, 2)
        elif p.activity_type == "NEWSLETTER":
            agg.newsletter_income = round(agg.newsletter_income + net, 2)
            agg.newsletter_fee = round(agg.newsletter_fee + p.fee, 2)
        elif p.activity_type == "ILLUSTRATIONS":
            agg.illustrations_income = round(agg.illustrations_income + net, 2)
            agg.illustrations_fee = round(agg.illustrations_fee + p.fee, 2)

    result = sorted(buckets.values(), key=lambda a: (a.year, a.month))
    log.debug("ℹ️ Aggregated into %d monthly buckets", len(result))
    return result


def build_monthly_table(
    payments: list[ClassifiedPayment],
    geo_filter: GeoRegion,
    year: Optional[int] = None,
    quarter: Optional[int] = None,
) -> list[dict]:
    """Build a list of row dicts for a given region/quarter for dashboard/Excel display."""
    filtered = [p for p in payments if p.geo_region == geo_filter and p.activity_type != "UNKNOWN"]

    if year:
        filtered = [p for p in filtered if p.year == year]
    if quarter:
        months = {1: [1, 2, 3], 2: [4, 5, 6], 3: [7, 8, 9], 4: [10, 11, 12]}[quarter]
        filtered = [p for p in filtered if p.month in months]

    aggs = aggregate_by_month(filtered, geo_filter=geo_filter)
    agg_map = {(a.year, a.month): a for a in aggs}

    relevant_months = sorted({(p.year, p.month) for p in filtered})

    rows = []
    for y, m in relevant_months:
        a = agg_map.get((y, m), MonthlyAggregation(year=y, month=m, geo_region=geo_filter))
        month_start = date(y, m, 1)
        if m == 12:
            month_end = date(y + 1, 1, 1)
        else:
            month_end = date(y, m + 1, 1)
        import calendar
        last_day = calendar.monthrange(y, m)[1]
        month_end = date(y, m, last_day)

        rows.append({
            "Month": f"{MONTH_NAMES[m]} {y}",
            "Month Start": month_start.isoformat(),
            "Month End": month_end.isoformat(),
            "Coaching": round(a.coaching_income, 2),
            "Newsletter": round(a.newsletter_income, 2),
            "Illustrations": round(a.illustrations_income, 2),
            "Total Income": round(a.total_income, 2),
            "Coaching Fee": round(a.coaching_fee, 2),
            "Newsletter Fee": round(a.newsletter_fee, 2),
            "Illustrations Fee": round(a.illustrations_fee, 2),
            "Total Fee": round(a.total_fee, 2),
        })

    if rows:
        rows.append({
            "Month": "TOTAL",
            "Month Start": "",
            "Month End": "",
            "Coaching": round(sum(r["Coaching"] for r in rows), 2),
            "Newsletter": round(sum(r["Newsletter"] for r in rows), 2),
            "Illustrations": round(sum(r["Illustrations"] for r in rows), 2),
            "Total Income": round(sum(r["Total Income"] for r in rows), 2),
            "Coaching Fee": round(sum(r["Coaching Fee"] for r in rows), 2),
            "Newsletter Fee": round(sum(r["Newsletter Fee"] for r in rows), 2),
            "Illustrations Fee": round(sum(r["Illustrations Fee"] for r in rows), 2),
            "Total Fee": round(sum(r["Total Fee"] for r in rows), 2),
        })

    return rows


def calculate_grand_totals(payments: list[ClassifiedPayment]) -> dict:
    """Calculate overall totals across all regions."""
    totals: dict[str, float] = defaultdict(float)

    for p in payments:
        net = p.net_amount
        totals["total_income"] = round(totals["total_income"] + net, 2)
        totals["total_fee"] = round(totals["total_fee"] + p.fee, 2)

        if p.activity_type == "UNKNOWN":
            totals["unknown"] = round(totals["unknown"] + net, 2)
            totals["unknown_fee"] = round(totals["unknown_fee"] + p.fee, 2)
            continue

        act = p.activity_type.lower()
        totals[act] = round(totals[act] + net, 2)
        totals[f"{act}_fee"] = round(totals[f"{act}_fee"] + p.fee, 2)

    return dict(totals)


def calculate_regional_totals(payments: list[ClassifiedPayment]) -> dict:
    """Calculate totals grouped by geographic region."""
    result = {}
    for region in GEO_REGIONS:
        region_payments = [p for p in payments if p.geo_region == region]
        result[region] = calculate_grand_totals(region_payments)
    return result


def get_transaction_count(payments: list[ClassifiedPayment]) -> dict:
    """Return transaction counts by region and activity type."""
    counts: dict[str, int] = defaultdict(int)
    for p in payments:
        counts["total"] += 1
        counts[p.geo_region.lower()] = counts[p.geo_region.lower()] + 1
        counts[p.activity_type.lower()] = counts[p.activity_type.lower()] + 1
    return dict(counts)
