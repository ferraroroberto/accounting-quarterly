"""Annual pack (#103): Modelo 390, Modelo 347 (sales + purchases) and the P&L per IAE activity.

Everything the January/February annual filings and the Renta need, computed
live from the database (nothing is persisted), with markdown and CSV exports.

CLI::

    python -m src.annual_pack --year 2026                 # markdown to stdout
    python -m src.annual_pack --year 2026 --out tmp/annual_2026
    python -m src.annual_pack --year 2025 --db path/to/copy.db

No Streamlit here: the UI lives in ``app/annual_pack_tab.py``.
"""
from __future__ import annotations

import argparse
import csv
import io
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.logger import get_logger
from src.modelo_347 import Modelo347PurchasesResult, compute_modelo_347, compute_modelo_347_purchases
from src.modelo_390 import MODELO390_LABELS, Modelo390Result, compute_modelo_390
from src.pl_by_activity import ACTIVITY_LABELS, PLByActivity, compute_pl_by_activity
from src.tax_data import load_app_config
from src.tax_models import Modelo347Result

log = get_logger(__name__)


@dataclass
class AnnualPack:
    year: int
    m390: Modelo390Result
    m347_sales: Modelo347Result
    m347_purchases: Modelo347PurchasesResult
    pl: PLByActivity


def build_annual_pack(year: int, conn: sqlite3.Connection, config: Optional[dict] = None) -> AnnualPack:
    """Compute the three annual outputs of ``year`` (the app config when ``config`` is None)."""
    if config is None:
        config = load_app_config()
    from src.vendor_registry import load_registry

    registry = load_registry()
    return AnnualPack(
        year=year,
        m390=compute_modelo_390(year, conn, config),
        m347_sales=compute_modelo_347(year, conn, config),
        m347_purchases=compute_modelo_347_purchases(year, conn, registry, config),
        pl=compute_pl_by_activity(year, conn, config, registry),
    )


# ---------------------------------------------------------------------------
# Tables (shared by markdown, CSV and the UI)
# ---------------------------------------------------------------------------

def rows_390(pack: AnnualPack) -> list[dict]:
    return [{"box": box, "description": MODELO390_LABELS.get(box, ""), "value": value}
            for box, value in pack.m390.aeat_boxes().items()]


def rows_347(pack: AnnualPack) -> list[dict]:
    """Declarable 347 lines: sales (key B) then purchases (key A), with the quarterly split."""
    out = []
    for r in pack.m347_sales.rows:
        q = r.quarter_breakdown
        out.append({"key": "B", "side": "sales", "nif": r.counterparty_nif, "name": r.counterparty_name,
                    "total": r.total_operations, **{f"q{i}": round(q.get(i, 0.0), 2) for i in range(1, 5)}})
    for r in pack.m347_purchases.rows:
        q = r.quarter_breakdown
        out.append({"key": "A", "side": "purchases", "nif": r.nif, "name": r.name,
                    "total": r.total, **{f"q{i}": round(q.get(i, 0.0), 2) for i in range(1, 5)}})
    return out


def rows_pl(pack: AnnualPack) -> list[dict]:
    out = []
    for a in pack.pl.activities:
        out.append({"iae": a.iae, "activity": a.activity, "income": a.income,
                    "expenses_invoices": a.expenses_invoices, "reta": a.reta, "depreciation": a.depreciation,
                    "other_expenses": a.other_expenses, "total_expenses": a.total_expenses, "net": a.net})
    return out


