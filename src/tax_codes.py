"""Spanish tax codes and derivations shared by the database, classifier, vendor registry and engine.

EU country / VAT prefixes, the Spanish NIF pattern, the NIF → geography and VAT-treatment derivations,
the per-invoice ``tax_treatment`` values and the legacy ``vat_treatment`` mapping. Pure functions and
constants (no I/O, no storage), so any layer can import them without reaching into ``src.database``.
"""
from __future__ import annotations

import re
from typing import Optional

# ---------------------------------------------------------------------------
# Geographic / VAT classification helpers for invoices
# ---------------------------------------------------------------------------

# ISO 2-letter codes of the EU member states, Spain excluded (the taxpayer's home country).
EU_COUNTRY_CODES: frozenset[str] = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "FI", "FR",
    "GR", "HR", "HU", "IE", "IT", "LT", "LU", "LV", "MT", "NL",
    "PL", "PT", "RO", "SE", "SI", "SK",
})
# EU VAT-id prefixes: the country codes, plus "EL" — the prefix Greek VAT ids actually use.
EU_VAT_PREFIXES: frozenset[str] = EU_COUNTRY_CODES | {"EL"}

# Matches Spanish CIF (B12345678), DNI (12345678A), NIE (X1234567A), optionally ES-prefixed
_SPANISH_NIF_RE = re.compile(
    r"^(ES)?"
    r"([A-HJ-NP-SUVW]\d{7}[0-9A-J]"   # CIF (companies / entities)
    r"|\d{8}[A-Z]"                       # DNI (individuals)
    r"|[XYZ]\d{7}[A-Z])$",              # NIE (foreign residents)
    re.IGNORECASE,
)


def derive_geo_region_from_nif(nif: str | None) -> str:
    """Return SPAIN / EU_NOT_SPAIN / OUTSIDE_EU / UNKNOWN from a NIF or VAT number."""
    if not nif or not nif.strip():
        return "UNKNOWN"
    n = nif.strip().upper()
    # Explicit ES prefix or matches Spanish NIF/CIF/DNI/NIE pattern
    if n.startswith("ES") or _SPANISH_NIF_RE.match(n):
        return "SPAIN"
    # EU VAT number: starts with a 2-letter EU country prefix
    if len(n) >= 4 and n[:2] in EU_VAT_PREFIXES:
        return "EU_NOT_SPAIN"
    # Anything else (US EIN, no NIF at all after stripping, etc.)
    return "OUTSIDE_EU"


def derive_vat_treatment_for_invoice(
    direction: str,
    geo_region: str,
    iva_amount: float | None,
) -> str:
    """Infer vat_treatment for an invoice row given direction + geo + IVA presence.

    direction='in'  (expense): what IVA regime applies to our input VAT
    direction='out' (income):  what regime applies to our output VAT
    """
    has_iva = bool(iva_amount and iva_amount > 0)
    if direction == "in":
        if has_iva:
            return "IVA_ES_21"          # Spanish VAT charged → deductible soportado
        if geo_region == "EU_NOT_SPAIN":
            return "IVA_EU_B2B"         # reverse charge — no box_28 impact
        return "IVA_EXEMPT"             # outside EU or exempt — no IVA
    else:  # direction == 'out'
        if geo_region == "SPAIN":
            return "IVA_ES_21" if has_iva else "IVA_EXEMPT"
        if geo_region == "EU_NOT_SPAIN":
            return "IVA_EU_B2B"         # intracom B2B (ISP) — box_59
        if geo_region == "OUTSIDE_EU":
            return "IVA_EXPORT"         # export exemption — Art. 21 LIVA
        return "IVA_EXEMPT"


# ---------------------------------------------------------------------------
# Invoice ledger: tax treatment, exclusion reasons, locks (issue #90)
# ---------------------------------------------------------------------------

# Per-invoice tax treatment. Expense (direction='in') and income ('out') use
# disjoint value sets so a treatment always implies its direction.
TAX_TREATMENTS_IN: tuple[str, ...] = (
    "DOMESTIC",           # Spanish VAT charged by the vendor → deductible input VAT
    "DOMESTIC_CAPITAL",   # as DOMESTIC, but a capital good (303 boxes 30/31)
    "INTRA_EU_RC",        # intra-EU acquisition, reverse charge (boxes 10/11 + 36/37)
    "NON_EU_RC",          # non-EU service, reverse charge (boxes 12/13 + 28/29)
    "NO_VAT",             # no VAT involved (bank fees, exempt supplies, …)
    "NOT_DEDUCTIBLE",     # VAT charged but not deductible
)
TAX_TREATMENTS_OUT: tuple[str, ...] = (
    "ES_21",              # Spanish 21% to a Spanish client
    "EU_B2C_ES21",        # EU consumer charged Spanish 21% (no OSS)
    "EU_B2B",             # intra-EU B2B service, reverse charge at the client (box 59)
    "NON_EU_NOT_SUBJECT", # non-EU client, not subject by location rules
    "EXEMPT_TEACHING",    # exempt teaching (art. 20.1.9º LIVA)
)
EXCLUDED_REASONS: tuple[str, ...] = (
    "duplicate", "receipt", "personal", "other_period", "superseded",
)

