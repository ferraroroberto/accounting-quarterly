"""Stripe API client for fetching charges with extended metadata."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
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


def _field(obj: object, name: str) -> object:
    """Read ``name`` off a Stripe object or a plain dict (raw JSON)."""
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def fee_split_eur(balance_transaction: object) -> tuple[Optional[float], Optional[float]]:
    """(stripe part, application part) of a balance transaction's fee, in EUR.

    ``fee_details`` amounts are in the balance currency, in cents. The
    ``application_fee`` entries are what a connected platform keeps; every
    other entry (``stripe_fee``, ``tax`` on it, pass-through fees) is Stripe's
    and is already expensed from the monthly Stripe invoices. Returns
    ``(None, None)`` — split unknown, never guessed — when the balance
    transaction was not expanded or a detail is not in EUR.
    """
    details = _field(balance_transaction, "fee_details") if balance_transaction else None
    if details is None:
        return None, None
    stripe_cents = 0
    application_cents = 0
    for detail in details:
        currency = str(_field(detail, "currency") or "").lower()
        if currency != "eur":
            log.warning("⚠️ Balance transaction %s has a %s fee detail; fee split left unknown",
                        _field(balance_transaction, "id"), currency or "blank-currency")
            return None, None
        amount = int(_field(detail, "amount") or 0)
        if _field(detail, "type") == "application_fee":
            application_cents += amount
        else:
            stripe_cents += amount
    return stripe_cents / 100.0, application_cents / 100.0


def fetch_charges(
    start_date: datetime,
    end_date: datetime,
    api_key: Optional[str] = None,
    limit: int = 100,
) -> list[Payment]:
    """Fetch all paid charges from Stripe API between start_date and end_date.

    Extracts additional metadata when available:
    - card_country: issuing country of the payment card (from payment method details)
    - email: customer email, falling back to the charge's billing_details.email
      when the customer has none on file
    - billing_country: the charge's billing_details.address.country
    - balance_transaction fees, with the stripe / application split (fee_split_eur)
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
                fee_stripe_eur, fee_application_eur = fee_split_eur(charge.balance_transaction)

                email_meta = None
                if charge.customer and hasattr(charge.customer, "email"):
                    email_meta = charge.customer.email

                billing_details = getattr(charge, "billing_details", None)
                billing_email = getattr(billing_details, "email", None) if billing_details else None
                billing_address = getattr(billing_details, "address", None) if billing_details else None
                billing_country = getattr(billing_address, "country", None) if billing_address else None

                # Customer email is often blank on a raw charge; the charge's own
                # billing_details.email is filled in by Stripe Checkout/Payment
                # Element even without a Customer object.
                if not email_meta and billing_email:
                    email_meta = billing_email

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
                    fee_stripe=fee_stripe_eur,
                    fee_application=fee_application_eur,
                    currency=currency,
                    email_meta=email_meta,
                    card_country=card_country,
                    billing_country=billing_country,
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


@dataclass
class BillingBackfillResult:
    """Outcome of a raw-source billing-details backfill pass."""
    scanned: int = 0
    updated: int = 0
    email_filled: int = 0
    country_filled: int = 0
    dry_run: bool = True


