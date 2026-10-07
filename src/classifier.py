"""Activity and geographic classification using rules from classification_rules.json."""
from __future__ import annotations

import re
from typing import Optional

from src.logger import get_logger
from src.models import ActivityType, ClassifiedPayment, GeoRegion, Payment
from src.rules_engine import load_rules
from src.tax_codes import EU_COUNTRY_CODES

log = get_logger(__name__)


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
    """Return (GeoRegion, rule_description) using rules from JSON.

    EUR charges are classified as before: an explicit name/email override
    wins, otherwise the activity-based default (``eur_default`` /
    ``eur_newsletter_default``) applies.

    Non-EUR charges are classified by the charge country first — card
    issuing country, then billing address, then customer address (see
    :func:`_charge_country`) — since currency alone says nothing about EU
    membership (#111: DKK/SEK/PLN/... are EU currencies). Only when no
    country is known does currency act as a fallback: an EU non-euro
    currency (:data:`_EU_NON_EURO_CURRENCIES`) maps to ``EU_NOT_SPAIN`` and
    is flagged for review (``non_eur_currency_eu_review:*``), anything else
    falls to ``non_eur_default`` (``OUTSIDE_EU``), same as before.
    """
    rules = rules or load_rules()
    geo_rules = rules.get("geographic_rules", {})
    defaults = geo_rules.get("defaults", {})
    geo_overrides = geo_rules.get("geographic_overrides", {})
    email_overrides = geo_rules.get("email_overrides", {})

    if payment.currency == "eur":
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

    country = _charge_country(payment)
    if country:
        if country == "ES":
            return "SPAIN", f"country:{country}"
        if country in EU_COUNTRY_CODES:
            return "EU_NOT_SPAIN", f"country:{country}"
        return "OUTSIDE_EU", f"country:{country}"

    if payment.currency in _EU_NON_EURO_CURRENCIES:
        return "EU_NOT_SPAIN", f"non_eur_currency_eu_review:{payment.currency}"

    non_eur_default: GeoRegion = defaults.get("non_eur_default", "OUTSIDE_EU")  # type: ignore[assignment]
    return non_eur_default, f"non_eur_currency:{payment.currency}"


# EU member states that don't use the euro. A charge in one of these
# currencies with no known country is still probably an EU B2C sale, not
# outside the EU — but currency is a weaker signal than country, so it is
# flagged for review rather than trusted outright (#111).
_EU_NON_EURO_CURRENCIES: frozenset[str] = frozenset({
    "dkk", "sek", "pln", "czk", "huf", "ron", "bgn",
})


def _address_country(details: Optional[dict]) -> str:
    """Return the upper-cased ``address.country`` of a Stripe-shaped dict, or ``""``."""
    if not details:
        return ""
    return str((details.get("address") or {}).get("country") or "").strip().upper()


def _charge_country(payment: Payment) -> Optional[str]:
    """Best-known charge country: card issuing country -> billing -> customer address.

    Same Stripe fields :func:`foreign_customer_hint` (#94) inspects, read in
    priority order so the first known country wins. Returns ``None`` when
    none of them is set.
    """
    card_cc = (payment.card_country or "").strip().upper()
    if card_cc:
        return card_cc

    raw = payment.raw_source or {}
    billing_cc = _address_country(raw.get("billing_details"))
    if billing_cc:
        return billing_cc

    customer = raw.get("customer")
    if isinstance(customer, dict):
        customer_cc = _address_country(customer)
        if customer_cc:
            return customer_cc

    return None


# Two-letter TLDs that are marketed as generic domains (.io, .co, .me, …) and so
# say nothing about where the customer lives.
_GENERIC_CCTLDS: frozenset[str] = frozenset({
    "ai", "cc", "co", "fm", "gg", "io", "ly", "me", "so", "to", "tv", "ws",
})


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.([a-z]{2,})\b", re.IGNORECASE)


def foreign_customer_hint(payment: Payment) -> Optional[str]:
    """Return why a customer looks non-Spanish, or ``None`` when nothing says so.

    Signals: a card issuing country or a Stripe billing-address country other
    than ``ES``, or an email address (Stripe customer email, or one written in
    the charge description) on a country-code domain other than ``.es`` —
    generic-use ccTLDs like ``.io`` are ignored. Used to flag EUR charges that
    fell through to the ``eur_default`` (SPAIN) rule although the customer is
    probably abroad.
    """
    reasons: list[str] = []
    card_cc = (payment.card_country or "").strip().upper()
    if card_cc and card_cc != "ES":
        reasons.append(f"card country {card_cc}")
    billing = ((payment.raw_source or {}).get("billing_details") or {})
    billing_cc = _address_country(billing)
    if billing_cc and billing_cc != "ES" and billing_cc != card_cc:
        reasons.append(f"billing country {billing_cc}")
    text = f"{payment.email_meta or ''} {billing.get('email') or ''} {payment.description or ''}"
    tlds = sorted({m.group(1).lower() for m in _EMAIL_RE.finditer(text)})
    for tld in tlds:
        if len(tld) == 2 and tld != "es" and tld not in _GENERIC_CCTLDS:
            reasons.append(f"email domain .{tld}")
    return "; ".join(reasons) or None


