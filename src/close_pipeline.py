"""Quarter-close pipeline (issue #102): idempotent steps that each report what they changed.

``scripts/close_quarter.py`` is the thin CLI over this module and the
``/close-quarter`` skill drives the CLI. Steps, in pipeline order
(``STEP_ORDER``):

1. ``sweep``       copy new invoice PDFs into ``tmp/close_quarter/<Y>_Q<Q>/``.
2. ``ocr``         extract new/changed PDFs (MD5 vs stored ``file_hash``) through
                   ``src.invoice_ingest``; a failing file is reported, never fatal.
3. ``vendors``     apply the vendor registry; list the quarter's unknown vendors.
4. ``dedupe``      duplicate / receipt / out-of-period groups; writes only with ``apply``.
5. ``fx``          ECB backfill + stored-invoice recompute; recompute writes only with ``apply``.
6. ``stripe``      Stripe fetch (injected callable) + billing backfill + reclassify + review warnings.
7. ``reta``        import a bank export of RETA (TGSS) debits.
8. ``compute``     303/130/OSS/349/347 snapshots — persisted only when a payload changed.
9. ``reconcile``   filed vs app for this quarter if filed, else the previous quarter.
10. ``sheet``      filing sheet through ``filing_sheet_renderer`` (``src.filing_sheet``).
11. ``gestor-pack`` Stripe report (frozen with ``freeze``), the quarter's ledger invoices,
                   notes and a draft email.

Every step returns a ``StepResult``; ``changes`` lists what the step wrote, so
an empty list means the run was a no-op. Outputs go to the git-ignored
``tmp/close_quarter/``; nothing is sent or uploaded anywhere.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from src.classifier import eur_default_foreign_warning, validate_classifications
from src.database import (
    get_connection,
    get_invoices,
    load_classified_payments,
    load_tax_snapshots_for_period,
)
from src.declared_reports import (
    declared_vs_live_drift,
    freeze_report,
    freeze_sent_report,
    get_declared_report,
)
from src.excel_exporter import create_excel_report, generate_report_filename
from src.exceptions import ReportAlreadyFrozenError
from src.filing_sheet import render_filing_sheet
from src.fx_rates import (
    STALE_TOLERANCE_DAYS,
    backfill_to_today,
    get_rate_count,
    get_stored_date_range,
    recompute_stored_invoice_fx,
)
from src.invoice_dedupe import (
    apply_groups,
    find_duplicate_groups,
    load_sweep_rows,
    quarter_bounds,
)
from src.invoice_ingest import extract_and_save, pending_files
from src.invoice_scanner import resolve_invoice_dir, scan_invoice_pdfs
from src.logger import get_logger
from src.models import ClassifiedPayment
from src.reclassify import reclassify_stored
from src.reconciliation import (
    STATUS_ICONS,
    STATUS_UNCATALOGUED,
    STATUSES,
    load_catalogue,
    reconcile,
    result_boxes,
    to_markdown,
)
from src.social_security import get_ss_payments, load_bank_export, upsert_ss_payments
from src.stripe_client import backfill_billing_details_from_raw_source
from src.tax_engine import (
    compute_and_persist_tax_snapshots,
    compute_eu_b2c_threshold,
    compute_modelo_130,
    compute_modelo_303,
    compute_modelo_347,
    compute_modelo_349,
    compute_oss_return,
    load_app_config,
)
from src.tax_snapshot_codec import decode_snapshot, encode_snapshot
from src.tax_validator import find_filing, load_filings
from src.vendor_registry import apply_vendor_registry, find_unmatched_invoices

log = get_logger(__name__)

ROOT = Path(__file__).parent.parent
DEFAULT_OUT_ROOT = ROOT / "tmp" / "close_quarter"
GESTOR_NOTES_PATH = ROOT / "gestor_notes.md"  # git-ignored free-text notes for the accountant
PACK_INVOICES_DIR = "invoices"  # gestor-pack's copy of the quarter's ledger invoices, under the quarter folder

STEP_ORDER = (
    "sweep", "ocr", "vendors", "dedupe", "fx", "stripe", "reta",
    "compute", "reconcile", "sheet", "gestor-pack",
)
QUARTERLY_MODELS = ("303", "130", "349")
DEFAULT_GEO_RULES = {"eur_default", "eur_newsletter_default", "non_eur_default"}

# Ledger rows worth telling the accountant about (gestor pack notes).
_PLAIN_TREATMENTS = {None, "", "DOMESTIC", "ES_21"}


# ---------------------------------------------------------------------------
# Context + result types
# ---------------------------------------------------------------------------

@dataclass
class StepResult:
    """Outcome of one pipeline step. ``changes`` empty ⇒ the step was a no-op."""

    step: str
    changes: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    output: str = ""  # a document the CLI prints after the summary (markdown table, sheet)

    @property
    def changed(self) -> bool:
        return bool(self.changes)

    def merge(self, other: "StepResult") -> None:
        self.changes += other.changes
        self.info += other.info
        self.warnings += other.warnings
        self.errors += other.errors

    def render(self) -> str:
        head = f"[{self.step}] " + (f"{len(self.changes)} change(s)" if self.changes else "no changes")
        lines = [head]
        lines += [f"  + {c}" for c in self.changes]
        lines += [f"    {i}" for i in self.info]
        lines += [f"  ⚠ {w}" for w in self.warnings]
        lines += [f"  ❌ {e}" for e in self.errors]
        return "\n".join(lines)


@dataclass
class CloseContext:
    """Where one quarter close reads and writes. Defaults are the real app paths."""

    year: int
    quarter: int
    db_path: Optional[Path] = None          # None → data/accounting.db
    config: Optional[dict[str, Any]] = None  # None → config.json (empty if missing)
    rules: Optional[dict[str, Any]] = None   # None → classification_rules.json
    out_root: Path = DEFAULT_OUT_ROOT
    notes_path: Path = GESTOR_NOTES_PATH

    def __post_init__(self) -> None:
        if self.quarter not in (1, 2, 3, 4):
            raise ValueError(f"quarter must be 1-4, got {self.quarter}")
        if self.config is None:
            self.config = load_app_config()

    @property
    def period(self) -> str:
        return f"{self.year} Q{self.quarter}"

    @property
    def quarter_dir(self) -> Path:
        d = self.out_root / f"{self.year}_Q{self.quarter}"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def manifest_path(self) -> Path:
        return self.out_root / "invoice_copy_log.json"

    @property
    def date_bounds(self) -> tuple[str, str]:
        """Inclusive ISO date bounds of the quarter."""
        return quarter_bounds(self.year, self.quarter)

    @property
    def datetime_bounds(self) -> tuple[datetime, datetime]:
        """Quarter start 00:00:00 and end 23:59:59 (the Stripe transaction window)."""
        end_month = self.quarter * 3
        last_day = calendar.monthrange(self.year, end_month)[1]
        return (datetime(self.year, end_month - 2, 1),
                datetime(self.year, end_month, last_day, 23, 59, 59))

    def previous_period(self) -> tuple[int, int]:
        return (self.year - 1, 4) if self.quarter == 1 else (self.year, self.quarter - 1)

    def connect(self) -> sqlite3.Connection:
        return get_connection(self.db_path)


def write_if_changed(path: Path, text: str) -> bool:
    """Write ``text`` to ``path`` unless it already holds exactly that. True if written."""
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def _same_file(a: Path, b: Path) -> bool:
    return b.exists() and a.stat().st_size == b.stat().st_size and a.read_bytes() == b.read_bytes()


def _in_quarter(ctx: CloseContext, iso_date: Optional[str]) -> bool:
    start, end = ctx.date_bounds
    return bool(iso_date) and start <= str(iso_date)[:10] <= end


# ---------------------------------------------------------------------------
# 1. sweep
# ---------------------------------------------------------------------------

def _flat_name(prefix: str, rel: str) -> str:
    return f"{prefix} - " + rel.replace("\\", " - ").replace("/", " - ")


def step_sweep(ctx: CloseContext) -> StepResult:
    """Copy invoice PDFs not yet catalogued in the DB nor copied before into the quarter folder.

    The cumulative manifest (``invoice_copy_log.json``) makes a re-run pick up
    only files added since the last sweep.
    """
    res = StepResult("sweep")
    dest = ctx.quarter_dir
    conn = ctx.connect()
    try:
        known = {d: {r["filename"] for r in conn.execute(
            "SELECT filename FROM invoices WHERE direction = ?", (d,))} for d in ("in", "out")}
    finally:
        conn.close()

    manifest: dict[str, Any] = (json.loads(ctx.manifest_path.read_text(encoding="utf-8"))
                                if ctx.manifest_path.exists() else {"in": [], "out": []})
    for direction, prefix in (("in", "IN"), ("out", "OUT")):
        base = resolve_invoice_dir(direction, ctx.config)
        already = set(manifest.get(direction, []))
        copied = []
        for pdf in scan_invoice_pdfs(direction, ctx.config):
            rel = str(pdf.relative_to(base))
            if rel in known[direction] or rel in already:
                continue
            shutil.copy2(pdf, dest / _flat_name(prefix, rel))
            copied.append(rel)
            res.changes.append(f"copied {prefix:<3} {rel}")
        manifest[direction] = sorted(already | set(copied))
    manifest["last_run_at"] = datetime.now().isoformat(timespec="seconds")
    ctx.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    ctx.manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    res.info.append(f"Folder: {dest}")
    if not res.changes:
        res.info.append("No new invoices found.")
    log.info("ℹ️ Sweep %s: copied %d file(s) into %s", ctx.period, len(res.changes), dest)
    return res


# ---------------------------------------------------------------------------
# 2. ocr
# ---------------------------------------------------------------------------

def step_ocr(
    ctx: CloseContext,
    directions: tuple[str, ...] = ("in", "out"),
    dry_run: bool = False,
    model: Optional[str] = None,
) -> StepResult:
    """Extract every new or changed PDF (hash-based) and save it with FX + registry defaults.

    Continues past a failing file and reports it in ``errors``; the file stays
    pending, so the next run retries it.
    """
    res = StepResult("ocr")
    for direction in directions:
        label = direction.upper()
        pending = pending_files(direction, ctx.config, ctx.db_path)
        if not pending:
            res.info.append(f"{label}: all PDFs already extracted.")
            continue
        if dry_run:
            res.info.append(f"{label}: {len(pending)} PDF(s) would be extracted (dry run):")
            res.info += [f"  {f}" for f in pending]
            continue
        for fname in pending:
            try:
                rec = extract_and_save(fname, direction, config=ctx.config,
                                       db_path=ctx.db_path, model=model)
            except Exception as exc:  # one bad PDF must not stop the batch
                log.error("❌ OCR failed for %s (%s): %s", fname, direction, exc)
                res.errors.append(f"{label} {fname}: {exc}")
                continue
            party = rec.get("vendor_name") if direction == "in" else rec.get("client_name")
            total = rec.get("total_eur")
            res.changes.append(
                f"extracted {label} {fname} → {rec.get('invoice_date') or '?'} · {party or '?'} · "
                f"{'?' if total is None else f'{total:,.2f}'} EUR"
            )
    log.info("ℹ️ OCR %s: %d extracted, %d failed%s", ctx.period, len(res.changes),
             len(res.errors), " (dry run)" if dry_run else "")
    return res


# ---------------------------------------------------------------------------
# 3. vendors
# ---------------------------------------------------------------------------

def step_vendors(ctx: CloseContext) -> StepResult:
    """Apply the vendor registry to stored expenses; warn about the quarter's unknown vendors."""
    res = StepResult("vendors")
    applied = apply_vendor_registry(db_path=ctx.db_path)
    if applied.rows_updated:
        fields = ", ".join(f"{k}×{v}" for k, v in sorted(applied.field_updates.items()))
        res.changes.append(f"registry defaults written on {applied.rows_updated} expense invoice(s): {fields}")
    res.info.append(f"Expense invoices: {applied.scanned} scanned, {applied.matched} matched, "
                    f"{applied.unmatched} unknown vendor(s) in total.")

    unmatched = find_unmatched_invoices(db_path=ctx.db_path)
    in_quarter = [r for r in unmatched if _in_quarter(ctx, r.get("invoice_date"))]
    for row in in_quarter:
        res.warnings.append(
            f"unknown vendor: {row.get('filename')} ({row.get('vendor_name') or '?'}, "
            f"{row.get('invoice_date')}) — suggested key {row.get('suggested_key') or '?'}"
        )
    if in_quarter:
        res.info.append("Add unknown vendors in the Vendors tab (vendors.json), then rerun `vendors`.")
    elsewhere = len(unmatched) - len(in_quarter)
    if elsewhere:
        res.info.append(f"{elsewhere} unknown-vendor invoice(s) outside {ctx.period} (not listed).")
    return res


