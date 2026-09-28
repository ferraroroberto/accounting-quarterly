"""Invoice dedupe (#92): detectors, keeper rule, locked-row respect, and the
engine picking up automated exclusions. All data is synthetic."""
from __future__ import annotations

import sqlite3

import pytest

from src.database import (
    get_connection,
    get_invoices,
    init_db,
    set_invoice_exclusion,
    update_invoice_fields,
    upsert_invoice,
)
from src.invoice_dedupe import (
    apply_groups,
    detect_email_copies,
    detect_hash_duplicates,
    detect_number_duplicates,
    detect_out_of_period,
    detect_receipt_pairs,
    find_duplicate_groups,
    quarter_bounds,
)
from src.tax_engine import compute_modelo_130, compute_modelo_303

ES_CIF = "B00000000"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "dedupe.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _inv(**overrides) -> dict:
    rec = {
        "filename": "vendor/inv-001.pdf",
        "direction": "in",
        "invoice_number": "INV-001",
        "invoice_date": "2025-02-10",
        "vendor_name": "Acme Vendor SL",
        "vendor_nif": ES_CIF,
        "subtotal_eur": 100.0,
        "iva_rate": 21.0,
        "iva_amount": 21.0,
        "total_eur": 121.0,
        "currency": "EUR",
    }
    rec.update(overrides)
    return rec


def _add(db_path, **overrides) -> str:
    return upsert_invoice(_inv(**overrides), db_path=db_path)


def _rows(db_path) -> list[dict]:
    return get_invoices(db_path=db_path)


def _by_filename(rows: list[dict], filename: str) -> dict:
    return next(r for r in rows if r["filename"] == filename)


# ---------------------------------------------------------------------------
# Detector 1: same file_hash
# ---------------------------------------------------------------------------

class TestHashDuplicates:
    def test_two_files_same_hash_flagged(self, db):
        _add(db, filename="a.pdf", file_hash="H1", invoice_number="A")
        _add(db, filename="b.pdf", file_hash="H1", invoice_number="B")
        groups = detect_hash_duplicates(_rows(db))
        assert len(groups) == 1
        g = groups[0]
        assert g.detector == "file_hash" and g.reason == "duplicate"
        assert len(g.loser_ids) == 1

    def test_no_hash_no_group(self, db):
        _add(db, filename="a.pdf", file_hash=None)
        _add(db, filename="b.pdf", file_hash=None)
        assert detect_hash_duplicates(_rows(db)) == []

    def test_distinct_hashes_no_group(self, db):
        _add(db, filename="a.pdf", file_hash="H1")
        _add(db, filename="b.pdf", file_hash="H2")
        assert detect_hash_duplicates(_rows(db)) == []


# ---------------------------------------------------------------------------
# Detector 2: same (vendor, invoice_number)
# ---------------------------------------------------------------------------

class TestNumberDuplicates:
    def test_same_vendor_and_number_flagged(self, db):
        _add(db, filename="a.pdf", invoice_number="INV-9", file_hash="H1")
        _add(db, filename="b.pdf", invoice_number="inv-9", file_hash="H2")  # case-insensitive
        groups = detect_number_duplicates(_rows(db))
        assert len(groups) == 1 and len(groups[0].loser_ids) == 1

    def test_different_vendor_same_number_not_flagged(self, db):
        _add(db, filename="a.pdf", invoice_number="INV-9", vendor_nif=ES_CIF, file_hash="H1")
        _add(db, filename="b.pdf", invoice_number="INV-9", vendor_nif="B99999999", file_hash="H2")
        assert detect_number_duplicates(_rows(db)) == []

    def test_falls_back_to_vendor_name_when_no_nif(self, db):
        _add(db, filename="a.pdf", invoice_number="INV-9", vendor_nif=None,
             vendor_name="No Nif Vendor", file_hash="H1")
        _add(db, filename="b.pdf", invoice_number="INV-9", vendor_nif=None,
             vendor_name="no nif vendor", file_hash="H2")
        groups = detect_number_duplicates(_rows(db))
        assert len(groups) == 1


# ---------------------------------------------------------------------------
# Detector 3: invoice/receipt pair — the invoice always wins
# ---------------------------------------------------------------------------