def eur_default_foreign_warning(payment: ClassifiedPayment) -> Optional[str]:
    """Warning text when a EUR charge got ``eur_default`` but looks foreign.

    The ``eur_default`` rule puts every unmatched EUR charge in the default
    region (SPAIN). For a customer with a foreign card or email domain that is
    probably wrong and needs an explicit override. Returns ``None`` otherwise.
    """
    if payment.currency != "eur" or payment.geo_rule != "eur_default":
        return None
    hint = foreign_customer_hint(payment)
    if hint is None:
        return None
    return f"EUR charge classified {payment.geo_region} by eur_default, but {hint}"


def _match_vat_id_override(
    description: Optional[str],
    email_meta: Optional[str],
    vat_id_overrides: dict,
) -> Optional[str]:
    """Try to match a per-customer VAT id override from email or description.

    Mirrors :func:`_match_geo_override`'s precedence: an email match wins,
    then a name/description match, then the name overrides checked against
    the email too (a client sometimes typed as their own email domain).
    """
    email_ov = vat_id_overrides.get("email_vat_ids", {})
    name_ov = vat_id_overrides.get("name_vat_ids", {})
    desc_lower = (description or "").lower()

    if email_meta:
        email_lower = email_meta.lower()
        for key, vat_id in email_ov.items():
            if key.lower() in email_lower:
                return vat_id

    for key, vat_id in name_ov.items():
        if key.lower() in desc_lower:
            return vat_id

    if email_meta:
        email_lower = email_meta.lower()
        for key, vat_id in name_ov.items():
            if key.lower() in email_lower:
                return vat_id

    return None


def _vat_id_from_raw_customer(payment: Payment) -> Optional[str]:
    """Read-only fallback: a Stripe ``customer.tax_ids`` VAT id already present
    in the stored raw charge JSON.

    ``src.stripe_client.fetch_charges`` does not currently expand
    ``data.customer.tax_ids``, so this is a no-op for data fetched today —
    kept so the id is picked up automatically if that expand is ever added,
    or for raw sources populated by another path. Returns the first tax id's
    ``value`` (e.g. ``"DE123456789"``), or ``None``.
    """
    raw = payment.raw_source or {}
    customer = raw.get("customer")
    if not isinstance(customer, dict):
        return None
    tax_ids = customer.get("tax_ids")
    if isinstance(tax_ids, dict):
        entries = tax_ids.get("data")
    elif isinstance(tax_ids, list):
        entries = tax_ids
    else:
        entries = None
    for entry in entries or []:
        if isinstance(entry, dict) and entry.get("value"):
            return str(entry["value"]).strip()
    return None


def customer_vat_id(payment: Payment, rules: Optional[dict] = None) -> Optional[str]:
    """Return the buyer's EU VAT id for a payment, or ``None`` when unknown.

    Determines B2B vs. B2C for EU (non-Spain) sales (accounting-quarterly#113):
    a sale to a customer with a known VAT id is business-to-business
    (reverse charge, Modelo 349 key S); otherwise it is treated as a
    consumer sale. Looked up in order:

    1. A per-customer override in ``classification_rules.json``'s
       ``customer_vat_ids`` (email match first, then name/description match —
       see :func:`_match_vat_id_override`).
    2. A Stripe ``customer.tax_ids`` entry already present in the stored raw
       charge JSON (:func:`_vat_id_from_raw_customer`) — a read-only
       fallback, since the id is not requested by the Stripe fetch today.

    VIES validity of the id is not checked — out of scope for #113.
    """
    rules = rules or load_rules()
    override = _match_vat_id_override(
        payment.description, payment.email_meta, rules.get("customer_vat_ids", {})
    )
    if override:
        return override
    return _vat_id_from_raw_customer(payment)


def classify_payment(payment: Payment, rules: Optional[dict] = None) -> ClassifiedPayment:
    """Apply full classification (activity + geography + buyer VAT id) to a Payment."""
    rules = rules or load_rules()

    activity, act_rule = classify_activity(
        payment.description,
        payment.payment_type_meta,
        rules,
    )
    geo, geo_rule = classify_geography(payment, rules, activity_type=activity)
    buyer_vat_id = customer_vat_id(payment, rules)

    classified = ClassifiedPayment(
        **payment.model_dump(),
        activity_type=activity,
        geo_region=geo,
        classification_rule=act_rule,
        geo_rule=geo_rule,
        buyer_vat_id=buyer_vat_id,
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