# ---------------------------------------------------------------------------
# 4. dedupe
# ---------------------------------------------------------------------------

def step_dedupe(ctx: CloseContext, apply: bool = False) -> StepResult:
    """Detect duplicate / receipt / out-of-period groups; exclude the losers only with ``apply``.

    Out-of-period only looks at the invoices swept into this quarter's folder.
    Excluded (or exclusion-locked) rows are never proposed again, so a second
    run after ``apply`` finds nothing.
    """
    res = StepResult("dedupe")
    rows = get_invoices(db_path=ctx.db_path)
    names = {r["id"]: r.get("filename") or r["id"] for r in rows}
    sweep_rows = load_sweep_rows(ctx.year, ctx.quarter, db_path=ctx.db_path, sweep_dir=ctx.quarter_dir)
    groups = find_duplicate_groups(rows, sweep_rows=sweep_rows, year=ctx.year, quarter=ctx.quarter)
    if not groups:
        res.info.append("No duplicate/receipt/out-of-period groups found.")
        return res

    for g in groups:
        keeper = f" (keeps {names.get(g.keeper_id, g.keeper_id)})" if g.keeper_id else ""
        losers = ", ".join(names.get(i, i) for i in g.loser_ids)
        res.info.append(f"[{g.detector}/{g.reason}] exclude {losers}{keeper} — {g.note}")
    n_losers = sum(len(g.loser_ids) for g in groups)
    if not apply:
        res.warnings.append(f"{n_losers} exclusion(s) proposed in {len(groups)} group(s) — "
                            f"review, then rerun with --apply")
        return res
    out = apply_groups(groups, db_path=ctx.db_path)
    if out["applied"]:
        by = ", ".join(f"{k}×{v}" for k, v in sorted(out["by_detector"].items()))
        res.changes.append(f"excluded {out['applied']} invoice(s): {by}")
    if out["skipped_locked"]:
        res.warnings.append(f"{out['skipped_locked']} row(s) skipped: exclusion locked by a manual edit")
    return res


