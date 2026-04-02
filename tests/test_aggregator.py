"""Tests for the aggregation logic."""
import pytest

from src.aggregator import (
    aggregate_by_month,
    calculate_grand_totals,
    calculate_regional_totals,
    get_transaction_count,
)
from src.classifier import classify_batch


class TestAggregation:
    @pytest.fixture
    def classified_payments(self, sample_payments, sample_rules):
        classified, _ = classify_batch(sample_payments, rules=sample_rules)
        return classified

    def test_grand_totals(self, classified_payments):
        totals = calculate_grand_totals(classified_payments)
        assert totals["total_income"] > 0
        assert totals["total_fee"] > 0
        assert "coaching" in totals
        assert "newsletter" in totals
        assert "illustrations" in totals

    def test_regional_totals(self, classified_payments):
        regional = calculate_regional_totals(classified_payments)
        assert "SPAIN" in regional
        assert "EU_NOT_SPAIN" in regional
        assert "OUTSIDE_EU" in regional

    def test_transaction_count(self, classified_payments):
        counts = get_transaction_count(classified_payments)
        assert counts["total"] == 5
        assert counts.get("coaching", 0) > 0

    def test_aggregate_by_month(self, classified_payments):
        monthly = aggregate_by_month(classified_payments)
        assert len(monthly) > 0
        for agg in monthly:
            assert agg.year == 2025
            assert 1 <= agg.month <= 12

    def test_aggregate_by_month_with_geo_filter(self, classified_payments):
        spain_monthly = aggregate_by_month(classified_payments, geo_filter="SPAIN")
        for agg in spain_monthly:
            assert agg.geo_region == "SPAIN"