def _csv(rows: list[dict], columns: list[str]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def csv_390(pack: AnnualPack) -> str:
    return _csv(rows_390(pack), ["box", "description", "value"])


def csv_347(pack: AnnualPack) -> str:
    return _csv(rows_347(pack), ["key", "side", "nif", "name", "total", "q1", "q2", "q3", "q4"])


def csv_pl(pack: AnnualPack) -> str:
    return _csv(rows_pl(pack), ["iae", "activity", "income", "expenses_invoices", "reta", "depreciation",
                                "other_expenses", "total_expenses", "net"])


def csv_pl_lines(pack: AnnualPack) -> str:
    return _csv(pack.pl.lines, ["kind", "source", "id", "date", "description", "activity", "iae",
                                "allocated_by", "amount_eur"])


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _m(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:,.2f}"


def _cell(text: object) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def to_markdown(pack: AnnualPack) -> str:
    """The whole pack as GitHub-flavoured markdown."""
    y = pack.year
    m390, pl = pack.m390, pack.pl
    out = [f"# Annual pack {y}", "",
           f"Modelo 390 and 347 are due by 30 January / end of February {y + 1}; the P&L per activity feeds "
           f"the Renta {y}.", "", f"## Modelo 390 — resumen anual IVA {y}", ""]
    if m390.prorrata_applies:
        out += [f"Pro-rata: type {m390.prorrata_type}, definitive {m390.prorrata_definitive_pct:.0f}%.", ""]
    out += ["| Box | Description | Value |", "|---|---|--:|"]
    out += [f"| {r['box']} | {_cell(r['description'])} | {_m(r['value'])} |" for r in rows_390(pack)]
    if m390.notes:
        out += ["", f"Notes: {m390.notes}"]

    out += ["", f"## Modelo 347 — operaciones con terceras personas {y}", "",
            "Key B = sales, key A = purchases; amounts VAT-inclusive, declared when the year total "
            f"exceeds €{pack.m347_purchases.threshold:,.2f}.", "",
            "| Key | NIF | Name | Total | Q1 | Q2 | Q3 | Q4 |", "|---|---|---|--:|--:|--:|--:|--:|"]
    rows = rows_347(pack)
    out += [f"| {r['key']} | {_cell(r['nif'])} | {_cell(r['name'])} | {_m(r['total'])} | {_m(r['q1'])} "
            f"| {_m(r['q2'])} | {_m(r['q3'])} | {_m(r['q4'])} |" for r in rows]
    if not rows:
        out.append("| — | — | No counterparty above the threshold | — | — | — | — | — |")
    p = pack.m347_purchases
    if p.unidentified or p.excluded:
        out += ["", f"Purchases notes: {p.notes}"]
        out += [f"- excluded: {e['date']} {_cell(e['vendor'])} €{e['amount_eur']:,.2f} — {e['reason']}"
                for e in p.excluded]

    out += ["", f"## P&L per IAE activity {y}", "",
            "Allocation: " + ", ".join(f"{k} → {v}" for k, v in pl.allocation.items()) + ".", "",
            "| IAE | Activity | Income | Invoices | RETA | Depreciation | Other | Total expenses | Net |",
            "|---|---|--:|--:|--:|--:|--:|--:|--:|"]
    for a in pl.activities:
        out.append(f"| {a.iae} | {ACTIVITY_LABELS.get(a.activity, a.activity)} | {_m(a.income)} "
                   f"| {_m(a.expenses_invoices)} | {_m(a.reta)} | {_m(a.depreciation)} | {_m(a.other_expenses)} "
                   f"| {_m(a.total_expenses)} | {_m(a.net)} |")
    out.append(f"| | **Total** | **{_m(pl.total_income)}** | | | | | **{_m(pl.total_expenses)}** "
               f"| **{_m(pl.net)}** |")
    out += ["",
            f"- Gastos de difícil justificación (5 %, inside 130 box 02, not split): {_m(pl.gastos_dificil_justificacion)}",
            f"- Modelo 130 Q4: box 01 {_m(pl.m130_c01)} · real expenses {_m(pl.m130_gastos_reales)} "
            f"· box 02 {_m(pl.m130_c02)} — {'ties ✅' if pl.ties_to_130 else 'does NOT tie ⚠️'}"]
    if pl.notes:
        out += ["", f"Notes: {pl.notes}"]
    return "\n".join(out) + "\n"


def write_pack(pack: AnnualPack, out_dir: str | Path) -> list[Path]:
    """Write the markdown and the CSVs into ``out_dir``; returns the written paths."""
    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    y = pack.year
    files = {
        f"annual_pack_{y}.md": to_markdown(pack),
        f"modelo_390_{y}.csv": csv_390(pack),
        f"modelo_347_{y}.csv": csv_347(pack),
        f"pl_by_activity_{y}.csv": csv_pl(pack),
        f"pl_by_activity_lines_{y}.csv": csv_pl_lines(pack),
    }
    written = []
    for name, text in files.items():
        path = target / name
        path.write_text(text, encoding="utf-8")
        written.append(path)
    log.info("ℹ️ Annual pack %s written to %s (%d files)", y, target, len(written))
    return written


def main(argv: Optional[list[str]] = None) -> None:
    from src.database import get_connection

    parser = argparse.ArgumentParser(
        description="Annual pack: Modelo 390, Modelo 347 (sales + purchases), P&L per IAE activity.")
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--db", help="SQLite database (default: data/accounting.db)")
    parser.add_argument("--out", help="Directory for the markdown + CSV files (default: markdown to stdout)")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    conn = get_connection(args.db)
    try:
        pack = build_annual_pack(args.year, conn)
    finally:
        conn.close()
    if args.out:
        for path in write_pack(pack, args.out):
            print(path)
    else:
        print(to_markdown(pack))


if __name__ == "__main__":
    main()
