"""Import filed AEAT receipts (Modelo 303/130/349/390 PDFs) as reference data.

The official PDF receipt the AEAT Sede returns after a presentation is the
ground truth for validating the tax engine and for the carry-forward chain
(303 credit pending, 130 previous payments / negative results). This module
turns those PDFs into rows in two SQLite tables:

- ``filed_returns``        one row per non-empty box (casilla) of a return.
- ``filed_349_operators``  one row per intracommunity operator of a Modelo 349.

Parsing is coordinate based (pdfplumber): every amount word is paired with the
nearest box-number word to its left on the same visual row. Text-flow
extraction (``pdftotext -layout``) misaligns the 303 rows, so it is not used.

CLI::

    python -m src.filed_returns import <folder-or-pdf> [<folder-or-pdf> ...]
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterable, Optional

import pdfplumber

from src.database import get_connection
from src.exceptions import StripeAutomationError
from src.logger import get_logger

log = get_logger(__name__)

SUPPORTED_MODELS = ("130", "303", "349", "390")

# Spanish-formatted amount: optional minus, thousands with '.', 2 decimals.
AMOUNT_RE = re.compile(r"^-?\d{1,3}(?:\.\d{3})*,\d{2}$")
# Box (casilla) labels are printed as 2- or 3-digit numbers.
BOX_RE = re.compile(r"^\d{2,3}$")
_INT_RE = re.compile(r"^\d+$")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_PERIOD_RE = re.compile(r"^(?:[1-4]T|0A|0[1-9]|1[0-2])$")

# An amount and its box label count as the same row when their vertical
# centres are within this many points (labels are 6.5pt, values 9pt).
ROW_TOLERANCE_PT = 7.0
# The other layout tolerances of the receipt parser, all in PDF points. Each is a slack that lets a word
# still match its label or column when the printed positions are a hair off; widen one only to adapt the
# parser to a new receipt layout.
# A value may begin this far before the right edge of the label to its left (glyph boxes overlap slightly).
LABEL_OVERLAP_PT = 2
# A value may begin this far left of its label's left edge when the value sits below the label.
LABEL_LEFT_SLACK_PT = 5
# Vertical gap under which two words count as on the same text line.
SAME_LINE_PT = 3
# Farthest a value may sit below its label (one printed line of the form's value cell).
VALUE_BELOW_MAX_PT = 16.0
# 349 operator table: a word may start this far left of the NIF / name column header and still belong to it.
OPERATOR_COLUMN_SLACK_PT = 2
# Same, for the key and base columns.
OPERATOR_KEY_BASE_SLACK_PT = 5
# Words closer than this to the next block's header row belong to that block, not the current operator.
OPERATOR_BLOCK_END_MARGIN_PT = 1

# Modelo 303 "Tipo %" boxes: the form prints the fixed VAT/recargo rate in them
# whether or not the row is used, so they carry no declared value.
_303_RATE_BOXES = frozenset(
    {"02", "05", "08", "17", "20", "23", "151", "154", "157", "166", "169"}
)
# Modelo 349 summary boxes that hold an operator count (integer, no decimals).
_349_COUNT_BOXES = frozenset({"01", "03"})


class FiledReturnParseError(StripeAutomationError):
    """Raised when a PDF is not a supported AEAT presentation receipt."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Operator349:
    """One operator row of a filed Modelo 349."""
    seq: int
    country: str
    vat_id: str
    name: str
    key: str          # clave de operación: E, A, T, S, I, M, H, R, D, C
    base: float


@dataclass
class FiledReturn:
    """A parsed AEAT receipt: header fields plus every non-empty box."""
    model: str
    year: int
    period: str                       # '1T'..'4T', '0A' (annual) or '01'..'12'
    justificante: str
    source_file: str
    csv: Optional[str] = None         # Código Seguro de Verificación
    presented_at: Optional[str] = None  # ISO 'YYYY-MM-DDTHH:MM:SS'
    presenter: Optional[str] = None   # presenter NIF + name, as printed
    presenter_role: Optional[str] = None  # 'En calidad de', e.g. Colaborador
    boxes: dict[str, float] = field(default_factory=dict)
    operators: list[Operator349] = field(default_factory=list)

    @property
    def quarter(self) -> Optional[int]:
        return period_to_quarter(self.period)


