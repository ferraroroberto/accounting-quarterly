"""Tests for the FX rates module."""
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from src.fx_rates import (
    convert_to_eur,
    get_all_rates,
    get_rate,
    get_rate_count,
    get_rate_with_fallback,
    get_stored_date_range,
    init_fx_table,
    store_rates,
)


@pytest.fixture
def fx_db(tmp_db):
    """Initialise FX table in temp DB."""
    init_fx_table(tmp_db)
    return tmp_db


@pytest.fixture
def fx_db_with_rates(fx_db):
    """FX DB pre-loaded with sample rates."""
    sample_rates = {
        "2025-01-13": {"USD": 1.0250, "GBP": 0.8400, "CHF": 0.9380},
        "2025-01-14": {"USD": 1.0300, "GBP": 0.8420, "CHF": 0.9400},
        "2025-01-15": {"USD": 1.0280, "GBP": 0.8410, "CHF": 0.9390},
        "2025-01-16": {"USD": 1.0320, "GBP": 0.8430, "CHF": 0.9410},
        "2025-01-17": {"USD": 1.0350, "GBP": 0.8450, "CHF": 0.9420},
    }
    store_rates(sample_rates, fx_db)
    return fx_db


class TestStoreAndRetrieve:
    def test_store_rates(self, fx_db):
        rates = {
            "2025-01-15": {"USD": 1.028, "GBP": 0.841},
        }
        count = store_rates(rates, fx_db)
        assert count == 2

    def test_get_rate(self, fx_db_with_rates):
        rate = get_rate(date(2025, 1, 15), "USD", fx_db_with_rates)
        assert rate == 1.0280

    def test_get_rate_missing(self, fx_db_with_rates):
        rate = get_rate(date(2025, 1, 12), "USD", fx_db_with_rates)
        assert rate is None

    def test_get_rate_count(self, fx_db_with_rates):
        count = get_rate_count(fx_db_with_rates)
        assert count == 15  # 5 dates * 3 currencies

    def test_get_stored_date_range(self, fx_db_with_rates):
        min_d, max_d = get_stored_date_range(fx_db_with_rates)
        assert min_d == date(2025, 1, 13)
        assert max_d == date(2025, 1, 17)

    def test_get_all_rates(self, fx_db_with_rates):
        rates = get_all_rates("USD", fx_db_with_rates)
        assert len(rates) == 5
        assert rates[0][0] == "2025-01-13"
        assert rates[0][1] == 1.0250

    def test_upsert_overwrites(self, fx_db_with_rates):
        updated = {"2025-01-15": {"USD": 9.999}}
        store_rates(updated, fx_db_with_rates)
        rate = get_rate(date(2025, 1, 15), "USD", fx_db_with_rates)
        assert rate == 9.999


class TestFallback:
    def test_fallback_to_previous_date(self, fx_db_with_rates):
        # Jan 18 is a Saturday, no rate stored - should fall back to Jan 17
        rate = get_rate_with_fallback(date(2025, 1, 18), "USD", fx_db_with_rates)
        assert rate == 1.0350  # Jan 17 rate

    def test_fallback_exact_date_works(self, fx_db_with_rates):
        rate = get_rate_with_fallback(date(2025, 1, 15), "GBP", fx_db_with_rates)
        assert rate == 0.8410

    def test_fallback_no_data_before(self, fx_db_with_rates):
        # Before any stored data, tries API then returns None
        with patch("src.fx_rates.fetch_single_date", side_effect=Exception("no network")):
            rate = get_rate_with_fallback(date(2020, 1, 1), "USD", fx_db_with_rates)
            assert rate is None


class TestConvertToEur:
    def test_eur_passthrough(self, fx_db):
        amount, rate = convert_to_eur(100.0, "eur", date(2025, 1, 15), fx_db)
        assert amount == 100.0
        assert rate == 1.0

    def test_usd_conversion(self, fx_db_with_rates):
        # 100 USD with rate 1.028 (1 EUR = 1.028 USD)
        # amount_eur = 100 / 1.028 = 97.28
        amount, rate = convert_to_eur(100.0, "USD", date(2025, 1, 15), fx_db_with_rates)
        assert rate == 1.0280
        assert amount == round(100.0 / 1.0280, 2)

    def test_gbp_conversion(self, fx_db_with_rates):
        # 50 GBP with rate 0.841 (1 EUR = 0.841 GBP)
        # amount_eur = 50 / 0.841 = 59.45
        amount, rate = convert_to_eur(50.0, "GBP", date(2025, 1, 15), fx_db_with_rates)
        assert rate == 0.8410
        assert amount == round(50.0 / 0.8410, 2)

    def test_no_rate_returns_original(self, fx_db):
        with patch("src.fx_rates.fetch_single_date", side_effect=Exception("no network")):
            amount, rate = convert_to_eur(100.0, "JPY", date(2025, 1, 15), fx_db)
            assert amount == 100.0
            assert rate is None

    def test_zero_amount(self, fx_db_with_rates):
        amount, rate = convert_to_eur(0.0, "USD", date(2025, 1, 15), fx_db_with_rates)
        assert amount == 0.0
        assert rate == 1.0280


class TestFetchRates:
    @patch("src.fx_rates.requests.get")
    def test_fetch_rates_range(self, mock_get, fx_db):
        from src.fx_rates import fetch_rates_range

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rates": {
                "2025-01-15": {"USD": 1.028, "GBP": 0.841, "CHF": 0.939},
            }
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        rates = fetch_rates_range(date(2025, 1, 15), date(2025, 1, 15))
        assert "2025-01-15" in rates
        assert rates["2025-01-15"]["USD"] == 1.028

    @patch("src.fx_rates.requests.get")
    def test_fetch_single_date(self, mock_get, fx_db):
        from src.fx_rates import fetch_single_date

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "rates": {"USD": 1.028, "GBP": 0.841, "CHF": 0.939}
        }
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        rates = fetch_single_date(date(2025, 1, 15))
        assert rates["USD"] == 1.028
