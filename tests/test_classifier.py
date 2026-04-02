"""Tests for the classification engine."""
import pytest

from src.classifier import classify_activity, classify_batch, classify_geography, classify_payment
from src.models import Payment


class TestClassifyActivity:
    def test_empty_description_defaults_to_coaching(self, sample_rules):
        activity, rule = classify_activity("", rules=sample_rules)
        assert activity == "COACHING"
        assert rule == "empty_description_default"

    def test_none_description_defaults_to_coaching(self, sample_rules):
        activity, rule = classify_activity(None, rules=sample_rules)
        assert activity == "COACHING"

    def test_luma_registration(self, sample_rules):
        activity, rule = classify_activity("Some event", payment_type_meta="registration", rules=sample_rules)
        assert activity == "COACHING"
        assert "luma_registration" in rule

    def test_illustrations_keyword(self, sample_rules):
        activity, rule = classify_activity("Charge for illustration project", rules=sample_rules)
        assert activity == "ILLUSTRATIONS"
        assert "charge for" in rule

    def test_newsletter_keyword(self, sample_rules):
        activity, rule = classify_activity("Subscription creation", rules=sample_rules)
        assert activity == "NEWSLETTER"
        assert "subscription" in rule

    def test_coaching_keyword_calendly(self, sample_rules):
        activity, rule = classify_activity("Calendly booking", rules=sample_rules)
        assert activity == "COACHING"
        assert "calendly" in rule

    def test_coaching_keyword_consulting(self, sample_rules):
        activity, rule = classify_activity("Consulting session", rules=sample_rules)
        assert activity == "COACHING"

    def test_unknown_no_match(self, sample_rules):
        activity, rule = classify_activity("Random payment description", rules=sample_rules)
        assert activity == "UNKNOWN"
        assert rule == "no_pattern_matched"

    def test_case_insensitive(self, sample_rules):
        activity, _ = classify_activity("CALENDLY Session", rules=sample_rules)
        assert activity == "COACHING"

    def test_priority_order_illustrations_before_newsletter(self, sample_rules):
        """If description contains both 'charge for' and 'subscription',
        illustrations should win (priority 3 < 4)."""
        activity, _ = classify_activity("charge for subscription art", rules=sample_rules)
        assert activity == "ILLUSTRATIONS"


class TestClassifyGeography:
    def test_non_eur_defaults_to_outside_eu(self, sample_rules, sample_payment):
        sample_payment.currency = "gbp"
        geo, rule = classify_geography(sample_payment, rules=sample_rules)
        assert geo == "OUTSIDE_EU"
        assert "non_eur_currency" in rule

    def test_eur_defaults_to_spain(self, sample_rules, sample_payment):
        geo, rule = classify_geography(sample_payment, rules=sample_rules, activity_type="COACHING")
        assert geo == "SPAIN"
        assert rule == "eur_default"

    def test_eur_newsletter_defaults_to_eu_not_spain(self, sample_rules, sample_payment):
        geo, rule = classify_geography(sample_payment, rules=sample_rules, activity_type="NEWSLETTER")
        assert geo == "EU_NOT_SPAIN"
        assert rule == "eur_newsletter_default"

    def test_name_override(self, sample_rules, sample_payment):
        sample_payment.description = "Payment from John Doe"
        geo, rule = classify_geography(sample_payment, rules=sample_rules)
        assert geo == "OUTSIDE_EU"
        assert "name_override" in rule

    def test_email_override(self, sample_rules, sample_payment):
        sample_payment.email_meta = "test@example.de"
        geo, rule = classify_geography(sample_payment, rules=sample_rules)
        assert geo == "EU_NOT_SPAIN"
        assert "email_override" in rule


class TestClassifyPayment:
    def test_full_classification(self, sample_rules, sample_payment):
        classified = classify_payment(sample_payment, rules=sample_rules)
        assert classified.activity_type == "COACHING"
        assert classified.geo_region == "SPAIN"
        assert classified.IND_COACHING == 1
        assert classified.IND_SPAIN == 1
        assert classified.activity_valid
        assert classified.geo_valid

    def test_newsletter_classification(self, sample_rules):
        p = Payment(
            id="ch_test_nl",
            created_date="2025-01-15T10:00:00",
            converted_amount=10.0,
            converted_amount_refunded=0.0,
            description="Subscription update",
            fee=0.50,
            currency="eur",
        )
        classified = classify_payment(p, rules=sample_rules)
        assert classified.activity_type == "NEWSLETTER"
        assert classified.geo_region == "EU_NOT_SPAIN"
        assert classified.IND_NEWSLETTER == 1
        assert classified.IND_OUT_SPAIN == 1


class TestClassifyBatch:
    def test_batch_classification(self, sample_rules, sample_payments):
        classified, error_ids = classify_batch(sample_payments, rules=sample_rules)
        assert len(classified) == 5
        assert len(error_ids) == 0

        activities = [c.activity_type for c in classified]
        assert "COACHING" in activities
        assert "NEWSLETTER" in activities
        assert "ILLUSTRATIONS" in activities

    def test_batch_counts_are_correct(self, sample_rules, sample_payments):
        classified, _ = classify_batch(sample_payments, rules=sample_rules)
        coaching = [c for c in classified if c.activity_type == "COACHING"]
        newsletter = [c for c in classified if c.activity_type == "NEWSLETTER"]
        illustrations = [c for c in classified if c.activity_type == "ILLUSTRATIONS"]
        assert len(coaching) == 3  # calendly + consulting + empty desc
        assert len(newsletter) == 1
        assert len(illustrations) == 1
