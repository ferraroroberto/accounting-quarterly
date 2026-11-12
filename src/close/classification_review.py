"""The review of a quarter's classified Stripe charges, shared by the close pipeline and the CLI.

``review_classifications`` finds what needs a human look; the ``stripe`` step turns the
findings into ``StepResult`` lines and ``stripe-fetch`` prints them.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Optional

from src.classifier import eur_default_foreign_warning, validate_classifications
from src.models import ClassifiedPayment
from src.returns.oss_return import compute_eu_b2c_threshold

DEFAULT_GEO_RULES = {"eur_default", "eur_newsletter_default", "non_eur_default"}


@dataclass
class ClassificationReview:
    """What a quarter's classification needs reviewed."""
    validation: dict[str, int]                       # activity_errors / geo_errors / unknown_activity
    on_default: list[ClassifiedPayment]              # charges on a default geo rule, no client override
    foreign: list[tuple[ClassifiedPayment, str]]     # EUR charges on eur_default that look foreign, with why
    threshold: Any                                   # EU B2C (OSS) threshold tracker

    @property
    def has_validation_issues(self) -> bool:
        v = self.validation
        return bool(v["activity_errors"] or v["geo_errors"] or v["unknown_activity"])


def review_classifications(
    payments: list[ClassifiedPayment], year: int, quarter: int, conn: sqlite3.Connection,
    config: Optional[dict],
) -> ClassificationReview:
    """Validate ``payments`` and collect the default-rule, foreign-looking and EU B2C threshold findings."""
    return ClassificationReview(
        validation=validate_classifications(payments),
        on_default=[p for p in payments if p.geo_rule in DEFAULT_GEO_RULES],
        foreign=[(p, w) for p in payments if (w := eur_default_foreign_warning(p))],
        threshold=compute_eu_b2c_threshold(year, quarter, conn, config),
    )
