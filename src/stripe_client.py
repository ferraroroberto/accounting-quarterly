from __future__ import annotations

from datetime import datetime
from typing import Optional

from src.config import get_stripe_api_key
from src.exceptions import StripeAPIError
from src.logger import get_logger
from src.models import Payment

log = get_logger(__name__)


def _get_stripe():
    try:
        import stripe
        return stripe
    except ImportError as exc:
        raise StripeAPIError("stripe library not installed: pip install stripe") from exc


def fetch_charges(
    start_date: datetime,
    end_date: datetime,
    api_key: Optional[str] = None,
    limit: int = 100,
) -> list[Payment]:
    """Fetch all charges from Stripe API between start_date and end_date."""
    stripe = _get_stripe()
    stripe.api_key = api_key or get_stripe_api_key()

    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())

    payments: list[Payment] = []
    has_more = True
    starting_after = None

    while has_more:
        params = {
            "limit": limit,
            "created": {"gte": start_ts, "lte": end_ts},
            "expand": ["data.customer", "data.payment_intent"],
        }
        if starting_after:
            params["starting_after"] = starting_after

        try:
            response = stripe.Charge.list(**params)
        except stripe.error.AuthenticationError as exc:
            raise StripeAPIError(f"Stripe authentication failed: {exc}") from exc
        except stripe.error.StripeError as exc:
            raise StripeAPIError(f"Stripe API error: {exc}") from exc

        for charge in response.data:
            if not charge.paid:
                continue

            currency = charge.currency.lower() if charge.currency else "eur"
            description = charge.description or ""

            try:
                amount_eur = charge.amount / 100.0
                amount_refunded_eur = charge.amount_refunded / 100.0
                fee_eur = 0.0
                if hasattr(charge, "balance_transaction") and charge.balance_transaction:
                    bt = charge.balance_transaction
                    if hasattr(bt, "fee"):
                        fee_eur = bt.fee / 100.0

                email_meta = None
                if charge.customer and hasattr(charge.customer, "email"):
                    email_meta = charge.customer.email

                p = Payment(
                    id=charge.id,
                    created_date=datetime.fromtimestamp(charge.created),
                    converted_amount=amount_eur,
                    converted_amount_refunded=amount_refunded_eur,
                    description=description,
                    fee=fee_eur,
                    currency=currency,
                    email_meta=email_meta,
                )
                payments.append(p)
            except Exception as exc:
                log.warning("⚠️ Skipping charge %s: %s", charge.id, exc)

        has_more = response.has_more
        if has_more and response.data:
            starting_after = response.data[-1].id

    log.info("ℹ️ Fetched %d charges from Stripe API", len(payments))
    return payments


def test_connection(api_key: Optional[str] = None) -> tuple[bool, str]:
    """Verify the key can list charges (same API as fetch_charges, not Account.retrieve)."""
    try:
        stripe = _get_stripe()
        stripe.api_key = api_key or get_stripe_api_key()
        stripe.Charge.list(limit=1)
        return True, "Connected: key can list charges."
    except Exception as exc:
        return False, str(exc)
