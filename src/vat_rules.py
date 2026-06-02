"""Single source of truth for the VAT treatment decision matrix and rates.

The activity × geography → ``vat_treatment`` matrix, the OSS rate lookup, and
the VAT-inclusive base extraction were previously implemented twice — once on
the Pydantic model in :func:`src.classifier.classify_vat`, and again as a
fallback in ``src.tax_engine._get_vat_treatment`` / ``_get_vat_base`` /
``_get_vat_amount``. Divergence between the two copies would produce inconsistent
figures between what gets stored on a transaction and what the engine recomputes.

This module holds the rules once. It depends only on ``src.tax_models`` (for the
OSS rate table) so it can be imported from both the classifier and the tax engine
without a circular import.
"""
from __future__ import annotations

from typing import Optional

from src.tax_models import OSS_RATES

# Standard Spanish IVA rate.
IVA_ES_RATE = 0.21


def oss_rate(country_code: Optional[str]) -> float:
    """Return the OSS VAT rate for an EU destination country code.

    Falls back to ``DEFAULT_EU`` for unknown or missing codes.
    """
    cc = (country_code or "").upper()
    return OSS_RATES.get(cc, OSS_RATES["DEFAULT_EU"])


def vat_treatment(
    activity: Optional[str],
    geo: Optional[str],
    config: Optional[dict] = None,
) -> str:
    """Derive the ``vat_treatment`` from the activity × geography matrix.

    The single rule set:

    - ``OUTSIDE_EU`` → ``IVA_EXPORT``
    - ``SPAIN`` → ``IVA_ES_21``
    - ``EU_NOT_SPAIN`` + ``NEWSLETTER`` → ``OSS_EU``
    - ``EU_NOT_SPAIN`` + other activity → EU B2B reverse charge. The treatment
      may be overridden per-activity via
      ``tax.default_vat_treatment_eu_<activity>`` in ``config``; defaults to
      ``IVA_EU_B2B``.
    - anything else → ``UNKNOWN``

    ``config`` is the full app config dict (with a ``tax`` section). When
    ``None``, the EU B2B default ``IVA_EU_B2B`` is used.
    """
    geo = geo or "UNKNOWN"
    activity = activity or "UNKNOWN"

    if geo == "OUTSIDE_EU":
        return "IVA_EXPORT"
    if geo == "SPAIN":
        return "IVA_ES_21"
    if geo == "EU_NOT_SPAIN":
        if activity == "NEWSLETTER":
            return "OSS_EU"
        tax_cfg = (config or {}).get("tax", {})
        return tax_cfg.get(
            f"default_vat_treatment_eu_{activity.lower()}", "IVA_EU_B2B"
        )
    return "UNKNOWN"


def vat_base_from_inclusive(
    net: float, treatment: str, country_code: Optional[str] = None
) -> float:
    """Extract the ex-VAT taxable base from a VAT-inclusive (gross) net amount.

    Stripe amounts are VAT-inclusive (the customer paid the gross amount). For
    Spain (``IVA_ES_21``) and EU B2C (``OSS_EU``) the base is ``net / (1 + rate)``.
    For exports and EU B2B (reverse charge) no VAT was charged, so the full net
    amount is the income base.
    """
    if treatment == "IVA_ES_21":
        return round(net / (1 + IVA_ES_RATE), 2)
    if treatment == "OSS_EU":
        return round(net / (1 + oss_rate(country_code)), 2)
    # IVA_EXPORT, IVA_EU_B2B, EXEMPT, UNKNOWN — full net amount is income base
    return net


def vat_amount_on_base(
    base: float, treatment: str, country_code: Optional[str] = None
) -> float:
    """Return the VAT cuota charged on an ex-VAT base for a given treatment."""
    if treatment == "IVA_ES_21":
        return round(base * IVA_ES_RATE, 2)
    if treatment == "OSS_EU":
        return round(base * oss_rate(country_code), 2)
    return 0.0
