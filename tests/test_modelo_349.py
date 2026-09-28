"""Modelo 349 keys I and S (#99). All operators, VAT ids and amounts are synthetic."""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.database import init_db
from src.tax_engine import compute_modelo_349
from src.tax_snapshot_codec import decode_snapshot, encode_snapshot


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "m349.db"
    init_db(path)
    c = sqlite3.connect(str(path))
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _purchase(conn, id, date, base, *, vat=None, vendor="Vendor", treatment="INTRA_EU_RC",
              excluded=0, filename=None):
    conn.execute(
        """INSERT INTO invoices (id, filename, direction, invoice_date, subtotal_eur, iva_amount,
               tax_treatment, vendor_name, vendor_nif, vendor_vat_id_norm, geo_region, excluded)
           VALUES (?, ?, 'in', ?, ?, 0, ?, ?, ?, ?, 'EU_NOT_SPAIN', ?)""",
        (id, filename or f"{id}.pdf", date, base, treatment, vendor, vat, vat, excluded),
    )
    conn.commit()


def _sale_invoice(conn, id, date, base, client_nif, client="Client", treatment="EU_B2B"):
    conn.execute(
        """INSERT INTO invoices (id, filename, direction, invoice_date, subtotal_eur, iva_amount,
               tax_treatment, client_name, client_nif, geo_region)
           VALUES (?, ?, 'out', ?, ?, 0, ?, ?, ?, 'EU_NOT_SPAIN')""",
        (id, f"{id}.pdf", date, base, treatment, client, client_nif),
    )
    conn.commit()


def _stripe_b2b(conn, id, date, amount, buyer_vat_id):
    conn.execute(
        """INSERT INTO transactions (id, created_date, converted_amount, converted_amount_refunded,
               description, fee, currency, activity_type, geo_region, vat_treatment,
               vat_base_eur, vat_amount_eur, buyer_vat_id, email_meta)
           VALUES (?, ?, ?, 0, 'synthetic', 0, 'eur', 'COACHING', 'EU_NOT_SPAIN', 'IVA_EU_B2B',
                   ?, 0, ?, 'buyer@example.test')""",
        (id, date, amount, amount, buyer_vat_id),
    )
    conn.commit()


def _ops(result):
    return {(o["country"] + o["vat_id"], o["key"]): o for o in result.operators()}


class TestKeys:
    def test_purchases_are_key_i_and_sales_key_s(self, conn):
        _purchase(conn, "p1", "2026-04-10", 30.00, vat="IE0000000XX", vendor="Cloud IE")
        _sale_invoice(conn, "s1", "2026-05-02", 200.00, "fr 00 111111111", client="Client FR")
        _stripe_b2b(conn, "t1", "2026-06-01T10:00:00", 50.00, "DE000000001")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        ops = _ops(r)
        assert set(ops) == {("IE0000000XX", "I"), ("FR00111111111", "S"), ("DE000000001", "S")}
        assert ops[("IE0000000XX", "I")] == {"country": "IE", "vat_id": "0000000XX",
                                             "name": "Cloud IE", "key": "I", "base": 30.0}
        assert r.aeat_boxes() == {"01": 3.0, "02": 280.0, "03": 0.0, "04": 0.0}

    def test_other_treatments_and_other_quarters_are_ignored(self, conn):
        _purchase(conn, "p1", "2026-04-10", 30.00, vat="IE0000000XX", treatment="DOMESTIC")
        _purchase(conn, "p2", "2026-04-11", 40.00, vat="US000000001", treatment="NON_EU_RC")
        _purchase(conn, "p3", "2026-03-31", 50.00, vat="IE0000000XX")   # Q1 invoice date
        _sale_invoice(conn, "s1", "2026-05-02", 200.00, "FR00111111111", treatment="ES_21")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert r.rows == [] and r.total == 0.0 and r.notes == ""


class TestGrouping:
    def test_two_invoices_from_the_same_operator_are_summed(self, conn):
        _purchase(conn, "p1", "2026-04-10", 30.00, vat="SE000000000001")
        _purchase(conn, "p2", "2026-05-10", 12.50, vat="SE 0000 0000 0001")  # vendor_nif as read
        conn.execute("UPDATE invoices SET vendor_vat_id_norm = NULL WHERE id = 'p2'")
        conn.commit()
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert len(r.rows) == 1
        row = r.rows[0]
        assert (row.vat_id, row.country, row.base, row.n_records) == ("SE000000000001", "SE", 42.5, 2)

    def test_same_vat_id_under_both_keys_is_two_lines(self, conn):
        _purchase(conn, "p1", "2026-04-10", 30.00, vat="IE0000000XX")
        _sale_invoice(conn, "s1", "2026-05-02", 70.00, "IE0000000XX")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert {(o["key"], o["base"]) for o in r.operators()} == {("I", 30.0), ("S", 70.0)}

    def test_excluded_invoices_are_ignored(self, conn):
        _purchase(conn, "p1", "2026-04-10", 30.00, vat="IE0000000XX")
        _purchase(conn, "p2", "2026-04-10", 30.00, vat="IE0000000XX", excluded=1)  # duplicate
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert r.rows[0].base == 30.0 and r.rows[0].n_records == 1