@dataclass
class ImportResult:
    """Outcome of importing one file."""
    source_file: str
    status: str                       # imported | replaced | unchanged | superseded | skipped
    detail: str = ""
    filed: Optional[FiledReturn] = None


# ---------------------------------------------------------------------------
# Pure helpers (unit-tested on synthetic word lists)
# ---------------------------------------------------------------------------

def parse_amount(text: str) -> float:
    """'-1.234,56' -> -1234.56."""
    return float(text.replace(".", "").replace(",", "."))


def period_to_quarter(period: str) -> Optional[int]:
    """'2T' -> 2; annual ('0A') and monthly periods -> None."""
    if len(period) == 2 and period[1] == "T" and period[0] in "1234":
        return int(period[0])
    return None


def _center_y(word: dict) -> float:
    return (word["top"] + word["bottom"]) / 2


def _base_font(fontname: Optional[str]) -> Optional[str]:
    """Strip the 6-letter embedded-subset prefix: 'ABCDEF+NewsGothicStd' -> 'NewsGothicStd'."""
    if not fontname:
        return None
    return fontname.split("+", 1)[1] if "+" in fontname else fontname


def detect_template_font(words: list[dict]) -> Optional[str]:
    """Return the base font of the form template, or None if it can't be told apart.

    Box labels are template text and vastly outnumber any other 2-3 digit
    word, so the most common font among box-number words is the template font.
    The form also prints a few amounts in that font (e.g. the Modelo 130
    "máximo: 660,14 euros" note, the 390 recargo rates) — those are not
    declared values. When every amount is in the template font the document
    does not distinguish template from data, so no font filter is applied.
    """
    fonts = Counter(_base_font(w.get("fontname")) for w in words if BOX_RE.match(w["text"]))
    fonts.pop(None, None)
    if not fonts:
        return None
    template = fonts.most_common(1)[0][0]
    amount_fonts = {_base_font(w.get("fontname")) for w in words if AMOUNT_RE.match(w["text"])}
    if amount_fonts and amount_fonts <= {template}:
        return None
    return template


def _label_left_of(value: dict, labels: list[dict], row_tolerance: float) -> Optional[dict]:
    """Nearest box label to the left of ``value`` on the same visual row."""
    cy = _center_y(value)
    cands = [
        b for b in labels
        if abs(_center_y(b) - cy) <= row_tolerance and b["x1"] <= value["x0"] + LABEL_OVERLAP_PT
    ]
    if not cands:
        return None
    # Rightmost label wins; on a near-tie in x, the vertically closer one.
    return min(cands, key=lambda b: (-round(b["x1"]), abs(_center_y(b) - cy)))


def pair_boxes(
    words: list[dict],
    *,
    template_font: Optional[str] = None,
    skip_boxes: Iterable[str] = (),
    int_boxes: Iterable[str] = (),
    row_tolerance: float = ROW_TOLERANCE_PT,
) -> dict[str, float]:
    """Pair every amount word on one page with its box label.

    ``words`` are pdfplumber word dicts (text, x0, x1, top, bottom and,
    optionally, fontname). Words in ``template_font`` are form text, never
    values. ``int_boxes`` also accept plain-integer values (e.g. 349 operator
    counts). The first value seen for a box wins; a conflicting later value is
    logged and ignored.
    """
    skip = set(skip_boxes)
    ints = set(int_boxes)
    labels = [w for w in words if BOX_RE.match(w["text"])]
    out: dict[str, float] = {}
    for w in words:
        is_amount = bool(AMOUNT_RE.match(w["text"]))
        is_int = bool(ints) and bool(_INT_RE.match(w["text"]))
        if not (is_amount or is_int):
            continue
        if template_font and _base_font(w.get("fontname")) == template_font:
            continue
        label = _label_left_of(w, [b for b in labels if b is not w], row_tolerance)
        if label is None:
            continue
        box = label["text"]
        if box in skip or (not is_amount and box not in ints):
            continue
        value = parse_amount(w["text"]) if is_amount else float(w["text"])
        if box in out:
            if abs(out[box] - value) > 0.005:
                log.warning("⚠️ Box %s read twice with different values; keeping the first", box)
            continue
        out[box] = value
    return out


