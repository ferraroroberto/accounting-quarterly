"""Invoice Explorer: an untouched range filter keeps invoices with no date or subtotal."""
from __future__ import annotations

from datetime import date

import pandas as pd

from app.views.invoice_explorer import _apply_filters, _Filters


def _frame() -> pd.DataFrame:
    return pd.DataFrame({
        "direction": ["in", "in", "out"],
        "category": ["a", "a", "b"],
        "invoice_date": pd.to_datetime(["2025-01-10", None, "2025-06-10"]),
        "subtotal_eur": [100.0, None, 500.0],
    })


def _filters(**kw) -> _Filters:
    base = dict(
        direction="All", categories=[], invoice_types=[], vendor_query="", client_query="",
        date_from=date(2025, 1, 10), date_to=date(2025, 6, 10),
        subtotal_range=(100.0, 500.0), rectificativas_only=False,
    )
    return _Filters(**{**base, **kw})


def test_untouched_ranges_keep_rows_missing_date_and_subtotal():
    assert len(_apply_filters(_frame(), _filters())) == 3


def test_narrowed_subtotal_range_filters():
    out = _apply_filters(_frame(), _filters(subtotal_range=(100.0, 200.0)))
    assert out["subtotal_eur"].tolist() == [100.0]


def test_narrowed_date_range_filters():
    out = _apply_filters(_frame(), _filters(date_to=date(2025, 3, 1)))
    assert out["subtotal_eur"].tolist() == [100.0]
