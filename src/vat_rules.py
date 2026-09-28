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

# EU B2C sale taxed in Spain at 21% (art. 73 LIVA): while the taxpayer's
# cross-border B2C sales of electronically supplied services stay under the
# EU-wide €10,000 threshold and they are not enrolled in OSS, the supply is
# located in Spain and carries Spanish IVA — base = gross / 1.21.
EU_B2C_ES21 = "EU_B2C_ES21"

# Treatments that carry Spanish 21% IVA devengado (Modelo 303 boxes 01/03).
SPANISH_21_TREATMENTS: frozenset[str] = frozenset({"IVA_ES_21", EU_B2C_ES21})

# Treatments that count as EU B2C distance sales for the art. 73 LIVA threshold.
EU_B2C_TREATMENTS: frozenset[str] = frozenset({EU_B2C_ES21, "OSS_EU"})

# Art. 73 LIVA: EU-wide B2C distance-sales threshold (ex-VAT, per calendar year)
# and the ratio at which the app starts warning.
EU_B2C_THRESHOLD_EUR = 10_000.0
EU_B2C_WARN_RATIO = 0.8


def is_oss_registered(config: Optional[dict] = None) -> bool:
    """Return ``tax.oss_registered`` from the app config — default **False**.

    OSS is an opt-in special regime (Modelo 035). Unless the config explicitly
    says the taxpayer is enrolled, EU B2C sales cannot be routed to an OSS
    return and are taxed in Spain instead (``EU_B2C_ES21``).
    """
    return (config or {}).get("tax", {}).get("oss_registered", False) is True


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
      the per-activity default is EU B2C for ``NEWSLETTER`` (digital services
      to consumers) and ``IVA_EU_B2B`` (reverse charge) for everything else.
      EU B2C is ``OSS_EU`` only when ``tax.oss_registered`` is true; otherwise
      it is ``EU_B2C_ES21`` (Spanish 21%, art. 73 LIVA) — an ``OSS_EU``
      setting without OSS registration is coerced to ``EU_B2C_ES21``, since
      there is no OSS return to declare it on. Not IVA-registered →
      ``IVA_EXEMPT``, mirroring Spain.
    - anything else → ``UNKNOWN``

    ``config`` is the full app config dict (with a ``tax`` section). When
    ``None``, the documented defaults are used.
    """
    geo = geo or "UNKNOWN"
    activity = activity or "UNKNOWN"
    tax_cfg = (config or {}).get("tax", {})

    vat_registered = tax_cfg.get("vat_registered", True) is not False

    if geo == "OUTSIDE_EU":
        return "IVA_EXPORT"
    if geo == "SPAIN":
        # Not IVA-registered (franquicia) → no domestic IVA is charged.
        return "IVA_ES_21" if vat_registered else "IVA_EXEMPT"
    if geo == "EU_NOT_SPAIN":
        oss_registered = is_oss_registered(config)
        eu_b2c = "OSS_EU" if oss_registered else EU_B2C_ES21
        default = eu_b2c if activity == "NEWSLETTER" else "IVA_EU_B2B"
        treatment = tax_cfg.get(f"default_vat_treatment_eu_{activity.lower()}", default)
        if treatment == "OSS_EU" and not oss_registered:
            treatment = EU_B2C_ES21
        if treatment == EU_B2C_ES21 and not vat_registered:
            return "IVA_EXEMPT"
        return treatment
    return "UNKNOWN"


def vat_base_from_inclusive(
    net: float, treatment: str, country_code: Optional[str] = None
) -> float:
    """Extract the ex-VAT taxable base from a VAT-inclusive (gross) net amount.

    Stripe amounts are VAT-inclusive (the customer paid the gross amount). For
    Spain (``IVA_ES_21``), EU B2C taxed in Spain (``EU_B2C_ES21``) and EU B2C
    under OSS (``OSS_EU``) the base is ``net / (1 + rate)``.
    For exports and EU B2B (reverse charge) no VAT was charged, so the full net
    amount is the income base.
    """
    if treatment in SPANISH_21_TREATMENTS:
        return round(net / (1 + IVA_ES_RATE), 2)
    if treatment == "OSS_EU":
        return round(net / (1 + oss_rate(country_code)), 2)
    # IVA_EXPORT, IVA_EU_B2B, EXEMPT, UNKNOWN — full net amount is income base
    return net


def vat_amount_on_base(
    base: float, treatment: str, country_code: Optional[str] = None
) -> float:
    """Return the VAT cuota charged on an ex-VAT base for a given treatment."""
    if treatment in SPANISH_21_TREATMENTS:
        return round(base * IVA_ES_RATE, 2)
    if treatment == "OSS_EU":
        return round(base * oss_rate(country_code), 2)
    return 0.0