def find_value_near_label(
    words: list[dict], label_re: re.Pattern, value_re: re.Pattern, max_below: float = VALUE_BELOW_MAX_PT
) -> Optional[str]:
    """First word matching ``value_re`` right of / just below a ``label_re`` word."""
    for label in (w for w in words if label_re.match(w["text"])):
        cands = [
            w for w in words
            if value_re.match(w["text"])
            and w["x0"] >= label["x0"] - LABEL_LEFT_SLACK_PT
            and label["top"] - SAME_LINE_PT <= w["top"] <= label["top"] + max_below
        ]
        if cands:
            best = min(cands, key=lambda w: (abs(w["top"] - label["top"]) > SAME_LINE_PT, w["x0"] - label["x0"]))
            return best["text"]
    return None


def parse_header_text(text: str) -> dict[str, Optional[str]]:
    """Header fields from the receipt's first page ('Información de la presentación')."""
    def grab(pattern: str) -> Optional[str]:
        m = re.search(pattern, text)
        return m.group(1).strip() if m else None

    presented_at = None
    m = re.search(r"realizada el:\s*(\d{2})-(\d{2})-(\d{4})\s+a las\s+(\d{2}:\d{2}:\d{2})", text)
    if m:
        presented_at = f"{m.group(3)}-{m.group(2)}-{m.group(1)}T{m.group(4)}"
    nif = grab(r"NIF Presentador:\s*(\S+)")
    name = grab(r"Raz[oó]n social:\s*([^\n]+)")
    presenter = " ".join(p for p in (nif, name) if p) or None
    return {
        "model": grab(r"Modelo\s+(\d{3})"),
        "justificante": grab(r"justificante:\s*(\d{6,})"),
        "csv": grab(r"Verificaci[oó]n:\s*([A-Z0-9]{8,})"),
        "presented_at": presented_at,
        "presenter": presenter,
        "presenter_role": grab(r"En calidad de:\s*([^\n]+)"),
    }


def parse_349_operators(words: list[dict]) -> list[Operator349]:
    """Operator rows of a Modelo 349 'Hoja interior' page.

    Each operator block starts at a header row holding 'Clave' and 'Base' and
    ends at the next 'A cumplimentar…' / 'Operador' row. Columns are taken
    from the header words' x positions.
    """
    ops: list[Operator349] = []
    headers = []
    for clave in (w for w in words if w["text"] == "Clave"):
        row = [w for w in words if abs(_center_y(w) - _center_y(clave)) <= SAME_LINE_PT]
        by_text = {w["text"]: w for w in row}
        if {"Base", "NIF", "Apellidos"} <= by_text.keys():
            headers.append((clave, by_text))
    headers.sort(key=lambda h: h[0]["top"])
    stops = sorted(
        w["top"] for w in words if w["text"] in ("cumplimentar", "Operador")
    )
    for clave, hdr in headers:
        start = max(w["bottom"] for w in hdr.values())
        end = next((s for s in stops if s > start), float("inf"))
        block = sorted(
            (w for w in words if start < w["top"] < end - OPERATOR_BLOCK_END_MARGIN_PT),
            key=lambda w: (round(w["top"]), w["x0"]),
        )
        nif_x = hdr["NIF"]["x0"] - OPERATOR_COLUMN_SLACK_PT
        name_x = hdr["Apellidos"]["x0"] - OPERATOR_COLUMN_SLACK_PT
        key_x = clave["x0"] - OPERATOR_KEY_BASE_SLACK_PT
        base_x = hdr["Base"]["x0"] - OPERATOR_KEY_BASE_SLACK_PT
        cols: dict[str, list[str]] = {"country": [], "vat": [], "name": [], "key": [], "base": []}
        for w in block:
            x = w["x0"]
            col = ("country" if x < nif_x else "vat" if x < name_x else
                   "name" if x < key_x else "key" if x < base_x else "base")
            cols[col].append(w["text"])
        base_txt = next((t for t in cols["base"] if AMOUNT_RE.match(t)), None)
        if base_txt is None:
            continue  # empty operator slot
        ops.append(Operator349(
            seq=len(ops) + 1,
            country=" ".join(cols["country"]),
            vat_id="".join(cols["vat"]),
            name=" ".join(cols["name"]),
            key=" ".join(cols["key"]),
            base=parse_amount(base_txt),
        ))
    return ops