# ---------------------------------------------------------------------------
# 5. fx
# ---------------------------------------------------------------------------

def step_fx(ctx: CloseContext, apply: bool = False) -> StepResult:
    """Backfill ECB rates to today, then re-resolve the quarter's stored non-EUR invoices.

    The rate backfill always runs (an upserting cache). The invoice recompute
    (invoices dated from the quarter start on) writes only with ``apply``.
    """
    res = StepResult("fx")
    before = get_rate_count(ctx.db_path)
    backfill_to_today(ctx.db_path)
    added = get_rate_count(ctx.db_path) - before
    _, latest = get_stored_date_range(ctx.db_path)
    if added:
        res.changes.append(f"stored {added} new ECB rate entr{'y' if added == 1 else 'ies'} (latest {latest})")
    else:
        res.info.append(f"ECB rates already up to date (latest {latest or 'none'}).")
    target = min(date.fromisoformat(ctx.date_bounds[1]), date.today())
    if latest is None or latest < target - timedelta(days=STALE_TOLERANCE_DAYS):
        res.warnings.append(f"latest stored ECB rate is {latest or 'none'}, short of {target} — "
                            f"the backfill did not reach it (network failure? see the log)")

    since = ctx.date_bounds[0]
    rc = recompute_stored_invoice_fx(db_path=ctx.db_path, dry_run=not apply, since=since)
    res.info.append(f"Invoice FX recompute (from {since}): scanned {rc.scanned}, "
                    f"{'changed' if apply else 'would change'} {rc.changed}, locked {rc.locked_skipped}.")
    for row in rc.rows:
        line = (f"{row.filename} ({row.direction}, {row.currency}): {row.old_total_eur} → "
                f"{row.new_total_eur} EUR [{row.fx_source}]{' STALE' if row.fx_stale else ''}")
        if row.locked_skipped:
            res.info.append(f"skipped (locked): {row.filename}")
        elif apply:
            res.changes.append(f"re-resolved {line}")
        else:
            res.info.append(f"would re-resolve {line}")
    if not apply and rc.changed:
        res.warnings.append(f"{rc.changed} invoice(s) would change — review, then rerun with --apply")
    if rc.stale:
        res.warnings.append(f"{rc.stale} invoice(s) resolved on a stale ECB rate (> {STALE_TOLERANCE_DAYS} days away)")
    if rc.cross_check_flagged:
        res.warnings.append(f"{rc.cross_check_flagged} invoice(s) differ > 1% from the document's own EUR figure")
    return res


