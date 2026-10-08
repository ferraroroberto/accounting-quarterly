"""Config plumbing, record loaders and per-row figures shared by every tax model.

Everything the Modelo 303 / 130 / 349 / 347 / OSS computations (``src.tax_engine``) and the P&L by
activity (``src.pl_by_activity``) read their records through: the ``config.tax`` accessors, the
``tax.activity_start_date`` floor and its audit note, the quarter / year-to-date loaders for Stripe
transactions and invoices, and the VAT base / treatment of a row. No model logic lives here.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from src.declared_reports import apply_frozen_amounts
from src.periods import quarter_iso_bounds
from src.tax_codes import derive_tax_treatment_for_invoice
from src.vat_rules import vat_amount_on_base, vat_base_from_inclusive, vat_treatment

# ---------------------------------------------------------------------------
# App-config plumbing
# ---------------------------------------------------------------------------
# The tax engine is config-aware: several ``config.tax`` settings alter the
# computation (EU VAT-treatment overrides, IVA/OSS registration, prorrata,
# fiscal regime). The public compute functions take an optional ``config``
# dict; when it is ``None`` they fall back to an empty dict, i.e. the documented
# defaults — this keeps the engine pure and the unit tests deterministic. The
# app entry points (``compute_and_persist_tax_snapshots`` and the tax validator)
# load the real ``config.json`` once and thread it down.

def tax_settings(config: Optional[dict]) -> dict:
    """Return the ``tax`` sub-section of the app config (empty dict if absent)."""
    return (config or {}).get("tax", {})


def load_app_config() -> dict:
    """Load ``config.json`` for the engine, returning ``{}`` if unavailable.

    Guarded so the engine never crashes on a missing/broken config file — a
    fresh checkout with no ``config.json`` simply runs with documented defaults.
    """
    try:
        from src.config import load_config

        return load_config()
    except Exception:  # pragma: no cover - config is optional for the engine
        return {}


def activity_start_date(config: Optional[dict]) -> Optional[str]:
    """ISO date (``YYYY-MM-DD``) the business activity began, from ``tax.activity_start_date``.

    ``None`` when the key is absent — every date-range query then keeps its
    natural lower bound (documented default, unchanged behaviour). Set, it
    floors every quarter/YTD range the tax models query: a transaction or
    invoice dated before it never feeds a return (issue #133).
    """
    raw = tax_settings(config).get("activity_start_date")
    return str(raw)[:10] if raw else None


def clamp_start(start: str, config: Optional[dict]) -> str:
    """Raise a query's ``start`` bound to ``tax.activity_start_date`` when that is later."""
    floor = activity_start_date(config)
    return max(start, floor) if floor else start


def _excluded_by_activity_start(
    conn: sqlite3.Connection, config: Optional[dict], natural_start: str, end: str,
    table: str, date_col: str, amount_sql: str, where_extra: str = "",
) -> Optional[tuple[int, float]]:
    """(count, total EUR) of ``table`` rows in ``[natural_start, end]`` left out of this
    period by ``tax.activity_start_date`` — feeds the models' audit notes (issue #133).

    ``None`` when no floor is set, or it does not reach into this period (nothing excluded).
    """
    floor = activity_start_date(config)
    if not floor or floor <= natural_start:
        return None
    cutoff = min(floor, end)
    if cutoff <= natural_start:
        return None
    row = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({amount_sql}), 0) FROM {table} "
        f"WHERE {date_col} >= ? AND {date_col} < ? {where_extra}",
        (natural_start, cutoff),
    ).fetchone()
    n = row[0]
    return (n, float(row[1])) if n else None