# ---------------------------------------------------------------------------
# PDF parsing
# ---------------------------------------------------------------------------

def parse_pdf(source: str | Path | BinaryIO, source_name: Optional[str] = None) -> FiledReturn:
    """Parse one AEAT receipt PDF (path or binary stream).

    Raises FiledReturnParseError when the PDF is not a presentation receipt
    or is for a model this importer does not handle.
    """
    name = source_name or (Path(source).name if isinstance(source, (str, Path)) else "upload.pdf")
    try:
        pdf = pdfplumber.open(source)
    except Exception as exc:  # pdfminer raises a zoo of exception types
        raise FiledReturnParseError(f"cannot open PDF: {exc}") from exc
    with pdf:
        if not pdf.pages:
            raise FiledReturnParseError("empty PDF")
        first_page = pdf.pages[0].extract_text() or ""
        header = parse_header_text(first_page)
        if not header["justificante"] or "realizada el" not in first_page:
            raise FiledReturnParseError("not an AEAT presentation receipt")
        model = header["model"]
        if model not in SUPPORTED_MODELS:
            raise FiledReturnParseError(f"unsupported model {model!r}")

        pages = [p.extract_words(extra_attrs=["fontname", "size"]) for p in pdf.pages[1:]]

    all_words = [w for ws in pages for w in ws]
    year_txt = find_value_near_label(all_words, re.compile(r"^Ejercicio$"), _YEAR_RE)
    if year_txt is None:
        raise FiledReturnParseError("fiscal year (Ejercicio) not found")
    period = find_value_near_label(all_words, re.compile(r"^Per[ií]odo$"), _PERIOD_RE)
    if period is None:
        if model != "390":
            raise FiledReturnParseError("period (Período) not found")
        period = "0A"

    template = detect_template_font(all_words)
    skip = _303_RATE_BOXES if model == "303" else ()
    ints = _349_COUNT_BOXES if model == "349" else ()
    boxes: dict[str, float] = {}
    for ws in pages:
        for box, value in pair_boxes(ws, template_font=template, skip_boxes=skip, int_boxes=ints).items():
            boxes.setdefault(box, value)

    operators: list[Operator349] = []
    if model == "349":
        for ws in pages:
            for op in parse_349_operators(ws):
                op.seq = len(operators) + 1
                operators.append(op)

    return FiledReturn(
        model=model,
        year=int(year_txt),
        period=period,
        justificante=header["justificante"],
        source_file=name,
        csv=header["csv"],
        presented_at=header["presented_at"],
        presenter=header["presenter"],
        presenter_role=header["presenter_role"],
        boxes=boxes,
        operators=operators,
    )


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def ensure_filed_returns_schema(conn: sqlite3.Connection) -> None:
    """Create the filed_returns / filed_349_operators tables if missing."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS filed_returns (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            model          TEXT NOT NULL,
            year           INTEGER NOT NULL,
            period         TEXT NOT NULL,
            quarter        INTEGER,
            box            TEXT NOT NULL,
            value          REAL NOT NULL,
            justificante   TEXT NOT NULL,
            csv            TEXT,
            presented_at   TEXT,
            presenter      TEXT,
            presenter_role TEXT,
            source_file    TEXT NOT NULL,
            imported_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (model, year, period, box)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS filed_349_operators (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            year           INTEGER NOT NULL,
            period         TEXT NOT NULL,
            quarter        INTEGER,
            seq            INTEGER NOT NULL,
            country        TEXT,
            vat_id         TEXT,
            name           TEXT,
            operation_key  TEXT,
            base           REAL NOT NULL,
            justificante   TEXT NOT NULL,
            source_file    TEXT NOT NULL,
            imported_at    TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (year, period, seq)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_filed_returns_justificante
            ON filed_returns(justificante)
    """)