# ---------------------------------------------------------------------------
# 6. stripe
# ---------------------------------------------------------------------------

def _payment_fingerprint(p: ClassifiedPayment) -> tuple:
    return (p.created_date.isoformat(), p.currency, round(p.converted_amount, 2),
            round(p.converted_amount_refunded, 2), round(p.fee, 2),
            p.activity_type, p.geo_region, p.geo_rule, p.classification_rule)


def _quarter_payments(ctx: CloseContext) -> list[ClassifiedPayment]:
    start, end = ctx.datetime_bounds
    return load_classified_payments(start, end, db_path=ctx.db_path)


def step_stripe(ctx: CloseContext, fetch: Optional[Callable[[], Any]] = None) -> StepResult:
    """Fetch (via ``fetch``) + billing backfill + reclassify the quarter, then list review warnings.

    ``fetch`` pulls the quarter from Stripe and persists it classified — the
    CLI passes ``app.data_loader.get_classified_for_period`` in API mode (this
    module never imports the UI package). ``None`` skips the fetch.
    """
    res = StepResult("stripe")
    before = {p.id: _payment_fingerprint(p) for p in _quarter_payments(ctx)}
    if fetch is None:
        res.info.append("Stripe fetch skipped (no fetcher given).")
    else:
        fetch()

    bf = backfill_billing_details_from_raw_source(dry_run=False, db_path=ctx.db_path)
    if bf.updated:
        res.changes.append(f"filled billing email/country on {bf.updated} stored transaction(s) "
                           f"({bf.email_filled} emails, {bf.country_filled} countries)")
    start, end = ctx.datetime_bounds
    rc = reclassify_stored(start, end, rules=ctx.rules, db_path=ctx.db_path)

    payments = _quarter_payments(ctx)
    after = {p.id: _payment_fingerprint(p) for p in payments}
    new_ids = sorted(after.keys() - before.keys())
    if new_ids:
        res.changes.append(f"stored {len(new_ids)} new transaction(s)")
    for pid in sorted(k for k in after.keys() & before.keys() if after[k] != before[k]):
        old, new = before[pid], after[pid]
        diff = ", ".join(f"{name} {o} → {n}" for name, o, n in zip(
            ("date", "currency", "amount", "refunded", "fee", "activity", "geo", "geo_rule", "rule"),
            old, new) if o != n)
        res.changes.append(f"updated {pid}: {diff}")
    res.info.append(f"{ctx.period}: {len(payments)} transaction(s); reclassify scanned {rc.scanned}, "
                    f"changed {len(rc.changes)}.")

    val = validate_classifications(payments)
    if val["activity_errors"] or val["geo_errors"] or val["unknown_activity"]:
        res.warnings.append(f"classification: {val['activity_errors']} activity error(s), "
                            f"{val['geo_errors']} geo error(s), {val['unknown_activity']} unclassified")
    on_default = [p for p in payments if p.geo_rule in DEFAULT_GEO_RULES]
    if on_default:
        res.info.append(f"{len(on_default)} transaction(s) on a default geo rule (no client override) — "
                        f"review them with `stripe-fetch`.")
    for p in payments:
        warning = eur_default_foreign_warning(p)
        if warning:
            res.warnings.append(f"{p.created_date:%Y-%m-%d} {p.id} {p.converted_amount:,.2f} EUR — {warning} "
                                f"(add-override if not in Spain)")
    conn = ctx.connect()
    try:
        tracker = compute_eu_b2c_threshold(ctx.year, ctx.quarter, conn, ctx.config)
    finally:
        conn.close()
    (res.info if tracker.status == "OK" else res.warnings).append(tracker.message)
    return res


