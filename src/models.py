from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Optional

from pydantic import BaseModel, field_validator, model_validator


ActivityType = Literal["COACHING", "NEWSLETTER", "ILLUSTRATIONS", "UNKNOWN"]
GeoRegion = Literal["SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU", "UNKNOWN"]


class Payment(BaseModel):
    id: str
    created_date: datetime
    converted_amount: float
    converted_amount_refunded: float
    description: str
    fee: float
    currency: str = "eur"
    payment_type_meta: Optional[str] = None
    event_api_id_meta: Optional[str] = None
    email_meta: Optional[str] = None
    card_country: Optional[str] = None
    amount_original: Optional[float] = None
    fx_rate: Optional[float] = None
    # Traceability fields (raw source snapshots / key Stripe IDs)
    stripe_customer_id: Optional[str] = None
    stripe_payment_intent_id: Optional[str] = None
    stripe_balance_transaction_id: Optional[str] = None
    stripe_invoice_id: Optional[str] = None
    raw_source: Optional[dict[str, Any]] = None
    raw_source_type: Optional[str] = None  # "stripe_api" | "stripe_csv" | etc.

    @field_validator("currency", mode="before")
    @classmethod
    def normalise_currency(cls, v: str) -> str:
        return str(v).lower().strip() if v else "eur"

    @property
    def net_amount(self) -> float:
        return round(self.converted_amount - self.converted_amount_refunded, 2)

    @property
    def quarter(self) -> int:
        return (self.created_date.month - 1) // 3 + 1

    @property
    def month(self) -> int:
        return self.created_date.month

    @property
    def year(self) -> int:
        return self.created_date.year

    @property
    def month_label(self) -> str:
        return self.created_date.strftime("%b")


class ClassifiedPayment(Payment):
    activity_type: ActivityType = "UNKNOWN"
    geo_region: GeoRegion = "UNKNOWN"
    classification_rule: str = ""
    geo_rule: str = ""

    IND_COACHING: int = 0
    IND_NEWSLETTER: int = 0
    IND_ILLUSTRATIONS: int = 0
    IND_SPAIN: int = 0
    IND_OUT_SPAIN: int = 0
    IND_EXEU: int = 0
    IND_EU: int = 0

    @model_validator(mode="after")
    def sync_indicators(self) -> "ClassifiedPayment":
        if self.activity_type == "COACHING":
            self.IND_COACHING = 1
            self.IND_NEWSLETTER = 0
            self.IND_ILLUSTRATIONS = 0
        elif self.activity_type == "NEWSLETTER":
            self.IND_COACHING = 0
            self.IND_NEWSLETTER = 1
            self.IND_ILLUSTRATIONS = 0
        elif self.activity_type == "ILLUSTRATIONS":
            self.IND_COACHING = 0
            self.IND_NEWSLETTER = 0
            self.IND_ILLUSTRATIONS = 1

        if self.geo_region == "SPAIN":
            self.IND_SPAIN = 1
            self.IND_OUT_SPAIN = 0
            self.IND_EXEU = 0
        elif self.geo_region == "EU_NOT_SPAIN":
            self.IND_SPAIN = 0
            self.IND_OUT_SPAIN = 1
            self.IND_EXEU = 0
        elif self.geo_region == "OUTSIDE_EU":
            self.IND_SPAIN = 0
            self.IND_OUT_SPAIN = 0
            self.IND_EXEU = 1

        self.IND_EU = 1 - self.IND_EXEU
        return self

    @property
    def activity_valid(self) -> bool:
        return self.IND_COACHING + self.IND_NEWSLETTER + self.IND_ILLUSTRATIONS == 1

    @property
    def geo_valid(self) -> bool:
        return self.IND_SPAIN + self.IND_OUT_SPAIN + self.IND_EXEU == 1


class MonthlyAggregation(BaseModel):
    year: int
    month: int
    geo_region: GeoRegion
    coaching_income: float = 0.0
    newsletter_income: float = 0.0
    illustrations_income: float = 0.0
    coaching_fee: float = 0.0
    newsletter_fee: float = 0.0
    illustrations_fee: float = 0.0

    @property
    def total_income(self) -> float:
        return round(self.coaching_income + self.newsletter_income + self.illustrations_income, 2)

    @property
    def total_fee(self) -> float:
        return round(self.coaching_fee + self.newsletter_fee + self.illustrations_fee, 2)

    @property
    def month_label(self) -> str:
        return date(self.year, self.month, 1).strftime("%b %Y")