def store_filed_return(conn: sqlite3.Connection, filed: FiledReturn) -> str:
    """Persist one parsed return; returns the ImportResult status.

    Idempotent: a receipt already stored (same justificante) is a no-op
    ('unchanged'). A different receipt for the same model/year/period replaces
    the stored one when it was presented later (a rectifying return) —
    'replaced' — and is ignored when it is older ('superseded').
    """
    ensure_filed_returns_schema(conn)
    key = (filed.model, filed.year, filed.period)
    existing = conn.execute(
        "SELECT justificante, presented_at FROM filed_returns "
        "WHERE model = ? AND year = ? AND period = ? LIMIT 1",
        key,
    ).fetchone()
    status = "imported"
    if existing is not None:
        if existing[0] == filed.justificante:
            return "unchanged"
        if existing[1] and filed.presented_at and filed.presented_at < existing[1]:
            return "superseded"
        status = "replaced"

    with conn:
        conn.execute("DELETE FROM filed_returns WHERE model = ? AND year = ? AND period = ?", key)
        conn.executemany(
            """INSERT INTO filed_returns
               (model, year, period, quarter, box, value, justificante, csv,
                presented_at, presenter, presenter_role, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (filed.model, filed.year, filed.period, filed.quarter, box, value,
                 filed.justificante, filed.csv, filed.presented_at, filed.presenter,
                 filed.presenter_role, filed.source_file)
                for box, value in sorted(filed.boxes.items())
            ],
        )
        if filed.model == "349":
            conn.execute("DELETE FROM filed_349_operators WHERE year = ? AND period = ?", key[1:])
            conn.executemany(
                """INSERT INTO filed_349_operators
                   (year, period, quarter, seq, country, vat_id, name, operation_key,
                    base, justificante, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [
                    (filed.year, filed.period, filed.quarter, op.seq, op.country, op.vat_id,
                     op.name, op.key, op.base, filed.justificante, filed.source_file)
                    for op in filed.operators
                ],
            )
    return status


def import_pdf(
    conn: sqlite3.Connection, source: str | Path | BinaryIO, source_name: Optional[str] = None
) -> ImportResult:
    """Parse and store one receipt, never raising for a bad/unsupported file."""
    name = source_name or (Path(source).name if isinstance(source, (str, Path)) else "upload.pdf")
    try:
        filed = parse_pdf(source, name)
    except FiledReturnParseError as exc:
        return ImportResult(name, "skipped", str(exc))
    if not filed.boxes and not filed.operators:
        log.warning("⚠️ %s: receipt parsed but no box values found", name)
    status = store_filed_return(conn, filed)
    log.info("ℹ️ Filed return %s %s %s from %s: %s (%d boxes, %d operators)",
             filed.model, filed.year, filed.period, name, status,
             len(filed.boxes), len(filed.operators))
    return ImportResult(name, status, filed=filed)


def iter_pdfs(paths: Iterable[str | Path]) -> list[Path]:
    """Expand folders recursively into their PDF files (case-insensitive)."""
    out: list[Path] = []
    for p in map(Path, paths):
        if p.is_dir():
            out.extend(sorted(f for f in p.rglob("*") if f.is_file() and f.suffix.lower() == ".pdf"))
        elif p.is_file():
            out.append(p)
        else:
            log.warning("⚠️ Path not found: %s", p)
    return out


def import_paths(conn: sqlite3.Connection, paths: Iterable[str | Path]) -> list[ImportResult]:
    """Import every PDF under ``paths`` (files or folders, recursive)."""
    return [import_pdf(conn, pdf) for pdf in iter_pdfs(paths)]


# ---------------------------------------------------------------------------
# Reading back (validator-facing)
# ---------------------------------------------------------------------------

# Box -> key in the validator's value dict (same keys as validation.yaml).
VALIDATOR_KEYS: dict[str, dict[str, str]] = {
    "130": {
        "01": "01_ingresos_ytd", "02": "02_gastos_ytd", "03": "03_rendimiento_neto",
        "04": "04_veinte_pct", "05": "05_trimestres_anteriores",
        "06": "06_retenciones_ytd", "07": "07_pago_fraccionado", "19": "19_result",
    },
    "303": {
        "07": "07_base_21pct", "09": "09_cuota_21pct", "27": "27_total_cuota_devengada",
        "28": "28_base_soportado", "29": "29_cuota_soportado", "46": "46_resultado",
        "59": "59_entregas_intracom", "60": "60_exportaciones",
    },
    "349": {"01": "01_total_operators", "02": "02_total_amount"},
    "390": {
        "05": "05_base_ord_21", "06": "06_cuota_ord_21", "33": "33_total_bases",
        "34": "34_total_cuotas", "48": "48_base_interior", "64": "64_suma_deducciones",
        "65": "65_resultado", "86": "86_resultado_liquidacion",
        "99": "99_regimen_general", "103": "103_entregas_intracom",
        "104": "104_exportaciones", "108": "108_total_volumen",
    },
}


def load_filed_boxes(
    conn: sqlite3.Connection, model: str, year: int, period: str
) -> dict[str, float]:
    """All stored boxes of one filed return ({} when not imported)."""
    ensure_filed_returns_schema(conn)
    rows = conn.execute(
        "SELECT box, value FROM filed_returns WHERE model = ? AND year = ? AND period = ?",
        (model, year, period),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def load_filings(conn: sqlite3.Connection) -> list[dict]:
    """Imported returns in the validator's filing-dict shape.

    An empty box on a filed return means zero, so every mapped validator key
    is present (0.0 when the box was blank). Monthly periods are skipped —
    the validator works per quarter / year.
    """
    ensure_filed_returns_schema(conn)
    heads = conn.execute(
        """SELECT model, year, period, quarter, MIN(presented_at), MIN(justificante)
           FROM filed_returns GROUP BY model, year, period
           ORDER BY year, COALESCE(quarter, 5), model"""
    ).fetchall()
    filings: list[dict] = []
    for model, year, period, quarter, presented_at, justificante in heads:
        if quarter is None and period != "0A":
            continue
        boxes = load_filed_boxes(conn, model, year, period)
        values = {key: boxes.get(box, 0.0) for box, key in VALIDATOR_KEYS.get(model, {}).items()}
        filing = {
            "model": model,
            "year": year,
            "quarter": quarter,
            "filed_date": (presented_at or "")[:10] or "—",
            "justificante": justificante,
            "source": "db",
            "values": values,
            "boxes": boxes,
        }
        if model == "349":
            ops = conn.execute(
                """SELECT country, vat_id, name, operation_key, base
                   FROM filed_349_operators WHERE year = ? AND period = ? ORDER BY seq""",
                (year, period),
            ).fetchall()
            filing["operators"] = [
                {"country": o[0], "vat_id": o[1], "name": o[2], "clave": o[3], "amount": o[4]}
                for o in ops
            ]
        filings.append(filing)
    return filings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _format_result(r: ImportResult) -> str:
    if r.filed is None:
        return f"  {r.status:<10} {r.source_file}  ({r.detail})"
    f = r.filed
    extra = f", {len(f.operators)} operators" if f.model == "349" else ""
    return (f"  {r.status:<10} Modelo {f.model} {f.year} {f.period}: "
            f"{len(f.boxes)} boxes{extra}  <- {r.source_file}")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m src.filed_returns",
        description="Import filed AEAT receipts (Modelo 303/130/349/390 PDFs) as reference data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p_import = sub.add_parser("import", help="Import receipt PDFs (folders are scanned recursively)")
    p_import.add_argument("paths", nargs="+", help="PDF files or folders")
    p_import.add_argument("--db", default=None, help="SQLite path (default: data/accounting.db)")
    args = parser.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    conn = get_connection(args.db)
    try:
        results = import_paths(conn, args.paths)
    finally:
        conn.close()
    for r in results:
        print(_format_result(r))
    counts = Counter(r.status for r in results)
    print(f"{len(results)} file(s): " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