# ---------------------------------------------------------------------------
# 7. reta
# ---------------------------------------------------------------------------

def step_reta(ctx: CloseContext, export_file: str | Path) -> StepResult:
    """Import the RETA (TGSS) debits of a bank export; already-stored rows are skipped.

    Column names come from ``config.json → social_security`` (defaults
    ``Fecha`` / ``Importe``, the header row is auto-detected).
    """
    res = StepResult("reta")
    ss_cfg = (ctx.config or {}).get("social_security", {})
    path = Path(export_file)
    rows = load_bank_export(
        path,
        date_column=ss_cfg.get("date_column", "Fecha"),
        amount_column=ss_cfg.get("amount_column", "Importe"),
        description_column=ss_cfg.get("description_column") or None,
        concept_column=ss_cfg.get("concept_column") or None,
        concept_patterns=ss_cfg.get("concept_patterns") or None,
    )
    inserted, skipped = upsert_ss_payments(rows, source_file=path.name, db_path=ctx.db_path)
    if inserted:
        res.changes.append(f"imported {inserted} RETA payment(s) from {path.name}")
    res.info.append(f"{len(rows)} row(s) parsed, {skipped} already stored.")
    start, end = ctx.date_bounds
    in_q = get_ss_payments(start, end, db_path=ctx.db_path)
    total = round(sum(float(r["amount_eur"]) for r in in_q), 2)
    if in_q:
        res.info.append(f"RETA in {ctx.period}: {total:,.2f} EUR ({len(in_q)} payment(s)).")
    else:
        res.warnings.append(f"no RETA payment stored in {ctx.period}")
    return res


