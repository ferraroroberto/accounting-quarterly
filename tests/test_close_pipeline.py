"""Quarter-close pipeline (#102): every step on a temp DB with synthetic files.

OCR, the ECB rate fetch and the Stripe fetch are mocked — no hub, network or
real-DB access. Each step is also checked for idempotence: a second run with
nothing changed reports no changes. All data is synthetic (fake vendors,
example.* emails, round amounts).
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

import src.database as database
import src.fx_rates as fx_rates
import src.invoice_ingest as invoice_ingest
import src.tax_validator as tax_validator
from src import close_pipeline as cp
from src.classifier import classify_payment
from src.close_pipeline import CloseContext, StepResult
from src.database import get_invoices, init_db, upsert_classified, upsert_invoice, upsert_payments
from src.declared_reports import get_declared_report
from src.filed_returns import FiledReturn, store_filed_return
from src.models import Payment

ROOT = Path(__file__).parent.parent
YEAR, QUARTER = 2025, 1


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def ctx(tmp_path, monkeypatch, sample_rules) -> CloseContext:
    db = tmp_path / "accounting.db"
    monkeypatch.setattr(database, "_DB_PATH", db)  # safety net: nothing may reach the real DB
    monkeypatch.setattr(tax_validator, "_YAML_PATH", tmp_path / "no-validation.yaml")
    init_db(db)
    inv_in, inv_out = tmp_path / "invoices" / "in", tmp_path / "invoices" / "out"
    inv_in.mkdir(parents=True)
    inv_out.mkdir(parents=True)
    config = {"app": {"invoice_in_dir": str(inv_in), "invoice_out_dir": str(inv_out)}, "tax": {}}
    return CloseContext(YEAR, QUARTER, db_path=db, config=config, rules=sample_rules,
                        out_root=tmp_path / "close_quarter", notes_path=tmp_path / "gestor_notes.md")


def _dir(ctx: CloseContext, direction: str) -> Path:
    return Path(ctx.config["app"][f"invoice_{direction}_dir"])


def _pdf(ctx: CloseContext, direction: str, rel: str, content: str = "") -> Path:
    path = _dir(ctx, direction) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"%PDF-1.4 synthetic {rel} {content}".encode())
    return path


# Extraction results keyed by PDF file name (the mock OCR backend).
OCR_DATA = {
    "inv-001.pdf": {"invoice_number": "A-1", "invoice_date": "2025-02-10", "vendor_name": "Example Cloud Ltd",
                    "vendor_nif": "IE0000000XX", "subtotal_eur": 100.0, "iva_rate": 0.0, "iva_amount": 0.0,
                    "total_eur": 100.0, "currency": "EUR"},
    "inv-002.pdf": {"invoice_number": "B-7", "invoice_date": "2025-03-05", "vendor_name": "Sample Supplies SL",
                    "vendor_nif": "B00000000", "subtotal_eur": 50.0, "iva_rate": 21.0, "iva_amount": 10.5,
                    "total_eur": 60.5, "currency": "EUR"},
    "out-001.pdf": {"invoice_number": "F-1", "invoice_date": "2025-01-20", "client_name": "Example Client SL",
                    "client_nif": "B11111111", "subtotal_eur": 200.0, "iva_rate": 21.0, "iva_amount": 42.0,
                    "total_eur": 242.0, "currency": "EUR"},
}


@pytest.fixture
def fake_ocr(monkeypatch):
    """Mock OCR backend: records calls; files named ``bad*`` raise."""
    calls: list[tuple[str, object]] = []

    def extract(pdf_path, api_key=None, provider=None, model=None):
        pdf_path = Path(pdf_path)
        calls.append((pdf_path.name, model))
        if pdf_path.name.startswith("bad"):
            raise RuntimeError("OCR backend returned non-JSON")
        data = dict(OCR_DATA[pdf_path.name])
        data["_file_hash"] = hashlib.md5(pdf_path.read_bytes()).hexdigest()
        data["_raw_response"] = json.dumps(data)
        return data

    monkeypatch.setattr(invoice_ingest, "extract_invoice", extract)
    return calls


@pytest.fixture
def fake_ecb(monkeypatch):
    """Mock Frankfurter: USD rates on two Q1 2025 dates, whatever range is asked."""
    rates = {"2025-02-10": {"USD": 1.0}, "2025-03-31": {"USD": 1.08}}
    monkeypatch.setattr(fx_rates, "fetch_rates_range", lambda start, end, currencies=None: rates)
    return rates


def _payment(pid: str, when: str, amount: float, **kw) -> Payment:
    return Payment(id=pid, created_date=when, converted_amount=amount, converted_amount_refunded=0.0,
                   description=kw.pop("description", "Subscription creation"), fee=0.0,
                   currency=kw.pop("currency", "eur"), **kw)


def _stripe_fetch(ctx: CloseContext, payments: list[Payment]):
    """A fake of the API fetch: persist the charges and their classification."""
    def fetch():
        upsert_payments(payments, db_path=ctx.db_path)
        upsert_classified([classify_payment(p, ctx.rules) for p in payments], db_path=ctx.db_path)
    return fetch


PAYMENTS = [
    _payment("ch_1", "2025-01-15T10:00:00", 100.0),
    _payment("ch_2", "2025-02-20T12:00:00", 50.0),
]


def _rows(ctx: CloseContext) -> dict[str, dict]:
    return {r["filename"]: r for r in get_invoices(db_path=ctx.db_path)}


def _assert_noop(result: StepResult) -> None:
    assert result.changes == [], f"{result.step} was not a no-op: {result.changes}"


# ---------------------------------------------------------------------------
# 1. sweep
# ---------------------------------------------------------------------------

def test_sweep_copies_new_files_once(ctx):
    _pdf(ctx, "in", "Example Cloud/inv-001.pdf")
    _pdf(ctx, "out", "out-001.pdf")
    upsert_invoice({"filename": "known.pdf", "direction": "in"}, db_path=ctx.db_path)
    _pdf(ctx, "in", "known.pdf")  # already catalogued in the DB → never copied

    first = cp.step_sweep(ctx)
    assert first.changes == [f"copied IN  {Path('Example Cloud', 'inv-001.pdf')}", "copied OUT out-001.pdf"]
    assert sorted(p.name for p in ctx.quarter_dir.iterdir()) == [
        "IN - Example Cloud - inv-001.pdf", "OUT - out-001.pdf"]
    _assert_noop(cp.step_sweep(ctx))

    _pdf(ctx, "in", "inv-002.pdf")
    assert cp.step_sweep(ctx).changes == ["copied IN  inv-002.pdf"]


# ---------------------------------------------------------------------------
# 2. ocr
# ---------------------------------------------------------------------------

def test_ocr_extracts_pending_files_once(ctx, fake_ocr):
    _pdf(ctx, "in", "inv-001.pdf")
    _pdf(ctx, "out", "out-001.pdf")

    first = cp.step_ocr(ctx, model="some_model")
    assert len(first.changes) == 2 and not first.errors
    assert fake_ocr == [("inv-001.pdf", "some_model"), ("out-001.pdf", "some_model")]
    rows = _rows(ctx)
    assert rows["inv-001.pdf"]["total_eur"] == 100.0
    assert rows["out-001.pdf"]["direction"] == "out"

    _assert_noop(cp.step_ocr(ctx))
    assert len(fake_ocr) == 2  # nothing re-extracted

    _pdf(ctx, "in", "inv-001.pdf", content="changed")  # new bytes → new hash → re-extracted
    again = cp.step_ocr(ctx)
    assert len(again.changes) == 1 and "inv-001.pdf" in again.changes[0]


def test_ocr_continues_past_a_failing_file(ctx, fake_ocr):
    _pdf(ctx, "in", "bad-scan.pdf")
    _pdf(ctx, "in", "inv-002.pdf")

    res = cp.step_ocr(ctx, directions=("in",))
    assert len(res.errors) == 1 and "bad-scan.pdf" in res.errors[0]
    assert len(res.changes) == 1 and "inv-002.pdf" in res.changes[0]
    assert set(_rows(ctx)) == {"inv-002.pdf"}
    # The failing file stays pending and is retried.
    retry = cp.step_ocr(ctx, directions=("in",))
    assert retry.changes == [] and len(retry.errors) == 1


def test_ocr_dry_run_writes_nothing(ctx, fake_ocr):
    _pdf(ctx, "in", "inv-001.pdf")
    res = cp.step_ocr(ctx, dry_run=True)
    _assert_noop(res)
    assert fake_ocr == [] and _rows(ctx) == {}
    assert any("inv-001.pdf" in line for line in res.info)


# ---------------------------------------------------------------------------
# 3. vendors
# ---------------------------------------------------------------------------

def test_vendors_applies_registry_and_lists_unknown(ctx, isolated_vendor_registry):
    isolated_vendor_registry.write_text(json.dumps({"vendors": [{
        "key": "example cloud", "aliases": ["Example Cloud Ltd"], "country": "IE",
        "vat_id": "IE0000000XX", "default_tax_treatment": "INTRA_EU_RC",
        "default_deductible_pct_vat": 100, "default_deductible_pct_irpf": 100,
    }]}), encoding="utf-8")
    upsert_invoice({"filename": "Example Cloud/inv-001.pdf", "direction": "in", "invoice_date": "2025-02-10",
                    "vendor_name": "Example Cloud Ltd", "vendor_nif": "IE0000000XX"}, db_path=ctx.db_path)
    upsert_invoice({"filename": "misc/receipt.pdf", "direction": "in", "invoice_date": "2025-03-01",
                    "vendor_name": "Nobody Known SL"}, db_path=ctx.db_path)
    upsert_invoice({"filename": "misc/old.pdf", "direction": "in", "invoice_date": "2024-11-01",
                    "vendor_name": "Old Unknown SL"}, db_path=ctx.db_path)

    first = cp.step_vendors(ctx)
    assert len(first.changes) == 1 and "supply_country" in first.changes[0]
    assert _rows(ctx)["Example Cloud/inv-001.pdf"]["supply_country"] == "IE"
    assert len(first.warnings) == 1 and "misc/receipt.pdf" in first.warnings[0]  # only this quarter's
    assert any("1 unknown-vendor invoice(s) outside" in line for line in first.info)

    second = cp.step_vendors(ctx)
    _assert_noop(second)
    assert second.warnings == first.warnings  # the review list is repeated, not a change


# ---------------------------------------------------------------------------
# 4. dedupe
# ---------------------------------------------------------------------------

def test_dedupe_proposes_then_applies_once(ctx):
    for name in ("a.pdf", "b.pdf"):
        upsert_invoice({"filename": name, "direction": "in", "file_hash": "SAMEHASH", "invoice_number": name,
                        "invoice_date": "2025-02-10", "vendor_name": "Sample Supplies SL",
                        "total_eur": 10.0}, db_path=ctx.db_path)
    # A swept file dated outside the quarter → out-of-period.
    upsert_invoice({"filename": "late.pdf", "direction": "in", "invoice_date": "2024-12-20",
                    "vendor_name": "Other SL", "total_eur": 5.0}, db_path=ctx.db_path)
    (ctx.quarter_dir / "IN - late.pdf").write_bytes(b"%PDF")

    review = cp.step_dedupe(ctx)
    _assert_noop(review)
    assert "2 exclusion(s) proposed" in review.warnings[0]
    assert not any(r["excluded"] for r in _rows(ctx).values())

    applied = cp.step_dedupe(ctx, apply=True)
    assert applied.changes == ["excluded 2 invoice(s): file_hash×1, out_of_period×1"]
    rows = _rows(ctx)
    assert rows["late.pdf"]["excluded_reason"] == "other_period"
    assert sum(r["excluded"] for r in rows.values()) == 2

    _assert_noop(cp.step_dedupe(ctx, apply=True))


# ---------------------------------------------------------------------------
# 5. fx
# ---------------------------------------------------------------------------

def test_fx_backfills_and_recomputes_only_with_apply(ctx, fake_ecb):
    upsert_invoice({"filename": "usd.pdf", "direction": "in", "invoice_date": "2025-02-10",
                    "currency": "USD", "original_currency": "USD", "original_amount": 100.0,
                    "subtotal_eur": 90.0, "iva_amount": 0.0, "total_eur": 90.0}, db_path=ctx.db_path)

    dry = cp.step_fx(ctx)
    assert dry.changes == ["stored 2 new ECB rate entries (latest 2025-03-31)"]
    assert "1 invoice(s) would change" in dry.warnings[0]
    assert _rows(ctx)["usd.pdf"]["total_eur"] == 90.0

    applied = cp.step_fx(ctx, apply=True)
    assert len(applied.changes) == 1 and "usd.pdf" in applied.changes[0]
    assert _rows(ctx)["usd.pdf"]["total_eur"] == 100.0  # 100 USD at 1.0

    again = cp.step_fx(ctx, apply=True)
    _assert_noop(again)
    assert again.warnings == []


def test_fx_warns_when_the_backfill_falls_short(ctx, monkeypatch):
    monkeypatch.setattr(fx_rates, "fetch_rates_range", lambda *a, **k: {"2025-01-02": {"USD": 1.03}})
    res = cp.step_fx(ctx)
    assert any("did not reach" in w for w in res.warnings)


# ---------------------------------------------------------------------------
# 6. stripe
# ---------------------------------------------------------------------------

def test_stripe_fetch_is_reported_once(ctx):
    first = cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, PAYMENTS))
    assert first.changes == ["stored 2 new transaction(s)"]
    assert any("2 transaction(s)" in line for line in first.info)

    _assert_noop(cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, PAYMENTS)))


def test_stripe_reports_a_changed_amount_and_foreign_warning(ctx):
    cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, PAYMENTS))
    refunded = PAYMENTS[1].model_copy(update={"converted_amount_refunded": 50.0})
    foreign = _payment("ch_3", "2025-03-01T09:00:00", 30.0, description="", card_country="DE")
    res = cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, [PAYMENTS[0], refunded, foreign]))
    assert "stored 1 new transaction(s)" in res.changes
    assert any(c.startswith("updated ch_2: refunded 0.0 → 50.0") for c in res.changes)
    assert any("ch_3" in w and "eur_default" in w for w in res.warnings)


# ---------------------------------------------------------------------------
# 7. reta
# ---------------------------------------------------------------------------

def _bank_export(tmp_path: Path) -> Path:
    path = tmp_path / "bank_export.csv"
    path.write_text("Fecha,Importe\n31/01/2025,\"-100,00\"\n"
                    "28/02/2025,\"-100,00\"\n31/03/2025,\"-100,00\"\n", encoding="utf-8")
    return path


def test_reta_imports_once(ctx, tmp_path):
    export = _bank_export(tmp_path)
    first = cp.step_reta(ctx, export)
    assert first.changes == ["imported 3 RETA payment(s) from bank_export.csv"]
    assert any("300.00 EUR (3 payment(s))" in line for line in first.info)
    _assert_noop(cp.step_reta(ctx, export))


# ---------------------------------------------------------------------------
# 8. compute
# ---------------------------------------------------------------------------

def test_compute_persists_only_when_figures_change(ctx):
    first = cp.step_compute(ctx)
    assert len(first.changes) == 1 and "303" in first.changes[0]
    assert any(line.startswith("Modelo 303:") for line in first.info)
    _assert_noop(cp.step_compute(ctx))

    cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, PAYMENTS))
    again = cp.step_compute(ctx)
    assert len(again.changes) == 1 and "303" in again.changes[0]


# ---------------------------------------------------------------------------
# 9. reconcile
# ---------------------------------------------------------------------------

def _file_303(ctx: CloseContext, year: int, period: str, boxes: dict[str, float]) -> None:
    conn = sqlite3.connect(str(ctx.db_path))
    try:
        store_filed_return(conn, FiledReturn(
            model="303", year=year, period=period, justificante=f"J303{year}{period}",
            source_file="synthetic.pdf", presented_at=f"{year}-04-18T10:00:00", boxes=boxes, operators=[],
        ))
    finally:
        conn.close()


def test_reconcile_uses_the_previous_quarter_until_this_one_is_filed(ctx):
    res = cp.step_reconcile(ctx)
    assert "reconciling the previous quarter 2024 Q4" in res.info[0]
    assert (ctx.quarter_dir / "reconciliation_2024_Q4.md").exists()
    assert any("no filed Modelo 303 for 2024 Q4" in w for w in res.warnings)
    _assert_noop(cp.step_reconcile(ctx))

    _file_303(ctx, YEAR, "1T", {"07": 999.0})
    filed = cp.step_reconcile(ctx)
    assert "has a filed return" in filed.info[0]
    assert (ctx.quarter_dir / "reconciliation_2025_Q1.md").exists()
    assert any("box 07" in w and "uncatalogued" in w for w in filed.warnings)
    assert "## Modelo 303 — 2025 Q1 — filed vs app" in filed.output


# ---------------------------------------------------------------------------
# 10. sheet
# ---------------------------------------------------------------------------

def test_sheet_renders_the_stored_snapshots(ctx):
    before = cp.step_sheet(ctx)
    assert any("run `compute` first" in w for w in before.warnings)

    cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, PAYMENTS))
    cp.step_compute(ctx)
    res = cp.step_sheet(ctx)
    assert res.warnings == []
    assert "## Modelo 303" in res.output and "| 07 |" in res.output
    assert (ctx.quarter_dir / "filing_sheet_2025_Q1.md").read_text(encoding="utf-8") == res.output
    _assert_noop(cp.step_sheet(ctx))


def test_sheet_renderer_is_pluggable(ctx, monkeypatch):
    monkeypatch.setattr(cp, "filing_sheet_renderer", lambda y, q, conn, cfg: f"custom {y} Q{q}\n")
    assert cp.step_sheet(ctx).output == "custom 2025 Q1\n"


# ---------------------------------------------------------------------------
# 11. gestor-pack
# ---------------------------------------------------------------------------

def test_gestor_pack_draft_then_freeze(ctx):
    cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, PAYMENTS))
    upsert_invoice({"filename": "shared/device.pdf", "direction": "in", "invoice_date": "2025-02-01",
                    "vendor_name": "Gadget Store SL", "total_eur": 121.0, "tax_treatment": "DOMESTIC",
                    "deductible_pct_vat": 20.0, "deductible_pct_irpf": 20.0}, db_path=ctx.db_path)
    _pdf(ctx, "in", "shared/device.pdf")

    draft = cp.step_gestor_pack(ctx)
    report = ctx.quarter_dir / "Stripe_Report_Q1_2025.xlsx"
    assert report.exists()
    assert any("NOT frozen" in w for w in draft.warnings)
    assert any("gestor_notes.md" in w for w in draft.warnings)  # template in use
    notes = (ctx.quarter_dir / "gestor_notes_2025_Q1.md").read_text(encoding="utf-8")
    assert "VAT business use 20%" in notes and "shared/device.pdf" in notes
    email = (ctx.quarter_dir / "gestor_email_2025_Q1.txt").read_text(encoding="utf-8")
    assert email.startswith("DRAFT") and "1 factura(s) recibida(s)" in email
    _assert_noop(cp.step_gestor_pack(ctx))

    ctx.notes_path.write_text("- The shared device is 20% business use.\n", encoding="utf-8")
    with_notes = cp.step_gestor_pack(ctx)
    assert any("gestor_notes_2025_Q1.md" in c for c in with_notes.changes)
    assert "The shared device is 20% business use." in (
        ctx.quarter_dir / "gestor_notes_2025_Q1.md").read_text(encoding="utf-8")

    frozen = cp.step_gestor_pack(ctx, freeze=True)
    assert any("declared report v1" in c for c in frozen.changes)
    conn = sqlite3.connect(str(ctx.db_path))
    try:
        assert get_declared_report(conn, YEAR, QUARTER).version == 1
    finally:
        conn.close()
    _assert_noop(cp.step_gestor_pack(ctx, freeze=True))  # already declared: never re-frozen


def test_gestor_pack_invoices_come_from_the_quarter_ledger(ctx):
    """#157: invoices OCR'd before ``sweep`` ran still reach the pack; the email counts the pack."""
    # Stored like src.invoice_ingest stores it: the path relative to the invoice dir, native separators.
    def ingest(direction: str, rel: str, invoice_date: str, **kw) -> None:
        _pdf(ctx, direction, rel)
        upsert_invoice({"filename": str(Path(rel)), "direction": direction, "invoice_date": invoice_date,
                        "total_eur": 10.0, **kw}, db_path=ctx.db_path)

    ingest("in", "Example Cloud/early.pdf", "2025-01-10")      # ingested mid-quarter
    ingest("in", "Sample Supplies/late.pdf", "2025-03-30")
    ingest("out", "F-1.pdf", "2025-02-14")
    ingest("in", "dup.pdf", "2025-02-02", excluded=1, excluded_reason="duplicate")
    ingest("in", "previous.pdf", "2024-12-20")                  # other quarter
    ingest("out", "next.pdf", "2025-04-01")                     # other quarter
    ingest("in", "gone.pdf", "2025-02-03")
    (_dir(ctx, "in") / "gone.pdf").unlink()                     # PDF missing on disk

    _assert_noop(cp.step_sweep(ctx))  # every PDF is already catalogued: sweep copies nothing

    res = cp.step_gestor_pack(ctx)
    email = (ctx.quarter_dir / "gestor_email_2025_Q1.txt").read_text(encoding="utf-8")
    assert "2 factura(s) recibida(s) y 1 factura(s) emitida(s)" in email
    pack = ctx.quarter_dir / "invoices"
    assert sorted(p.name for p in pack.iterdir()) == [
        "IN - Example Cloud - early.pdf", "IN - Sample Supplies - late.pdf", "OUT - F-1.pdf"]
    assert any("gone.pdf" in w for w in res.warnings)
    _assert_noop(cp.step_gestor_pack(ctx))

    # A row excluded after the first pack leaves it on the next run, and the counts follow.
    conn = sqlite3.connect(str(ctx.db_path))
    try:
        conn.execute("UPDATE invoices SET excluded = 1 WHERE filename = ?", (str(Path("Sample Supplies/late.pdf")),))
        conn.commit()
    finally:
        conn.close()
    res = cp.step_gestor_pack(ctx)
    assert any("Sample Supplies - late.pdf" in c for c in res.changes)
    assert sorted(p.name for p in pack.iterdir()) == ["IN - Example Cloud - early.pdf", "OUT - F-1.pdf"]
    email = (ctx.quarter_dir / "gestor_email_2025_Q1.txt").read_text(encoding="utf-8")
    assert "1 factura(s) recibida(s) y 1 factura(s) emitida(s)" in email