# The records ``tax.activity_start_date`` can leave out, per source: the audit-note label, the table,
# its date column, the EUR amount SQL and the filter that mirrors the matching loader's WHERE clause
# (``_load_classified_range`` / the invoice loaders). One table, so the three notes stay in step with them.
_ACTIVITY_START_SOURCES: dict[str, tuple[str, str, str, str, str]] = {
    "stripe": ("Stripe transaction(s)", "transactions", "created_date",
               "converted_amount - converted_amount_refunded",
               "AND activity_type IS NOT NULL AND activity_type != 'UNKNOWN'"),
    "income": ("issued invoice(s)", "invoices", "invoice_date",
               "COALESCE(eur_received, subtotal_eur, 0)",
               "AND direction = 'out' AND COALESCE(excluded, 0) = 0"),
    "expense": ("received invoice(s)", "invoices", "invoice_date", "subtotal_eur",
                "AND direction = 'in' AND COALESCE(excluded, 0) = 0"),
}


def activity_start_note(
    conn: sqlite3.Connection, config: Optional[dict], natural_start: str, ends: dict[str, str], model: str,
) -> Optional[str]:
    """Audit note for the records ``tax.activity_start_date`` left out of a ``model`` return.

    ``ends`` maps each source in ``_ACTIVITY_START_SOURCES`` that feeds the return to the end bound
    of its query. ``None`` when nothing was left out.
    """
    parts = []
    for key, (label, table, date_col, amount_sql, where_extra) in _ACTIVITY_START_SOURCES.items():
        if key not in ends:
            continue
        excl = _excluded_by_activity_start(
            conn, config, natural_start, ends[key], table, date_col, amount_sql, where_extra)
        if excl:
            n, total = excl
            parts.append(f"{n} {label} ({total:,.2f} EUR)")
    if not parts:
        return None
    return (f"Dated before the activity start ({activity_start_date(config)}) and left out of the {model}: "
            + "; ".join(parts) + ".")


# ---------------------------------------------------------------------------
# Internal DB helpers
# ---------------------------------------------------------------------------

def _load_classified_range(
    start: str, end: str, conn: sqlite3.Connection
) -> list[dict]:
    """Load classified transactions in ``[start, end]`` from an open connection.

    Shared loader for the quarter- and YTD-scoped wrappers below: the SELECT,
    table, ``activity_type`` filter and ordering are identical between them —
    only the ``start`` bound differs.

    Transactions that appear in a frozen (declared) Stripe report carry the
    declared EUR amounts instead of the live ones, so later FX re-conversions
    cannot move a quarter that was already sent to the gestor.
    """
    rows = conn.execute(
        """SELECT id, created_date, converted_amount, converted_amount_refunded,
                  activity_type, geo_region, card_country, email_meta,
                  vat_treatment, vat_base_eur, vat_amount_eur, oss_country, buyer_vat_id,
                  fee_application
           FROM transactions
           WHERE created_date >= ? AND created_date <= ?
             AND activity_type IS NOT NULL AND activity_type != 'UNKNOWN'
           ORDER BY created_date""",
        (start, end),
    ).fetchall()
    return apply_frozen_amounts([dict(r) for r in rows], conn)


def classified_quarter_end(year: int, quarter: int) -> str:
    """End bound (inclusive, end-of-day) for transaction queries up to ``quarter``."""
    return f"{quarter_iso_bounds(year, quarter)[1]}T23:59:59"


def load_classified_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict] = None
) -> list[dict]:
    """Load classified transactions for a specific quarter from an open connection.

    ``config``'s ``tax.activity_start_date`` (issue #133), when set and later
    than the quarter start, raises the lower bound — a charge before it is left out.
    """
    month_start = (quarter - 1) * 3 + 1
    start = clamp_start(f"{year}-{month_start:02d}-01", config)
    end = classified_quarter_end(year, quarter)
    return _load_classified_range(start, end, conn)


def load_classified_ytd(
    year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict] = None
) -> list[dict]:
    """Load classified transactions from Q1 through the given quarter.

    ``config``'s ``tax.activity_start_date`` (issue #133) raises the lower bound
    from 1 January when set and later, same as the quarter-scoped loader.
    """
    end = classified_quarter_end(year, quarter)
    start = clamp_start(f"{year}-01-01", config)
    return _load_classified_range(start, end, conn)