# ---------------------------------------------------------------------------
# 8. compute
# ---------------------------------------------------------------------------

def _compute_all(ctx: CloseContext, conn: sqlite3.Connection) -> dict[str, Any]:
    """Every engine ``compute_and_persist_tax_snapshots`` persists, keyed by snapshot model."""
    y, q, cfg = ctx.year, ctx.quarter, ctx.config
    return {
        "303": compute_modelo_303(y, q, conn, cfg),
        "130": compute_modelo_130(y, q, conn, cfg),
        "OSS": compute_oss_return(y, q, conn, cfg),
        "349": compute_modelo_349(y, q, conn, cfg),
        "347": compute_modelo_347(y, conn, cfg),
    }


def _box_summary(model: str, result: Any) -> str:
    boxes = {b: v for b, v in result_boxes(model, result).items() if abs(v) >= 0.005}
    if not boxes:
        return f"Modelo {model}: all boxes 0"
    return f"Modelo {model}: " + ", ".join(f"[{b}] {v:,.2f}" for b, v in sorted(boxes.items(), key=_box_key))


def _box_key(item: tuple[str, float]) -> tuple[int, str]:
    box = item[0]
    return (int(box) if box.isdigit() else 10_000, box)


def step_compute(ctx: CloseContext) -> StepResult:
    """Compute the quarter's 303/130/349 (+ OSS, 347) and persist snapshots if any payload changed."""
    res = StepResult("compute")
    conn = ctx.connect()
    try:
        results = _compute_all(ctx, conn)
        stored = {row["model"]: row for row in load_tax_snapshots_for_period(ctx.year, ctx.quarter, conn)}
        changed = [m for m, r in results.items()
                   if stored.get(m) is None or stored[m]["payload_json"] != encode_snapshot(m, r)]
        if changed:
            computed_at = compute_and_persist_tax_snapshots(ctx.year, ctx.quarter, conn, ctx.config)
            res.changes.append(f"saved snapshots at {computed_at} (changed: {', '.join(changed)})")
        else:
            res.info.append(f"Snapshots unchanged since {stored['303']['computed_at']}.")
    finally:
        conn.close()
    for model in QUARTERLY_MODELS:
        res.info.append(_box_summary(model, results[model]))
        notes = getattr(results[model], "notes", "")
        if notes:
            res.info.append(f"  note: {notes}")
    log.info("ℹ️ Compute %s: %s", ctx.period, "changed " + ", ".join(changed) if changed else "unchanged")
    return res


# ---------------------------------------------------------------------------
# 9. reconcile
# ---------------------------------------------------------------------------