def test_freeze_sent_report_file(ctx, tmp_path):
    import openpyxl

    from src.exceptions import ReportAlreadyFrozenError

    cp.step_stripe(ctx, fetch=_stripe_fetch(ctx, PAYMENTS))
    sent = tmp_path / "Stripe_Report_Q1_2025.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "import"
    ws.append(["id", "Created Date", "Converted Amount", "Converted Amount Refunded", "Fee", "Currency"])
    ws.append(["ch_1", "2025-01-15 10:00:00", 99.0, 0.0, 0.0, "EUR"])     # live: 100.0
    ws.append(["ch_old", "2025-03-01 10:00:00", 20.0, 0.0, 0.0, "EUR"])   # not in the live table
    wb.save(sent)

    res = cp.freeze_sent_stripe_report(ctx, sent)
    assert any("declared report v1: 2 transactions, net 119.00 EUR" in c for c in res.changes)
    assert any("not in the live transactions table" in w and "ch_old" in w for w in res.warnings)
    assert any("not in the file" in w and "ch_2" in w for w in res.warnings)
    assert any(i.startswith("1 live row(s) had a different EUR amount") for i in res.info)
    with pytest.raises(ReportAlreadyFrozenError):
        cp.freeze_sent_stripe_report(ctx, sent)
    assert cp.freeze_sent_stripe_report(ctx, sent, supersede=True).changes[0].startswith(
        "froze Stripe_Report_Q1_2025.xlsx as declared report v2")