class TestReceiptPairs:
    def test_receipt_loses_to_invoice(self, db):
        _add(db, filename="factura/inv.pdf", invoice_type="factura_completa",
             invoice_date="2025-03-10", total_eur=50.0, file_hash="H1")
        _add(db, filename="recibo/pay.pdf", invoice_type="recibo",
             invoice_date="2025-03-12", total_eur=50.0, file_hash="H2")
        groups = detect_receipt_pairs(_rows(db))
        assert len(groups) == 1
        g = groups[0]
        rows = _rows(db)
        keeper = next(r for r in rows if r["id"] == g.keeper_id)
        loser = next(r for r in rows if r["id"] == g.loser_ids[0])
        assert keeper["filename"] == "factura/inv.pdf"
        assert loser["filename"] == "recibo/pay.pdf"
        assert g.reason == "receipt"

    def test_dates_more_than_3_days_apart_not_paired(self, db):
        _add(db, filename="inv.pdf", invoice_type="factura_completa",
             invoice_date="2025-03-10", total_eur=50.0, file_hash="H1")
        _add(db, filename="recibo.pdf", invoice_type="recibo",
             invoice_date="2025-03-20", total_eur=50.0, file_hash="H2")
        assert detect_receipt_pairs(_rows(db)) == []

    def test_different_totals_not_paired(self, db):
        _add(db, filename="inv.pdf", invoice_type="factura_completa",
             invoice_date="2025-03-10", total_eur=50.0, file_hash="H1")
        _add(db, filename="recibo.pdf", invoice_type="recibo",
             invoice_date="2025-03-11", total_eur=75.0, file_hash="H2")
        assert detect_receipt_pairs(_rows(db)) == []

    def test_two_receipts_not_paired(self, db):
        _add(db, filename="r1.pdf", invoice_type="recibo",
             invoice_date="2025-03-10", total_eur=50.0, file_hash="H1")
        _add(db, filename="r2.pdf", invoice_type="recibo",
             invoice_date="2025-03-11", total_eur=50.0, file_hash="H2")
        assert detect_receipt_pairs(_rows(db)) == []

    def test_receipt_detected_by_filename_keyword(self, db):
        _add(db, filename="factura.pdf", invoice_date="2025-03-10", total_eur=50.0, file_hash="H1")
        _add(db, filename="vendor/Receipt-2025.pdf", invoice_type=None,
             invoice_date="2025-03-11", total_eur=50.0, file_hash="H2")
        groups = detect_receipt_pairs(_rows(db))
        assert len(groups) == 1
        loser = _by_filename(_rows(db), "vendor/Receipt-2025.pdf")
        assert groups[0].loser_ids == (loser["id"],)


# ---------------------------------------------------------------------------
# Detector 4: email-folder copies
# ---------------------------------------------------------------------------

class TestEmailCopies:
    def test_email_copy_matched_by_invoice_number(self, db):
        _add(db, filename="vendor/inv-1.pdf", invoice_number="X1", file_hash="H1")
        _add(db, filename="vendor/email/inv-1.pdf", invoice_number="X1", file_hash="H2")
        groups = detect_email_copies(_rows(db))
        assert len(groups) == 1
        rows = _rows(db)
        keeper = next(r for r in rows if r["id"] == groups[0].keeper_id)
        assert keeper["filename"] == "vendor/inv-1.pdf"
        assert groups[0].reason == "duplicate"

    def test_email_copy_matched_by_amount_and_date_when_no_number(self, db):
        _add(db, filename="vendor/inv-1.pdf", invoice_number=None,
             invoice_date="2025-04-01", total_eur=30.0, file_hash="H1")
        _add(db, filename="vendor/email/inv-1.pdf", invoice_number=None,
             invoice_date="2025-04-01", total_eur=30.0, file_hash="H2")
        groups = detect_email_copies(_rows(db))
        assert len(groups) == 1

    def test_windows_style_email_path_detected(self, db):
        _add(db, filename="vendor/inv-1.pdf", invoice_number="X1", file_hash="H1")
        _add(db, filename="vendor\\email\\inv-1.pdf", invoice_number="X1", file_hash="H2")
        groups = detect_email_copies(_rows(db))
        assert len(groups) == 1

    def test_no_main_folder_match_no_group(self, db):
        _add(db, filename="vendor/email/inv-1.pdf", invoice_number="X1", file_hash="H1")
        assert detect_email_copies(_rows(db)) == []


