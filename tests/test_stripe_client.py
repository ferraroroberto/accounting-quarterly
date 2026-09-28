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
from src.stripe_client import backfill_billing_details_from_raw_source, backfill_fee_split, fetch_charges


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


# ---------------------------------------------------------------------------
# Fee split: Stripe fee vs a connected platform's application fee (#135)
# ---------------------------------------------------------------------------

def _balance_txn(fee: int, details: list[tuple[str, int, str]]) -> SimpleNamespace:
    return SimpleNamespace(
        id="txn_synthetic_1", fee=fee,
        fee_details=[SimpleNamespace(type=t, amount=a, currency=c) for t, a, c in details],
    )


class TestFetchChargesFeeSplit:
    def test_application_fee_split_from_fee_details(self, patch_stripe):
        bt = _balance_txn(140, [("stripe_fee", 40, "eur"), ("application_fee", 100, "eur")])
        patch_stripe([_fake_charge(currency="eur", balance_transaction=bt)])

        p = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")[0]

        assert (p.fee, p.fee_stripe, p.fee_application) == (1.40, 0.40, 1.00)

    def test_no_application_fee_is_a_known_zero(self, patch_stripe):
        bt = _balance_txn(55, [("stripe_fee", 45, "eur"), ("tax", 10, "eur")])
        patch_stripe([_fake_charge(currency="eur", balance_transaction=bt)])

        p = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")[0]

        assert (p.fee_stripe, p.fee_application) == (0.55, 0.0)

    def test_unexpanded_balance_transaction_leaves_split_unknown(self, patch_stripe):
        patch_stripe([_fake_charge(balance_transaction="txn_synthetic_unexpanded")])

        p = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")[0]

        assert (p.fee_stripe, p.fee_application) == (None, None)

    def test_non_eur_fee_detail_is_not_guessed(self, patch_stripe):
        bt = _balance_txn(140, [("stripe_fee", 40, "usd"), ("application_fee", 100, "usd")])
        patch_stripe([_fake_charge(balance_transaction=bt)])

        p = fetch_charges(datetime(2025, 1, 1), datetime(2025, 1, 31), api_key="sk_test_fake")[0]

        assert (p.fee_stripe, p.fee_application) == (None, None)


def _split_row(db_path, pid: str) -> tuple:
    from src.database import get_connection

    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT fee_stripe, fee_application FROM transactions WHERE id = ?", (pid,)).fetchone()
        return tuple(row)
    finally:
        conn.close()


class TestUpsertFeeSplit:
    def test_insert_stores_the_split(self, tmp_db):
        init_db(tmp_db)
        upsert_payments([_stored_payment("ch_f1", None, fee_stripe=0.4, fee_application=1.0)], db_path=tmp_db)
        assert _split_row(tmp_db, "ch_f1") == (0.4, 1.0)

    def test_refetch_fills_a_legacy_row(self, tmp_db):
        init_db(tmp_db)
        upsert_payments([_stored_payment("ch_f2", None)], db_path=tmp_db)
        assert _split_row(tmp_db, "ch_f2") == (None, None)

        _, updated = upsert_payments([_stored_payment("ch_f2", None, fee_stripe=0.4, fee_application=1.0)],
                                     db_path=tmp_db)

        assert updated == 1
        assert _split_row(tmp_db, "ch_f2") == (0.4, 1.0)

    def test_unknown_split_never_overwrites_a_known_one(self, tmp_db):
        init_db(tmp_db)
        upsert_payments([_stored_payment("ch_f3", None, fee_stripe=0.4, fee_application=1.0)], db_path=tmp_db)
        changed = _stored_payment("ch_f3", None).model_copy(update={"description": "changed"})
        upsert_payments([changed], db_path=tmp_db)
        assert _split_row(tmp_db, "ch_f3") == (0.4, 1.0)


class TestBackfillFeeSplit:
    @pytest.fixture
    def refetch(self, monkeypatch):
        def _set(payments: list[Payment]) -> None:
            monkeypatch.setattr("src.stripe_client.fetch_charges", lambda *a, **k: payments)
        return _set

    def test_fills_only_the_split_of_stored_rows(self, tmp_db, refetch):
        init_db(tmp_db)
        upsert_payments([_stored_payment("ch_b1", None, email_meta="kept@example.com")], db_path=tmp_db)
        refetch([
            _stored_payment("ch_b1", None, fee_stripe=0.4, fee_application=1.0).model_copy(
                update={"description": "refetched"}),
            _stored_payment("ch_b2", None, fee_stripe=0.3, fee_application=0.0),     # not stored
            _stored_payment("ch_b3", None),                                           # split unreadable
        ])

        result = backfill_fee_split(datetime(2025, 1, 1), datetime(2025, 3, 31), dry_run=False, db_path=tmp_db)

        assert (result.fetched, result.updated, result.not_stored, result.split_unknown) == (3, 1, 1, 1)
        assert _split_row(tmp_db, "ch_b1") == (0.4, 1.0)
        stored = load_classified_payments(db_path=tmp_db)
        assert [p.id for p in stored] == ["ch_b1"]                      # nothing inserted
        assert (stored[0].description, stored[0].email_meta) == ("", "kept@example.com")   # untouched

    def test_dry_run_writes_nothing(self, tmp_db, refetch):
        init_db(tmp_db)
        upsert_payments([_stored_payment("ch_b4", None)], db_path=tmp_db)
        refetch([_stored_payment("ch_b4", None, fee_stripe=0.4, fee_application=1.0)])

        result = backfill_fee_split(datetime(2025, 1, 1), datetime(2025, 3, 31), dry_run=True, db_path=tmp_db)

        assert result.updated == 1
        assert _split_row(tmp_db, "ch_b4") == (None, None)