def _load_expense_invoices_range(
    start: str, end: str, conn: sqlite3.Connection
) -> list[dict]:
    """Expense invoices (direction='in') in ``[start, end]``, keyed by ``invoice_date``.

    The accounting date is the invoice date (``supply_date`` is informational
    only), and rows marked ``excluded`` (duplicates, receipts, …) are skipped.
    Shared loader for the quarter- and YTD-scoped wrappers below: the projection,
    ``direction='in'`` filter, date keying and ordering are identical — only the
    ``start`` bound differs.
    """
    rows = conn.execute(
        """SELECT id, invoice_date AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment, tax_treatment,
                  COALESCE(deductible_pct_vat, deductible_pct, 100.0) AS deductible_pct_vat,
                  COALESCE(deductible_pct_irpf, deductible_pct, 100.0) AS deductible_pct_irpf,
                  vendor_nif, vendor_name, description
           FROM invoices
           WHERE direction = 'in'
             AND COALESCE(excluded, 0) = 0
             AND invoice_date >= ?
             AND invoice_date <= ?
           ORDER BY tx_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def load_expense_invoices_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict] = None
) -> list[dict]:
    """Expense invoices (direction='in') for the quarter, keyed by invoice_date.

    ``config``'s ``tax.activity_start_date`` (issue #133) raises the lower bound
    when set and later than the quarter start.
    """
    start, end = quarter_iso_bounds(year, quarter)
    return _load_expense_invoices_range(clamp_start(start, config), end, conn)


def load_expense_invoices_ytd(
    year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict] = None
) -> list[dict]:
    """Expense invoices (direction='in') from Q1 through the given quarter (YTD).

    ``config``'s ``tax.activity_start_date`` (issue #133) raises the lower bound
    from 1 January when set and later.
    """
    _, end = quarter_iso_bounds(year, quarter)
    return _load_expense_invoices_range(clamp_start(f"{year}-01-01", config), end, conn)


def _load_income_invoices_range(
    start: str, end: str, conn: sqlite3.Connection
) -> list[dict]:
    """Income invoices (direction='out') in ``[start, end]``, keyed by ``invoice_date``.

    These are manually-issued invoices (bank transfer, etc.) NOT processed through
    Stripe — Stripe income already lives in the ``transactions`` table.

    Shared loader for the quarter- and YTD-scoped wrappers below: the projection,
    ``direction='out'`` filter, invoice-date keying, ``excluded`` filter and
    ordering are identical — only the ``start`` bound differs. Kept separate from
    the expense loader because the projection differs (client_nif/client_name vs
    vendor_nif/vendor_name).
    """
    rows = conn.execute(
        """SELECT id, invoice_date AS tx_date,
                  subtotal_eur, iva_rate, iva_amount, irpf_rate, irpf_amount,
                  total_eur, category, geo_region, vat_treatment, tax_treatment,
                  client_nif, client_name, description, eur_received
           FROM invoices
           WHERE direction = 'out'
             AND COALESCE(excluded, 0) = 0
             AND invoice_date >= ?
             AND invoice_date <= ?
           ORDER BY tx_date""",
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def load_income_invoices_ytd(
    year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict] = None
) -> list[dict]:
    """Income invoices (direction='out') from Q1 through the given quarter (YTD).

    ``config``'s ``tax.activity_start_date`` (issue #133) raises the lower bound
    from 1 January when set and later.
    """
    _, end = quarter_iso_bounds(year, quarter)
    return _load_income_invoices_range(clamp_start(f"{year}-01-01", config), end, conn)


def load_income_invoices_for_quarter(
    year: int, quarter: int, conn: sqlite3.Connection, config: Optional[dict] = None
) -> list[dict]:
    """Income invoices (direction='out') for the quarter only.

    ``config``'s ``tax.activity_start_date`` (issue #133) raises the lower bound
    when set and later than the quarter start.
    """
    start, end = quarter_iso_bounds(year, quarter)
    return _load_income_invoices_range(clamp_start(start, config), end, conn)


def income_invoice_eur(inv: dict) -> float:
    """Effective EUR value of an income (direction='out') invoice (issue #93).

    ``eur_received`` wins when set — the money was actually converted on
    receipt. Otherwise the stored ``subtotal_eur`` already holds the ECB rate
    at the invoice date (resolved at OCR time by
    ``src.fx_rates.resolve_invoice_amounts``), which is final per decision D5
    (art. 79.Once LIVA) for income kept in a foreign-currency account — not a
    provisional figure to be revisited later.
    """
    eur_received = inv.get("eur_received")
    return float(eur_received) if eur_received is not None else (inv.get("subtotal_eur") or 0.0)


def net_amount(row: dict) -> float:
    # Row-dict counterpart of Payment.net_amount (src/models.py) — same formula,
    # different input shape (SQL row dict vs. Pydantic model); kept in sync by hand.
    return round(row["converted_amount"] - row["converted_amount_refunded"], 2)


def get_vat_treatment(row: dict, config: Optional[dict] = None) -> str:
    """Return stored vat_treatment, or derive it on-the-fly if missing.

    The fallback derivation delegates to the shared treatment matrix in
    ``src.vat_rules`` so the classifier and the engine cannot diverge. The
    ``config`` dict is threaded through so the EU VAT-treatment overrides
    (``tax.default_vat_treatment_eu_*``) and the ``tax.vat_registered`` flag
    are honoured. Rows carrying an explicit stored treatment (manual override)
    win over the derivation.
    """
    stored = row.get("vat_treatment")
    if stored and stored != "UNKNOWN":
        return stored
    # Derive from activity × geo (fallback for rows not yet VAT-classified).
    # buyer_vat_id (accounting-quarterly#113) decides the EU B2B/B2C split.
    return vat_treatment(
        row.get("activity_type"), row.get("geo_region"), config=config,
        buyer_vat_id=row.get("buyer_vat_id"),
    )


def invoice_tax_treatment(direction: str, inv: dict) -> Optional[str]:
    """The ledger ``tax_treatment``, derived from the legacy columns when unset."""
    return inv.get("tax_treatment") or derive_tax_treatment_for_invoice(
        direction, inv.get("vat_treatment"), inv.get("geo_region"), inv.get("iva_amount")
    )


def oss_country_code(row: dict) -> str:
    return (row.get("oss_country") or row.get("card_country") or "").upper()


def get_vat_base(row: dict, config: Optional[dict] = None) -> float:
    """Return the ex-VAT taxable base for a transaction row.

    Stripe amounts are VAT-inclusive (the customer paid the gross amount).
    For Spain (IVA_ES_21) and EU B2C (OSS_EU) we extract the base by
    dividing by (1 + rate).  For exports and EU B2B (ISP) the full net
    amount is the income base — no VAT was charged.
    """
    if row.get("vat_base_eur") is not None:
        return row["vat_base_eur"]
    return vat_base_from_inclusive(
        net_amount(row), get_vat_treatment(row, config), oss_country_code(row)
    )


def get_vat_amount(row: dict, config: Optional[dict] = None) -> float:
    if row.get("vat_amount_eur") is not None:
        return row["vat_amount_eur"]
    return vat_amount_on_base(
        get_vat_base(row, config), get_vat_treatment(row, config), oss_country_code(row)
    )


def get_tax_entries_total(
    year: int, quarter: int, entry_type: str, conn: sqlite3.Connection, ytd: bool = False
) -> float:
    if ytd:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount_eur), 0) AS total
               FROM quarterly_tax_entries
               WHERE year = ? AND quarter <= ? AND entry_type = ?""",
            (year, quarter, entry_type),
        ).fetchone()
    else:
        row = conn.execute(
            """SELECT COALESCE(SUM(amount_eur), 0) AS total
               FROM quarterly_tax_entries
               WHERE year = ? AND quarter = ? AND entry_type = ?""",
            (year, quarter, entry_type),
        ).fetchone()
    return float(row["total"]) if row else 0.0