# ---------------------------------------------------------------------------
# Detector 5: out-of-period
# ---------------------------------------------------------------------------

class TestOutOfPeriod:
    def test_quarter_bounds(self):
        assert quarter_bounds(2026, 2) == ("2026-04-01", "2026-06-30")
        assert quarter_bounds(2025, 4) == ("2025-10-01", "2025-12-31")

    def test_row_outside_quarter_flagged(self, db):
        _add(db, filename="a.pdf", invoice_date="2025-07-15", file_hash="H1")  # Q3, sweeping Q2
        groups = detect_out_of_period(_rows(db), 2025, 2)
        assert len(groups) == 1
        assert groups[0].reason == "other_period"
        assert groups[0].keeper_id is None

    def test_row_inside_quarter_not_flagged(self, db):
        _add(db, filename="a.pdf", invoice_date="2025-05-15", file_hash="H1")
        assert detect_out_of_period(_rows(db), 2025, 2) == []


# ---------------------------------------------------------------------------
# Keeper rule and cross-detector claiming
# ---------------------------------------------------------------------------

class TestKeeperRule:
    def test_invoice_beats_receipt_even_via_the_number_detector(self, db):
        # Anthropic-style case: the invoice and its receipt share the same
        # invoice_number, so detector 2 (not the strict receipt-pair detector 3)
        # claims the group first. The receipt must still never be kept.
        receipt = _add(db, filename="vendor/Receipt-0018.pdf", invoice_type="recibo",
                       invoice_number="N1", file_hash="H-receipt")
        invoice = _add(db, filename="vendor/Invoice-0018.pdf", invoice_type="factura_completa",
                       invoice_number="N1", file_hash="H-invoice")
        groups = detect_number_duplicates(_rows(db))
        assert len(groups) == 1
        assert groups[0].keeper_id == invoice
        assert groups[0].loser_ids == (receipt,)

    def test_non_email_path_preferred_over_email_even_if_ingested_later(self, db):
        # Insert the email copy first (earlier extracted_at) — non-email path must still win.
        _add(db, filename="vendor/email/dup.pdf", file_hash="SAMEHASH", invoice_number="N1")
        _add(db, filename="vendor/dup.pdf", file_hash="SAMEHASH", invoice_number="N2")
        groups = detect_hash_duplicates(_rows(db))
        keeper = next(r for r in _rows(db) if r["id"] == groups[0].keeper_id)
        assert keeper["filename"] == "vendor/dup.pdf"

    def test_earliest_ingested_wins_among_non_email_ties(self, db):
        first = _add(db, filename="a.pdf", file_hash="SAMEHASH", invoice_number="N1")
        second = _add(db, filename="b.pdf", file_hash="SAMEHASH", invoice_number="N2")
        # Force a deterministic extracted_at ordering — both inserts can land in the
        # same wall-clock second, which would otherwise make the tie-break flaky.
        c = get_connection(db)
        c.execute("UPDATE invoices SET extracted_at = '2025-01-01T00:00:00' WHERE id = ?", (first,))
        c.execute("UPDATE invoices SET extracted_at = '2025-01-01T00:00:01' WHERE id = ?", (second,))
        c.commit()
        c.close()
        groups = detect_hash_duplicates(_rows(db))
        assert groups[0].keeper_id == first

    def test_find_duplicate_groups_never_double_claims_a_row(self, db):
        # a and b share a file_hash *and* the same (vendor, invoice_number) —
        # only the file_hash detector (priority 1) should claim the loser.
        _add(db, filename="a.pdf", file_hash="SAMEHASH", invoice_number="N1")
        _add(db, filename="b.pdf", file_hash="SAMEHASH", invoice_number="N1")
        groups = find_duplicate_groups(_rows(db))
        all_losers = [lid for g in groups for lid in g.loser_ids]
        assert len(all_losers) == len(set(all_losers))
        assert [g.detector for g in groups] == ["file_hash"]


# ---------------------------------------------------------------------------
# Locked rows are never auto-excluded
# ---------------------------------------------------------------------------

