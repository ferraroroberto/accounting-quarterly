from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.aggregator import calculate_grand_totals, calculate_regional_totals
from src.classifier import classify_batch, validate_classifications
from src.config import load_config
from src.csv_importer import merge_csv_files, parse_stripe_csv
from src.logger import get_logger
from src.models import ClassifiedPayment, Payment, ValidationResult

log = get_logger(__name__)

TOLERANCE = 0.02


def _diff_label(diff: float) -> str:
    if abs(diff) <= TOLERANCE:
        return "✓"
    elif abs(diff) < 1.0:
        return "⚠"
    return "✗"


def run_validation(
    payments: Optional[list[Payment]] = None,
    cfg: Optional[dict] = None,
) -> tuple[ValidationResult, list[ClassifiedPayment]]:
    """Run full validation against known historical totals.

    Returns (ValidationResult, classified_payments).
    """
    cfg = cfg or load_config()
    known = cfg.get("known_totals", {})
    app_cfg = cfg.get("app", {})

    if payments is None:
        period_start = datetime.strptime(known.get("period", "2023-07-01 to 2025-12-31").split(" to ")[0], "%Y-%m-%d")
        period_end_str = known.get("period", "2023-07-01 to 2025-12-31").split(" to ")[1]
        period_end = datetime.strptime(period_end_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)

        csv_new = app_cfg.get("csv_path_new")
        csv_old = app_cfg.get("csv_path")

        if csv_new and csv_old:
            payments = merge_csv_files(csv_old, csv_new, period_start, period_end)
        elif csv_old:
            payments = parse_stripe_csv(csv_old, period_start, period_end)
        elif csv_new:
            payments = parse_stripe_csv(csv_new, period_start, period_end)
        else:
            raise ValueError("No CSV path configured")

    classified, error_ids = classify_batch(payments, cfg)
    val_report = validate_classifications(classified)
    grand = calculate_grand_totals(classified)
    regional = calculate_regional_totals(classified)

    discrepancies = []

    def check(metric: str, expected: float, actual: float):
        diff = round(actual - expected, 2)
        if abs(diff) > TOLERANCE:
            discrepancies.append({
                "metric": metric,
                "expected": expected,
                "actual": actual,
                "diff": diff,
                "pct": round((diff / expected * 100) if expected else 0, 2),
                "status": _diff_label(diff),
            })

    coaching_actual = round(grand.get("coaching", 0.0), 2)
    newsletter_actual = round(grand.get("newsletter", 0.0), 2)
    illustrations_actual = round(grand.get("illustrations", 0.0), 2)
    coaching_fee_actual = round(grand.get("coaching_fee", 0.0), 2)
    newsletter_fee_actual = round(grand.get("newsletter_fee", 0.0), 2)
    illustrations_fee_actual = round(grand.get("illustrations_fee", 0.0), 2)
    total_income_actual = round(grand.get("total_income", 0.0), 2)
    total_fee_actual = round(grand.get("total_fee", 0.0), 2)

    check("Coaching Income", known.get("coaching", 0), coaching_actual)
    check("Newsletter Income", known.get("newsletter", 0), newsletter_actual)
    check("Illustrations Income", known.get("illustrations", 0), illustrations_actual)
    check("Total Income", known.get("total_income", 0), total_income_actual)
    check("Coaching Fee", known.get("coaching_fee", 0), coaching_fee_actual)
    check("Newsletter Fee", known.get("newsletter_fee", 0), newsletter_fee_actual)
    check("Illustrations Fee", known.get("illustrations_fee", 0), illustrations_fee_actual)
    check("Total Fee", known.get("total_fee", 0), total_fee_actual)

    regional_expected = known.get("regional", {})
    for region_key, region_data in regional_expected.items():
        region_name = region_key.upper()
        actual_region = regional.get(region_name, {})
        for act, exp_val in region_data.items():
            actual_val = round(actual_region.get(act, 0.0), 2)
            check(f"{region_key.capitalize()} {act.capitalize()}", exp_val, actual_val)

    passed = len(discrepancies) == 0 and val_report["activity_errors"] == 0

    period_parts = known.get("period", "2023-07-01 to 2025-12-31").split(" to ")

    result = ValidationResult(
        period_start=period_parts[0],
        period_end=period_parts[1],
        total_transactions=len(payments),
        classification_errors=val_report["activity_errors"],
        geo_errors=val_report["geo_errors"],
        coaching_actual=coaching_actual,
        newsletter_actual=newsletter_actual,
        illustrations_actual=illustrations_actual,
        coaching_fee_actual=coaching_fee_actual,
        newsletter_fee_actual=newsletter_fee_actual,
        illustrations_fee_actual=illustrations_fee_actual,
        total_income_actual=total_income_actual,
        total_fee_actual=total_fee_actual,
        coaching_expected=known.get("coaching", 0),
        newsletter_expected=known.get("newsletter", 0),
        illustrations_expected=known.get("illustrations", 0),
        total_income_expected=known.get("total_income", 0),
        total_fee_expected=known.get("total_fee", 0),
        regional_actual=regional,
        regional_expected=regional_expected,
        passed=passed,
        discrepancies=discrepancies,
        unclassified_ids=val_report["unknown_ids"],
    )

    status = "✓ PASS" if passed else f"✗ FAIL ({len(discrepancies)} discrepancies)"
    log.info("ℹ️ Validation result: %s | transactions=%d | unclassified=%d",
             status, len(payments), len(val_report["unknown_ids"]))

    return result, classified


