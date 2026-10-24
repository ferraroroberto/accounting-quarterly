"""Tax tables: manual entries, filing status, versioned snapshots and the audit log."""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.database.connection import get_connection
from src.database.schema import SNAPSHOT_COMPUTED, SNAPSHOT_FILED, _ensure_audit_schema

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# quarterly_tax_entries helpers
# ---------------------------------------------------------------------------

def get_tax_entries(year: int, quarter: int,
                    db_path: Optional[str | Path] = None) -> list[dict]:
    """Return all manual tax entries for the given year/quarter."""
    conn = get_connection(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM quarterly_tax_entries WHERE year = ? AND quarter = ? ORDER BY id",
            (year, quarter),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_tax_entry(year: int, quarter: int, entry_type: str, amount_eur: float,
                  description: str = "", notes: str = "",
                  db_path: Optional[str | Path] = None,
                  vat_rate: Optional[float] = None) -> int:
    """Insert a manual tax entry. Returns the new row id.

    ``vat_rate`` (percent, e.g. 21) is required for ``IVA_SOPORTADO`` entries
    to derive their Modelo 303 base; ``None`` for other entry types.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            """INSERT INTO quarterly_tax_entries
               (year, quarter, entry_type, amount_eur, description, notes, vat_rate)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (year, quarter, entry_type, amount_eur, description, notes, vat_rate),
        )
        conn.commit()
        return cursor.lastrowid
    finally:
        conn.close()


