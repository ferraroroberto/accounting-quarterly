"""Stripe API client for fetching charges with extended metadata."""
from __future__ import annotations

import json
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
    """Fetch all paid charges from Stripe API between start_date and end_date.

    Extracts additional metadata when available:
    - card_country: issuing country of the payment card (from payment method details)
    - email: customer email
    - balance_transaction fees
    """
    stripe = _get_stripe()
    stripe.api_key = api_key or get_stripe_api_key()

    start_ts = int(start_date.timestamp())
    end_ts = int(end_date.timestamp())

    payments: list[Payment] = []
    has_more = True
    starting_after = None

    while has_more:
        expand = ["data.customer", "data.balance_transaction"]
        params = {
            "limit": limit,
            "created": {"gte": start_ts, "lte": end_ts},
            "expand": expand,
        }
        if starting_after:
            params["starting_after"] = starting_after

        try:
            response = stripe.Charge.list(**params)
        except stripe.error.PermissionError as exc:
            # Restricted keys may not have access to expand customer / balance transaction.
            # Retry with reduced expansions so we can still load core charge data.
            msg = str(exc)
            if "customer" in msg.lower() and "data.customer" in expand:
                log.warning("⚠️ Stripe key lacks customer read permission; retrying without customer expand.")
                expand = [e for e in expand if e != "data.customer"]
                params["expand"] = expand
                response = stripe.Charge.list(**params)
            elif "balance" in msg.lower() and "data.balance_transaction" in expand:
                log.warning("⚠️ Stripe key lacks balance transaction permission; retrying without balance_transaction expand.")
                expand = [e for e in expand if e != "data.balance_transaction"]
                params["expand"] = expand
                response = stripe.Charge.list(**params)
            else:
                raise StripeAPIError(f"Stripe permission error: {exc}") from exc
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
                if charge.balance_transaction and hasattr(charge.balance_transaction, "fee"):
                    fee_eur = charge.balance_transaction.fee / 100.0

                email_meta = None
                if charge.customer and hasattr(charge.customer, "email"):
                    email_meta = charge.customer.email

                card_country = None
                pmd = getattr(charge, "payment_method_details", None)
                if pmd:
                    card = getattr(pmd, "card", None)
                    if card:
                        card_country = getattr(card, "country", None)

                # Traceability: keep important IDs and a raw charge snapshot.
                customer_id = getattr(charge, "customer", None)
                if hasattr(customer_id, "id"):
                    customer_id = customer_id.id
                payment_intent_id = getattr(charge, "payment_intent", None)
                if hasattr(payment_intent_id, "id"):
                    payment_intent_id = payment_intent_id.id
                balance_txn_id = getattr(charge, "balance_transaction", None)
                if hasattr(balance_txn_id, "id"):
                    balance_txn_id = balance_txn_id.id
                invoice_id = getattr(charge, "invoice", None)
                if hasattr(invoice_id, "id"):
                    invoice_id = invoice_id.id

                raw_charge = None
                try:
                    raw_charge = charge.to_dict_recursive()
                except Exception:
                    try:
                        raw_charge = json.loads(str(charge))
                    except Exception:
                        raw_charge = {"id": charge.id}

                p = Payment(
                    id=charge.id,
                    created_date=datetime.fromtimestamp(charge.created),
                    converted_amount=amount_eur,
                    converted_amount_refunded=amount_refunded_eur,
                    description=description,
                    fee=fee_eur,
                    currency=currency,
                    email_meta=email_meta,
                    card_country=card_country,
                    stripe_customer_id=customer_id,
                    stripe_payment_intent_id=payment_intent_id,
                    stripe_balance_transaction_id=balance_txn_id,
                    stripe_invoice_id=invoice_id,
                    raw_source=raw_charge,
                    raw_source_type="stripe_api",
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
    """Verify the key can list charges."""
    try:
        stripe = _get_stripe()
        stripe.api_key = api_key or get_stripe_api_key()
        stripe.Charge.list(limit=1)
        return True, "Connected: key can list charges."
    except Exception as exc:
        return False, str(exc)


def check_permissions(api_key: Optional[str] = None) -> dict[str, bool]:
    """Check which Stripe permissions are available with the current key.

    Tests read access for: charges, balance_transactions, customers.
    These cover the permissions needed: read transactions and read fees.
    """
    stripe = _get_stripe()
    stripe.api_key = api_key or get_stripe_api_key()

    permissions = {}
    for resource_name, test_fn in [
        ("charges", lambda: stripe.Charge.list(limit=1)),
        ("balance_transactions", lambda: stripe.BalanceTransaction.list(limit=1)),
        ("customers", lambda: stripe.Customer.list(limit=1)),
    ]:
        try:
            test_fn()
            permissions[resource_name] = True
        except Exception:
            permissions[resource_name] = False

    return permissions


def fetch_charge_with_card_country(charge_id: str,
                                    api_key: Optional[str] = None) -> Optional[str]:
    """Fetch the card issuing country for a specific charge.

    Returns ISO 3166-1 alpha-2 country code (e.g., 'ES', 'DE', 'US') or None.
    The card country is available in charge.payment_method_details.card.country
    when the payment was made with a card. This requires 'Read charges' permission.
    """
    try:
        stripe = _get_stripe()
        stripe.api_key = api_key or get_stripe_api_key()
        charge = stripe.Charge.retrieve(charge_id)
        pmd = getattr(charge, "payment_method_details", None)
        if pmd:
            card = getattr(pmd, "card", None)
            if card:
                return getattr(card, "country", None)
        return None
    except Exception as exc:
        log.warning("⚠️ Could not fetch card country for %s: %s", charge_id, exc)
        return None
