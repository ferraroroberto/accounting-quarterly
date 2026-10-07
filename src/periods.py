"""Calendar-quarter bounds, in the three forms the codebase needs."""
from __future__ import annotations

import calendar
from datetime import date, datetime


def quarter_date_bounds(year: int, quarter: int) -> tuple[date, date]:
    """First and last day of ``year``-Q``quarter``."""
    end_month = quarter * 3
    return date(year, end_month - 2, 1), date(year, end_month, calendar.monthrange(year, end_month)[1])


def quarter_iso_bounds(year: int, quarter: int) -> tuple[str, str]:
    """Inclusive ISO ``(start, end)`` date strings for ``year``-Q``quarter``."""
    start, end = quarter_date_bounds(year, quarter)
    return start.isoformat(), end.isoformat()


def quarter_datetime_bounds(year: int, quarter: int) -> tuple[datetime, datetime]:
    """Quarter start 00:00:00 and end 23:59:59 (the Stripe transaction window)."""
    start, end = quarter_date_bounds(year, quarter)
    return (datetime(start.year, start.month, start.day),
            datetime(end.year, end.month, end.day, 23, 59, 59))
