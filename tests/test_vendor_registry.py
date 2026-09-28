"""Vendor registry (#91): matching, applying defaults, locks, unknown vendors, seed import.

Synthetic vendors and ids only (IE0000000XX style) — never real registry data.
"""
from __future__ import annotations

import json
from pathlib import Path

import openpyxl
import pytest

from src.database import (
    get_invoice_by_filename,
    init_db,
    parse_locked_fields,
    update_invoice_fields,
    upsert_invoice,
)
from src.vendor_registry import (
    ACTIVITY_IAE,
    ACTIVITIES,
    Vendor,
    VendorRegistry,
    apply_vendor_registry,
    find_unmatched_invoices,
    invoice_folder,
    load_registry,
    normalize_vendor_name,
    save_registry,
    seed_from_xlsx,
    upsert_invoice_with_registry,
)

ROOT = Path(__file__).parent.parent


@pytest.fixture
def registry() -> VendorRegistry:
    return VendorRegistry.from_dict({"vendors": [
        {"key": "example cloud", "aliases": ["ExampleCloud"], "legal_entity": "Example Cloud Ireland Limited",
         "country": "IE", "vat_id": "IE0000000XX", "default_tax_treatment": "INTRA_EU_RC",
         "default_deductible_pct_vat": 100, "default_deductible_pct_irpf": 100, "activity": "newsletter"},
        {"key": "sample ai", "legal_entity": "Sample AI, Inc.", "country": "US",
         "default_tax_treatment": "NON_EU_RC", "activity": "ILLUSTRATIONS"},
        {"key": "power utility", "country": "ES", "vat_id": "A00000000",
         "default_tax_treatment": "DOMESTIC", "default_deductible_pct_vat": 5,
         "default_deductible_pct_irpf": 5, "activity": "COACHING"},
        {"key": "gadget store", "country": "LU", "vat_id": "LU00000000", "alt_vat_ids": ["ESW0000000J"],
         "default_tax_treatment": "DOMESTIC", "asset_class": "IT"},
    ]})


@pytest.fixture
def db(tmp_path) -> Path:
    path = tmp_path / "registry.db"
    init_db(path)
    return path


def _invoice(filename: str, **fields) -> dict:
    return {"filename": filename, "direction": "in", "invoice_date": "2025-02-10",
            "subtotal_eur": 100.0, "total_eur": 100.0, **fields}


# ── matching ──────────────────────────────────────────────────────────────────

def test_normalize_and_folder_helpers():
    assert normalize_vendor_name("Éxample-Cloud, S.L.") == "example cloud s l"
    assert invoice_folder("example cloud\\archive\\2025-01.pdf") == "example cloud"
    assert invoice_folder("sample ai/2025-01.pdf") == "sample ai"
    assert invoice_folder("loose.pdf") is None


def test_folder_is_the_first_signal(registry):
    # The folder names the vendor even when the document was billed by someone else.
    m = registry.match("Example Cloud/2025-01.pdf", vendor_nif="LU00000000", vendor_name="Sample AI, Inc.")
    assert (m.vendor.key, m.signal) == ("example cloud", "folder")
    assert registry.match("examplecloud/x.pdf").vendor.key == "example cloud"  # alias as folder


def test_vat_id_then_name(registry):
    m = registry.match("unsorted/x.pdf", vendor_nif="ie-0000000xx", vendor_name="Someone else")
    assert (m.vendor.key, m.signal) == ("example cloud", "vat_id")
    assert registry.match(vendor_nif="W0000000J").vendor.key == "gadget store"  # ES-prefixed alt id
    m = registry.match("loose.pdf", vendor_nif=None, vendor_name="SAMPLE AI INC")
    assert (m.vendor.key, m.signal) == ("sample ai", "name")


def test_name_match_is_whole_token(registry):
    assert registry.match(vendor_name="Resample AIX Corp") is None
    assert registry.match(vendor_name="Unknown Vendor") is None


def test_validation_rejects_bad_entries():
    with pytest.raises(ValueError, match="default_tax_treatment"):
        Vendor.from_dict({"key": "x co", "default_tax_treatment": "ES_21"})  # income-side value
    with pytest.raises(ValueError, match="between 0 and 100"):
        Vendor.from_dict({"key": "x co", "default_deductible_pct_irpf": 120})
    with pytest.raises(ValueError, match="activity"):
        Vendor.from_dict({"key": "x co", "activity": "gardening"})
    with pytest.raises(ValueError, match="Duplicate"):
        VendorRegistry.from_dict({"vendors": [{"key": "X Co"}, {"key": "x co"}]})