def delete_tax_entry(entry_id: int, db_path: Optional[str | Path] = None) -> bool:
    """Delete a manual tax entry by id. Returns True if deleted."""
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "DELETE FROM quarterly_tax_entries WHERE id = ?", (entry_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# tax_filing_status helpers
# ---------------------------------------------------------------------------

def upsert_filing_status(year: int, model: str, quarter: Optional[int],
                         status: str, amount_eur: Optional[float] = None,
                         notes: str = "", filed_at: Optional[str] = None,
                         db_path: Optional[str | Path] = None) -> bool:
    """Insert or update a filing status record. Returns whether the row was written."""
    conn = get_connection(db_path)
    try:
        written = upsert_filing_status_conn(conn, year, model, quarter, status, amount_eur, notes, filed_at)
        conn.commit()
    finally:
        conn.close()
    return written


def upsert_filing_status_conn(conn: sqlite3.Connection, year: int, model: str, quarter: Optional[int],
                              status: str, amount_eur: Optional[float] = None,
                              notes: str = "", filed_at: Optional[str] = None) -> bool:
    """``upsert_filing_status`` on an open connection (the caller commits).

    A FILED row is never downgraded: a non-FILED status for a period already
    marked filed leaves the row untouched and returns ``False`` (#187).
    """
    cur = conn.execute(
        """INSERT INTO tax_filing_status (year, model, quarter, status, amount_eur, notes, filed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(year, model, COALESCE(quarter, -1)) DO UPDATE SET
               status = excluded.status,
               amount_eur = excluded.amount_eur,
               notes = excluded.notes,
               filed_at = excluded.filed_at
           WHERE tax_filing_status.status != 'FILED' OR excluded.status = 'FILED'""",
        (year, model, quarter, status, amount_eur, notes, filed_at),
    )
    if cur.rowcount == 0:
        log.info("ℹ️ Modelo %s %s Q%s is already FILED; %s status not written",
                 model, year, quarter or "annual", status)
        return False
    return True


# Quarter value for annual-only snapshots (e.g. Modelo 347).
TAX_SNAPSHOT_QUARTER_ANNUAL = 0


def upsert_tax_snapshot_conn(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    model: str,
    payload_json: str,
    computed_at: str,
) -> None:
    """Store a freshly computed snapshot without ever touching a FILED version.

    The single write path of ``compute_and_persist_tax_snapshots`` (so the
    FILED guard lives here rather than in the engine):

    - no snapshot yet → version 1, COMPUTED;
    - latest version COMPUTED → that draft is rewritten in place;
    - latest version FILED → a new COMPUTED version (latest + 1) is added,
      unless the payload is identical to the filed one (nothing changed).
    """
    latest = conn.execute(
        """SELECT snapshot_version, status, payload_json FROM tax_computation_snapshots
           WHERE year = ? AND quarter = ? AND model = ?
           ORDER BY snapshot_version DESC LIMIT 1""",
        (year, quarter, model),
    ).fetchone()
    if latest is not None and latest[1] == SNAPSHOT_COMPUTED:
        conn.execute(
            """UPDATE tax_computation_snapshots SET payload_json = ?, computed_at = ?
               WHERE year = ? AND quarter = ? AND model = ? AND snapshot_version = ?""",
            (payload_json, computed_at, year, quarter, model, latest[0]),
        )
        return
    if latest is not None and latest[2] == payload_json:
        return  # recompute matches the filed version: no new draft
    version = 1 if latest is None else latest[0] + 1
    conn.execute(
        """INSERT INTO tax_computation_snapshots
               (year, quarter, model, snapshot_version, status, payload_json, computed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (year, quarter, model, version, SNAPSHOT_COMPUTED, payload_json, computed_at),
    )
    if latest is not None:
        log.info("ℹ️ Modelo %s %s Q%s is filed (version %d): recompute stored as new draft version %d",
                 model, year, quarter, latest[0], version)


_SNAPSHOT_COLUMNS = ("model, quarter, snapshot_version, status, payload_json, computed_at, "
                     "justificante, presented_on, filed_at")


def load_tax_snapshots_for_period(
    year: int,
    quarter: int,
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """Latest snapshot version of each quarterly model for ``quarter``, plus annual Modelo 347 (``quarter`` 0)."""
    return conn.execute(
        f"""SELECT {_SNAPSHOT_COLUMNS}
            FROM tax_computation_snapshots s
            WHERE year = ? AND (quarter = ? OR (model = '347' AND quarter = ?))
              AND snapshot_version = (
                  SELECT MAX(t.snapshot_version) FROM tax_computation_snapshots t
                  WHERE t.year = s.year AND t.quarter = s.quarter AND t.model = s.model)""",
        (year, quarter, TAX_SNAPSHOT_QUARTER_ANNUAL),
    ).fetchall()


def load_tax_snapshot_versions(
    conn: sqlite3.Connection, year: int, quarter: int, model: str,
) -> list[sqlite3.Row]:
    """Every stored version of one model's snapshot, oldest first."""
    return conn.execute(
        f"""SELECT {_SNAPSHOT_COLUMNS} FROM tax_computation_snapshots
            WHERE year = ? AND quarter = ? AND model = ? ORDER BY snapshot_version""",
        (year, quarter, model),
    ).fetchall()


def filed_snapshot_periods(conn: sqlite3.Connection) -> set[tuple[int, int]]:
    """(year, quarter) of every period with a FILED snapshot.

    An annual one (quarter ``TAX_SNAPSHOT_QUARTER_ANNUAL``) covers its year's four quarters.
    """
    periods: set[tuple[int, int]] = set()
    for year, quarter in conn.execute(
        "SELECT DISTINCT year, quarter FROM tax_computation_snapshots WHERE status = ?", (SNAPSHOT_FILED,)
    ):
        if quarter == TAX_SNAPSHOT_QUARTER_ANNUAL:
            periods.update((year, q) for q in (1, 2, 3, 4))
        else:
            periods.add((year, quarter))
    return periods


def insert_filed_tax_snapshot_conn(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    model: str,
    justificante: str,
    presented_on: str,
    filed_at: str,
) -> tuple[int, str]:
    """Freeze the latest COMPUTED snapshot as a new, immutable FILED version.

    Returns ``(new_version, payload_json)``. Raises ``ValueError`` when there is
    no snapshot, or when the latest version is already FILED (recompute first
    to file a corrected return: that creates the draft to freeze). The caller
    commits.
    """
    latest = conn.execute(
        """SELECT snapshot_version, status, payload_json, computed_at FROM tax_computation_snapshots
           WHERE year = ? AND quarter = ? AND model = ?
           ORDER BY snapshot_version DESC LIMIT 1""",
        (year, quarter, model),
    ).fetchone()
    if latest is None:
        raise ValueError(f"no computed snapshot for Modelo {model} {year} Q{quarter} — calculate it first")
    if latest[1] == SNAPSHOT_FILED:
        raise ValueError(
            f"Modelo {model} {year} Q{quarter} version {latest[0]} is already filed — "
            "recompute to create a new draft before filing again"
        )
    version = latest[0] + 1
    conn.execute(
        """INSERT INTO tax_computation_snapshots
               (year, quarter, model, snapshot_version, status, payload_json, computed_at,
                justificante, presented_on, filed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (year, quarter, model, version, SNAPSHOT_FILED, latest[2], latest[3],
         justificante, presented_on, filed_at),
    )
    return version, latest[2]


# ---------------------------------------------------------------------------
# tax_audit_log helpers
# ---------------------------------------------------------------------------

def upsert_audit_entries_conn(
    conn: sqlite3.Connection,
    entries: list,  # list[AuditEntry]
    computed_at: str,
) -> None:
    """Persist a list of AuditEntry objects for the given computed_at timestamp.

    Replaces any existing entries for the same (year, quarter, model, computed_at)
    so re-running a calculation always gives a fresh, consistent audit trail.
    """
    if not entries:
        return
    first = entries[0]
    conn.execute(
        "DELETE FROM tax_audit_log WHERE year = ? AND quarter = ? AND model = ? AND computed_at = ?",
        (first.year, first.quarter, first.model, computed_at),
    )
    conn.executemany(
        """INSERT INTO tax_audit_log
               (computed_at, year, quarter, model, cell, label, formula, inputs_json, value)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (computed_at, e.year, e.quarter, e.model, e.cell,
             e.label, e.formula, e.inputs_json, e.value)
            for e in entries
        ],
    )


def load_audit_entries_conn(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    model: str,
    computed_at: Optional[str] = None,
) -> list[dict]:
    """``load_audit_entries`` on an open connection.

    If *computed_at* is None, returns entries from the most recent computation run.
    """
    _ensure_audit_schema(conn)
    if computed_at is None:
        row = conn.execute(
            """SELECT MAX(computed_at) FROM tax_audit_log
               WHERE year = ? AND quarter = ? AND model = ?""",
            (year, quarter, model),
        ).fetchone()
        if not row or not row[0]:
            return []
        computed_at = row[0]
    cur = conn.execute(
        """SELECT id, computed_at, year, quarter, model, cell, label, formula, inputs_json, value
           FROM tax_audit_log
           WHERE year = ? AND quarter = ? AND model = ? AND computed_at = ?
           ORDER BY id""",
        (year, quarter, model, computed_at),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def load_audit_entries(
    year: int,
    quarter: int,
    model: str,
    computed_at: Optional[str] = None,
    db_path: Optional[str | Path] = None,
) -> list[dict]:
    """Load audit log rows for a given period and model.

    If *computed_at* is None, returns entries from the most recent computation run.
    """
    conn = get_connection(db_path)
    try:
        return load_audit_entries_conn(conn, year, quarter, model, computed_at)
    finally:
        conn.close()
