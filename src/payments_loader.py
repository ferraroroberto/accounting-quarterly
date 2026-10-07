"""Period-based Stripe payment loading, FX conversion, and classification.

Moved out of ``app/data_loader.py`` (#166) — the CLI (``scripts/close_quarter.py``)
and ``src/close_pipeline.py``'s stripe step need this data-layer logic without
importing Streamlit via the UI package. ``app/data_loader.py`` keeps a thin
``@st.cache_data``-wrapped version of ``get_classified_for_period`` for the
Streamlit tabs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable, Optional

from src.classifier import classify_batch
from src.database import load_classified_payments, upsert_classified, upsert_payments
from src.fx_rates import convert_to_eur, init_fx_table
from src.logger import get_logger
from src.models import ClassifiedPayment, Payment
from src.periods import quarter_datetime_bounds
from src.rules_engine import load_rules
from src.stripe_client import fetch_charges

log = get_logger(__name__)

def load_payments_for_period_api(
    start_date: datetime,
    end_date: datetime,
) -> list[Payment]:
    """Load payments from Stripe API.

    This function always hits the API directly — no caching at this layer.
    """
    return fetch_charges(start_date, end_date)


def apply_fx_conversion(payments: list[Payment]) -> list[Payment]:
    """Convert non-EUR payments to EUR using stored FX rates."""
    init_fx_table()
    converted = []
    for p in payments:
        if p.currency != "eur" and p.fx_rate is None:
            tx_date = p.created_date.date()
            amount_eur, rate = convert_to_eur(p.converted_amount, p.currency, tx_date)
            refund_eur, _ = convert_to_eur(p.converted_amount_refunded, p.currency, tx_date)
            # ``fee`` comes from the balance transaction, already in the balance
            # currency (EUR): converting it again would divide it by the rate (#144).
            p = p.model_copy(update={
                "amount_original": p.converted_amount,
                "converted_amount": amount_eur,
                "converted_amount_refunded": refund_eur,
                "fx_rate": rate,
            })
        elif p.currency == "eur" and p.fx_rate is None:
            p = p.model_copy(update={"fx_rate": 1.0})
        converted.append(p)
    return converted


def classify_payments(payments: list[Payment]) -> list[ClassifiedPayment]:
    """Classify payments against the stored rules (uncached)."""
    rules = load_rules()
    classified, _ = classify_batch(payments, rules)
    return classified


def get_classified_for_period(
    year: int,
    quarter: Optional[int],
    start_date: Optional[datetime] = None,
    end_date: Optional[datetime] = None,
    *,
    input_mode: Optional[str] = None,
    classify_fn: Optional[Callable[[list[Payment]], list[ClassifiedPayment]]] = None,
) -> list[ClassifiedPayment]:
    """Fetch+classify (API mode) or read stored classifications (db mode).

    This is the CLI/pipeline entry point — no Streamlit caching happens here.
    ``classify_fn`` lets a caller swap in a cached classifier; the Streamlit
    wrapper in ``app.data_loader`` passes one backed by ``@st.cache_data``.
    Defaults to the uncached ``classify_payments``.
    """
    classify = classify_fn or classify_payments
    mode = (input_mode or "api").lower()

    if start_date is None or end_date is None:
        if quarter:
            start_date, end_date = quarter_datetime_bounds(year, quarter)
        else:
            start_date = datetime(year, 1, 1)
            end_date = datetime(year, 12, 31, 23, 59, 59)

    if mode == "db":
        # Return stored classifications directly — no re-classification needed.
        return load_classified_payments(start_date, end_date)

    # API mode: fetch fresh data from Stripe, classify, and persist.
    payments = load_payments_for_period_api(start_date, end_date)
    payments = apply_fx_conversion(payments)
    upsert_payments(payments, source="api")

    classified = classify(payments)
    # Persist classification back to DB (best-effort).
    try:
        upsert_classified(classified)
    except Exception as exc:
        # Caller can still function even if DB update fails (e.g. readonly
        # file), but a silent failure here leaves the next "db" load with
        # stale or unclassified rows, so it must be visible.
        log.warning("⚠️ Could not persist classifications: %s", exc)
    return classified
