"""Foreign exchange rates: fetch from ECB via Frankfurter API, store in SQLite."""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from src.logger import get_logger

log = get_logger(__name__)

FRANKFURTER_BASE = "https://api.frankfurter.dev"
SUPPORTED_CURRENCIES = ["USD", "GBP", "CHF"]

_DB_PATH = Path(__file__).parent.parent / "data" / "accounting.db"


def _get_connection(db_path: Optional[str | Path] = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else _DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init_fx_table(db_path: Optional[str | Path] = None) -> None:
    """Create the fx_rates table if it doesn't exist."""
    conn = _get_connection(db_path)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS fx_rates (
                rate_date TEXT NOT NULL,
                currency TEXT NOT NULL,
                rate REAL NOT NULL,
                PRIMARY KEY (rate_date, currency)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_fx_rates_currency
                ON fx_rates(currency)
        """)
        conn.commit()
    finally:
        conn.close()


def fetch_rates_range(
    start_date: date,
    end_date: date,
    currencies: Optional[list[str]] = None,
) -> dict[str, dict[str, float]]:
    """Fetch daily ECB rates from Frankfurter API for a date range.

    Returns {date_str: {currency: rate}} where rate means 1 EUR = rate CURRENCY.
    """
    currencies = currencies or SUPPORTED_CURRENCIES
    to_param = ",".join(currencies)

    url = f"{FRANKFURTER_BASE}/{start_date.isoformat()}..{end_date.isoformat()}"
    params = {"from": "EUR", "to": to_param}

    log.info("ℹ️ Fetching FX rates from %s to %s for %s", start_date, end_date, to_param)

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    return data.get("rates", {})


def fetch_single_date(
    rate_date: date,
    currencies: Optional[list[str]] = None,
) -> dict[str, float]:
    """Fetch ECB rates for a single date.

    Returns {currency: rate} where rate means 1 EUR = rate CURRENCY.
    """
    currencies = currencies or SUPPORTED_CURRENCIES
    to_param = ",".join(currencies)

    url = f"{FRANKFURTER_BASE}/{rate_date.isoformat()}"
    params = {"from": "EUR", "to": to_param}

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    return data.get("rates", {})


def store_rates(
    rates: dict[str, dict[str, float]],
    db_path: Optional[str | Path] = None,
) -> int:
    """Store rates dict into SQLite. Returns number of rows inserted/updated."""
    conn = _get_connection(db_path)
    count = 0
    try:
        for date_str, currency_rates in rates.items():
            for currency, rate in currency_rates.items():
                conn.execute("""
                    INSERT INTO fx_rates (rate_date, currency, rate)
                    VALUES (?, ?, ?)
                    ON CONFLICT(rate_date, currency) DO UPDATE SET rate = excluded.rate
                """, (date_str, currency.upper(), rate))
                count += 1
        conn.commit()
        log.info("ℹ️ Stored %d FX rate entries", count)
    finally:
        conn.close()
    return count


def load_and_store_range(
    start_date: date,
    end_date: date,
    currencies: Optional[list[str]] = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """Fetch rates from API and store in database. Returns row count."""
    init_fx_table(db_path)
    rates = fetch_rates_range(start_date, end_date, currencies)
    return store_rates(rates, db_path)


def get_rate(
    rate_date: date,
    currency: str,
    db_path: Optional[str | Path] = None,
) -> Optional[float]:
    """Get the FX rate for a specific date and currency from the database.

    Returns the rate (1 EUR = rate CURRENCY) or None if not found.
    """
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE rate_date = ? AND currency = ?",
            (rate_date.isoformat(), currency.upper()),
        ).fetchone()
        if row:
            return row["rate"]
        return None
    finally:
        conn.close()


def get_rate_with_fallback(
    rate_date: date,
    currency: str,
    db_path: Optional[str | Path] = None,
) -> Optional[float]:
    """Get the FX rate for a date, falling back to the most recent available rate.

    First tries the exact date, then searches backwards for the closest available.
    If nothing in DB, tries to fetch from the API for that date.
    """
    rate = get_rate(rate_date, currency, db_path)
    if rate is not None:
        return rate

    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE currency = ? AND rate_date <= ? "
            "ORDER BY rate_date DESC LIMIT 1",
            (currency.upper(), rate_date.isoformat()),
        ).fetchone()
        if row:
            log.debug("ℹ️ FX fallback: using previous rate for %s on %s", currency, rate_date)
            return row["rate"]
    finally:
        conn.close()

    # Try fetching from API
    try:
        rates = fetch_single_date(rate_date, [currency])
        if currency.upper() in rates:
            store_rates({rate_date.isoformat(): rates}, db_path)
            return rates[currency.upper()]
    except Exception as exc:
        log.warning("⚠️ Could not fetch FX rate for %s on %s: %s", currency, rate_date, exc)

    return None


def convert_to_eur(
    amount: float,
    currency: str,
    rate_date: date,
    db_path: Optional[str | Path] = None,
) -> tuple[float, Optional[float]]:
    """Convert an amount from a foreign currency to EUR.

    Returns (amount_eur, fx_rate_used).
    If currency is already EUR, returns (amount, 1.0).
    If no rate found, returns (amount, None) unchanged.
    """
    if currency.lower() == "eur":
        return amount, 1.0

    rate = get_rate_with_fallback(rate_date, currency.upper(), db_path)
    if rate is None or rate == 0:
        log.warning("⚠️ No FX rate for %s on %s, returning original amount", currency, rate_date)
        return amount, None

    # rate = how many units of currency per 1 EUR
    # So: amount_eur = amount_in_currency / rate
    amount_eur = round(amount / rate, 2)
    return amount_eur, rate


def get_all_rates(
    currency: str,
    db_path: Optional[str | Path] = None,
) -> list[tuple[str, float]]:
    """Get all stored rates for a currency, sorted by date."""
    conn = _get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT rate_date, rate FROM fx_rates WHERE currency = ? ORDER BY rate_date",
            (currency.upper(),),
        ).fetchall()
        return [(row["rate_date"], row["rate"]) for row in rows]
    finally:
        conn.close()


def get_stored_date_range(
    db_path: Optional[str | Path] = None,
) -> tuple[Optional[date], Optional[date]]:
    """Get the min and max dates stored in the fx_rates table."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT MIN(rate_date) as min_date, MAX(rate_date) as max_date FROM fx_rates"
        ).fetchone()
        if row and row["min_date"]:
            return (
                date.fromisoformat(row["min_date"]),
                date.fromisoformat(row["max_date"]),
            )
        return None, None
    finally:
        conn.close()


def get_rate_count(db_path: Optional[str | Path] = None) -> int:
    """Get total number of FX rate entries stored."""
    conn = _get_connection(db_path)
    try:
        row = conn.execute("SELECT COUNT(*) as cnt FROM fx_rates").fetchone()
        return row["cnt"]
    finally:
        conn.close()