# ---------------------------------------------------------------------------
# all
# ---------------------------------------------------------------------------

def test_all_runs_every_step_and_a_rerun_is_a_noop(ctx, tmp_path, fake_ocr, fake_ecb):
    _pdf(ctx, "in", "inv-001.pdf")
    _pdf(ctx, "out", "out-001.pdf")
    export = _bank_export(tmp_path)
    fetch = _stripe_fetch(ctx, PAYMENTS)
    seen: list[str] = []

    first = cp.run_all(ctx, apply=True, freeze=True, reta_file=export, stripe_fetch=fetch,
                       on_step=lambda n, r: seen.append(r.step))
    assert [r.step for r in first] == list(cp.STEP_ORDER) == seen
    assert not any(r.errors for r in first), [r.errors for r in first]
    changed = {r.step for r in first if r.changes}
    assert {"sweep", "ocr", "fx", "stripe", "reta", "compute", "reconcile", "sheet", "gestor-pack"} <= changed

    second = cp.run_all(ctx, apply=True, freeze=True, reta_file=export, stripe_fetch=fetch)
    assert {r.step: r.changes for r in second} == {s: [] for s in cp.STEP_ORDER}


def test_all_stops_at_a_failing_step(ctx):
    def broken_fetch():
        raise ConnectionError("Stripe unreachable")

    results = cp.run_all(ctx, stripe_fetch=broken_fetch)
    assert results[-1].step == "stripe" and "Stripe unreachable" in results[-1].errors[0]
    assert len(results) == cp.STEP_ORDER.index("stripe") + 1


