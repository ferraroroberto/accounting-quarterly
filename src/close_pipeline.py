"""Quarter-close pipeline: idempotent steps that each report what they changed.

``scripts/close_quarter.py`` is the thin CLI over this module and the
``/close-quarter`` skill drives the CLI. Steps, in pipeline order
(``STEP_ORDER``):

1. ``sweep``       copy new invoice PDFs into ``tmp/close_quarter/<Y>_Q<Q>/``.
2. ``ocr``         extract new/changed PDFs (MD5 vs stored ``file_hash``) through
                   ``src.invoice_ingest``; a failing file is reported, never fatal.
3. ``vendors``     apply the vendor registry to the quarter (``all_periods``: everywhere);
                   list the quarter's unknown vendors.
4. ``dedupe``      duplicate / receipt / out-of-period groups; writes only with ``apply``
                   (all groups, or the ``groups`` numbers); numbering conflicts are warnings.
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

import json
import shutil
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from src.classifier import eur_default_foreign_warning, validate_classifications
from src.close.context import (
    ROOT,
    CloseContext,
    StepResult,
    is_in_quarter,
    quarter_payments,
    same_file,
    write_if_changed,
)
from src.close.gestor_pack import (  # noqa: F401  (re-exported for scripts/close_quarter.py)
    freeze_sent_stripe_report,
    step_gestor_pack,
    write_stripe_report,
)
from src.database import (
    filed_snapshot_periods,
    get_invoices,
    load_tax_snapshots_for_period,
)
from src.filing_sheet import render_filing_sheet
from src.fx_rates import (
    FX_CROSS_CHECK_THRESHOLD_PCT,
    STALE_TOLERANCE_DAYS,
    backfill_to_today,
    get_rate_count,
    get_stored_date_range,
    recompute_stored_invoice_fx,
)
from src.invoice_dedupe import (
    apply_groups,
    find_duplicate_groups,
    find_numbering_conflicts,
    flat_sweep_name,
    load_sweep_rows,
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
from src.returns.oss_return import compute_eu_b2c_threshold
from src.tax_engine import compute_and_persist_tax_snapshots, compute_tax_results
from src.tax_snapshot_codec import decode_snapshot, encode_snapshot
from src.tax_validator import find_filing, load_filings
from src.vendor_registry import apply_vendor_registry, find_unmatched_invoices

log = get_logger(__name__)

STEP_ORDER = (
    "sweep", "ocr", "vendors", "dedupe", "fx", "stripe", "reta",
    "compute", "reconcile", "sheet", "gestor-pack",
)
QUARTERLY_MODELS = ("303", "130", "349")
DEFAULT_GEO_RULES = {"eur_default", "eur_newsletter_default", "non_eur_default"}



# ---------------------------------------------------------------------------
# 1. sweep
# ---------------------------------------------------------------------------

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
            shutil.copy2(pdf, dest / flat_sweep_name(prefix, rel))
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

def step_vendors(ctx: CloseContext, all_periods: bool = False) -> StepResult:
    """Apply the vendor registry to the quarter's expenses; warn about its unknown vendors.

    Only expense invoices dated in the quarter are written, and none when the
    quarter has a FILED snapshot, so a later registry edit never silently
    re-treats a filed period. ``all_periods`` is the explicit opt-in to apply
    it to every stored expense invoice, filed periods included; it reports the
    rows written per year/quarter.
    """
    res = StepResult("vendors")
    conn = ctx.connect()
    try:
        filed = filed_snapshot_periods(conn)
        quarter_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM invoices WHERE direction = 'in' AND substr(invoice_date, 1, 10) BETWEEN ? AND ?",
            ctx.date_bounds)]
    finally:
        conn.close()

    applied = None
    if all_periods:
        applied = apply_vendor_registry(db_path=ctx.db_path)
        scope = "all periods"
    elif (ctx.year, ctx.quarter) in filed:
        res.warnings.append(f"{ctx.period} has a FILED return — registry defaults not written. "
                            f"Re-applying them needs `vendors --all-periods`.")
    else:
        applied = apply_vendor_registry(db_path=ctx.db_path, invoice_ids=quarter_ids)
        scope = ctx.period
    if applied is not None:
        if applied.rows_updated:
            fields = ", ".join(f"{k}×{v}" for k, v in sorted(applied.field_updates.items()))
            res.changes.append(f"registry defaults written on {applied.rows_updated} expense invoice(s): {fields}")
            if all_periods:
                res.changes.append(f"per period: {applied.period_summary()}")
        res.info.append(f"Expense invoices ({scope}): {applied.scanned} scanned, {applied.matched} matched, "
                        f"{applied.unmatched} unknown vendor(s).")

    unmatched = find_unmatched_invoices(db_path=ctx.db_path)
    in_quarter = [r for r in unmatched if is_in_quarter(ctx, r.get("invoice_date"))]
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

def step_dedupe(ctx: CloseContext, apply: bool = False, groups: Optional[list[int]] = None) -> StepResult:
    """Detect duplicate / receipt / out-of-period groups; exclude the losers only with ``apply``.

    Groups are numbered ``#1``, ``#2``… in the review; ``groups`` limits
    ``apply`` to those numbers (an unknown number applies nothing). Out-of-period
    only looks at the invoices swept into this quarter's folder. Excluded (or
    exclusion-locked) rows are never proposed again, so a second run after
    ``apply`` finds nothing. A shared invoice number with a different total or
    date is reported as a numbering warning and never excluded.
    """
    res = StepResult("dedupe")
    rows = get_invoices(db_path=ctx.db_path)
    names = {r["id"]: r.get("filename") or r["id"] for r in rows}
    for conflict in find_numbering_conflicts(rows):
        res.warnings.append(f"numbering, not a duplicate (nothing excluded): {conflict.note}")
    sweep_rows = load_sweep_rows(ctx.year, ctx.quarter, db_path=ctx.db_path, sweep_dir=ctx.quarter_dir)
    found = find_duplicate_groups(rows, sweep_rows=sweep_rows, year=ctx.year, quarter=ctx.quarter)
    if not found:
        res.info.append("No duplicate/receipt/out-of-period groups found.")
        return res

    for n, g in enumerate(found, start=1):
        keeper = f" (keeps {names.get(g.keeper_id, g.keeper_id)})" if g.keeper_id else ""
        losers = ", ".join(names.get(i, i) for i in g.loser_ids)
        res.info.append(f"#{n} [{g.detector}/{g.reason}] exclude {losers}{keeper} — {g.note}")
    n_losers = sum(len(g.loser_ids) for g in found)
    if not apply:
        res.warnings.append(f"{n_losers} exclusion(s) proposed in {len(found)} group(s) — review, then "
                            f"rerun with --apply (all) or --apply --group N (only group #N)")
        return res
    if groups:
        unknown = sorted({n for n in groups if not 1 <= n <= len(found)})
        if unknown:
            res.errors.append(f"no group {', '.join(f'#{n}' for n in unknown)} (there are {len(found)}) "
                              f"— nothing applied; review again, numbers shift after each apply")
            return res
        found = [found[n - 1] for n in sorted(set(groups))]
    out = apply_groups(found, db_path=ctx.db_path)
    if out["applied"]:
        by = ", ".join(f"{k}×{v}" for k, v in sorted(out["by_detector"].items()))
        res.changes.append(f"excluded {out['applied']} invoice(s): {by}")
    if out["skipped_locked"]:
        res.warnings.append(f"{out['skipped_locked']} row(s) skipped: exclusion locked by a manual edit")
    for g, why in out["skipped_groups"]:
        res.warnings.append(f"group [{g.detector}] keeping {names.get(g.keeper_id, g.keeper_id)} skipped: "
                            f"{why}, so excluding the rest would leave no active row")
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
        res.warnings.append(f"{rc.cross_check_flagged} invoice(s) differ > {FX_CROSS_CHECK_THRESHOLD_PCT:g}% from the document's own EUR figure")
    return res


# ---------------------------------------------------------------------------
# 6. stripe
# ---------------------------------------------------------------------------

def _payment_fingerprint(p: ClassifiedPayment) -> tuple:
    return (p.created_date.isoformat(), p.currency, round(p.converted_amount, 2),
            round(p.converted_amount_refunded, 2), round(p.fee, 2),
            p.activity_type, p.geo_region, p.geo_rule, p.classification_rule)


def step_stripe(ctx: CloseContext, fetch: Optional[Callable[[], Any]] = None) -> StepResult:
    """Fetch (via ``fetch``) + billing backfill + reclassify the quarter, then list review warnings.

    ``fetch`` pulls the quarter from Stripe and persists it classified — the
    CLI passes ``src.payments_loader.get_classified_for_period`` in API mode
    (this module never imports the UI package). ``None`` skips the fetch.
    """
    res = StepResult("stripe")
    before = {p.id: _payment_fingerprint(p) for p in quarter_payments(ctx)}
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

    payments = quarter_payments(ctx)
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
        results = compute_tax_results(ctx.year, ctx.quarter, conn, ctx.config)
        stored = {row["model"]: row for row in load_tax_snapshots_for_period(ctx.year, ctx.quarter, conn)}
        changed = [m for m, r in results.items()
                   if stored.get(m) is None or stored[m]["payload_json"] != encode_snapshot(m, r)]
        if changed:
            computed_at = compute_and_persist_tax_snapshots(
                ctx.year, ctx.quarter, conn, ctx.config, results=results)
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


# The real filing sheet: AEAT form order, credit chain, deadlines,
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
        if same_file(f, target):
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
