"""Tests for Pydantic data models."""
import pytest

from src.models import ClassifiedPayment, MonthlyAggregation, Payment


class TestPayment:
    def test_basic_creation(self):
        p = Payment(
            id="ch_123",
            created_date="2025-03-15T10:00:00",
            converted_amount=100.0,
            converted_amount_refunded=0.0,
            description="Test payment",
            fee=3.50,
        )
        assert p.id == "ch_123"
        assert p.currency == "eur"
        assert p.net_amount == 100.0

    def test_net_amount_with_refund(self):
        p = Payment(
            id="ch_124",
            created_date="2025-03-15T10:00:00",
            converted_amount=100.0,
            converted_amount_refunded=25.0,
            description="Partial refund",
            fee=3.50,
        )
        assert p.net_amount == 75.0

    def test_currency_normalisation(self):
        p = Payment(
            id="ch_125",
            created_date="2025-03-15T10:00:00",
            converted_amount=100.0,
            converted_amount_refunded=0.0,
            description="GBP payment",
            fee=3.50,
            currency="GBP",
        )
        assert p.currency == "gbp"

    def test_quarter_calculation(self):
        p = Payment(
            id="ch_q",
            created_date="2025-07-15T10:00:00",
            converted_amount=100.0,
            converted_amount_refunded=0.0,
            description="Q3",
            fee=0,
        )
        assert p.quarter == 3
        assert p.month == 7
        assert p.year == 2025

    def test_card_country_optional(self):
        p = Payment(
            id="ch_cc",
            created_date="2025-01-01T00:00:00",
            converted_amount=10.0,
            converted_amount_refunded=0.0,
            description="test",
            fee=0,
            card_country="ES",
        )
        assert p.card_country == "ES"


class TestClassifiedPayment:
    def test_indicators_coaching_spain(self):
        cp = ClassifiedPayment(
            id="ch_c",
            created_date="2025-01-01T00:00:00",
            converted_amount=100.0,
            converted_amount_refunded=0.0,
            description="coaching",
            fee=3.0,
            activity_type="COACHING",
            geo_region="SPAIN",
        )
        assert cp.IND_COACHING == 1
        assert cp.IND_NEWSLETTER == 0
        assert cp.IND_ILLUSTRATIONS == 0
        assert cp.IND_SPAIN == 1
        assert cp.IND_OUT_SPAIN == 0
        assert cp.IND_EXEU == 0
        assert cp.IND_EU == 1

    def test_indicators_newsletter_eu(self):
        cp = ClassifiedPayment(
            id="ch_n",
            created_date="2025-01-01T00:00:00",
            converted_amount=10.0,
            converted_amount_refunded=0.0,
            description="newsletter",
            fee=0.5,
            activity_type="NEWSLETTER",
            geo_region="EU_NOT_SPAIN",
        )
        assert cp.IND_NEWSLETTER == 1
        assert cp.IND_OUT_SPAIN == 1
        assert cp.IND_EU == 1

    def test_indicators_outside_eu(self):
        cp = ClassifiedPayment(
            id="ch_o",
            created_date="2025-01-01T00:00:00",
            converted_amount=50.0,
            converted_amount_refunded=0.0,
            description="overseas",
            fee=2.0,
            activity_type="COACHING",
            geo_region="OUTSIDE_EU",
        )
        assert cp.IND_EXEU == 1
        assert cp.IND_EU == 0

    def test_activity_valid(self):
        cp = ClassifiedPayment(
            id="ch_v",
            created_date="2025-01-01T00:00:00",
            converted_amount=10.0,
            converted_amount_refunded=0.0,
            description="",
            fee=0,
            activity_type="COACHING",
            geo_region="SPAIN",
        )
        assert cp.activity_valid
        assert cp.geo_valid


class TestMonthlyAggregation:
    def test_totals(self):
        m = MonthlyAggregation(
            year=2025, month=1, geo_region="SPAIN",
            coaching_income=100.0, newsletter_income=50.0, illustrations_income=200.0,
            coaching_fee=3.0, newsletter_fee=1.5, illustrations_fee=6.0,
        )
        assert m.total_income == 350.0
        assert m.total_fee == 10.5
        assert m.month_label == "Jan 2025"