# Legacy `vat_treatment` (+ geo_region for the ambiguous IVA_EXEMPT bucket) →
# `tax_treatment`. The mapping preserves what the tax engine did with the
# legacy value: VAT-charged expenses stay deductible, zero-VAT rows stay
# VAT-neutral. `None` means "cannot be decided from the legacy data — review".
#   in  IVA_ES_21                    → DOMESTIC
#   in  IVA_EU_B2B                   → INTRA_EU_RC
#   in  IVA_EXEMPT + OUTSIDE_EU      → NON_EU_RC
#   in  IVA_EXEMPT + other geo / any other legacy value → NO_VAT
#   out IVA_ES_21                    → ES_21
#   out OSS_EU                       → EU_B2C_ES21
#   out IVA_EU_B2B                   → EU_B2B
#   out IVA_EXPORT                   → NON_EU_NOT_SUBJECT
#   out IVA_EXEMPT + SPAIN           → EXEMPT_TEACHING
#   out IVA_EXEMPT + other geo       → None (review)
_LEGACY_TO_TAX_TREATMENT: dict[tuple[str, str], str] = {
    ("in", "IVA_ES_21"): "DOMESTIC",
    ("in", "IVA_EU_B2B"): "INTRA_EU_RC",
    ("out", "IVA_ES_21"): "ES_21",
    ("out", "OSS_EU"): "EU_B2C_ES21",
    ("out", "IVA_EU_B2B"): "EU_B2B",
    ("out", "IVA_EXPORT"): "NON_EU_NOT_SUBJECT",
}

# Reverse direction: keeps the legacy `vat_treatment` column (still read by the
# engine until the #97 box model lands) consistent when `tax_treatment` is edited.
_TAX_TREATMENT_TO_LEGACY: dict[str, str] = {
    "DOMESTIC": "IVA_ES_21",
    "DOMESTIC_CAPITAL": "IVA_ES_21",
    "INTRA_EU_RC": "IVA_EU_B2B",
    "NON_EU_RC": "IVA_EXEMPT",
    "NO_VAT": "IVA_EXEMPT",
    "NOT_DEDUCTIBLE": "IVA_EXEMPT",
    "ES_21": "IVA_ES_21",
    "EU_B2C_ES21": "IVA_ES_21",
    "EU_B2B": "IVA_EU_B2B",
    "NON_EU_NOT_SUBJECT": "IVA_EXPORT",
    "EXEMPT_TEACHING": "IVA_EXEMPT",
}

_VAT_ID_SEPARATORS_RE = re.compile(r"[\s.\-/_]")


def normalize_vat_id(raw: Optional[str]) -> Optional[str]:
    """Canonical VAT id for matching: upper-case, separators stripped, Spanish ids ES-prefixed.

    ``"es-b12.345.678"`` and ``"B12345678"`` both become ``"ESB12345678"``; other
    ids keep whatever country prefix they carry. Returns ``None`` for blank input.
    """
    if not raw or not raw.strip():
        return None
    n = _VAT_ID_SEPARATORS_RE.sub("", raw.strip().upper())
    if not n:
        return None
    if not n.startswith("ES") and _SPANISH_NIF_RE.match(n):
        n = "ES" + n
    return n


def derive_tax_treatment_for_invoice(
    direction: str,
    vat_treatment: Optional[str],
    geo_region: Optional[str],
    iva_amount: Optional[float] = None,
) -> Optional[str]:
    """Map the legacy ``vat_treatment`` (derived first when missing) to a ``tax_treatment``.

    See the mapping table above ``_LEGACY_TO_TAX_TREATMENT``. Returns ``None``
    when the legacy data cannot decide (an income invoice without VAT to a
    non-Spanish or unknown counterparty).
    """
    geo = geo_region or "UNKNOWN"
    legacy = vat_treatment or derive_vat_treatment_for_invoice(direction, geo, iva_amount)
    mapped = _LEGACY_TO_TAX_TREATMENT.get((direction, legacy))
    if mapped:
        return mapped
    if direction == "in":
        return "NON_EU_RC" if (legacy == "IVA_EXEMPT" and geo == "OUTSIDE_EU") else "NO_VAT"
    if legacy == "IVA_EXEMPT" and geo == "SPAIN":
        return "EXEMPT_TEACHING"
    return None


def legacy_vat_treatment_for(tax_treatment: str) -> Optional[str]:
    """Legacy ``vat_treatment`` equivalent of a ``tax_treatment`` (None if unknown)."""
    return _TAX_TREATMENT_TO_LEGACY.get(tax_treatment)
