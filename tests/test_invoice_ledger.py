"""Invoice ledger (#90): schema migration, tax_treatment backfill, edit locks,
excluded rows and invoice-date quarter keying. All data is synthetic."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.database import (
    backfill_invoice_ledger_fields,
    get_connection,
    get_invoice_by_filename,
    init_db,
    parse_locked_fields,
    unlock_invoice_fields,
    update_invoice_fields,
    upsert_invoice,
)
from src.tax_codes import (
    EXCLUDED_REASONS,
    TAX_TREATMENTS_IN,
    TAX_TREATMENTS_OUT,
    derive_tax_treatment_for_invoice,
    normalize_vat_id,
)
from src.tax_engine import compute_modelo_130, compute_modelo_303, compute_modelo_347

# Synthetic, structurally valid ids — not real taxpayers.
ES_CIF = "B00000000"
EU_VAT = "IE0000000XX"
US_EIN = "00-0000000"

_LEDGER_COLUMNS = {
    "tax_treatment", "deductible_pct_vat", "deductible_pct_irpf", "is_capital_asset",
    "asset_class", "excluded", "excluded_reason", "eur_received", "payment_date",
    "vendor_vat_id_norm", "locked_fields", "reviewed_at",
}


def _columns(db_path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
    finally:
        conn.close()


def _make_legacy_db(db_path) -> None:
    """An `invoices` table as it existed before #90, with legacy-classified rows."""
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE invoices (
            id TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            direction TEXT NOT NULL CHECK(direction IN ('in', 'out')),
            invoice_number TEXT, invoice_date TEXT,
            vendor_name TEXT, vendor_nif TEXT, vendor_address TEXT,
            client_name TEXT, client_nif TEXT, client_address TEXT,
            description TEXT, subtotal_eur REAL, iva_rate REAL, iva_amount REAL,
            irpf_rate REAL, irpf_amount REAL, total_eur REAL,
            currency TEXT DEFAULT 'EUR', original_currency TEXT, original_amount REAL,
            fx_rate REAL, payment_method TEXT, category TEXT, notes TEXT, raw_json TEXT,
            file_hash TEXT, supply_date TEXT, deductible_pct REAL DEFAULT 100,
            geo_region TEXT, vat_treatment TEXT,
            extracted_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(filename, direction)
        );
    """)
    rows = [
        # id, direction, vendor_nif, client_nif, iva, geo, vat_treatment, deductible_pct
        ("a", "in", ES_CIF, None, 21.0, "SPAIN", "IVA_ES_21", 50.0),
        ("b", "in", EU_VAT, None, None, "EU_NOT_SPAIN", "IVA_EU_B2B", 100.0),
        ("c", "in", US_EIN, None, None, "OUTSIDE_EU", "IVA_EXEMPT", 100.0),
        ("d", "in", None, None, None, "UNKNOWN", "IVA_EXEMPT", None),
        ("e", "out", None, ES_CIF, 21.0, "SPAIN", "IVA_ES_21", 100.0),
        ("f", "out", None, EU_VAT, None, "EU_NOT_SPAIN", "IVA_EU_B2B", 100.0),
        ("g", "out", None, US_EIN, None, "OUTSIDE_EU", "IVA_EXPORT", 100.0),
        ("h", "out", None, ES_CIF, None, "SPAIN", "IVA_EXEMPT", 100.0),
        ("i", "out", None, None, None, "UNKNOWN", "IVA_EXEMPT", 100.0),
    ]
    for rid, direction, vnif, cnif, iva, geo, vat, ded in rows:
        conn.execute(
            """INSERT INTO invoices (id, filename, direction, invoice_date, vendor_nif,
                   client_nif, subtotal_eur, iva_amount, geo_region, vat_treatment, deductible_pct)
               VALUES (?, ?, ?, '2025-02-10', ?, ?, 100.0, ?, ?, ?, ?)""",
            (rid, f"{rid}.pdf", direction, vnif, cnif, iva, geo, vat, ded),
        )
    conn.commit()
    conn.close()


def _row(db_path, rid: str) -> dict:
    conn = get_connection(db_path)
    try:
        return dict(conn.execute("SELECT * FROM invoices WHERE id = ?", (rid,)).fetchone())
    finally:
        conn.close()


def _ocr_record(**overrides) -> dict:
    """Shape of the record `src/invoice_ingest.extract_and_save` passes to upsert_invoice."""
    rec = {
        "filename": "vendor/inv-001.pdf",
        "direction": "in",
        "file_hash": "hash-1",
        "invoice_number": "INV-001",
        "invoice_date": "2025-02-10",
        "vendor_name": "Example Vendor SL",
        "vendor_nif": ES_CIF,
        "description": "Software subscription",
        "subtotal_eur": 100.0,
        "iva_rate": 21.0,
        "iva_amount": 21.0,
        "total_eur": 121.0,
        "currency": "EUR",
        "supply_date": None,
        "deductible_pct": None,
    }
    rec.update(overrides)
    return rec


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "ledger.db"
    init_db(path)
    return path


@pytest.fixture
def conn(db):
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Migration + backfill
# ---------------------------------------------------------------------------

class TestMigration:
    def test_legacy_db_gains_ledger_columns_and_is_idempotent(self, tmp_path):
        path = tmp_path / "legacy.db"
        _make_legacy_db(path)
        assert not (_LEDGER_COLUMNS & _columns(path))

        init_db(path)
        assert _LEDGER_COLUMNS <= _columns(path)
        first = {rid: _row(path, rid) for rid in "abcdefghi"}

        init_db(path)  # second startup: no error, nothing changes
        assert {rid: _row(path, rid) for rid in "abcdefghi"} == first
        conn = get_connection(path)
        try:
            counts = backfill_invoice_ledger_fields(conn)
        finally:
            conn.close()
        assert counts == {"deductible_pct_vat": 0, "deductible_pct_irpf": 0,
                          "vendor_vat_id_norm": 0, "tax_treatment": 0}

    def test_backfill_maps_legacy_vat_treatment(self, tmp_path):
        path = tmp_path / "legacy.db"
        _make_legacy_db(path)
        init_db(path)
        expected = {
            "a": "DOMESTIC", "b": "INTRA_EU_RC", "c": "NON_EU_RC", "d": "NO_VAT",
            "e": "ES_21", "f": "EU_B2B", "g": "NON_EU_NOT_SUBJECT",
            "h": "EXEMPT_TEACHING", "i": None,
        }
        assert {rid: _row(path, rid)["tax_treatment"] for rid in expected} == expected
        # The legacy column stays readable and untouched.
        assert _row(path, "b")["vat_treatment"] == "IVA_EU_B2B"

    def test_backfill_splits_deductible_pct_and_normalises_vat_id(self, tmp_path):
        path = tmp_path / "legacy.db"
        _make_legacy_db(path)
        init_db(path)
        a, d = _row(path, "a"), _row(path, "d")
        assert (a["deductible_pct_vat"], a["deductible_pct_irpf"]) == (50.0, 50.0)
        assert (d["deductible_pct_vat"], d["deductible_pct_irpf"]) == (100.0, 100.0)
        assert a["vendor_vat_id_norm"] == "ES" + ES_CIF
        assert a["excluded"] == 0 and a["is_capital_asset"] == 0
        assert a["locked_fields"] is None

    def test_backfill_never_overwrites_set_values(self, tmp_path):
        path = tmp_path / "legacy.db"
        _make_legacy_db(path)
        init_db(path)
        update_invoice_fields("a", {"tax_treatment": "NOT_DEDUCTIBLE", "deductible_pct_irpf": 20},
                              db_path=path)
        init_db(path)
        a = _row(path, "a")
        assert a["tax_treatment"] == "NOT_DEDUCTIBLE"
        assert a["deductible_pct_irpf"] == 20.0


class TestDerivations:
    @pytest.mark.parametrize("direction,legacy,geo,iva,expected", [
        ("in", "IVA_ES_21", "EU_NOT_SPAIN", 21.0, "DOMESTIC"),  # foreign vendor charging ES VAT
        ("in", "IVA_EU_B2B", "EU_NOT_SPAIN", None, "INTRA_EU_RC"),
        ("in", "IVA_EXEMPT", "OUTSIDE_EU", None, "NON_EU_RC"),
        ("in", "IVA_EXEMPT", "SPAIN", None, "NO_VAT"),
        ("in", None, "UNKNOWN", None, "NO_VAT"),
        ("in", None, "UNKNOWN", 5.0, "DOMESTIC"),  # legacy derived first from IVA presence
        ("out", "OSS_EU", "EU_NOT_SPAIN", 21.0, "EU_B2C_ES21"),
        ("out", "IVA_EXPORT", "OUTSIDE_EU", None, "NON_EU_NOT_SUBJECT"),
        ("out", "IVA_EXEMPT", "SPAIN", None, "EXEMPT_TEACHING"),
        ("out", "IVA_EXEMPT", "UNKNOWN", None, None),
    ])
    def test_tax_treatment_mapping(self, direction, legacy, geo, iva, expected):
        assert derive_tax_treatment_for_invoice(direction, legacy, geo, iva) == expected

    def test_mapped_values_belong_to_their_direction(self):
        for legacy in ("IVA_ES_21", "IVA_EU_B2B", "IVA_EXPORT", "IVA_EXEMPT", "OSS_EU"):
            for geo in ("SPAIN", "EU_NOT_SPAIN", "OUTSIDE_EU", "UNKNOWN"):
                assert derive_tax_treatment_for_invoice("in", legacy, geo) in TAX_TREATMENTS_IN
                out = derive_tax_treatment_for_invoice("out", legacy, geo)
                assert out is None or out in TAX_TREATMENTS_OUT

    @pytest.mark.parametrize("raw,expected", [
        ("b-0000000.0", "ESB00000000"),
        ("es b00000000", "ESB00000000"),
        ("IE 0000000XX", "IE0000000XX"),
        ("00-0000000", "000000000"),
        ("  ", None),
        (None, None),
    ])
    def test_normalize_vat_id(self, raw, expected):
        assert normalize_vat_id(raw) == expected

    def test_parse_locked_fields_tolerates_bad_values(self):
        assert parse_locked_fields(None) == []
        assert parse_locked_fields("not json") == []
        assert parse_locked_fields('{"a": 1}') == []
        assert parse_locked_fields('["subtotal_eur"]') == ["subtotal_eur"]


# ---------------------------------------------------------------------------
# Edits and locks
# ---------------------------------------------------------------------------

class TestLocks:
    def test_upsert_derives_ledger_fields(self, db):
        upsert_invoice(_ocr_record(deductible_pct=40.0), db_path=db)
        rec = get_invoice_by_filename("vendor/inv-001.pdf", "in", db_path=db)
        assert rec["tax_treatment"] == "DOMESTIC"
        assert rec["deductible_pct_vat"] == 40.0 and rec["deductible_pct_irpf"] == 40.0
        assert rec["vendor_vat_id_norm"] == "ES" + ES_CIF
        assert rec["excluded"] == 0 and rec["locked_fields"] is None

    def test_edited_fields_survive_re_extract(self, db):
        rid = upsert_invoice(_ocr_record(), db_path=db)
        changed = update_invoice_fields(rid, {
            "subtotal_eur": 50.0,
            "tax_treatment": "INTRA_EU_RC",
            "deductible_pct_vat": 20,
            "invoice_date": "2025-02-10",  # unchanged → not locked
        }, db_path=db)
        assert sorted(changed) == ["deductible_pct_vat", "subtotal_eur", "tax_treatment"]

        # Re-OCR returns different values for everything.
        rid2 = upsert_invoice(_ocr_record(
            subtotal_eur=999.0, iva_amount=1.0, invoice_date="2025-02-11",
            deductible_pct=100.0, file_hash="hash-2",
        ), db_path=db)
        rec = get_invoice_by_filename("vendor/inv-001.pdf", "in", db_path=db)
        assert rid2 == rid
        assert rec["subtotal_eur"] == 50.0
        assert rec["tax_treatment"] == "INTRA_EU_RC"
        assert rec["vat_treatment"] == "IVA_EU_B2B"  # legacy kept in sync
        assert rec["deductible_pct_vat"] == 20.0
        # Unlocked fields follow the new extraction.
        assert rec["iva_amount"] == 1.0
        assert rec["invoice_date"] == "2025-02-11"
        assert rec["deductible_pct_irpf"] == 100.0
        assert rec["file_hash"] == "hash-2"
        assert json.loads(rec["locked_fields"]) == ["deductible_pct_vat", "subtotal_eur", "tax_treatment"]
        assert rec["reviewed_at"] is not None

    def test_ledger_only_fields_survive_re_extract_without_lock(self, db, conn):
        rid = upsert_invoice(_ocr_record(), db_path=db)
        # e.g. a dedupe detector (#92) writing directly, without a user lock
        conn.execute(
            "UPDATE invoices SET excluded = 1, excluded_reason = 'duplicate', eur_received = 90 WHERE id = ?",
            (rid,),
        )
        conn.commit()
        upsert_invoice(_ocr_record(file_hash="hash-2"), db_path=db)
        rec = get_invoice_by_filename("vendor/inv-001.pdf", "in", db_path=db)
        assert (rec["excluded"], rec["excluded_reason"], rec["eur_received"]) == (1, "duplicate", 90.0)

    def test_unlock_lets_re_extract_overwrite(self, db):
        rid = upsert_invoice(_ocr_record(), db_path=db)
        update_invoice_fields(rid, {"subtotal_eur": 50.0, "vendor_name": "Fixed"}, db_path=db)
        assert unlock_invoice_fields(rid, ["subtotal_eur"], db_path=db) == ["vendor_name"]
        upsert_invoice(_ocr_record(subtotal_eur=80.0, vendor_name="OCR"), db_path=db)
        rec = get_invoice_by_filename("vendor/inv-001.pdf", "in", db_path=db)
        assert rec["subtotal_eur"] == 80.0 and rec["vendor_name"] == "Fixed"
        assert unlock_invoice_fields(rid, db_path=db) == []

    def test_vendor_nif_edit_renormalises(self, db):
        rid = upsert_invoice(_ocr_record(vendor_nif=None), db_path=db)
        update_invoice_fields(rid, {"vendor_nif": "ie 0000000xx"}, db_path=db)
        assert _row(db, rid)["vendor_vat_id_norm"] == "IE0000000XX"

    @pytest.mark.parametrize("changes", [
        {"tax_treatment": "ES_21"},             # out-value on an expense
        {"excluded_reason": "because"},
        {"deductible_pct_vat": 150},
        {"invoice_date": "10/02/2025"},
        {"raw_json": "{}"},                     # not user-editable
    ])
    def test_invalid_edits_are_rejected(self, db, changes):
        rid = upsert_invoice(_ocr_record(), db_path=db)
        with pytest.raises(ValueError):
            update_invoice_fields(rid, changes, db_path=db)
        assert _row(db, rid)["locked_fields"] is None

    def test_unknown_invoice_raises(self, db):
        with pytest.raises(KeyError):
            update_invoice_fields("missing", {"notes": "x"}, db_path=db)

    def test_exclusion_edit(self, db):
        rid = upsert_invoice(_ocr_record(), db_path=db)
        assert "receipt" in EXCLUDED_REASONS
        update_invoice_fields(rid, {"excluded": True, "excluded_reason": "receipt"}, db_path=db)
        rec = _row(db, rid)
        assert (rec["excluded"], rec["excluded_reason"]) == (1, "receipt")


# ---------------------------------------------------------------------------
# Tax engine: excluded rows, invoice-date keying, split percentages
# ---------------------------------------------------------------------------

class TestEngine:
    def test_excluded_duplicate_counted_once(self, db, conn):
        upsert_invoice(_ocr_record(filename="v/a.pdf"), db_path=db)
        dup = upsert_invoice(_ocr_record(filename="v/email/a.pdf"), db_path=db)
        assert compute_modelo_303(2025, 1, conn).c29_cuota == pytest.approx(42.0)

        update_invoice_fields(dup, {"excluded": 1, "excluded_reason": "duplicate"}, db_path=db)
        assert compute_modelo_303(2025, 1, conn).c29_cuota == pytest.approx(21.0)
        assert compute_modelo_130(2025, 1, conn).c02_gastos == pytest.approx(100.0)

    def test_excluded_income_ignored(self, db, conn):
        out = dict(direction="out", vendor_nif=None, client_nif=ES_CIF, vendor_name=None)
        upsert_invoice(_ocr_record(filename="o/1.pdf", **out), db_path=db)
        rid = upsert_invoice(_ocr_record(filename="o/2.pdf", **out), db_path=db)
        update_invoice_fields(rid, {"excluded": 1, "excluded_reason": "superseded"}, db_path=db)
        assert compute_modelo_303(2025, 1, conn).c07_base == pytest.approx(100.0)
        assert compute_modelo_130(2025, 1, conn).c01_ingresos == pytest.approx(100.0)

    def test_april_invoice_for_march_supply_counts_in_q2(self, db, conn):
        upsert_invoice(_ocr_record(
            filename="o/class.pdf", direction="out", vendor_nif=None, client_nif=ES_CIF,
            invoice_date="2025-04-04", supply_date="2025-03-16",
        ), db_path=db)
        assert compute_modelo_303(2025, 1, conn).c07_base == 0.0
        assert compute_modelo_303(2025, 2, conn).c07_base == pytest.approx(100.0)

    def test_expense_keyed_by_invoice_date(self, db, conn):
        upsert_invoice(_ocr_record(invoice_date="2025-04-01", supply_date="2025-03-31"), db_path=db)
        assert compute_modelo_303(2025, 1, conn).c29_cuota == 0.0
        assert compute_modelo_303(2025, 2, conn).c29_cuota == pytest.approx(21.0)

    def test_347_ignores_excluded_and_keys_by_invoice_date(self, db, conn):
        out = dict(direction="out", vendor_nif=None, client_nif=ES_CIF, client_name="Client SL",
                   subtotal_eur=4000.0, iva_amount=840.0)
        upsert_invoice(_ocr_record(filename="o/1.pdf", invoice_date="2025-01-10",
                                   supply_date="2024-12-20", **out), db_path=db)
        rid = upsert_invoice(_ocr_record(filename="o/2.pdf", **out), db_path=db)
        update_invoice_fields(rid, {"excluded": 1, "excluded_reason": "duplicate"}, db_path=db)
        rows = compute_modelo_347(2025, conn).rows
        assert len(rows) == 1
        assert rows[0].total_operations == pytest.approx(4840.0)
        assert compute_modelo_347(2024, conn).rows == []

    def test_vat_and_irpf_business_use_are_independent(self, db, conn):
        rid = upsert_invoice(_ocr_record(), db_path=db)
        update_invoice_fields(rid, {"deductible_pct_vat": 50, "deductible_pct_irpf": 20}, db_path=db)
        assert compute_modelo_303(2025, 1, conn).c29_cuota == pytest.approx(10.5)
        assert compute_modelo_130(2025, 1, conn).c02_gastos == pytest.approx(20.0)

    def test_zero_percent_is_not_treated_as_full(self, db, conn):
        rid = upsert_invoice(_ocr_record(), db_path=db)
        update_invoice_fields(rid, {"deductible_pct_vat": 0, "deductible_pct_irpf": 0}, db_path=db)
        assert compute_modelo_303(2025, 1, conn).c29_cuota == 0.0
        assert compute_modelo_130(2025, 1, conn).c02_gastos == 0.0
