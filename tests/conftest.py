"""Shared pytest fixtures."""
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.models import ClassifiedPayment, Payment
from src.rules_engine import load_rules


@pytest.fixture
def sample_rules():
    """Minimal classification rules for testing."""
    return {
        "activity_rules": [
            {
                "priority": 1,
                "name": "empty_description_default",
                "activity_type": "COACHING",
                "match_type": "empty_description",
            },
            {
                "priority": 2,
                "name": "luma_registration",
                "activity_type": "COACHING",
                "match_type": "payment_type",
                "match_value": "registration",
            },
            {
                "priority": 3,
                "name": "illustrations_keywords",
                "activity_type": "ILLUSTRATIONS",
                "match_type": "description_contains",
                "keywords": ["charge for"],
            },
            {
                "priority": 4,
                "name": "newsletter_keywords",
                "activity_type": "NEWSLETTER",
                "match_type": "description_contains",
                "keywords": ["subscription"],
            },
            {
                "priority": 5,
                "name": "coaching_keywords",
                "activity_type": "COACHING",
                "match_type": "description_contains",
                "keywords": ["calendly", "coach", "consulting"],
            },
        ],
        "geographic_rules": {
            "defaults": {
                "eur_default": "SPAIN",
                "eur_newsletter_default": "EU_NOT_SPAIN",
                "non_eur_default": "OUTSIDE_EU",
            },
            "geographic_overrides": {
                "john doe": "OUTSIDE_EU",
            },
            "email_overrides": {
                "test@example.de": "EU_NOT_SPAIN",
            },
        },
    }


@pytest.fixture
def sample_payment():
    """A basic EUR coaching payment."""
    return Payment(
        id="ch_test_001",
        created_date="2025-01-15T10:00:00",
        converted_amount=100.0,
        converted_amount_refunded=0.0,
        description="Calendly coaching session",
        fee=3.50,
        currency="eur",
    )


@pytest.fixture
def sample_payments():
    """A list of diverse payments for testing."""
    return [
        Payment(
            id="ch_test_001",
            created_date="2025-01-15T10:00:00",
            converted_amount=100.0,
            converted_amount_refunded=0.0,
            description="Calendly coaching session",
            fee=3.50,
            currency="eur",
        ),
        Payment(
            id="ch_test_002",
            created_date="2025-02-10T14:30:00",
            converted_amount=50.0,
            converted_amount_refunded=0.0,
            description="Subscription creation",
            fee=1.80,
            currency="eur",
        ),
        Payment(
            id="ch_test_003",
            created_date="2025-03-05T09:00:00",
            converted_amount=200.0,
            converted_amount_refunded=0.0,
            description="Charge for illustration project",
            fee=6.20,
            currency="eur",
        ),
        Payment(
            id="ch_test_004",
            created_date="2025-01-20T16:00:00",
            converted_amount=75.0,
            converted_amount_refunded=0.0,
            description="Consulting call",
            fee=2.50,
            currency="gbp",
        ),
        Payment(
            id="ch_test_005",
            created_date="2025-02-28T11:00:00",
            converted_amount=120.0,
            converted_amount_refunded=10.0,
            description="",
            fee=4.00,
            currency="eur",
        ),
    ]


@pytest.fixture
def tmp_db(tmp_path):
    """Temporary database path for testing."""
    return tmp_path / "test.db"


@pytest.fixture
def tmp_rules(tmp_path, sample_rules):
    """Write sample rules to a temp file and return the path."""
    path = tmp_path / "classification_rules.json"
    path.write_text(json.dumps(sample_rules, indent=2))
    return path