class TestNotDeclarable:
    def test_negative_total_is_excluded_with_a_warning(self, conn):
        _purchase(conn, "p1", "2026-04-10", 20.00, vat="IE0000000XX")
        _purchase(conn, "p2", "2026-05-10", -35.00, vat="IE0000000XX")   # credit note
        _purchase(conn, "p3", "2026-05-11", 10.00, vat="SE000000000001")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert [o["vat_id"] for o in r.operators()] == ["000000000001"]
        assert [(e.vat_id, e.base) for e in r.excluded] == [("IE0000000XX", -15.0)]
        assert r.aeat_boxes()["01"] == 1.0 and r.total == 10.0
        assert "IE0000000XX" in r.notes and "negative" in r.notes

    def test_zero_total_is_excluded(self, conn):
        _purchase(conn, "p1", "2026-04-10", 20.00, vat="IE0000000XX")
        _purchase(conn, "p2", "2026-05-10", -20.00, vat="IE0000000XX")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert r.rows == [] and len(r.excluded) == 1

    def test_missing_vat_id_is_warned_and_listed_separately(self, conn):
        _purchase(conn, "p1", "2026-04-10", 9.99, vat=None, vendor="Mystery Tool")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert r.rows == [] and r.total == 0.0
        assert [(u.name, u.base, u.key) for u in r.unidentified] == [("Mystery Tool", 9.99, "I")]
        assert "Mystery Tool" in r.notes and "no VAT id" in r.notes

    def test_non_eu_prefix_is_flagged_but_declared(self, conn):
        _sale_invoice(conn, "s1", "2026-05-02", 70.00, "B12345678")   # Spanish NIF -> ESB12345678
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert r.rows[0].vat_id == "ESB12345678"
        assert "EU country prefix" in r.notes


class TestVendorRegistry:
    def test_registry_supplies_vat_id_and_legal_name(self, conn, isolated_vendor_registry):
        isolated_vendor_registry.write_text(json.dumps({"vendors": [
            {"key": "cloudco", "legal_entity": "CloudCo Ireland Ltd", "country": "IE",
             "vat_id": "IE0000000XX", "default_tax_treatment": "INTRA_EU_RC"},
        ]}), encoding="utf-8")
        _purchase(conn, "p1", "2026-04-10", 30.00, vat=None, vendor="CloudCo invoice",
                  filename="cloudco/2026-04.pdf")
        _purchase(conn, "p2", "2026-05-10", 15.00, vat="IE0000000XX", vendor="cloudco")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        assert [(o["country"], o["vat_id"], o["name"], o["base"]) for o in r.operators()] == [
            ("IE", "0000000XX", "CloudCo Ireland Ltd", 45.0)]


class TestAuditAndSnapshot:
    def test_audit_has_one_cell_per_operator_and_the_summary_boxes(self, conn):
        _purchase(conn, "p1", "2026-04-10", 30.00, vat="IE0000000XX")
        _purchase(conn, "p2", "2026-04-11", -5.00, vat="SE000000000001")
        cells = {a.cell: a for a in compute_modelo_349(2026, 2, conn, {"tax": {}}).audit}
        assert set(cells) == {"op_I_IE0000000XX", "excluded_I_SE000000000001",
                              "c01_operadores", "c02_importe"}
        assert json.loads(cells["op_I_IE0000000XX"].inputs_json)["records"][0]["id"] == "p1"
        assert cells["c02_importe"].value == 30.0

    def test_snapshot_roundtrip(self, conn):
        _purchase(conn, "p1", "2026-04-10", 30.00, vat="IE0000000XX")
        _purchase(conn, "p2", "2026-04-11", 5.00, vat=None, vendor="No Id")
        r = compute_modelo_349(2026, 2, conn, {"tax": {}})
        back = decode_snapshot("349", encode_snapshot("349", r))
        assert back.rows == r.rows and back.unidentified == r.unidentified
        assert back.aeat_boxes() == r.aeat_boxes() and back.operators() == r.operators()

    def test_pre_99_snapshot_decodes_as_key_s(self):
        legacy = {"year": 2025, "quarter": 1, "total": 500.0, "notes": "",
                  "rows": [{"buyer_name": "b@example.test", "buyer_vat_id": "DE 000000001",
                            "total_amount": 500.0}]}
        r = decode_snapshot("349", json.dumps(legacy))
        assert r.operators() == [{"country": "DE", "vat_id": "000000001", "name": "b@example.test",
                                  "key": "S", "base": 500.0}]
        assert r.excluded == [] and r.unidentified == []