def backfill_billing_details_from_raw_source(
    dry_run: bool = True,
    db_path: Optional[str | Path] = None,
) -> BillingBackfillResult:
    """Fill empty ``email_meta`` / ``billing_country`` from stored ``raw_source_json``.

    Rows fetched before the billing_details fallback existed already have the
    full Stripe charge saved as ``raw_source_json`` (see :func:`fetch_charges`),
    so this re-parses ``billing_details.email`` / ``billing_details.address.country``
    out of that saved JSON with no Stripe API call. A non-empty stored value is
    never overwritten. With ``dry_run=True`` (the default) nothing is written;
    the returned counts describe what *would* change.
    """
    # Imported lazily to avoid a module-load-time dependency between the two
    # data-access modules; src.database does not import src.stripe_client.
    from src.database import _ensure_transactions_schema, get_connection

    conn = get_connection(db_path)
    result = BillingBackfillResult(dry_run=dry_run)
    try:
        _ensure_transactions_schema(conn)
        rows = conn.execute(
            "SELECT id, email_meta, billing_country, raw_source_json FROM transactions "
            "WHERE raw_source_json IS NOT NULL "
            "AND (email_meta IS NULL OR email_meta = '' "
            "OR billing_country IS NULL OR billing_country = '')"
        ).fetchall()
        result.scanned = len(rows)

        for row in rows:
            try:
                raw = json.loads(row["raw_source_json"])
            except (TypeError, ValueError):
                continue

            billing = raw.get("billing_details") or {}
            billing_email = billing.get("email") or None
            billing_country = ((billing.get("address") or {}).get("country")) or None

            fill_email = bool(billing_email) and not row["email_meta"]
            fill_country = bool(billing_country) and not row["billing_country"]
            if not (fill_email or fill_country):
                continue

            new_email = row["email_meta"] or billing_email
            new_country = row["billing_country"] or billing_country

            if fill_email:
                result.email_filled += 1
            if fill_country:
                result.country_filled += 1
            result.updated += 1

            if not dry_run:
                conn.execute(
                    "UPDATE transactions SET email_meta = ?, billing_country = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (new_email, new_country, row["id"]),
                )

        if not dry_run:
            conn.commit()
            log.info(
                "ℹ️ Backfilled billing details: %d/%d rows updated (%d emails, %d countries)",
                result.updated, result.scanned, result.email_filled, result.country_filled,
            )
    finally:
        conn.close()
    return result


@dataclass
class FeeSplitBackfillResult:
    """Outcome of a fee-split backfill pass over a re-fetched date range."""
    fetched: int = 0
    updated: int = 0
    unchanged: int = 0
    not_stored: int = 0
    split_unknown: int = 0
    dry_run: bool = True


def backfill_fee_split(
    start_date: datetime,
    end_date: datetime,
    dry_run: bool = True,
    db_path: Optional[str | Path] = None,
    api_key: Optional[str] = None,
) -> FeeSplitBackfillResult:
    """Re-fetch ``[start_date, end_date]`` from Stripe and fill the stored fee split (#135).

    Only ``fee_stripe`` / ``fee_application`` of rows already in ``transactions``
    are written — no insert, no reclassification, no other column touched, so
    a closed quarter's amounts and classifications cannot move. A charge the
    API returns without a readable split is counted, never guessed. With
    ``dry_run=True`` (the default) nothing is written.
    """
    from src.database import _ensure_transactions_schema, get_connection

    payments = fetch_charges(start_date, end_date, api_key=api_key)
    conn = get_connection(db_path)
    result = FeeSplitBackfillResult(fetched=len(payments), dry_run=dry_run)
    try:
        _ensure_transactions_schema(conn)
        for p in payments:
            if p.fee_stripe is None or p.fee_application is None:
                result.split_unknown += 1
                continue
            row = conn.execute(
                "SELECT fee_stripe, fee_application FROM transactions WHERE id = ?", (p.id,)
            ).fetchone()
            if row is None:
                result.not_stored += 1
                continue
            if row["fee_stripe"] == p.fee_stripe and row["fee_application"] == p.fee_application:
                result.unchanged += 1
                continue
            result.updated += 1
            if not dry_run:
                conn.execute(
                    "UPDATE transactions SET fee_stripe = ?, fee_application = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (p.fee_stripe, p.fee_application, p.id),
                )
        if not dry_run:
            conn.commit()
        log.info(
            "ℹ️ Fee split backfill %s..%s: %d fetched, %d %s, %d unchanged, %d not stored, %d split unknown",
            start_date.date(), end_date.date(), result.fetched, result.updated,
            "would update" if dry_run else "updated", result.unchanged, result.not_stored,
            result.split_unknown,
        )
    finally:
        conn.close()
    return result
