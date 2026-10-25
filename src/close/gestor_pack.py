"""Stripe report and gestor pack: the quarter's report (+ freeze), ledger invoices, notes and a draft email."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from src.close.context import CloseContext, StepResult, is_in_quarter, quarter_payments, same_file, write_if_changed
from src.database import get_invoices
from src.declared_reports import (
    declared_vs_live_drift,
    freeze_report,
    freeze_sent_report,
    get_declared_report,
)
from src.excel_exporter import create_excel_report, generate_report_filename
from src.exceptions import ReportAlreadyFrozenError
from src.invoice_dedupe import flat_sweep_name
from src.invoice_scanner import resolve_invoice_dir
from src.models import ClassifiedPayment
from src.reclassify import reclassify_stored

PACK_INVOICES_DIR = "invoices"  # gestor-pack's copy of the quarter's ledger invoices, under the quarter folder

# Ledger rows worth telling the accountant about (gestor pack notes).
_PLAIN_TREATMENTS = {None, "", "DOMESTIC", "ES_21"}


# ---------------------------------------------------------------------------
# Stripe report (the `report` subcommand, and the gestor pack's report)
# ---------------------------------------------------------------------------

def _reclassified_payments(ctx: CloseContext, res: StepResult) -> list[ClassifiedPayment]:
    """Reclassify the quarter's stored rows (never export them stale), then load them."""
    start, end = ctx.datetime_bounds
    rc = reclassify_stored(start, end, rules=ctx.rules, db_path=ctx.db_path)
    res.changes += [f"reclassified {c.describe()}" for c in rc.changes]
    res.info.append(f"Reclassify: scanned {rc.scanned}, changed {len(rc.changes)}.")
    return quarter_payments(ctx)


def write_stripe_report(ctx: CloseContext, freeze: bool = False, supersede: bool = False) -> StepResult:
    """Write the quarter's Excel Stripe report; ``freeze`` stores it as the declared report.

    Raises ``ReportAlreadyFrozenError`` on ``freeze`` of an already-declared
    quarter without ``supersede``. Once declared, a plain run writes
    ``*_live.xlsx`` and never overwrites the sent file.
    """
    res = StepResult("report")
    conn = ctx.connect()
    try:
        declared = get_declared_report(conn, ctx.year, ctx.quarter)
        if freeze and declared and not supersede:
            raise ReportAlreadyFrozenError(
                f"Q{ctx.quarter} {ctx.year} is already declared (v{declared.version}, "
                f"{declared.created_at}, sha256 {declared.sha256[:12]}…). "
                f"Use --freeze --supersede only for a corrected re-send."
            )
        payments = _reclassified_payments(ctx, res)
        filename = generate_report_filename(ctx.year, ctx.quarter)
        if declared and not freeze:
            filename = filename.replace(".xlsx", "_live.xlsx")  # never overwrite the sent file
        dest = ctx.quarter_dir / filename
        create_excel_report(payments, dest, ctx.year, ctx.quarter, f"Q{ctx.quarter}_{ctx.year}")
        res.changes.append(f"wrote {dest}")
        if freeze:
            report = freeze_report(conn, ctx.year, ctx.quarter, payments, dest, supersede=supersede)
            res.changes.append(f"froze {dest.name} as declared report v{report.version}: "
                               f"{report.n_transactions} transactions, net {report.total_net_eur:,.2f} EUR, "
                               f"sha256 {report.sha256}")
        elif declared:
            drift = declared_vs_live_drift(conn, ctx.year, ctx.quarter, payments)
            res.warnings.append(
                f"{ctx.period} was declared on {declared.created_at} (v{declared.version}, sha256 "
                f"{declared.sha256[:12]}…); the tax engine uses the declared EUR amounts. Live vs "
                f"declared: {len(drift['amount_differs'])} amount difference(s), "
                f"{len(drift['live_not_declared'])} live-only, {len(drift['declared_not_live'])} declared-only.")
    finally:
        conn.close()
    return res


def freeze_sent_stripe_report(ctx: CloseContext, report_file: Path, supersede: bool = False) -> StepResult:
    """Freeze a Stripe report file already sent to the gestor as the quarter's declared report.

    For quarters sent before freezing existed: the declared EUR amounts come
    from the file, not the live rows. Raises ``InvalidSentReportError`` (bad
    file, a row outside the quarter, duplicate ids) or
    ``ReportAlreadyFrozenError`` (declared already, no ``supersede``); nothing
    is stored on either.
    """
    res = StepResult("freeze-sent")
    conn = ctx.connect()
    try:
        frozen = freeze_sent_report(conn, ctx.year, ctx.quarter, report_file, supersede=supersede)
        report = frozen.report
        res.changes.append(f"froze {report.file_name} as declared report v{report.version}: "
                           f"{report.n_transactions} transactions, net {report.total_net_eur:,.2f} EUR, "
                           f"sha256 {report.sha256}")
        if frozen.missing_from_live:
            res.warnings.append(
                f"{len(frozen.missing_from_live)} id(s) in the file are not in the live transactions "
                f"table (frozen anyway; used once fetched): {', '.join(frozen.missing_from_live[:10])}")
        drift = declared_vs_live_drift(conn, ctx.year, ctx.quarter, quarter_payments(ctx))
        res.info.append(f"{len(drift['amount_differs'])} live row(s) had a different EUR amount; "
                        f"the tax engine now uses the file's.")
        if drift["live_not_declared"]:
            res.warnings.append(
                f"{len(drift['live_not_declared'])} live transaction(s) of {ctx.period} are not in the "
                f"file (the engine keeps their live amounts): {', '.join(drift['live_not_declared'][:10])}")
    finally:
        conn.close()
    return res