# ---------------------------------------------------------------------------
# CLI + skill documentation
# ---------------------------------------------------------------------------

def _cli_subcommands() -> dict:
    """Subcommand name → its argparse sub-parser, from the real CLI module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("close_quarter_cli", ROOT / "scripts" / "close_quarter.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parser = module.build_parser()
    return next(a for a in parser._actions if a.dest == "command").choices


SKILL = ROOT / ".claude" / "skills" / "close-quarter" / "SKILL.md"


def test_cli_exposes_every_pipeline_step():
    assert set(cp.STEP_ORDER) | {"all"} <= set(_cli_subcommands())


def test_skill_documents_every_subcommand():
    skill = SKILL.read_text(encoding="utf-8")
    missing = [c for c in sorted(_cli_subcommands()) if not re.search(rf"`{re.escape(c)}[` ]", skill)]
    assert not missing, f"SKILL.md does not mention: {missing}"


def test_skill_only_uses_flags_the_script_accepts():
    commands = _cli_subcommands()
    unknown = []
    for cmd, rest in re.findall(r"`([a-z][\w-]*)( [^`]*)?`", SKILL.read_text(encoding="utf-8")):
        if cmd not in commands:
            continue
        accepted = {opt for action in commands[cmd]._actions for opt in action.option_strings}
        unknown += [f"{cmd} {flag}" for flag in re.findall(r"--[\w-]+", rest) if flag not in accepted]
    assert not unknown, f"SKILL.md uses flags the script does not accept: {unknown}"


def test_context_rejects_a_bad_quarter():
    with pytest.raises(ValueError):
        CloseContext(2025, 5, config={})


def test_context_period_bounds():
    start, end = CloseContext(2024, 4, config={}).datetime_bounds
    assert (start.isoformat(), end.isoformat()) == ("2024-10-01T00:00:00", "2024-12-31T23:59:59")
    assert CloseContext(2024, 1, config={}).previous_period() == (2023, 4)
