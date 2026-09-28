"""Frozen ("declared") Stripe reports — the figures actually sent to the gestor.

Once a quarter's Stripe report is sent, its per-transaction EUR amounts are
what the filed returns were built on. Re-fetching from Stripe later re-converts
non-EUR charges at whatever ECB rate is stored *now*, so the live
``transactions`` table drifts away from the declared basis. Freezing the report
stores those per-transaction EUR amounts immutably; the tax engine then prefers
them over the live row for every transaction that appears in a frozen report
(see :func:`apply_frozen_amounts`).

Schema lives here (own module, own ``_ensure_declared_reports_schema``) rather
than in ``src/database.py``; every public function ensures it lazily, and
read-side helpers treat a missing table as "nothing frozen".

Immutability is enforced by SQLite triggers: rows can be inserted, never
updated or deleted. A corrected re-send is a new *version* for the same
quarter (``supersede=True``); the latest version is the one the engine uses.

Two ways in: :func:`freeze_report` stores the rows the pipeline itself just
wrote into the report; :func:`freeze_sent_report` stores the lines read back
from a report file sent earlier (before freezing existed), so its EUR amounts —
not today's live rows — become the declared basis.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from openpyxl import load_workbook

from src.exceptions import InvalidSentReportError, ReportAlreadyFrozenError
from src.logger import get_logger
from src.models import ClassifiedPayment

log = get_logger(__name__)

# The sheet ``src.excel_exporter._write_import_sheet`` writes, one row per charge.
IMPORT_SHEET = "import"
_COL_ID = "id"
_COL_DATE = "Created Date"
_COL_AMOUNT = "Converted Amount"
_COL_REFUNDED = "Converted Amount Refunded"
_COL_FEE = "Fee"
_COL_CURRENCY = "Currency"
_COL_NET = "Net Amount"
_COL_ACTIVITY = "Activity Type"
_COL_GEO = "Geo Region"
_REQUIRED_COLUMNS = (_COL_ID, _COL_DATE, _COL_AMOUNT, _COL_REFUNDED, _COL_FEE, _COL_CURRENCY)
_MAX_LISTED = 10  # ids / row problems quoted in one error or warning


@dataclass(frozen=True)
class DeclaredReport:
    id: int
    year: int
    quarter: int
    version: int
    file_name: str
    sha256: str
    n_transactions: int
    total_net_eur: float
    created_at: str


@dataclass(frozen=True)
class DeclaredLine:
    """One transaction of a declared report, as stored in ``declared_report_lines``."""

    transaction_id: str
    created_date: datetime
    currency: str
    amount_original: Optional[float]
    converted_amount: float
    converted_amount_refunded: float
    fee: float
    net_amount_eur: float
    activity_type: Optional[str]
    geo_region: Optional[str]

    @classmethod
    def from_payment(cls, p: ClassifiedPayment) -> "DeclaredLine":
        return cls(
            transaction_id=p.id, created_date=p.created_date, currency=p.currency,
            amount_original=p.amount_original, converted_amount=p.converted_amount,
            converted_amount_refunded=p.converted_amount_refunded, fee=p.fee,
            net_amount_eur=p.net_amount, activity_type=p.activity_type, geo_region=p.geo_region,
        )


@dataclass(frozen=True)
class SentReportFreeze:
    """Outcome of :func:`freeze_sent_report`.

    ``missing_from_live`` lists file ids with no row in ``transactions``: they
    are frozen anyway (the file is the evidence) but the engine cannot use them
    until the charge is fetched.
    """

    report: DeclaredReport
    missing_from_live: tuple[str, ...]


def _ensure_declared_reports_schema(conn: sqlite3.Connection) -> None:
    """Create ``declared_reports`` + ``declared_report_lines`` and their guards."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS declared_reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            year INTEGER NOT NULL,
            quarter INTEGER NOT NULL,
            version INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            n_transactions INTEGER NOT NULL,
            total_net_eur REAL NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE (year, quarter, version)
        );

        CREATE TABLE IF NOT EXISTS declared_report_lines (
            report_id INTEGER NOT NULL REFERENCES declared_reports(id),
            transaction_id TEXT NOT NULL,
            created_date TEXT NOT NULL,
            currency TEXT NOT NULL,
            amount_original REAL,
            converted_amount REAL NOT NULL,
            converted_amount_refunded REAL NOT NULL,
            fee REAL NOT NULL,
            net_amount_eur REAL NOT NULL,
            activity_type TEXT,
            geo_region TEXT,
            PRIMARY KEY (report_id, transaction_id)
        );

        CREATE INDEX IF NOT EXISTS idx_declared_report_lines_tx
            ON declared_report_lines(transaction_id);

        CREATE TRIGGER IF NOT EXISTS declared_reports_no_update
            BEFORE UPDATE ON declared_reports
            BEGIN SELECT RAISE(ABORT, 'declared_reports rows are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS declared_reports_no_delete
            BEFORE DELETE ON declared_reports
            BEGIN SELECT RAISE(ABORT, 'declared_reports rows are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS declared_report_lines_no_update
            BEFORE UPDATE ON declared_report_lines
            BEGIN SELECT RAISE(ABORT, 'declared_report_lines rows are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS declared_report_lines_no_delete
            BEFORE DELETE ON declared_report_lines
            BEGIN SELECT RAISE(ABORT, 'declared_report_lines rows are immutable'); END;
    """)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def file_sha256(path: str | Path) -> str:
    """SHA-256 hex digest of a file's bytes."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _row_to_report(row: sqlite3.Row) -> DeclaredReport:
    return DeclaredReport(
        id=row["id"], year=row["year"], quarter=row["quarter"], version=row["version"],
        file_name=row["file_name"], sha256=row["sha256"],
        n_transactions=row["n_transactions"], total_net_eur=row["total_net_eur"],
        created_at=row["created_at"],
    )


def get_declared_report(
    conn: sqlite3.Connection, year: int, quarter: int
) -> Optional[DeclaredReport]:
    """Return the latest declared report version for a quarter, or ``None``."""
    if not _table_exists(conn, "declared_reports"):
        return None
    prev_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT * FROM declared_reports WHERE year = ? AND quarter = ?
               ORDER BY version DESC LIMIT 1""",
            (year, quarter),
        ).fetchone()
    finally:
        conn.row_factory = prev_factory
    return _row_to_report(row) if row else None


def freeze_report(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    payments: list[ClassifiedPayment],
    report_path: str | Path,
    *,
    supersede: bool = False,
) -> DeclaredReport:
    """Store the report sent to the gestor as an immutable declared version.

    ``payments`` must be exactly the rows written into ``report_path`` — their
    EUR amounts become the declared basis for the quarter. Raises
    :class:`ReportAlreadyFrozenError` when the quarter already has a declared
    report and ``supersede`` is false.
    """
    return _store_declared_report(
        conn, year, quarter, [DeclaredLine.from_payment(p) for p in payments], report_path,
        supersede=supersede,
    )


def _store_declared_report(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    lines: list[DeclaredLine],
    report_path: str | Path,
    *,
    supersede: bool,
) -> DeclaredReport:
    """Insert one declared version + its lines in a single transaction."""
    _ensure_declared_reports_schema(conn)
    existing = get_declared_report(conn, year, quarter)
    if existing and not supersede:
        raise ReportAlreadyFrozenError(
            f"Q{quarter} {year} already has a declared report "
            f"(v{existing.version}, sha256 {existing.sha256[:12]}…, {existing.created_at}). "
            f"Pass supersede (CLI: --supersede) to store a corrected re-send as a new version."
        )
    version = (existing.version + 1) if existing else 1
    sha = file_sha256(report_path)
    total_net = round(sum(ln.net_amount_eur for ln in lines), 2)
    created_at = datetime.now().isoformat(timespec="seconds")

    try:
        cur = conn.execute(
            """INSERT INTO declared_reports
                   (year, quarter, version, file_name, sha256, n_transactions,
                    total_net_eur, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (year, quarter, version, Path(report_path).name, sha, len(lines),
             total_net, created_at),
        )
        report_id = cur.lastrowid
        conn.executemany(
            """INSERT INTO declared_report_lines
                   (report_id, transaction_id, created_date, currency, amount_original,
                    converted_amount, converted_amount_refunded, fee, net_amount_eur,
                    activity_type, geo_region)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (report_id, ln.transaction_id, ln.created_date.isoformat(), ln.currency,
                 ln.amount_original, ln.converted_amount, ln.converted_amount_refunded, ln.fee,
                 ln.net_amount_eur, ln.activity_type, ln.geo_region)
                for ln in lines
            ],
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    log.info(
        "✅ Froze declared report Q%d %d v%d | %d transactions | net %.2f EUR | sha256 %s",
        quarter, year, version, len(lines), total_net, sha,
    )
    report = get_declared_report(conn, year, quarter)
    assert report is not None
    return report


# ---------------------------------------------------------------------------
# Freezing a report file sent before freezing existed
# ---------------------------------------------------------------------------

def _cell_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _cell_amount(value: Any, column: str, blank_as_zero: bool) -> float:
    """A numeric cell; raises ``ValueError`` with a readable reason otherwise."""
    if _cell_text(value) is None:
        if blank_as_zero:
            return 0.0
        raise ValueError(f"{column} is empty")
    if isinstance(value, bool):
        raise ValueError(f"{column} is not a number ({value!r})")
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except ValueError:
        raise ValueError(f"{column} is not a number ({value!r})") from None


def _cell_datetime(value: Any) -> datetime:
    """``Created Date`` as written by the exporter (text) or re-typed by Excel (a date)."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day)
    text = _cell_text(value)
    if text is None:
        raise ValueError(f"{_COL_DATE} is empty")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{_COL_DATE} is not a date ({text!r})") from None


def _problems_error(path: Path, problems: list[str]) -> InvalidSentReportError:
    shown = "; ".join(problems[:_MAX_LISTED])
    more = f" (+{len(problems) - _MAX_LISTED} more)" if len(problems) > _MAX_LISTED else ""
    return InvalidSentReportError(
        f"{path.name}: {len(problems)} problem(s), nothing stored — {shown}{more}")


def read_sent_report_lines(xlsx_path: str | Path) -> list[DeclaredLine]:
    """Read the ``import`` sheet of a sent Stripe report into declared lines.

    Columns are matched by header name (case-insensitive), not position, so a
    file written by an older version of the exporter still reads. The sheet
    has no original-currency amount, so ``amount_original`` is ``None``.
    Raises :class:`InvalidSentReportError` for a missing file / sheet /
    column, an unreadable cell, a ``Net Amount`` that is not ``Converted
    Amount - Converted Amount Refunded``, a duplicated id, or no rows at all.
    """
    path = Path(xlsx_path)
    if not path.is_file():
        raise InvalidSentReportError(f"{path} does not exist")
    try:
        wb = load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several unrelated types for a non-xlsx file
        raise InvalidSentReportError(f"{path.name} is not a readable .xlsx workbook: {exc}") from exc
    try:
        sheet_name = next((n for n in wb.sheetnames if n.strip().lower() == IMPORT_SHEET), None)
        if sheet_name is None:
            raise InvalidSentReportError(
                f"{path.name} has no '{IMPORT_SHEET}' sheet (sheets: {', '.join(wb.sheetnames)})")
        rows = list(wb[sheet_name].iter_rows(values_only=True))
    finally:
        wb.close()

    if not rows:
        raise InvalidSentReportError(f"{path.name}: the '{IMPORT_SHEET}' sheet is empty")
    header: dict[str, int] = {}
    for idx, name in enumerate(rows[0]):
        key = (_cell_text(name) or "").lower()
        if key and key not in header:
            header[key] = idx
    missing_cols = [c for c in _REQUIRED_COLUMNS if c.lower() not in header]
    if missing_cols:
        raise InvalidSentReportError(
            f"{path.name}: the '{IMPORT_SHEET}' sheet lacks column(s) {', '.join(missing_cols)}")

    def cell(row: tuple, column: str) -> Any:
        idx = header.get(column.lower())
        return row[idx] if idx is not None and idx < len(row) else None

    lines: list[DeclaredLine] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for row_num, row in enumerate(rows[1:], start=2):
        if all(_cell_text(v) is None for v in row):
            continue
        tx_id = _cell_text(cell(row, _COL_ID))
        try:
            if tx_id is None:
                raise ValueError(f"{_COL_ID} is empty")
            created = _cell_datetime(cell(row, _COL_DATE))
            amount = _cell_amount(cell(row, _COL_AMOUNT), _COL_AMOUNT, blank_as_zero=False)
            refunded = _cell_amount(cell(row, _COL_REFUNDED), _COL_REFUNDED, blank_as_zero=True)
            fee = _cell_amount(cell(row, _COL_FEE), _COL_FEE, blank_as_zero=True)
            currency = _cell_text(cell(row, _COL_CURRENCY))
            if currency is None:
                raise ValueError(f"{_COL_CURRENCY} is empty")
            net = round(amount - refunded, 2)
            stated_net = cell(row, _COL_NET)
            if _cell_text(stated_net) is not None:
                stated = _cell_amount(stated_net, _COL_NET, blank_as_zero=False)
                if abs(round(stated, 2) - net) > 0.005:
                    raise ValueError(
                        f"{_COL_NET} {stated:.2f} is not {_COL_AMOUNT} - {_COL_REFUNDED} ({net:.2f})")
        except ValueError as exc:
            problems.append(f"row {row_num}{f' ({tx_id})' if tx_id else ''}: {exc}")
            continue
        if tx_id in seen:
            problems.append(f"row {row_num}: duplicate id {tx_id} (first on row {seen[tx_id]})")
            continue
        seen[tx_id] = row_num
        lines.append(DeclaredLine(
            transaction_id=tx_id, created_date=created, currency=currency.lower(),
            amount_original=None, converted_amount=amount, converted_amount_refunded=refunded,
            fee=fee, net_amount_eur=net,
            activity_type=_cell_text(cell(row, _COL_ACTIVITY)),
            geo_region=_cell_text(cell(row, _COL_GEO)),
        ))
    if problems:
        raise _problems_error(path, problems)
    if not lines:
        raise InvalidSentReportError(f"{path.name}: the '{IMPORT_SHEET}' sheet has no transaction rows")
    return lines


def _ids_missing_from_live(conn: sqlite3.Connection, ids: list[str]) -> list[str]:
    """The ``ids`` with no row in the live ``transactions`` table, sorted."""
    if not _table_exists(conn, "transactions"):
        return sorted(ids)
    live: set[str] = set()
    for i in range(0, len(ids), 500):  # stay under SQLite's host-parameter limit
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        live.update(r[0] for r in conn.execute(
            f"SELECT id FROM transactions WHERE id IN ({placeholders})", chunk))
    return sorted(set(ids) - live)


def freeze_sent_report(
    conn: sqlite3.Connection,
    year: int,
    quarter: int,
    xlsx_path: str | Path,
    *,
    supersede: bool = False,
) -> SentReportFreeze:
    """Freeze a Stripe report file sent to the gestor before freezing existed.

    The declared lines come from the file's ``import`` sheet — its EUR amounts
    are what the filed returns were built on — not from today's live rows; the
    file's SHA-256 is stored as with :func:`freeze_report`. The engine uses
    only the amounts (:func:`apply_frozen_amounts`): classification stays
    live, so a region mistake in the sent file is not re-imported.

    Everything is validated before anything is stored: a row dated outside the
    quarter, a duplicate id or an unreadable row raises
    :class:`InvalidSentReportError`. Ids with no live ``transactions`` row are
    frozen anyway and returned in ``missing_from_live``. Raises
    :class:`ReportAlreadyFrozenError` when the quarter is already declared and
    ``supersede`` is false.
    """
    path = Path(xlsx_path)
    lines = read_sent_report_lines(path)
    outside = [ln for ln in lines
               if ln.created_date.year != year or (ln.created_date.month - 1) // 3 + 1 != quarter]
    if outside:
        raise _problems_error(path, [
            f"{ln.transaction_id} dated {ln.created_date:%Y-%m-%d} is outside Q{quarter} {year}"
            for ln in outside
        ])

    missing = _ids_missing_from_live(conn, [ln.transaction_id for ln in lines])
    report = _store_declared_report(conn, year, quarter, lines, path, supersede=supersede)
    if missing:
        log.warning(
            "⚠️ %d id(s) of %s are not in the live transactions table (frozen anyway; the "
            "engine uses them once fetched): %s",
            len(missing), path.name, ", ".join(missing[:_MAX_LISTED]),
        )
    return SentReportFreeze(report=report, missing_from_live=tuple(missing))


def load_frozen_lines(
    conn: sqlite3.Connection, transaction_ids: Iterable[str]
) -> dict[str, dict]:
    """Declared EUR amounts for the given transactions, keyed by transaction id.

    Only the latest version of each quarter's declared report counts. Returns an
    empty dict when nothing is frozen (or the table does not exist yet).
    """
    ids = list(dict.fromkeys(transaction_ids))
    if not ids or not _table_exists(conn, "declared_report_lines"):
        return {}
    out: dict[str, dict] = {}
    # Chunk to stay under SQLite's host-parameter limit.
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT l.transaction_id, l.converted_amount, l.converted_amount_refunded,
                       l.fee, l.net_amount_eur, r.id AS report_id, r.year, r.quarter, r.version
                FROM declared_report_lines l
                JOIN declared_reports r ON r.id = l.report_id
                WHERE l.transaction_id IN ({placeholders})
                  AND r.version = (SELECT MAX(r2.version) FROM declared_reports r2
                                   WHERE r2.year = r.year AND r2.quarter = r.quarter)""",
            chunk,
        ).fetchall()
        for r in rows:
            out[r[0]] = {
                "converted_amount": r[1],
                "converted_amount_refunded": r[2],
                "fee": r[3],
                "net_amount_eur": r[4],
                "report_id": r[5],
                "year": r[6],
                "quarter": r[7],
                "version": r[8],
            }
    return out


def apply_frozen_amounts(rows: list[dict], conn: sqlite3.Connection) -> list[dict]:
    """Overlay declared EUR amounts onto live transaction row dicts (in place).

    Every row whose ``id`` appears in a frozen report gets the declared
    ``converted_amount`` / ``converted_amount_refunded`` and a
    ``declared_report_id`` marker; other rows are untouched. Rows lacking an
    ``id`` key are skipped.
    """
    frozen = load_frozen_lines(conn, (r["id"] for r in rows if r.get("id")))
    if not frozen:
        return rows
    drifted = 0
    for row in rows:
        f = frozen.get(row.get("id"))
        if not f:
            continue
        if (round(row.get("converted_amount") or 0.0, 2) != round(f["converted_amount"], 2)
                or round(row.get("converted_amount_refunded") or 0.0, 2)
                != round(f["converted_amount_refunded"], 2)):
            drifted += 1
        row["converted_amount"] = f["converted_amount"]
        row["converted_amount_refunded"] = f["converted_amount_refunded"]
        row["declared_report_id"] = f["report_id"]
    log.info(
        "ℹ️ Using declared-report EUR amounts for %d transaction(s) (%d differ from live rows)",
        sum(1 for r in rows if "declared_report_id" in r), drifted,
    )
    return rows


def declared_vs_live_drift(
    conn: sqlite3.Connection, year: int, quarter: int, payments: list[ClassifiedPayment]
) -> dict:
    """Compare a quarter's declared report against live rows.

    Returns counts of transactions whose EUR net differs, that are live but not
    declared, and that are declared but no longer live. Empty dict when the
    quarter has no declared report.
    """
    report = get_declared_report(conn, year, quarter)
    if report is None:
        return {}
    rows = conn.execute(
        """SELECT transaction_id, net_amount_eur FROM declared_report_lines
           WHERE report_id = ?""",
        (report.id,),
    ).fetchall()
    declared = {r[0]: r[1] for r in rows}
    live = {p.id: p.net_amount for p in payments}
    amount_diff = [tid for tid in declared.keys() & live.keys()
                   if round(declared[tid], 2) != round(live[tid], 2)]
    return {
        "report": report,
        "amount_differs": sorted(amount_diff),
        "live_not_declared": sorted(live.keys() - declared.keys()),
        "declared_not_live": sorted(declared.keys() - live.keys()),
    }
