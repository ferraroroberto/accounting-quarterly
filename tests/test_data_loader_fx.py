"""apply_fx_conversion keeps the Stripe fee in EUR (#144). Synthetic payments only."""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import data_loader  # noqa: E402
from src.models import Payment  # noqa: E402


def _payment(currency: str) -> Payment:
    return Payment(id="ch_test_1", created_date=datetime(2026, 3, 10, 12, 0), converted_amount=10.0,
                   converted_amount_refunded=0.0, description="synthetic", fee=1.5, currency=currency)


def test_non_eur_fee_is_not_converted_again(monkeypatch):
    monkeypatch.setattr(data_loader, "init_fx_table", lambda: None)
    monkeypatch.setattr(data_loader, "convert_to_eur", lambda amount, currency, d: (round(amount / 2.0, 2), 2.0))

    [p] = data_loader.apply_fx_conversion([_payment("usd")])

    assert p.converted_amount == 5.0      # the amount is in the charge currency: converted
    assert p.fee == 1.5                   # balance-transaction fee is already EUR: untouched
    assert p.fx_rate == 2.0


def test_eur_payment_unchanged(monkeypatch):
    monkeypatch.setattr(data_loader, "init_fx_table", lambda: None)

    [p] = data_loader.apply_fx_conversion([_payment("eur")])

    assert (p.converted_amount, p.fee, p.fx_rate) == (10.0, 1.5, 1.0)
