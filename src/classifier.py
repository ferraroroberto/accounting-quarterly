"""Activity and geographic classification using rules from classification_rules.json."""
from __future__ import annotations

from typing import Optional

from src.logger import get_logger
from src.models import ActivityType, ClassifiedPayment, GeoRegion, Payment
from src.rules_engine import load_rules

log = get_logger(__name__)

MONTH_LABELS = {
    1: "Jan", 2: "Feb", 3: "Mar", 4: "Apr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Aug", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dec",
}


def classify_activity(
    description: str,
    payment_type_meta: Optional[str] = None,
    rules: Optional[dict] = None,
) -> tuple[ActivityType, str]:
    """Return (ActivityType, rule_description) using rules from JSON."""
    rules = rules or load_rules()
    activity_rules = sorted(rules.get("activity_rules", []), key=lambda r: r.get("priority", 99))
    desc_lower = (description or "").strip().lower()

    for rule in activity_rules:
        match_type = rule.get("match_type", "")
        activity: ActivityType = rule.get("activity_type", "UNKNOWN")  # type: ignore[assignment]
        rule_name = rule.get("name", "unknown_rule")

        if match_type == "empty_description":
            if not desc_lower:
                return activity, rule_name

        elif match_type == "payment_type":
            match_value = rule.get("match_value", "").lower()
            if payment_type_meta and payment_type_meta.lower() == match_value:
                return activity, f"{rule_name}:{payment_type_meta}"

        elif match_type == "description_contains":
            keywords = rule.get("keywords", [])
            for keyword in keywords:
                if keyword.lower() in desc_lower:
                    return activity, f"{rule_name}:{keyword}"

    return "UNKNOWN", "no_pattern_matched"


def _match_geo_override(
    description: str,
    email_meta: Optional[str],
    geo_overrides: dict,
    email_overrides: dict,
) -> Optional[tuple[GeoRegion, str]]:
    """Try to match a geographic override from description or email."""
    desc_lower = (description or "").lower()

    if email_meta:
        email_lower = email_meta.lower()
        for key, region in email_overrides.items():
            if key.lower() in email_lower:
                return region, f"email_override:{key}"

    for key, region in geo_overrides.items():
        if key.lower() in desc_lower:
            return region, f"name_override:{key}"

    if email_meta:
        email_lower = email_meta.lower()
        for key, region in geo_overrides.items():
            if key.lower() in email_lower:
                return region, f"email_in_geo_override:{key}"

    return None


def classify_geography(
    payment: Payment,
    rules: Optional[dict] = None,
    activity_type: Optional[str] = None,
) -> tuple[GeoRegion, str]:
    """Return (GeoRegion, rule_description) using rules from JSON."""
    rules = rules or load_rules()
    geo_rules = rules.get("geographic_rules", {})
    defaults = geo_rules.get("defaults", {})
    geo_overrides = geo_rules.get("geographic_overrides", {})
    email_overrides = geo_rules.get("email_overrides", {})

    if payment.currency != "eur":
        non_eur_default: GeoRegion = defaults.get("non_eur_default", "OUTSIDE_EU")  # type: ignore[assignment]
        return non_eur_default, f"non_eur_currency:{payment.currency}"

    override = _match_geo_override(
        payment.description,
        payment.email_meta,
        geo_overrides,
        email_overrides,
    )
    if override:
        region_str, rule = override
        region: GeoRegion = region_str  # type: ignore[assignment]
        return region, rule

    if activity_type == "NEWSLETTER":
        eur_newsletter_default: GeoRegion = defaults.get("eur_newsletter_default", "EU_NOT_SPAIN")  # type: ignore[assignment]
        return eur_newsletter_default, "eur_newsletter_default"

    eur_default: GeoRegion = defaults.get("eur_default", "SPAIN")  # type: ignore[assignment]
    return eur_default, "eur_default"


def classify_payment(payment: Payment, rules: Optional[dict] = None) -> ClassifiedPayment:
    """Apply full classification (activity + geography) to a Payment."""
    rules = rules or load_rules()

    activity, act_rule = classify_activity(
        payment.description,
        payment.payment_type_meta,
        rules,
    )
    geo, geo_rule = classify_geography(payment, rules, activity_type=activity)

    classified = ClassifiedPayment(
        **payment.model_dump(),
        activity_type=activity,
        geo_region=geo,
        classification_rule=act_rule,
        geo_rule=geo_rule,
    )

    if activity == "UNKNOWN":
        log.warning(
            "⚠️ Unclassified transaction | id=%s | desc=%r | rule=%s",
            payment.id, payment.description, act_rule,
        )

    log.debug(
        "ℹ️ Classified | id=%s | activity=%s (%s) | geo=%s (%s)",
        payment.id, activity, act_rule, geo, geo_rule,
    )
    return classified


def classify_batch(
    payments: list[Payment],
    rules: Optional[dict] = None,
) -> tuple[list[ClassifiedPayment], list[str]]:
    """Classify a list of payments. Returns (classified_list, error_ids)."""
    rules = rules or load_rules()
    classified = []
    error_ids = []

    for p in payments:
        cp = classify_payment(p, rules)
        classified.append(cp)
        if not cp.activity_valid or not cp.geo_valid:
            error_ids.append(p.id)

    log.info(
        "ℹ️ Classified %d payments | %d errors",
        len(classified), len(error_ids),
    )
    return classified, error_ids


def validate_classifications(payments: list[ClassifiedPayment]) -> dict:
    """Validate indicator sums and return a report dict."""
    activity_errors = [
        p for p in payments
        if p.IND_COACHING + p.IND_NEWSLETTER + p.IND_ILLUSTRATIONS != 1
    ]
    geo_errors = [
        p for p in payments
        if p.IND_SPAIN + p.IND_OUT_SPAIN + p.IND_EXEU != 1
    ]
    unknown = [p for p in payments if p.activity_type == "UNKNOWN"]

    return {
        "total": len(payments),
        "activity_errors": len(activity_errors),
        "geo_errors": len(geo_errors),
        "unknown_activity": len(unknown),
        "activity_error_ids": [p.id for p in activity_errors],
        "geo_error_ids": [p.id for p in geo_errors],
        "unknown_ids": [p.id for p in unknown],
    }