def format_validation_report(result: ValidationResult) -> str:
    """Format a human-readable validation report (Markdown)."""
    lines = [
        "# Validation Report: Stripe Automation System",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "## Summary",
        f"- Date range: {result.period_start} – {result.period_end}",
        f"- Total transactions processed: {result.total_transactions}",
        f"- Classification errors: {result.classification_errors}",
        f"- Geo classification errors: {result.geo_errors}",
        f"- Unclassified transactions: {len(result.unclassified_ids)}",
        f"- Validation status: {'**PASS ✓**' if result.passed else '**FAIL ✗**'}",
        "",
        "## Grand Totals Comparison",
        "",
        "| Metric | Expected | Actual | Diff | Status |",
        "|--------|----------|--------|------|--------|",
    ]

    rows = [
        ("Coaching", result.coaching_expected, result.coaching_actual),
        ("Newsletter", result.newsletter_expected, result.newsletter_actual),
        ("Illustrations", result.illustrations_expected, result.illustrations_actual),
        ("Total Income", result.total_income_expected, result.total_income_actual),
        ("Coaching Fee", result.coaching_fee_actual, result.coaching_fee_actual),
        ("Newsletter Fee", result.newsletter_fee_actual, result.newsletter_fee_actual),
        ("Illustrations Fee", result.illustrations_fee_actual, result.illustrations_fee_actual),
    ]

    for name, exp, act in rows:
        diff = round(act - exp, 2)
        status = _diff_label(diff)
        lines.append(f"| {name} | {exp:,.2f} | {act:,.2f} | {diff:+.2f} | {status} |")

    lines += [
        "",
        "## Discrepancies",
        "",
    ]

    if result.discrepancies:
        lines.append("| Metric | Expected | Actual | Diff | % | Status |")
        lines.append("|--------|----------|--------|------|---|--------|")
        for d in result.discrepancies:
            lines.append(
                f"| {d['metric']} | {d['expected']:,.2f} | {d['actual']:,.2f} | "
                f"{d['diff']:+.2f} | {d['pct']:+.1f}% | {d['status']} |"
            )
    else:
        lines.append("None – all totals match.")

    if result.unclassified_ids:
        lines += [
            "",
            "## Unclassified Transactions",
            "",
            "The following transaction IDs could not be auto-classified:",
            "",
        ]
        for tid in result.unclassified_ids:
            lines.append(f"- `{tid}`")

    lines += [
        "",
        "## Conclusion",
        "",
        ("✓ System validated successfully. Ready for production use."
         if result.passed
         else "✗ Discrepancies found. Review overrides and classification rules."),
    ]

    return "\n".join(lines)
