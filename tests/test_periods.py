"""Quarter bounds helpers (the single implementation behind the tax, dedupe, close and UI code)."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from src.periods import quarter_date_bounds, quarter_datetime_bounds, quarter_iso_bounds


@pytest.mark.parametrize("year, quarter, start, end", [
    (2026, 1, "2026-01-01", "2026-03-31"),
    (2026, 2, "2026-04-01", "2026-06-30"),
    (2026, 3, "2026-07-01", "2026-09-30"),
    (2025, 4, "2025-10-01", "2025-12-31"),
    (2024, 1, "2024-01-01", "2024-03-31"),
])
def test_bounds_agree_across_the_three_forms(year, quarter, start, end):
    assert quarter_iso_bounds(year, quarter) == (start, end)
    assert quarter_date_bounds(year, quarter) == (date.fromisoformat(start), date.fromisoformat(end))
    assert quarter_datetime_bounds(year, quarter) == (
        datetime.fromisoformat(start), datetime.fromisoformat(end).replace(hour=23, minute=59, second=59))
