"""Locale-aware numeric string parsing.

Single source for interpreting a number written in either the Spanish
(``"1.234,56"``) or the US/international (``"1,234.56"``) locale — shared by
the invoice-OCR field parser (``src/invoice_ocr.py``) and the Social Security
bank-export importer (``src/social_security.py``), which previously each had
their own, disagreeing implementation (#189).
"""
from __future__ import annotations


def parse_locale_number(val: str) -> float:
    """Parse a number the caller received as a string, in either locale.

    The last of ``.`` / ``,`` is the decimal separator and the other is a
    thousands separator (``"1.234,56"`` and ``"1,234.56"`` are both 1234.56).
    A lone separator is a decimal point, except a single ``.`` followed by
    exactly three digits (``"1.234"``), which on a Spanish document is a
    thousands separator. Raises ``ValueError`` when it is not a number.
    """
    s = val.strip().replace(" ", "")
    last_dot, last_comma = s.rfind("."), s.rfind(",")
    if last_dot >= 0 and last_comma >= 0:
        decimal = "." if last_dot > last_comma else ","
    elif last_comma >= 0:
        decimal = "," if s.count(",") == 1 else None
    elif last_dot >= 0:
        thousands_only = s.count(".") > 1 or len(s) - last_dot - 1 == 3
        decimal = None if thousands_only else "."
    else:
        decimal = None
    if decimal is None:
        s = s.replace(".", "").replace(",", "")
    else:
        thousands = "," if decimal == "." else "."
        s = s.replace(thousands, "").replace(decimal, ".")
    return float(s)
