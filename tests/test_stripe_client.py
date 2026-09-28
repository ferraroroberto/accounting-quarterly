"""Tests for the Stripe fetch billing-details fallback (issue #112) and the
raw_source backfill for already-stored rows.

All data is synthetic (example.* emails); no network calls — the ``stripe``
module is stubbed out with plain ``SimpleNamespace`` objects that mimic the
attributes ``fetch_charges`` reads off a real ``stripe.Charge``.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from src.database import init_db, load_classified_payments, upsert_payments
from src.models import Payment
from src.stripe_client import backfill_billing_details_from_raw_source, fetch_charges


# ---------------------------------------------------------------------------
# fetch_charges: billing_details fallback
# ---------------------------------------------------------------------------

class _FakeStripeError(Exception):
    pass


class _FakeErrorModule:
    PermissionError = _FakeStripeError
    AuthenticationError = _FakeStripeError
    StripeError = _FakeStripeError


def _fake_charge(**overrides) -> SimpleNamespace:
    defaults = dict(
        id="ch_synthetic_1",
        paid=True,
        currency="usd",
        description="",
        amount=1000,
        amount_refunded=0,
        balance_transaction=None,
        customer=None,
        payment_method_details=None,
        billing_details=None,
        payment_intent=None,
        invoice=None,
        created=1_700_000_000,
    )
    defaults.update(overrides)
    charge = SimpleNamespace(**defaults)
    charge.to_dict_recursive = lambda: {"id": charge.id}
    return charge


def _fake_stripe_module(charges: list[SimpleNamespace]) -> SimpleNamespace:
    response = SimpleNamespace(data=charges, has_more=False)

    class _Charge:
        @staticmethod
        def list(**kwargs):
            return response

    return SimpleNamespace(Charge=_Charge, error=_FakeErrorModule, api_key=None)


@pytest.fixture
def patch_stripe(monkeypatch):
    def _patch(charges: list[SimpleNamespace]):
        fake = _fake_stripe_module(charges)
        monkeypatch.setattr("src.stripe_client._get_stripe", lambda: fake)
        return fake

    return _patch


class TestFetchChargesBillingFallback:
    def test_email_falls_back_to_billing_details_when_customer_has_none(self, patch_stripe):
        charge = _fake_charge(
            customer=None,
            billing_details=SimpleNamespace(email="a@example.com", address=None),
        )
        patch_stripe([charge])

        payments = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")

        assert len(payments) == 1
        assert payments[0].email_meta == "a@example.com"

    def test_customer_email_takes_priority_over_billing_details(self, patch_stripe):
        charge = _fake_charge(
            customer=SimpleNamespace(email="customer@example.com", id="cus_1"),
            billing_details=SimpleNamespace(email="other@example.com", address=None),
        )
        patch_stripe([charge])

        payments = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")

        assert payments[0].email_meta == "customer@example.com"

    def test_billing_country_captured_from_billing_details_address(self, patch_stripe):
        charge = _fake_charge(
            billing_details=SimpleNamespace(
                email=None, address=SimpleNamespace(country="DE")
            ),
        )
        patch_stripe([charge])

        payments = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")

        assert payments[0].billing_country == "DE"

    def test_no_billing_details_leaves_fields_empty(self, patch_stripe):
        charge = _fake_charge(customer=None, billing_details=None)
        patch_stripe([charge])

        payments = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")

        assert payments[0].email_meta is None
        assert payments[0].billing_country is None

    def test_unpaid_charge_skipped(self, patch_stripe):
        charge = _fake_charge(paid=False)
        patch_stripe([charge])

        payments = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")

        assert payments == []


# ---------------------------------------------------------------------------
# backfill_billing_details_from_raw_source: fill stored rows from raw_source
# ---------------------------------------------------------------------------

def _stored_payment(pid: str, raw_source: dict | None, **kw) -> Payment:
    return Payment(
        id=pid,
        created_date="2025-01-15T10:00:00",
        converted_amount=100.0,
        converted_amount_refunded=0.0,
        description="",
        fee=0.0,
        currency="eur",
        raw_source=raw_source,
        raw_source_type="stripe_api" if raw_source else None,
        **kw,
    )


class TestBackfillFromRawSource:
    def test_dry_run_reports_but_does_not_write(self, tmp_db):
        init_db(tmp_db)
        upsert_payments(
            [_stored_payment(
                "ch_1",
                raw_source={"billing_details": {"email": "legacy@example.com",
                                                 "address": {"country": "FR"}}},
            )],
            db_path=tmp_db,
        )

        result = backfill_billing_details_from_raw_source(dry_run=True, db_path=tmp_db)

        assert result.scanned == 1
        assert result.updated == 1
        assert result.email_filled == 1
        assert result.country_filled == 1

        reloaded = load_classified_payments(db_path=tmp_db)[0]
        assert reloaded.email_meta is None
        assert reloaded.billing_country is None

    def test_writes_when_not_dry_run(self, tmp_db):
        init_db(tmp_db)
        upsert_payments(
            [_stored_payment(
                "ch_2",
                raw_source={"billing_details": {"email": "legacy2@example.com",
                                                 "address": {"country": "IT"}}},
            )],
            db_path=tmp_db,
        )

        result = backfill_billing_details_from_raw_source(dry_run=False, db_path=tmp_db)
        assert result.updated == 1

        reloaded = load_classified_payments(db_path=tmp_db)[0]
        assert reloaded.email_meta == "legacy2@example.com"
        assert reloaded.billing_country == "IT"

    def test_never_overwrites_non_empty_value(self, tmp_db):
        init_db(tmp_db)
        upsert_payments(
            [_stored_payment(
                "ch_3",
                raw_source={"billing_details": {"email": "raw@example.com",
                                                 "address": {"country": "PT"}}},
                email_meta="already-set@example.com",
            )],
            db_path=tmp_db,
        )

        result = backfill_billing_details_from_raw_source(dry_run=False, db_path=tmp_db)
        # Email already present -> untouched; only the empty country gets filled.
        assert result.email_filled == 0
        assert result.country_filled == 1

        reloaded = load_classified_payments(db_path=tmp_db)[0]
        assert reloaded.email_meta == "already-set@example.com"
        assert reloaded.billing_country == "PT"

    def test_row_without_raw_source_is_skipped(self, tmp_db):
        init_db(tmp_db)
        upsert_payments([_stored_payment("ch_4", raw_source=None)], db_path=tmp_db)

        result = backfill_billing_details_from_raw_source(dry_run=True, db_path=tmp_db)

        assert result.scanned == 0
        assert result.updated == 0

    def test_row_already_complete_is_not_counted(self, tmp_db):
        init_db(tmp_db)
        upsert_payments(
            [_stored_payment(
                "ch_5",
                raw_source={"billing_details": {"email": "raw5@example.com",
                                                 "address": {"country": "NL"}}},
                email_meta="have@example.com",
                billing_country="NL",
            )],
            db_path=tmp_db,
        )

        result = backfill_billing_details_from_raw_source(dry_run=True, db_path=tmp_db)

        assert result.scanned == 0
        assert result.updated == 0