def step_reconcile(ctx: CloseContext) -> StepResult:
    """Reconcile the quarter against its filed returns — or, if not filed yet, the previous quarter.

    Writes ``reconciliation_<Y>_Q<Q>.md`` (the reconciled period) into this
    quarter's folder. 🔴 uncatalogued differences are warnings.
    """
    res = StepResult("reconcile")
    conn = ctx.connect()
    try:
        filings = load_filings(conn)
        if any(find_filing(filings, m, ctx.year, ctx.quarter) for m in QUARTERLY_MODELS):
            year, quarter = ctx.year, ctx.quarter
            res.info.append(f"{ctx.period} has a filed return: reconciling it.")
        else:
            year, quarter = ctx.previous_period()
            res.info.append(f"{ctx.period} not filed yet: reconciling the previous quarter "
                            f"{year} Q{quarter} (the chain this quarter's carry-forwards start from).")
        catalogue = load_catalogue()
        recs = [reconcile(m, year, quarter, conn, ctx.config, catalogue) for m in QUARTERLY_MODELS]
    finally:
        conn.close()

    for rec in recs:
        counts = rec.counts()
        res.info.append(f"Modelo {rec.model} {rec.period}: "
                        + " · ".join(f"{STATUS_ICONS[s]} {counts[s]}" for s in STATUSES))
        if not rec.filed_found:
            res.warnings.append(f"no filed Modelo {rec.model} for {rec.period} — import the AEAT receipt "
                                f"(python -m src.filed_returns import <pdf>)")
            continue
        for ln in rec.lines:
            if ln.status == STATUS_UNCATALOGUED:
                res.warnings.append(f"🔴 Modelo {rec.model} {rec.period} box {ln.box}: filed "
                                    f"{ln.filed} vs app {ln.app} — uncatalogued")

    text = (f"# Reconciliation — {year} Q{quarter} (close of {ctx.period})\n\n"
            + "\n".join(to_markdown(r) for r in recs))
    path = ctx.quarter_dir / f"reconciliation_{year}_Q{quarter}.md"
    if write_if_changed(path, text):
        res.changes.append(f"wrote {path}")
    else:
        res.info.append(f"{path} unchanged.")
    res.output = text
    return res


# ---------------------------------------------------------------------------
# 10. sheet — pluggable renderer, default ``src.filing_sheet.render_filing_sheet``
# ---------------------------------------------------------------------------

FilingSheetRenderer = Callable[[int, int, sqlite3.Connection, dict], str]


def _stored_results(year: int, quarter: int, conn: sqlite3.Connection) -> dict[str, Any]:
    rows = load_tax_snapshots_for_period(year, quarter, conn)
    return {r["model"]: decode_snapshot(r["model"], r["payload_json"]) for r in rows
            if r["model"] in QUARTERLY_MODELS}


# The real filing sheet (#101): AEAT form order, credit chain, deadlines,
# filed-version diff. Swappable (tests and callers may plug in another).
filing_sheet_renderer: FilingSheetRenderer = render_filing_sheet


def step_sheet(ctx: CloseContext, renderer: Optional[FilingSheetRenderer] = None) -> StepResult:
    """Render the filing sheet from the stored snapshots into ``filing_sheet_<Y>_Q<Q>.md``."""
    res = StepResult("sheet")
    conn = ctx.connect()
    try:
        missing = [m for m in QUARTERLY_MODELS if m not in _stored_results(ctx.year, ctx.quarter, conn)]
        text = (renderer or filing_sheet_renderer)(ctx.year, ctx.quarter, conn, ctx.config)
    finally:
        conn.close()
    if missing:
        res.warnings.append(f"no stored snapshot for Modelo {', '.join(missing)} — run `compute` first")
    path = ctx.quarter_dir / f"filing_sheet_{ctx.year}_Q{ctx.quarter}.md"
    if write_if_changed(path, text):
        res.changes.append(f"wrote {path}")
    else:
        res.info.append(f"{path} unchanged.")
    res.output = text
    return res


# ---------------------------------------------------------------------------
# Stripe report (the `report` subcommand, and the gestor pack's report)
# ---------------------------------------------------------------------------

