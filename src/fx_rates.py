"""Foreign exchange rates: fetch from ECB via Frankfurter API, store in SQLite."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import requests

from src.database import get_connection, _get_table_columns, _create_fx_rates_table, parse_locked_fields
from src.logger import get_logger

log = get_logger(__name__)

FRANKFURTER_BASES = [
    # Official Frankfurter API endpoint (ECB-based).
    "https://api.frankfurter.app",
    # Legacy/alternative endpoint kept as a fallback.
    "https://api.frankfurter.dev",
]
SUPPORTED_CURRENCIES = ["USD", "GBP", "CHF", "AUD"]

# A fallback rate older than this many days than the date it was requested for
# is flagged `is_stale` (issue #93) — the exact date is missing (weekend/holiday
# gaps are normal and stay unflagged), but a week-plus-old rate usually means
# nobody has loaded fresh data and the figure should be reviewed.
STALE_TOLERANCE_DAYS = 5

# The model's own EUR estimate is flagged when it differs from the resolved EUR figure by more than this
# many percent: past it the gap is a real conversion error, below it just rounding between rate sources.
FX_CROSS_CHECK_THRESHOLD_PCT = 1.0

# The default start date used when backfilling a currency that has no stored
# rates yet (mirrors the Currency tab's own default range start).
_BACKFILL_DEFAULT_START = date(2023, 7, 1)


@dataclass
class FxRateInfo:
    """Result of an FX rate lookup, with the staleness/provenance issue #93 needs."""

    rate: Optional[float]          # 1 EUR = `rate` units of the currency; None if unavailable
    rate_date: Optional[date]      # the date the rate actually comes from (may differ from requested)
    is_stale: bool                 # True when `rate_date` is more than STALE_TOLERANCE_DAYS before the request
    source: str                    # "exact" | "fallback" | "fetched" | "none"


def _ensure_fx_schema(conn: sqlite3.Connection) -> None:
    """Add timestamp columns to existing fx_rates tables (best-effort)."""
    existing = _get_table_columns(conn, "fx_rates")
    if "loaded_at" not in existing:
        try:
            conn.execute("ALTER TABLE fx_rates ADD COLUMN loaded_at TEXT")
            conn.execute("UPDATE fx_rates SET loaded_at = datetime('now') WHERE loaded_at IS NULL")
            log.info("ℹ️ Migrated DB: added fx_rates.loaded_at")
        except Exception as exc:
            log.warning("⚠️ DB migration skipped for fx_rates.loaded_at: %s", exc)
    if "updated_at" not in existing:
        try:
            conn.execute("ALTER TABLE fx_rates ADD COLUMN updated_at TEXT")
            conn.execute("UPDATE fx_rates SET updated_at = datetime('now') WHERE updated_at IS NULL")
            log.info("ℹ️ Migrated DB: added fx_rates.updated_at")
        except Exception as exc:
            log.warning("⚠️ DB migration skipped for fx_rates.updated_at: %s", exc)


def _get_with_fallback(path: str, *, params: dict[str, str], timeout_s: int = 30) -> requests.Response:
    last_exc: Exception | None = None
    for base in FRANKFURTER_BASES:
        url = f"{base}/{path.lstrip('/')}"
        try:
            resp = requests.get(url, params=params, timeout=timeout_s)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            log.warning("⚠️ FX fetch failed via %s: %s", base, exc)
            continue
    assert last_exc is not None
    raise last_exc


def init_fx_table(db_path: Optional[str | Path] = None) -> None:
    """Create the fx_rates table if it doesn't exist.

    Delegates the schema itself to `database._create_fx_rates_table` (the
    single owner) so this and `database.init_db` can never diverge again.
    """
    conn = get_connection(db_path)
    try:
        _create_fx_rates_table(conn)
        _ensure_fx_schema(conn)
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

    path = f"{start_date.isoformat()}..{end_date.isoformat()}"
    params = {"from": "EUR", "to": to_param}

    log.info("ℹ️ Fetching FX rates from %s to %s for %s", start_date, end_date, to_param)

    resp = _get_with_fallback(path, params=params, timeout_s=30)
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

    path = rate_date.isoformat()
    params = {"from": "EUR", "to": to_param}

    resp = _get_with_fallback(path, params=params, timeout_s=30)
    data = resp.json()

    return data.get("rates", {})