class TestLockedRowsRespected:
    def test_locked_excluded_field_excluded_from_grouping(self, db):
        a = _add(db, filename="a.pdf", file_hash="SAMEHASH", invoice_number="N1")
        _add(db, filename="b.pdf", file_hash="SAMEHASH", invoice_number="N2")
        # User reviewed row a and explicitly confirmed it is not a duplicate: toggling
        # `excluded` True then False (both real changes) leaves it locked at False.
        update_invoice_fields(a, {"excluded": True}, db_path=db)
        update_invoice_fields(a, {"excluded": False}, db_path=db)
        rec = next(r for r in _rows(db) if r["id"] == a)
        assert rec["excluded"] == 0 and "excluded" in (rec["locked_fields"] or "")
        groups = detect_hash_duplicates(_rows(db))
        assert groups == []  # only one eligible row left in the bucket

    def test_apply_groups_skips_locked_row(self, db):
        a = _add(db, filename="a.pdf", file_hash="SAMEHASH", invoice_number="N1")
        b = _add(db, filename="b.pdf", file_hash="SAMEHASH", invoice_number="N2")
        c = _add(db, filename="c.pdf", file_hash="SAMEHASH", invoice_number="N3")
        # Lock b's excluded field directly (bypassing the keeper-eligibility filter)
        # to prove apply_groups itself refuses to touch a locked row.
        update_invoice_fields(b, {"excluded": True}, db_path=db)
        update_invoice_fields(b, {"excluded": False}, db_path=db)
        from src.invoice_dedupe import DuplicateGroup
        group = DuplicateGroup(detector="file_hash", reason="duplicate",
                               loser_ids=(b, c), keeper_id=a)
        result = apply_groups([group], db_path=db)
        assert result == {"applied": 1, "skipped_locked": 1, "by_detector": {"file_hash": 1}}
        rows = {r["id"]: r for r in _rows(db)}
        assert rows[b]["excluded"] == 0  # untouched
        assert rows[c]["excluded"] == 1 and rows[c]["excluded_reason"] == "duplicate"

    def test_set_invoice_exclusion_does_not_lock(self, db):
        a = _add(db, filename="a.pdf", file_hash="H1")
        assert set_invoice_exclusion(a, True, "duplicate", db_path=db) is True
        rows = {r["id"]: r for r in _rows(db)}
        assert rows[a]["excluded"] == 1
        assert rows[a]["locked_fields"] is None  # not locked — re-extract may still change it


# ---------------------------------------------------------------------------
# apply_groups end to end + engine picks up the automated exclusion
# ---------------------------------------------------------------------------

class TestApplyAndEngine:
    def test_apply_groups_end_to_end(self, db):
        _add(db, filename="a.pdf", file_hash="SAMEHASH", invoice_number="N1")
        _add(db, filename="b.pdf", file_hash="SAMEHASH", invoice_number="N2")
        groups = find_duplicate_groups(_rows(db))
        result = apply_groups(groups, db_path=db)
        assert result["applied"] == 1 and result["skipped_locked"] == 0
        excluded = [r for r in _rows(db) if r["excluded"]]
        assert len(excluded) == 1
        assert excluded[0]["excluded_reason"] == "duplicate"

    def test_engine_ignores_automated_exclusion(self, db, conn):
        _add(db, filename="a.pdf", file_hash="SAMEHASH", invoice_number="N1",
             invoice_date="2025-01-05", subtotal_eur=100.0, iva_amount=21.0)
        dup_id = _add(db, filename="b.pdf", file_hash="SAMEHASH", invoice_number="N2",
                      invoice_date="2025-01-06", subtotal_eur=100.0, iva_amount=21.0)
        assert compute_modelo_303(2025, 1, conn).box_29_cuota_soportado == pytest.approx(42.0)
        assert compute_modelo_130(2025, 1, conn).box_02_gastos == pytest.approx(200.0)

        set_invoice_exclusion(dup_id, True, "duplicate", db_path=db)
        assert compute_modelo_303(2025, 1, conn).box_29_cuota_soportado == pytest.approx(21.0)
        assert compute_modelo_130(2025, 1, conn).box_02_gastos == pytest.approx(100.0)
