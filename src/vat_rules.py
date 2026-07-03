"""Single source of truth for the VAT treatment decision matrix and rates.

The activity × geography → ``vat_treatment`` matrix, the OSS rate lookup, and
the VAT-inclusive base extraction live here once and are consumed by
``src.tax_engine._get_vat_treatment`` / ``_get_vat_base`` / ``_get_vat_amount``,
which derive each transaction's figures lazily from this matrix. Keeping the
rules in a single module prevents the divergence that a second, parallel copy
would reintroduce.

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
    - ``SPAIN`` → ``IVA_ES_21`` (or ``IVA_EXEMPT`` when the taxpayer is not
      IVA-registered, i.e. ``tax.vat_registered`` is false — franquicia/no
      domestic IVA charged).
    - ``EU_NOT_SPAIN`` → EU treatment, per-activity. The treatment may be
      overridden via ``tax.default_vat_treatment_eu_<activity>`` in ``config``;
      the per-activity default is ``OSS_EU`` for ``NEWSLETTER`` (B2C digital
      services) and ``IVA_EU_B2B`` (reverse charge) for everything else.
    - anything else → ``UNKNOWN``

    ``config`` is the full app config dict (with a ``tax`` section). When
    ``None``, the documented defaults are used.
    """
    geo = geo or "UNKNOWN"
    activity = activity or "UNKNOWN"
    tax_cfg = (config or {}).get("tax", {})

    if geo == "OUTSIDE_EU":
        return "IVA_EXPORT"
    if geo == "SPAIN":
        # Not IVA-registered (franquicia) → no domestic IVA is charged.
        if tax_cfg.get("vat_registered", True) is False:
            return "IVA_EXEMPT"
        return "IVA_ES_21"
    if geo == "EU_NOT_SPAIN":
        default = "OSS_EU" if activity == "NEWSLETTER" else "IVA_EU_B2B"
        return tax_cfg.get(f"default_vat_treatment_eu_{activity.lower()}", default)
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