def store_rates(
    rates: dict[str, dict[str, float]],
    db_path: Optional[str | Path] = None,
) -> int:
    """Store rates dict into SQLite. Returns number of rows inserted/updated."""
    conn = get_connection(db_path)
    count = 0
    try:
        _ensure_fx_schema(conn)
        for date_str, currency_rates in rates.items():
            for currency, rate in currency_rates.items():
                conn.execute("""
                    INSERT INTO fx_rates (rate_date, currency, rate, loaded_at, updated_at)
                    VALUES (?, ?, ?, datetime('now'), datetime('now'))
                    ON CONFLICT(rate_date, currency) DO UPDATE SET
                        rate = excluded.rate,
                        updated_at = datetime('now')
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
    conn = get_connection(db_path)
    try:
        _ensure_fx_schema(conn)
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE rate_date = ? AND currency = ?",
            (rate_date.isoformat(), currency.upper()),
        ).fetchone()
        if row:
            return row["rate"]
        return None
    finally:
        conn.close()


def get_rate_with_fallback_info(
    rate_date: date,
    currency: str,
    db_path: Optional[str | Path] = None,
    tolerance_days: int = STALE_TOLERANCE_DAYS,
) -> FxRateInfo:
    """Get the FX rate for a date, with full provenance (issue #93).

    First tries the exact date, then searches backwards for the closest
    available, then tries a live fetch for the exact date. Unlike the legacy
    `get_rate_with_fallback` (kept as a thin wrapper below, returning just the
    rate), this makes a stale fallback **visible**: `is_stale` is True whenever
    the rate actually used is dated more than `tolerance_days` before the
    requested date, so callers can flag it instead of silently trusting it.
    """
    exact = get_rate(rate_date, currency, db_path)
    if exact is not None:
        return FxRateInfo(rate=exact, rate_date=rate_date, is_stale=False, source="exact")

    conn = get_connection(db_path)
    try:
        _ensure_fx_schema(conn)
        row = conn.execute(
            "SELECT rate, rate_date FROM fx_rates WHERE currency = ? AND rate_date <= ? "
            "ORDER BY rate_date DESC LIMIT 1",
            (currency.upper(), rate_date.isoformat()),
        ).fetchone()
        if row:
            found_date = date.fromisoformat(row["rate_date"])
            stale = (rate_date - found_date).days > tolerance_days
            if stale:
                log.warning(
                    "⚠️ FX fallback for %s on %s is stale — using %s (%d days old)",
                    currency, rate_date, found_date, (rate_date - found_date).days,
                )
            else:
                log.debug("ℹ️ FX fallback: using previous rate for %s on %s", currency, rate_date)
            return FxRateInfo(rate=row["rate"], rate_date=found_date, is_stale=stale, source="fallback")
    finally:
        conn.close()

    # Nothing stored at or before the requested date — try a live fetch for the exact date.
    try:
        rates = fetch_single_date(rate_date, [currency])
        if currency.upper() in rates:
            store_rates({rate_date.isoformat(): rates}, db_path)
            return FxRateInfo(rate=rates[currency.upper()], rate_date=rate_date, is_stale=False, source="fetched")
    except Exception as exc:
        log.warning("⚠️ Could not fetch FX rate for %s on %s: %s", currency, rate_date, exc)

    return FxRateInfo(rate=None, rate_date=None, is_stale=True, source="none")


def get_rate_with_fallback(
    rate_date: date,
    currency: str,
    db_path: Optional[str | Path] = None,
) -> Optional[float]:
    """Get the FX rate for a date, falling back to the most recent available rate.

    Thin backward-compatible wrapper around `get_rate_with_fallback_info` —
    kept for existing callers that only need the number. New code that must
    surface staleness (invoice FX, issue #93) should call the `_info` variant.
    """
    return get_rate_with_fallback_info(rate_date, currency, db_path).rate


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
    conn = get_connection(db_path)
    try:
        _ensure_fx_schema(conn)
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
    conn = get_connection(db_path)
    try:
        _ensure_fx_schema(conn)
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
    conn = get_connection(db_path)
    try:
        _ensure_fx_schema(conn)
        row = conn.execute("SELECT COUNT(*) as cnt FROM fx_rates").fetchone()
        return row["cnt"]
    finally:
        conn.close()


def get_latest_fx_sync_at(db_path: Optional[str | Path] = None) -> Optional[datetime]:
    """Get latest FX DB sync timestamp from stored rates."""
    conn = get_connection(db_path)
    try:
        _ensure_fx_schema(conn)
        row = conn.execute("SELECT MAX(updated_at) AS max_updated_at FROM fx_rates").fetchone()
        if row and row["max_updated_at"]:
            return datetime.fromisoformat(row["max_updated_at"])
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Backfill (issue #93): keep rates current for every currency actually in use,
# not just the hard-coded SUPPORTED_CURRENCIES list.
# ---------------------------------------------------------------------------

def get_currencies_in_use(db_path: Optional[str | Path] = None) -> list[str]:
    """Non-EUR currencies seen in stored invoices or transactions, plus SUPPORTED_CURRENCIES.

    Drives the backfill so a new currency (AUD today, something else tomorrow)
    is picked up automatically the moment it appears in ingested data, rather
    than needing a code change.
    """
    conn = get_connection(db_path)
    try:
        found: set[str] = set(SUPPORTED_CURRENCIES)
        for table, col in (("invoices", "original_currency"), ("transactions", "currency")):
            try:
                rows = conn.execute(
                    f"SELECT DISTINCT {col} FROM {table} WHERE {col} IS NOT NULL"
                ).fetchall()
            except sqlite3.OperationalError:
                continue  # table not created yet (fresh DB before init_db())
            for row in rows:
                val = (row[0] or "").strip().upper()
                if val and val != "EUR":
                    found.add(val)
        return sorted(found)
    finally:
        conn.close()


def backfill_to_today(
    db_path: Optional[str | Path] = None,
    currencies: Optional[list[str]] = None,
) -> int:
    """Fetch and store ECB rates from the last stored date up to today.

    Cheap and idempotent (`store_rates` upserts) — meant to be called once at
    app start and again at quarter close, so the table never goes stale the
    way it did before #93 (data only up to 2026-03-30 despite the app running
    daily). Currencies default to `get_currencies_in_use`, so a new currency
    in ingested data is backfilled without a code change. Network failures are
    caught and logged, never raised, so this can't break app startup.
    """
    try:
        init_fx_table(db_path)
        currencies = currencies or get_currencies_in_use(db_path)
        if not currencies:
            return 0
        _, max_date = get_stored_date_range(db_path)
        start = (max_date + timedelta(days=1)) if max_date else _BACKFILL_DEFAULT_START
        end = date.today()
        if start > end:
            return 0
        stored = load_and_store_range(start, end, currencies, db_path)
        log.info("ℹ️ FX backfill: %s to %s stored %d entries for %s", start, end, stored, ", ".join(currencies))
        return stored
    except Exception as exc:
        log.warning("⚠️ FX backfill failed (will retry next run): %s", exc)
        return 0


# ---------------------------------------------------------------------------
# Invoice FX resolution (issue #93): EUR for a foreign-currency invoice comes
# from the ECB rate on the invoice date — not the LLM's own guess — unless the
# document states the EUR actually charged, or (income) EUR was actually
# received. See docs/tax-conventions.md-equivalent guidance in issue #93 /
# private decision D5.
# ---------------------------------------------------------------------------

@dataclass
class InvoiceFxResolution:
    """Authoritative EUR amounts for an OCR-extracted invoice, plus provenance."""

    subtotal_eur: Optional[float]
    iva_amount: Optional[float]
    total_eur: Optional[float]
    fx_rate_used: Optional[float]      # 1 EUR = fx_rate_used units of the original currency
    fx_rate_date: Optional[str]        # ISO date the rate actually comes from
    fx_source: str                     # NATIVE_EUR | CHARGED_EUR | ECB | NO_RATE | INVALID_DATE | MISSING_FX_INPUT
    fx_stale: bool
    fx_cross_check_diff_pct: Optional[float]  # |LLM total − resolved total| / resolved total × 100
    fx_warning: Optional[str]


def resolve_invoice_amounts(
    direction: str,
    data: dict,
    db_path: Optional[str | Path] = None,
) -> InvoiceFxResolution:
    """Resolve the authoritative EUR amounts for an OCR-extracted invoice (#93).

    ``data`` is the dict returned by ``invoice_ocr.extract_invoice`` (or the
    merged row from the Invoice Ledger edit form): read for ``currency`` /
    ``original_currency`` / ``original_amount`` / ``invoice_date`` /
    ``charged_eur``, plus the LLM's own ``subtotal_eur`` / ``iva_amount`` /
    ``total_eur`` guess, which is kept only as a cross-check.

    Expenses (``direction='in'``): the EUR actually charged to the card, when
    the document states it (``charged_eur``, e.g. "Charged 42.50 EUR using 1
    USD = 0.8500 EUR"), wins. Otherwise EUR = ``original_amount`` / the ECB
    rate on the invoice date.

    Income (``direction='out'``): resolved the same way from the ECB rate.
    ``eur_received`` (set later, once known, via the Invoice Ledger tab) is
    NOT read here — callers must prefer it over this function's result once
    it is set. The ECB figure this function returns is final per decision D5
    (art. 79.Once LIVA), not provisional, for money kept in a foreign-currency
    account.

    A missing rate is never silently left unconverted: it comes back as
    ``fx_source="NO_RATE"`` with a populated ``fx_warning`` instead of a quiet
    pass-through of the LLM's own number.
    """
    currency = (data.get("original_currency") or data.get("currency") or "EUR").upper()
    original_amount = data.get("original_amount")
    invoice_date_str = data.get("invoice_date")
    llm_subtotal = data.get("subtotal_eur")
    llm_iva = data.get("iva_amount")
    llm_total = data.get("total_eur")
    charged_eur = data.get("charged_eur")

    if currency == "EUR":
        return InvoiceFxResolution(
            subtotal_eur=llm_subtotal, iva_amount=llm_iva, total_eur=llm_total,
            fx_rate_used=None, fx_rate_date=None, fx_source="NATIVE_EUR",
            fx_stale=False, fx_cross_check_diff_pct=None, fx_warning=None,
        )

    if not original_amount or not invoice_date_str:
        # A foreign-currency invoice with nothing to convert: the LLM's own EUR
        # figures are kept, but flagged — never labelled native EUR.
        missing = " and ".join(
            name for name, value in (("original_amount", original_amount), ("invoice_date", invoice_date_str))
            if not value
        )
        warning = (
            f"⚠️ {currency} invoice has no {missing} — cannot convert to EUR; "
            f"the LLM's own EUR estimate is kept unverified"
        )
        log.warning(warning)
        return InvoiceFxResolution(
            subtotal_eur=llm_subtotal, iva_amount=llm_iva, total_eur=llm_total,
            fx_rate_used=None, fx_rate_date=None, fx_source="MISSING_FX_INPUT",
            fx_stale=True, fx_cross_check_diff_pct=None, fx_warning=warning,
        )

    try:
        invoice_date = date.fromisoformat(str(invoice_date_str)[:10])
    except ValueError:
        warning = f"⚠️ Could not parse invoice_date {invoice_date_str!r} for FX conversion"
        log.warning(warning)
        return InvoiceFxResolution(
            subtotal_eur=llm_subtotal, iva_amount=llm_iva, total_eur=llm_total,
            fx_rate_used=None, fx_rate_date=None, fx_source="INVALID_DATE",
            fx_stale=False, fx_cross_check_diff_pct=None, fx_warning=warning,
        )

    fx_stale = False
    if direction == "in" and charged_eur is not None:
        total_new = round(float(charged_eur), 2)
        fx_rate_used = round(original_amount / total_new, 6) if total_new else None
        fx_source = "CHARGED_EUR"
        rate_date_str = invoice_date.isoformat()
    else:
        info = get_rate_with_fallback_info(invoice_date, currency, db_path)
        if info.rate is None:
            warning = (
                f"⚠️ No ECB rate available for {currency} on or before {invoice_date} — "
                f"amount left unconverted; load rates for this period in the Currency tab"
            )
            log.warning(warning)
            return InvoiceFxResolution(
                subtotal_eur=llm_subtotal, iva_amount=llm_iva, total_eur=llm_total,
                fx_rate_used=None, fx_rate_date=None, fx_source="NO_RATE",
                fx_stale=True, fx_cross_check_diff_pct=None, fx_warning=warning,
            )
        total_new = round(original_amount / info.rate, 2)
        fx_rate_used = info.rate
        fx_source = "ECB"
        fx_stale = info.is_stale
        rate_date_str = info.rate_date.isoformat() if info.rate_date else None

    # Rescale the LLM's subtotal/IVA split onto the resolved total, preserving
    # whatever VAT breakdown it gave; a foreign invoice with no VAT breakdown
    # (the common case — non-EU services) has no basis to invent one, so the
    # whole resolved amount becomes the base.
    if llm_total:
        ratio = total_new / llm_total
        subtotal_new = round((llm_subtotal or 0.0) * ratio, 2)
        iva_new = round((llm_iva or 0.0) * ratio, 2)
    else:
        subtotal_new = total_new
        iva_new = 0.0

    diff_pct: Optional[float] = None
    warning = None
    if llm_total and total_new:
        diff_pct = round(abs(llm_total - total_new) / total_new * 100, 2)
        if diff_pct > FX_CROSS_CHECK_THRESHOLD_PCT:
            warning = (
                f"⚠️ LLM EUR estimate ({llm_total:.2f}) differs from the {fx_source} "
                f"conversion ({total_new:.2f}) by {diff_pct:.1f}%"
            )
            log.warning(warning)

    return InvoiceFxResolution(
        subtotal_eur=subtotal_new, iva_amount=iva_new, total_eur=total_new,
        fx_rate_used=fx_rate_used, fx_rate_date=rate_date_str, fx_source=fx_source,
        fx_stale=fx_stale, fx_cross_check_diff_pct=diff_pct, fx_warning=warning,
    )


# ---------------------------------------------------------------------------
# Recompute stored invoice FX (issue #93 follow-up): resolve_invoice_amounts
# only ran at OCR-extraction time, so invoices already stored before this
# feature shipped keep whatever EUR figure the LLM guessed. This re-runs the
# resolver over stored non-EUR invoices and writes the corrected figures.
# ---------------------------------------------------------------------------

_FX_WRITE_FIELDS: tuple[str, ...] = (
    "subtotal_eur", "iva_amount", "total_eur",
    "fx_rate_used", "fx_rate_date", "fx_source", "fx_stale", "fx_cross_check_diff_pct",
)
# If a user has locked any of these on a row (a manual correction via the
# Invoice Ledger), the whole row is skipped — its EUR figures are no longer
# ours to touch. `eur_received` is deliberately not in this set: it already
# wins over `subtotal_eur` in the tax engine (src.tax_engine._income_invoice_eur)
# regardless of what this function does, and recomputing `subtotal_eur` here
# never overwrites it.
_FX_LOCK_GUARDS: frozenset[str] = frozenset({"subtotal_eur", "iva_amount", "total_eur"})


@dataclass
class InvoiceFxRecomputeRow:
    """One invoice's outcome from `recompute_stored_invoice_fx`."""

    invoice_id: str
    filename: str
    direction: str
    currency: str
    old_total_eur: Optional[float]
    new_total_eur: Optional[float]
    fx_source: str
    fx_stale: bool
    fx_cross_check_diff_pct: Optional[float]
    locked_skipped: bool


@dataclass
class InvoiceFxRecomputeResult:
    """Summary of a `recompute_stored_invoice_fx` run."""

    scanned: int
    changed: int
    stale: int
    cross_check_flagged: int
    locked_skipped: int
    dry_run: bool
    rows: list[InvoiceFxRecomputeRow]  # only rows that changed or were locked-skipped


def _floats_differ(a: Optional[float], b: Optional[float], tol: float = 0.01) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    return abs(a - b) > tol


def recompute_stored_invoice_fx(
    db_path: Optional[str | Path] = None,
    dry_run: bool = True,
    since: Optional[str] = None,
) -> InvoiceFxRecomputeResult:
    """Re-run `resolve_invoice_amounts` over every stored non-EUR invoice.

    Corrects invoices that were extracted before this feature shipped (or
    whose stored figure otherwise drifted from the ECB rate) — the dry run
    against the real DB (issue #93) found 134/135 foreign-currency invoices
    needed a new figure. Idempotent: a second run over already-correct rows
    reports zero changes.

    Never overwrites a row with any of `subtotal_eur` / `iva_amount` /
    `total_eur` in its `locked_fields` (a user's manual correction always
    wins) — such rows are skipped and reported in `locked_skipped`, not
    silently dropped. `eur_received` is never read or written here; the tax
    engine already prefers it over `subtotal_eur` once set.

    ``since`` (ISO date) restricts the scan to invoices with `invoice_date >=
    since`. ``dry_run=True`` (the default) computes and reports without
    writing.
    """
    conn = get_connection(db_path)
    try:
        query = (
            "SELECT id, filename, direction, invoice_date, currency, original_currency, "
            "original_amount, subtotal_eur, iva_amount, total_eur, charged_eur, "
            "fx_rate_used, fx_rate_date, fx_source, fx_stale, fx_cross_check_diff_pct, locked_fields "
            "FROM invoices WHERE original_currency IS NOT NULL AND original_currency != 'EUR'"
        )
        params: list = []
        if since:
            query += " AND invoice_date >= ?"
            params.append(since)
        rows = conn.execute(query, params).fetchall()

        scanned = 0
        changed = 0
        stale = 0
        cross_check_flagged = 0
        locked_skipped = 0
        result_rows: list[InvoiceFxRecomputeRow] = []

        for row in rows:
            scanned += 1
            locked = set(parse_locked_fields(row["locked_fields"]))
            if locked & _FX_LOCK_GUARDS:
                locked_skipped += 1
                result_rows.append(InvoiceFxRecomputeRow(
                    invoice_id=row["id"], filename=row["filename"], direction=row["direction"],
                    currency=row["original_currency"], old_total_eur=row["total_eur"],
                    new_total_eur=row["total_eur"], fx_source=row["fx_source"] or "",
                    fx_stale=bool(row["fx_stale"]), fx_cross_check_diff_pct=row["fx_cross_check_diff_pct"],
                    locked_skipped=True,
                ))
                continue

            data = {
                "invoice_date": row["invoice_date"],
                "currency": row["currency"],
                "original_currency": row["original_currency"],
                "original_amount": row["original_amount"],
                "subtotal_eur": row["subtotal_eur"],
                "iva_amount": row["iva_amount"],
                "total_eur": row["total_eur"],
                "charged_eur": row["charged_eur"],
            }
            fx = resolve_invoice_amounts(row["direction"], data, db_path)

            if fx.fx_stale:
                stale += 1
            if fx.fx_cross_check_diff_pct is not None and fx.fx_cross_check_diff_pct > FX_CROSS_CHECK_THRESHOLD_PCT:
                cross_check_flagged += 1

            new_values = {
                "subtotal_eur": fx.subtotal_eur, "iva_amount": fx.iva_amount, "total_eur": fx.total_eur,
                "fx_rate_used": fx.fx_rate_used, "fx_rate_date": fx.fx_rate_date, "fx_source": fx.fx_source,
                "fx_stale": fx.fx_stale, "fx_cross_check_diff_pct": fx.fx_cross_check_diff_pct,
            }
            old_values = {f: row[f] for f in _FX_WRITE_FIELDS}
            # "Changed" tracks the EUR value itself, not the provenance columns:
            # once a row has been corrected, its stored total_eur becomes the
            # new baseline `resolve_invoice_amounts` cross-checks against, so a
            # second pass naturally recomputes fx_cross_check_diff_pct as 0 —
            # that alone must not count as a change, or a second run would
            # never be idempotent. Only a genuine amount correction writes.
            needs_write = (
                _floats_differ(new_values["subtotal_eur"], old_values["subtotal_eur"])
                or _floats_differ(new_values["iva_amount"], old_values["iva_amount"])
                or _floats_differ(new_values["total_eur"], old_values["total_eur"])
            )

            if needs_write:
                changed += 1
                log.info(
                    "ℹ️ FX recompute: %s (%s) %s total %.2f → %.2f EUR [%s]%s",
                    row["filename"], row["direction"], row["original_currency"],
                    old_values["total_eur"] or 0.0, new_values["total_eur"] or 0.0, fx.fx_source,
                    " (dry-run, not written)" if dry_run else "",
                )
                result_rows.append(InvoiceFxRecomputeRow(
                    invoice_id=row["id"], filename=row["filename"], direction=row["direction"],
                    currency=row["original_currency"], old_total_eur=old_values["total_eur"],
                    new_total_eur=new_values["total_eur"], fx_source=fx.fx_source,
                    fx_stale=fx.fx_stale, fx_cross_check_diff_pct=fx.fx_cross_check_diff_pct,
                    locked_skipped=False,
                ))
                if not dry_run:
                    conn.execute(
                        """UPDATE invoices SET
                               subtotal_eur = :subtotal_eur, iva_amount = :iva_amount,
                               total_eur = :total_eur, fx_rate_used = :fx_rate_used,
                               fx_rate_date = :fx_rate_date, fx_source = :fx_source,
                               fx_stale = :fx_stale, fx_cross_check_diff_pct = :fx_cross_check_diff_pct
                           WHERE id = :id""",
                        {**new_values, "fx_stale": 1 if new_values["fx_stale"] else 0, "id": row["id"]},
                    )

        if not dry_run:
            conn.commit()

        return InvoiceFxRecomputeResult(
            scanned=scanned, changed=changed, stale=stale, cross_check_flagged=cross_check_flagged,
            locked_skipped=locked_skipped, dry_run=dry_run, rows=result_rows,
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Exchange differences (issue #93 / private decision D5): a later conversion
# of a foreign-currency balance from activity income into EUR realises a
# gain or loss against the EUR figure originally booked. Recorded here and
# fed into Modelo 130 box 01 (c01_ingresos) in the period of conversion.
# ---------------------------------------------------------------------------

def record_exchange_difference(
    conversion_date: str,
    currency: str,
    foreign_amount: float,
    eur_obtained: float,
    booked_eur: float,
    invoice_id: Optional[str] = None,
    notes: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> int:
    """Record a foreign-currency balance conversion and its resulting gain/loss.

    ``gain_loss_eur = eur_obtained - booked_eur``: positive when the
    conversion yields more EUR than was originally booked (ECB rate at
    accrual, or an earlier partial conversion), negative when it yields less.
    Returns the new row's id.
    """
    conn = get_connection(db_path)
    try:
        gain_loss = round(float(eur_obtained) - float(booked_eur), 2)
        cur = conn.execute(
            """INSERT INTO fx_exchange_differences
                   (invoice_id, conversion_date, currency, foreign_amount,
                    eur_obtained, booked_eur, gain_loss_eur, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (invoice_id, conversion_date, currency.upper(), float(foreign_amount),
             float(eur_obtained), float(booked_eur), gain_loss, notes),
        )
        conn.commit()
        log.info(
            "ℹ️ Recorded FX exchange difference: %s %.2f → %.2f EUR (booked %.2f, gain/loss %.2f)",
            currency.upper(), foreign_amount, eur_obtained, booked_eur, gain_loss,
        )
        return cur.lastrowid
    finally:
        conn.close()


def get_exchange_differences(
    year: Optional[int] = None,
    db_path: Optional[str | Path] = None,
) -> list[dict]:
    """List recorded exchange differences, newest first, optionally filtered by year."""
    conn = get_connection(db_path)
    try:
        if year is not None:
            rows = conn.execute(
                """SELECT * FROM fx_exchange_differences
                   WHERE conversion_date >= ? AND conversion_date <= ?
                   ORDER BY conversion_date DESC""",
                (f"{year}-01-01", f"{year}-12-31"),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM fx_exchange_differences ORDER BY conversion_date DESC"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_exchange_difference(diff_id: int, db_path: Optional[str | Path] = None) -> None:
    """Delete one recorded exchange difference (data-entry correction)."""
    conn = get_connection(db_path)
    try:
        conn.execute("DELETE FROM fx_exchange_differences WHERE id = ?", (diff_id,))
        conn.commit()
    finally:
        conn.close()