def test_activity_iae_mapping_covers_every_activity():
    assert set(ACTIVITY_IAE) == set(ACTIVITIES)


def test_example_file_loads_and_is_synthetic():
    reg = load_registry(ROOT / "vendors.json.example")
    assert len(reg.vendors) >= 4
    treatments = {v.default_tax_treatment for v in reg.vendors}
    assert {"INTRA_EU_RC", "NON_EU_RC", "DOMESTIC", "NO_VAT"} <= treatments
    for v in reg.vendors:
        for vat in (v.vat_id, *v.alt_vat_ids):
            assert vat is None or "0000000" in vat, f"example VAT id {vat!r} must be fake"


def test_save_and_reload_roundtrip(tmp_path, registry):
    path = save_registry(registry, tmp_path / "vendors.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [v["key"] for v in data["vendors"]] == sorted(v.key for v in registry.vendors)
    assert load_registry(path).to_dict() == VendorRegistry(
        sorted(registry.vendors, key=lambda v: v.key)).to_dict()


def test_missing_registry_file_is_empty(tmp_path):
    assert load_registry(tmp_path / "nope.json").vendors == []


# ── applying ─────────────────────────────────────────────────────────────────

def test_new_ocr_result_gets_registry_defaults(db, registry):
    """Acceptance: a new OCR result for a registered vendor gets treatment, % and activity."""
    upsert_invoice_with_registry(
        _invoice("power utility/2025-02.pdf", vendor_name="Power Utility SA", iva_amount=21.0,
                 category="UTILITIES"),
        registry=registry, db_path=db,
    )
    row = get_invoice_by_filename("power utility/2025-02.pdf", "in", db_path=db)
    assert row["tax_treatment"] == "DOMESTIC"
    assert row["vat_treatment"] == "IVA_ES_21"
    assert row["deductible_pct_vat"] == 5.0
    assert row["deductible_pct_irpf"] == 5.0
    assert row["activity_type"] == "COACHING"
    assert row["vendor_vat_id_norm"] == "ESA00000000"
    assert row["geo_region"] == "SPAIN"
    assert row["supply_country"] == "ES"
    assert parse_locked_fields(row["locked_fields"]) == []  # registry writes don't lock
    assert row["reviewed_at"] is None


def test_registry_overrides_heuristic_treatment(db, registry):
    # No VAT id, no VAT charged: the heuristic says NO_VAT / UNKNOWN; the registry knows better.
    upsert_invoice_with_registry(_invoice("sample ai/2025-02.pdf", vendor_name="Sample AI, Inc."),
                                 registry=registry, db_path=db)
    row = get_invoice_by_filename("sample ai/2025-02.pdf", "in", db_path=db)
    assert row["tax_treatment"] == "NON_EU_RC"
    assert row["geo_region"] == "OUTSIDE_EU"
    assert row["activity_type"] == "ILLUSTRATIONS"
    assert row["deductible_pct_vat"] == 100.0  # registry left it empty → untouched


def test_document_vat_id_wins_over_registry(db, registry):
    upsert_invoice_with_registry(
        _invoice("example cloud/2025-02.pdf", vendor_nif="IE 9999999ZZ"), registry=registry, db_path=db,
    )
    row = get_invoice_by_filename("example cloud/2025-02.pdf", "in", db_path=db)
    assert row["vendor_vat_id_norm"] == "IE9999999ZZ"
    assert row["tax_treatment"] == "INTRA_EU_RC"


def test_locked_fields_survive_apply_and_reextract(db, registry):
    rid = upsert_invoice_with_registry(_invoice("power utility/2025-03.pdf", iva_amount=21.0),
                                       registry=registry, db_path=db)
    update_invoice_fields(rid, {"deductible_pct_irpf": 30.0, "tax_treatment": "NOT_DEDUCTIBLE"}, db_path=db)

    result = apply_vendor_registry(registry, db_path=db)
    assert result.matched == 1
    row = get_invoice_by_filename("power utility/2025-03.pdf", "in", db_path=db)
    assert row["deductible_pct_irpf"] == 30.0
    assert row["tax_treatment"] == "NOT_DEDUCTIBLE"
    assert row["deductible_pct_vat"] == 5.0  # unlocked → still the registry's

    upsert_invoice_with_registry(_invoice("power utility/2025-03.pdf", iva_amount=21.0),
                                 registry=registry, db_path=db)  # re-OCR
    row = get_invoice_by_filename("power utility/2025-03.pdf", "in", db_path=db)
    assert (row["deductible_pct_irpf"], row["tax_treatment"]) == (30.0, "NOT_DEDUCTIBLE")
    assert sorted(parse_locked_fields(row["locked_fields"])) == ["deductible_pct_irpf", "tax_treatment"]


def test_apply_over_stored_rows_is_idempotent_and_counts(db, registry):
    upsert_invoice(_invoice("example cloud/a.pdf"), db_path=db)
    upsert_invoice(_invoice("unsorted/b.pdf", vendor_nif="IE0000000XX"), db_path=db)
    upsert_invoice(_invoice("mystery/c.pdf", vendor_name="Mystery Vendor Ltd"), db_path=db)
    upsert_invoice({**_invoice("out/d.pdf"), "direction": "out"}, db_path=db)  # income: ignored

    first = apply_vendor_registry(registry, db_path=db)
    assert (first.scanned, first.matched, first.unmatched) == (3, 2, 1)
    assert first.by_signal == {"folder": 1, "vat_id": 1}
    assert first.rows_updated == 2
    second = apply_vendor_registry(registry, db_path=db)
    assert second.rows_updated == 0


def test_unmatched_invoices_are_listed_with_a_suggested_key(db, registry):
    upsert_invoice(_invoice("mystery/c.pdf", vendor_name="Mystery Vendor Ltd"), db_path=db)
    upsert_invoice(_invoice("loose.pdf", vendor_name="Other Thing SL"), db_path=db)
    upsert_invoice(_invoice("example cloud/a.pdf"), db_path=db)
    rid = upsert_invoice(_invoice("mystery/dup.pdf"), db_path=db)
    update_invoice_fields(rid, {"excluded": True, "excluded_reason": "duplicate"}, db_path=db)

    unmatched = find_unmatched_invoices(registry, db_path=db)
    assert [(r["filename"], r["suggested_key"]) for r in unmatched] == [
        ("mystery/c.pdf", "mystery"), ("loose.pdf", "other thing sl"),
    ]
    assert len(find_unmatched_invoices(registry, db_path=db, include_excluded=True)) == 3


# ── spreadsheet seed / import ────────────────────────────────────────────────

def _write_xlsx(path: Path, rows: list[tuple]) -> Path:
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    wb.save(path)
    return path


def test_seed_from_xlsx_adds_and_merges(tmp_path, registry):
    xlsx = _write_xlsx(tmp_path / "items.xlsx", [
        ("item", "access", "recurrent", "recurrency", "business"),
        ("New Tool", "https://example.invalid", "yes", "monthly", "illustration"),
        ("ExampleCloud", None, "yes", "annual", "coaching"),   # existing via alias: activity kept
        ("gadget store", None, "no", "variable", "newsletter"),  # existing, empty activity → filled
        (None, None, None, None, None),
        ("odd one", None, "no", "canceled", "gardening"),
    ])
    reg, res = seed_from_xlsx(xlsx, registry)
    assert res.added == ["new tool", "odd one"]
    assert sorted(res.updated) == ["example cloud", "gadget store"]
    assert reg.lookup("new tool").activity == "ILLUSTRATIONS"
    assert reg.lookup("New-Tool").key == "new tool"
    assert reg.lookup("odd one").activity is None
    assert reg.lookup("example cloud").activity == "NEWSLETTER"   # hand-set value kept
    assert reg.lookup("example cloud").recurrence == "annual"     # empty → filled
    assert reg.lookup("gadget store").activity == "NEWSLETTER"

    again, res2 = seed_from_xlsx(xlsx, reg)
    assert res2.added == [] and res2.updated == []
    assert len(again.vendors) == len(reg.vendors)


def test_seed_requires_a_vendor_column(tmp_path):
    xlsx = _write_xlsx(tmp_path / "bad.xlsx", [("foo", "bar"), ("a", "b")])
    with pytest.raises(ValueError, match="vendor column"):
        seed_from_xlsx(xlsx)
