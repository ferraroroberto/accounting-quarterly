"""Re-run the classifier over transactions already stored in SQLite.

Classification is persisted at fetch time and ``db`` loads never re-run it, so
a rule change (a new override, a fixed default) — or a row whose currency was
corrected by a later re-fetch — leaves stale geography/activity pairings in the
table until something reclassifies it. :func:`reclassify_stored` is that
something: it classifies every stored row in a date range with the current
rules, reports each difference (old → new), and persists only the rows that
changed. Running it twice is a no-op the second time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.classifier import classify_payment
from src.database import load_classified_payments, upsert_classified
from src.logger import get_logger
from src.models import ClassifiedPayment, Payment
from src.rules_engine import load_rules

log = get_logger(__name__)

_CLASSIFICATION_FIELDS = ("activity_type", "geo_region", "classification_rule", "geo_rule")
_PAYMENT_FIELDS = frozenset(Payment.model_fields)


@dataclass(frozen=True)
class ClassificationChange:
    """One stored row whose classification differs from the current rules."""
    id: str
    created_date: datetime
    currency: str
    old: tuple[str, str, str, str]  # (activity_type, geo_region, classification_rule, geo_rule)
    new: tuple[str, str, str, str]

    @property
    def quarter_key(self) -> tuple[int, int]:
        return self.created_date.year, (self.created_date.month - 1) // 3 + 1

    def describe(self) -> str:
        old_act, old_geo, _, old_geo_rule = self.old
        new_act, new_geo, _, new_geo_rule = self.new
        return (
            f"{self.created_date:%Y-%m-%d} {self.id} [{self.currency.upper()}] "
            f"{old_act}/{old_geo} ({old_geo_rule or '-'}) -> "
            f"{new_act}/{new_geo} ({new_geo_rule or '-'})"
        )


@dataclass
class ReclassifyResult:
    scanned: int = 0
    dry_run: bool = False
    changes: list[ClassificationChange] = field(default_factory=list)

    def by_quarter(self) -> dict[tuple[int, int], int]:
        """Number of changed rows per (year, quarter), sorted."""
        counts: dict[tuple[int, int], int] = {}
        for c in self.changes:
            counts[c.quarter_key] = counts.get(c.quarter_key, 0) + 1
        return dict(sorted(counts.items()))


def _classification(p: ClassifiedPayment) -> tuple[str, str, str, str]:
    return tuple(getattr(p, f) or "" for f in _CLASSIFICATION_FIELDS)  # type: ignore[return-value]


def reclassify_stored(
    start: datetime,
    end: Optional[datetime] = None,
    *,
    dry_run: bool = False,
    rules: Optional[dict] = None,
    db_path: Optional[str | Path] = None,
) -> ReclassifyResult:
    """Reclassify stored transactions in ``[start, end]`` with the current rules.

    Every changed row is logged (old → new). Unless ``dry_run``, only the
    changed rows are written back, so the call is idempotent.
    """
    rules = rules or load_rules()
    stored = load_classified_payments(start, end, db_path=db_path)
    result = ReclassifyResult(scanned=len(stored), dry_run=dry_run)

    changed_rows: list[ClassifiedPayment] = []
    for old in stored:
        base = Payment(**old.model_dump(include=_PAYMENT_FIELDS))
        new = classify_payment(base, rules)
        if _classification(old) == _classification(new):
            continue
        change = ClassificationChange(
            id=old.id, created_date=old.created_date, currency=old.currency,
            old=_classification(old), new=_classification(new),
        )
        result.changes.append(change)
        changed_rows.append(new)
        log.info("ℹ️ Reclassify%s | %s", " (dry-run)" if dry_run else "", change.describe())

    if changed_rows and not dry_run:
        upsert_classified(changed_rows, db_path=db_path)

    log.info(
        "✅ Reclassify%s %s..%s | scanned %d | changed %d",
        " (dry-run)" if dry_run else "",
        start.date(), end.date() if end else "latest",
        result.scanned, len(result.changes),
    )
    return result