# ---------------------------------------------------------------------------
# 11. gestor-pack
# ---------------------------------------------------------------------------

_NOTES_TEMPLATE = """_No `gestor_notes.md` at the repo root (git-ignored) — write the special treatments to
communicate there and rerun `gestor-pack`. For example:_

- An item's business-use percentage for VAT / IRPF, and why.
- A correction to a previous quarter's filing.
- Anything booked differently from the accountant's usual convention.
"""


def _pack_state_path(ctx: CloseContext) -> Path:
    return ctx.quarter_dir / ".gestor_pack.json"


def _payments_digest(payments: list[ClassifiedPayment]) -> str:
    blob = "\n".join(p.model_dump_json() for p in sorted(payments, key=lambda p: p.id))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _pack_stripe_report(ctx: CloseContext, freeze: bool, res: StepResult) -> tuple[str, str]:
    """(report file name, status text for the email). No-op once the quarter is declared."""
    conn = ctx.connect()
    try:
        declared = get_declared_report(conn, ctx.year, ctx.quarter)
        if declared:
            res.info.append(f"Stripe report already frozen as v{declared.version} ({declared.file_name}, "
                            f"{declared.created_at}, sha256 {declared.sha256[:12]}…) — not regenerated.")
            if not (ctx.quarter_dir / declared.file_name).exists():
                res.warnings.append(f"the frozen {declared.file_name} is not in {ctx.quarter_dir}")
            drift = declared_vs_live_drift(conn, ctx.year, ctx.quarter, quarter_payments(ctx))
            n_drift = sum(len(drift[k]) for k in ("amount_differs", "live_not_declared", "declared_not_live"))
            if n_drift:
                res.warnings.append(f"{n_drift} live row(s) differ from the declared report "
                                    f"(`report` writes a *_live.xlsx to inspect them)")
            return declared.file_name, f"frozen v{declared.version}"
    finally:
        conn.close()

    if freeze:
        sub = write_stripe_report(ctx, freeze=True)
        res.merge(sub)
        return generate_report_filename(ctx.year, ctx.quarter), "frozen v1"

    payments = _reclassified_payments(ctx, res)
    dest = ctx.quarter_dir / generate_report_filename(ctx.year, ctx.quarter)
    state_path = _pack_state_path(ctx)
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    digest = _payments_digest(payments)
    if dest.exists() and state.get("report_digest") == digest:
        res.info.append(f"Draft Stripe report {dest.name} unchanged.")
    else:
        create_excel_report(payments, dest, ctx.year, ctx.quarter, f"Q{ctx.quarter}_{ctx.year}")
        state["report_digest"] = digest
        state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        res.changes.append(f"wrote draft {dest} ({len(payments)} transactions)")
    res.warnings.append("the Stripe report is NOT frozen — rerun `gestor-pack --freeze` once it is the "
                        "version you will send")
    return dest.name, "draft, not frozen yet"


def _special_treatments(ctx: CloseContext) -> list[str]:
    """One markdown bullet per quarter invoice with a non-default treatment."""
    lines = []
    for r in get_invoices(db_path=ctx.db_path):
        if not is_in_quarter(ctx, r.get("invoice_date")):
            continue
        notes = []
        if r.get("excluded"):
            notes.append(f"excluded ({r.get('excluded_reason') or 'no reason'})")
        if r.get("tax_treatment") not in _PLAIN_TREATMENTS:
            notes.append(f"treatment {r['tax_treatment']}")
        for col, label in (("deductible_pct_vat", "VAT"), ("deductible_pct_irpf", "IRPF")):
            pct = r.get(col)
            if pct is not None and float(pct) < 100:
                notes.append(f"{label} business use {float(pct):g}%")
        if r.get("is_capital_asset"):
            notes.append("fixed asset (depreciated)")
        cur = r.get("original_currency")
        if cur and cur != "EUR":
            eur = r.get("eur_received") if r.get("eur_received") is not None else r.get("total_eur")
            basis = "EUR received" if r.get("eur_received") is not None else r.get("fx_source") or "?"
            notes.append(f"{r.get('original_amount')} {cur} → {eur} EUR ({basis})")
        if notes:
            party = r.get("vendor_name") if r["direction"] == "in" else r.get("client_name")
            lines.append(f"- `{r['direction'].upper()}` {r.get('invoice_date')} {r.get('filename')} "
                         f"({party or '?'}): " + "; ".join(notes))
    return lines


