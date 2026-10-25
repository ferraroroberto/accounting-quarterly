"""Tax calendar and snapshot persistence, plus the public re-exports of every model's compute function.

Each model lives in its own module (``modelo_303``, ``modelo_130``, ``modelo_349``, ``modelo_347``,
``oss_return``); the names re-exported here keep ``from src.tax_engine import compute_modelo_*`` working.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from typing import Any, Optional

from src.logger import get_logger
from src.returns.modelo_130 import compute_modelo_130
from src.returns.modelo_303 import compute_modelo_303
from src.returns.modelo_347 import compute_modelo_347
from src.returns.modelo_349 import compute_modelo_349
from src.returns.oss_return import compute_eu_b2c_threshold, compute_oss_return  # noqa: F401  (re-exported)
from src.tax_data import load_app_config
from src.tax_deadlines import calendar_deadline
from src.tax_models import TaxDeadline

log = get_logger(__name__)

_DUE_SOON_DAYS = 15


def get_tax_calendar(year: int, db_conn: Optional[sqlite3.Connection] = None) -> list[TaxDeadline]:
    """Return all quarterly and annual tax deadlines for the year with their status."""
    today = date.today()
    deadlines: list[TaxDeadline] = []

    model_names = {
        "303": "Declaración IVA Trimestral",
        "130": "Pago Fraccionado IRPF",
        "349": "Operaciones Intracomunitarias",
        "OSS": "One Stop Shop (IVA digital services)",
        "390": "Resumen Anual IVA",
        "347": "Operaciones con Terceros",
    }

    # Fetch filed statuses from DB if connection provided
    filed_lookup: dict[str, dict] = {}
    if db_conn:
        rows = db_conn.execute(
            "SELECT model, quarter, status, amount_eur FROM tax_filing_status WHERE year = ?",
            (year,),
        ).fetchall()
        for r in rows:
            key = f"{r['model']}_{r['quarter'] or 'annual'}"
            filed_lookup[key] = dict(r)

    def _status(ddl: date, key: str) -> str:
        rec = filed_lookup.get(key, {})
        if rec.get("status") == "FILED":
            return "FILED"
        if ddl < today:
            return "OVERDUE"
        if (ddl - today).days <= _DUE_SOON_DAYS:
            return "DUE"
        return "PENDING"

    # Quarterly models
    for model in ("303", "130", "349", "OSS"):
        for q in range(1, 5):
            ddl = calendar_deadline(model, year, q)
            key = f"{model}_{q}"
            rec = filed_lookup.get(key, {})
            deadlines.append(TaxDeadline(
                model=model,  # type: ignore[arg-type]
                name=model_names[model],
                year=year,
                quarter=q,
                deadline=ddl,
                status=_status(ddl, key),  # type: ignore[arg-type]
                amount_eur=rec.get("amount_eur"),
            ))

    # Annual models
    for model in ("390", "347"):
        ddl = calendar_deadline(model, year)
        key = f"{model}_annual"
        rec = filed_lookup.get(key, {})
        deadlines.append(TaxDeadline(
            model=model,  # type: ignore[arg-type]
            name=model_names[model],
            year=year,
            quarter=None,
            deadline=ddl,
            status=_status(ddl, key),  # type: ignore[arg-type]
            amount_eur=rec.get("amount_eur"),
        ))

    deadlines.sort(key=lambda d: d.deadline)
    return deadlines


def compute_tax_results(
    year: int, quarter: int, db_conn: sqlite3.Connection, config: Optional[dict] = None
) -> dict[str, Any]:
    """Run every obligation engine for the selected period, keyed by snapshot model.

    The single place the model list lives (#189) — ``compute_and_persist_tax_snapshots``
    persists exactly these results instead of recomputing them, and callers that need
    the results *and* a change check (e.g. ``close_pipeline.step_compute``) call this
    once and pass the results on to persist.
    """
    if config is None:
        config = load_app_config()
    return {
        "303": compute_modelo_303(year, quarter, db_conn, config),
        "130": compute_modelo_130(year, quarter, db_conn, config),
        "OSS": compute_oss_return(year, quarter, db_conn, config),
        "349": compute_modelo_349(year, quarter, db_conn, config),
        "347": compute_modelo_347(year, db_conn, config),
    }


def compute_and_persist_tax_snapshots(
    year: int, quarter: int, db_conn: sqlite3.Connection, config: Optional[dict] = None,
    results: Optional[dict[str, Any]] = None,
) -> str:
    """Persist JSON snapshots for the selected period's tax obligations.

    Quarterly models (303, 130, OSS, 349) use ``quarter``; Modelo 347 is annual and is
    stored with ``quarter`` = ``TAX_SNAPSHOT_QUARTER_ANNUAL`` (0).

    The app config is loaded once here and threaded through every engine so the
    ``config.tax`` settings (EU VAT overrides, IVA/OSS registration, prorrata,
    regime) drive the computation. Callers may pass an explicit ``config`` dict.

    ``results``, when given, must be the dict ``compute_tax_results`` returns —
    callers that already computed it (to check for changes before persisting)
    pass it through so each model is computed exactly once. Otherwise it is
    computed here.

    Returns the shared ISO ``computed_at`` timestamp written on every snapshot row.
    """
    from src.database import (
        TAX_SNAPSHOT_QUARTER_ANNUAL,
        upsert_audit_entries_conn,
        upsert_tax_snapshot_conn,
    )
    from src.tax_snapshot_codec import encode_snapshot

    if config is None:
        config = load_app_config()
    if results is None:
        results = compute_tax_results(year, quarter, db_conn, config)

    computed_at = datetime.now().isoformat(timespec="seconds")

    for model in ("303", "130", "OSS", "349"):
        result = results[model]
        upsert_tax_snapshot_conn(db_conn, year, quarter, model, encode_snapshot(model, result), computed_at)
        upsert_audit_entries_conn(db_conn, result.audit, computed_at)

    r347 = results["347"]
    upsert_tax_snapshot_conn(
        db_conn, year, TAX_SNAPSHOT_QUARTER_ANNUAL, "347",
        encode_snapshot("347", r347), computed_at,
    )
    upsert_audit_entries_conn(db_conn, r347.audit, computed_at)

    db_conn.commit()
    return computed_at
