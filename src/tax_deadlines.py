"""Filing and direct-debit deadlines of the Spanish returns — the one source of truth.

Both the Filing sheet (``src/filing_sheet.py``) and the Obligations calendar
(``tax_engine.get_tax_calendar``) take their dates from here, so the two never
disagree about when a return is due.

Deadlines (``filing_deadline``) follow the AEAT taxpayer calendar:

- Modelo 303 / 130 / 349, quarters 1–3: 1st–20th of the month after the
  quarter; 4th quarter: 1–30 January. A deadline on a Saturday, Sunday or
  holiday moves to the next business day.
- Direct debit (domiciliación, 303 and 130 only — the 349 has no payment):
  Orden HAC/241/2025 (BOE-A-2025-5048, amending art. 3 of Orden
  EHA/1658/2009) requires at least three business days **or** five calendar
  days between the end of the direct-debit period and the end of the filing
  period. That is the 15th for the 20th, and 27 January for 30 January 2026
  (as published in the AEAT "Plazos de presentación de autoliquidaciones con
  domiciliación bancaria", calendario del contribuyente 2026); a date that
  would fall on a non-business day is moved back to the previous business day
  (conservative — pay earlier rather than miss the window). Business days
  exclude weekends, national holidays and the holidays of Madrid (where the
  AEAT IT department sits); only the national ones plus Maundy Thursday and
  Good Friday are built in — pass ``extra_holidays`` for any other, and
  check the AEAT calendar each period.
- ``calendar_deadline`` is the last filing day of any model on the Obligations
  calendar: the quarterly three as above, plus the OSS return and the annual
  390 / 347, each moved to the next business day the same way.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Iterable, Optional

SHEET_MODELS = ("303", "130", "349")
PAYMENT_MODELS = ("303", "130")          # the 349 is informative: no payment, no direct debit


def _easter_sunday(year: int) -> date:
    """Gregorian Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741 — the algorithm's own name
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


@lru_cache(maxsize=None)
def spanish_holidays(year: int) -> frozenset[date]:
    """National holidays plus Maundy Thursday (a Madrid holiday) and Good Friday."""
    fixed = {(1, 1), (1, 6), (5, 1), (8, 15), (10, 12), (11, 1), (12, 6), (12, 8), (12, 25)}
    easter = _easter_sunday(year)
    return frozenset({date(year, m, d) for m, d in fixed} | {easter - timedelta(days=3), easter - timedelta(days=2)})


def _is_business_day(d: date, extra: frozenset[date]) -> bool:
    return d.weekday() < 5 and d not in spanish_holidays(d.year) and d not in extra


def _next_business_day(d: date, extra: frozenset[date]) -> date:
    while not _is_business_day(d, extra):
        d += timedelta(days=1)
    return d


@dataclass(frozen=True)
class FilingDeadline:
    """When a quarterly return must be filed (and paid by direct debit)."""
    model: str
    year: int
    quarter: int
    nominal: date                          # 20th of the next month / 30 January
    last_day: date                         # nominal moved to the next business day
    direct_debit_last_day: Optional[date]  # None when the model has no payment (349)


def filing_deadline(model: str, year: int, quarter: int,
                    extra_holidays: Iterable[date] = ()) -> FilingDeadline:
    """Deadlines of a quarterly 303 / 130 / 349 (see the module docstring for the rules)."""
    if model not in SHEET_MODELS or quarter not in (1, 2, 3, 4):
        raise ValueError(f"no quarterly deadline for Modelo {model} Q{quarter}")
    extra = frozenset(extra_holidays)
    nominal = date(year + 1, 1, 30) if quarter == 4 else date(year, 3 * quarter + 1, 20)
    last = _next_business_day(nominal, extra)
    debit: Optional[date] = None
    if model in PAYMENT_MODELS:
        by_calendar = last - timedelta(days=5)
        by_business, seen = last, 0          # latest day with 3 business days after it
        while seen < 3:
            if _is_business_day(by_business, extra):
                seen += 1
            by_business -= timedelta(days=1)
        # The two minimums are alternatives: the later date satisfies one of them.
        # A non-business result is moved back to a business day (conservative).
        debit = max(by_calendar, by_business)
        while not _is_business_day(debit, extra):
            debit -= timedelta(days=1)
    return FilingDeadline(model, year, quarter, nominal, last, debit)


def calendar_deadline(model: str, year: int, quarter: int = 1,
                      extra_holidays: Iterable[date] = ()) -> date:
    """Last filing day of ``model`` for the Obligations calendar, moved to a business day.

    ``quarter`` is ignored by the annual 390 / 347.
    """
    extra = frozenset(extra_holidays)
    if model in SHEET_MODELS:
        return filing_deadline(model, year, quarter, extra).last_day
    if model == "OSS":                       # last day of the month after the quarter
        nominal = {1: date(year, 4, 30), 2: date(year, 7, 31),
                   3: date(year, 10, 31), 4: date(year + 1, 1, 31)}[quarter]
    elif model == "390":
        nominal = date(year + 1, 1, 30)
    elif model == "347":
        nominal = date(year + 1, 2, 28)
    else:
        raise ValueError(f"no calendar deadline for Modelo {model}")
    return _next_business_day(nominal, extra)