def _pack_notes(ctx: CloseContext) -> str:
    own = (ctx.notes_path.read_text(encoding="utf-8").strip() + "\n"
           if ctx.notes_path.exists() else _NOTES_TEMPLATE)
    detected = _special_treatments(ctx) or ["_Nothing non-default detected in the ledger._"]
    start, end = ctx.date_bounds
    return "\n".join([
        f"# Notes for the accountant — {ctx.period}", "",
        "## Special treatments to communicate", "", own,
        f"## Detected in the invoice ledger ({start} .. {end})", "", *detected, "",
    ])


def _pack_invoices(ctx: CloseContext, res: StepResult) -> dict[str, int]:
    """Copy every non-excluded in/out invoice dated in the quarter into ``PACK_INVOICES_DIR``.

    The set comes from the ledger, not from ``sweep``'s copies, so invoices
    ingested before the close are included. Pack files no longer in the set
    (excluded or re-dated since) are removed. Returns the per-direction count
    of invoices actually in the pack.
    """
    pack = ctx.quarter_dir / PACK_INVOICES_DIR
    pack.mkdir(exist_ok=True)
    wanted: dict[str, Path] = {}
    counts = {"in": 0, "out": 0}
    n_excluded = 0
    for r in get_invoices(db_path=ctx.db_path):
        if not is_in_quarter(ctx, r.get("invoice_date")):
            continue
        if r.get("excluded"):
            n_excluded += 1
            continue
        source = resolve_invoice_dir(r["direction"], ctx.config) / r["filename"]
        if not source.is_file():
            res.warnings.append(f"{r['direction'].upper()} {r['filename']}: PDF not found at {source} "
                                f"— not in the pack")
            continue
        name = flat_sweep_name(r["direction"], r["filename"])
        if name in wanted:
            res.warnings.append(f"{r['direction'].upper()} {r['filename']}: same pack name as "
                                f"{wanted[name]} — not in the pack, rename one of them")
            continue
        wanted[name] = source
        counts[r["direction"]] += 1
    for stale in sorted(p for p in pack.iterdir() if p.is_file() and p.name not in wanted):
        stale.unlink()
        res.changes.append(f"removed {PACK_INVOICES_DIR}/{stale.name} (no longer in the quarter's ledger)")
    for name, source in sorted(wanted.items()):
        if not same_file(source, pack / name):
            shutil.copy2(source, pack / name)
            res.changes.append(f"copied {PACK_INVOICES_DIR}/{name}")
    res.info.append(f"Invoices: {counts['in']} received + {counts['out']} issued in {pack}"
                    + (f"; {n_excluded} excluded row(s) left out" if n_excluded else ""))
    return counts


def _pack_email(ctx: CloseContext, report_name: str, report_status: str, notes_name: str,
                counts: dict[str, int]) -> str:
    n_in, n_out = counts["in"], counts["out"]
    return "\n".join([
        "DRAFT — generated by close_quarter.py gestor-pack; nothing has been sent.",
        "",
        f"Asunto: Documentación {ctx.quarter}T {ctx.year}",
        "",
        "Hola,",
        "",
        f"Os envío la documentación del {ctx.quarter}T {ctx.year}:",
        "",
        f"- {n_in} factura(s) recibida(s) y {n_out} factura(s) emitida(s) (PDF).",
        f"- Informe de Stripe: {report_name} ({report_status}).",
        f"- Notas sobre tratamientos especiales: {notes_name}.",
        "",
        "Cualquier duda me decís.",
        "",
        "Gracias,",
        "",
    ])


def step_gestor_pack(ctx: CloseContext, freeze: bool = False) -> StepResult:
    """Stripe report (+ freeze), the quarter's invoices, notes and a draft email. Sends nothing."""
    res = StepResult("gestor-pack")
    report_name, report_status = _pack_stripe_report(ctx, freeze, res)
    counts = _pack_invoices(ctx, res)
    notes_path = ctx.quarter_dir / f"gestor_notes_{ctx.year}_Q{ctx.quarter}.md"
    if not ctx.notes_path.exists():
        res.warnings.append(f"no {ctx.notes_path.name} — the notes carry a template; add yours and rerun")
    if write_if_changed(notes_path, _pack_notes(ctx)):
        res.changes.append(f"wrote {notes_path}")
    email_path = ctx.quarter_dir / f"gestor_email_{ctx.year}_Q{ctx.quarter}.txt"
    if write_if_changed(email_path, _pack_email(ctx, report_name, report_status, notes_path.name, counts)):
        res.changes.append(f"wrote {email_path}")
    res.info.append(f"Pack folder: {ctx.quarter_dir} — nothing was sent or uploaded.")
    return res