def _reclassified_payments(ctx: CloseContext, res: StepResult) -> list[ClassifiedPayment]:
    """Reclassify the quarter's stored rows (never export them stale), then load them."""
    start, end = ctx.datetime_bounds
    rc = reclassify_stored(start, end, rules=ctx.rules, db_path=ctx.db_path)
    res.changes += [f"reclassified {c.describe()}" for c in rc.changes]
    res.info.append(f"Reclassify: scanned {rc.scanned}, changed {len(rc.changes)}.")
    return _quarter_payments(ctx)


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
        drift = declared_vs_live_drift(conn, ctx.year, ctx.quarter, _quarter_payments(ctx))
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
            drift = declared_vs_live_drift(conn, ctx.year, ctx.quarter, _quarter_payments(ctx))
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
        if not _in_quarter(ctx, r.get("invoice_date")):
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
        if not _in_quarter(ctx, r.get("invoice_date")):
            continue
        if r.get("excluded"):
            n_excluded += 1
            continue
        source = resolve_invoice_dir(r["direction"], ctx.config) / r["filename"]
        if not source.is_file():
            res.warnings.append(f"{r['direction'].upper()} {r['filename']}: PDF not found at {source} "
                                f"— not in the pack")
            continue
        name = _flat_name(r["direction"].upper(), r["filename"])
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
        if not _same_file(source, pack / name):
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


# ---------------------------------------------------------------------------
# archive (after filing)
# ---------------------------------------------------------------------------

def step_archive(ctx: CloseContext) -> StepResult:
    """Copy the quarter folder and a dated database snapshot into ``app.archive_dir/<Y>T<Q>/``.

    Runbook step 19. It only adds or updates copies, and never deletes from the
    archive. A re-run on the same day with nothing new reports no changes.
    """
    res = StepResult("archive")
    raw = ((ctx.config or {}).get("app") or {}).get("archive_dir")
    if not raw:
        res.errors.append("app.archive_dir is not set in config.json — nowhere to archive the quarter")
        return res
    root = Path(raw) if Path(raw).is_absolute() else ROOT / raw
    dest = root / f"{ctx.year}T{ctx.quarter}"
    src_dir = ctx.quarter_dir
    for f in sorted(p for p in src_dir.rglob("*") if p.is_file()):
        target = dest / f.relative_to(src_dir)
        if _same_file(f, target):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, target)
        res.changes.append(f"copied {f.relative_to(src_dir)}")
    snapshot = dest / "database" / f"accounting_{date.today():%Y%m%d}.db"
    if not snapshot.exists():
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        conn = ctx.connect()
        try:
            with sqlite3.connect(snapshot) as out:
                conn.backup(out)
        finally:
            conn.close()
        res.changes.append(f"database snapshot {snapshot.name}")
    res.info.append(f"Archive folder: {dest}")
    log.info("ℹ️ Archive %s: %d change(s) into %s", ctx.period, len(res.changes), dest)
    return res


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------

def run_all(
    ctx: CloseContext,
    *,
    apply: bool = False,
    freeze: bool = False,
    reta_file: Optional[str | Path] = None,
    model: Optional[str] = None,
    stripe_fetch: Optional[Callable[[], Any]] = None,
    on_step: Optional[Callable[[int, StepResult], None]] = None,
) -> list[StepResult]:
    """Run every step in ``STEP_ORDER``. Stops at the first step that raises.

    ``apply`` lets ``dedupe`` and ``fx`` write; ``freeze`` lets ``gestor-pack``
    freeze the Stripe report. ``reta`` is skipped without ``reta_file``.
    """
    steps: dict[str, Callable[[], StepResult]] = {
        "sweep": lambda: step_sweep(ctx),
        "ocr": lambda: step_ocr(ctx, model=model),
        "vendors": lambda: step_vendors(ctx),
        "dedupe": lambda: step_dedupe(ctx, apply=apply),
        "fx": lambda: step_fx(ctx, apply=apply),
        "stripe": lambda: step_stripe(ctx, fetch=stripe_fetch),
        "reta": (lambda: step_reta(ctx, reta_file)) if reta_file else
                (lambda: StepResult("reta", info=["Skipped: no --reta-file given."])),
        "compute": lambda: step_compute(ctx),
        "reconcile": lambda: step_reconcile(ctx),
        "sheet": lambda: step_sheet(ctx),
        "gestor-pack": lambda: step_gestor_pack(ctx, freeze=freeze),
    }
    results: list[StepResult] = []
    for n, name in enumerate(STEP_ORDER, start=1):
        try:
            result = steps[name]()
        except Exception as exc:
            log.error("❌ Close %s: step %s failed: %s", ctx.period, name, exc)
            result = StepResult(name, errors=[f"step failed, pipeline stopped: {exc}"])
        results.append(result)
        if on_step:
            on_step(n, result)
        if result.errors and name != "ocr":  # per-file OCR errors are reported, not fatal
            break
    return results
