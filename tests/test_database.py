"""Tests for the SQLite database layer."""
import json
import sqlite3
from datetime import datetime

import pytest

from src.database import (
    backfill_tax_snapshot_legacy_keys,
    get_connection,
    get_transaction_count_db,
    get_uploaded_files,
    init_db,
    load_classified_payments,
    load_tax_snapshots_for_period,
    record_upload,
    upsert_classified,
    upsert_payments,
)
from src.models import ClassifiedPayment, Payment
from src.tax_snapshot_codec import decode_snapshot


class TestDatabase:
    def test_init_db(self, tmp_db):
        init_db(tmp_db)
        assert tmp_db.exists()

    def test_upsert_and_load(self, tmp_db, sample_payments):
        init_db(tmp_db)
        inserted, updated = upsert_payments(sample_payments, db_path=tmp_db)
        assert inserted == 5
        assert updated == 0

        assert get_transaction_count_db(db_path=tmp_db) == 5

    def test_upsert_idempotent(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)
        inserted, updated = upsert_payments(sample_payments, db_path=tmp_db)
        assert inserted == 0
        assert updated == 0

    def test_upsert_detects_changes(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)

        modified = [sample_payments[0].model_copy(update={"fee": 999.99})]
        inserted, updated = upsert_payments(modified, db_path=tmp_db)
        assert inserted == 0
        assert updated == 1

    def test_transaction_count(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)
        assert get_transaction_count_db(db_path=tmp_db) == 5


class TestLoadClassifiedPayments:
    def test_returns_classified_payment_objects(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)
        classified = [
            ClassifiedPayment(
                **sample_payments[0].model_dump(),
                activity_type="COACHING",
                geo_region="SPAIN",
                classification_rule="coaching_keywords",
                geo_rule="eur_default",
            )
        ]
        upsert_classified(classified, db_path=tmp_db)

        results = load_classified_payments(db_path=tmp_db)
        assert len(results) == 5
        assert all(isinstance(r, ClassifiedPayment) for r in results)

        coaching = next(r for r in results if r.id == "ch_test_001")
        assert coaching.activity_type == "COACHING"
        assert coaching.geo_region == "SPAIN"
        assert coaching.classification_rule == "coaching_keywords"
        assert coaching.geo_rule == "eur_default"

    def test_null_columns_default_to_unknown(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)

        results = load_classified_payments(db_path=tmp_db)
        for r in results:
            assert r.activity_type == "UNKNOWN"
            assert r.geo_region == "UNKNOWN"
            assert r.classification_rule == ""
            assert r.geo_rule == ""

    def test_date_filter(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)

        start = datetime(2025, 2, 1)
        end = datetime(2025, 2, 28, 23, 59, 59)
        results = load_classified_payments(start, end, db_path=tmp_db)
        assert len(results) == 2  # ch_test_002 and ch_test_005

    def test_safe_on_older_schema(self, tmp_db, sample_payments):
        """load_classified_payments() should not fail on a DB missing new columns."""
        import sqlite3
        # Create a minimal DB without the classification columns
        tmp_db.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(tmp_db))
        conn.row_factory = sqlite3.Row
        conn.execute("""
            CREATE TABLE transactions (
                id TEXT PRIMARY KEY,
                created_date TEXT NOT NULL,
                converted_amount REAL NOT NULL,
                converted_amount_refunded REAL NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT '',
                fee REAL NOT NULL DEFAULT 0,
                currency TEXT NOT NULL DEFAULT 'eur',
                payment_type_meta TEXT,
                event_api_id_meta TEXT,
                email_meta TEXT
            )
        """)
        conn.execute(
            "INSERT INTO transactions (id, created_date, converted_amount, "
            "converted_amount_refunded, description, fee, currency) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("ch_old_001", "2025-01-01T00:00:00", 50.0, 0.0, "old row", 1.0, "eur"),
        )
        conn.commit()
        conn.close()

        # Should not raise even though columns are missing
        results = load_classified_payments(db_path=tmp_db)
        assert len(results) == 1
        assert results[0].activity_type == "UNKNOWN"
        assert results[0].geo_region == "UNKNOWN"


class TestUploadLog:
    def test_record_upload(self, tmp_db):
        init_db(tmp_db)
        assert record_upload("invoice_001.pdf", "in", db_path=tmp_db)
        assert not record_upload("invoice_001.pdf", "in", db_path=tmp_db)  # duplicate

    def test_get_uploaded_files(self, tmp_db):
        init_db(tmp_db)
        record_upload("a.pdf", "in", db_path=tmp_db)
        record_upload("b.pdf", "in", db_path=tmp_db)
        record_upload("c.pdf", "out", db_path=tmp_db)

        in_files = get_uploaded_files("in", db_path=tmp_db)
        assert len(in_files) == 2

        out_files = get_uploaded_files("out", db_path=tmp_db)
        assert len(out_files) == 1


def _legacy_modelo303_payload() -> str:
    """A Modelo 303 snapshot payload as it would have been written pre-e08a3ff9 (#42),
    i.e. before box_28_iva_soportado -> box_29_cuota_soportado and
    box_29_base_soportado -> box_28_base_soportado."""
    return json.dumps({
        "year": 2026, "quarter": 1,
        "box_01_base": 100.0, "box_03_cuota": 21.0,
        "box_59_intracom_entregas": 0.0,
        "box_28_iva_soportado": 15.0,
        "box_29_base_soportado": 60.0,
        "box_46_diferencia": 6.0, "box_48_resultado": 6.0,
        "oss_base": 0.0, "oss_vat": 0.0, "export_base": 0.0, "notes": "",
    })


class TestTaxSnapshotLegacyKeyMigration:
    """Covers accounting-quarterly#58: stale pre-rename Modelo 303 snapshot rows
    must not hard-crash the default Tax Obligations/Tax Validation view."""

    def test_backfill_rewrites_legacy_keys_in_place(self, tmp_db):
        init_db(tmp_db)
        conn = get_connection(tmp_db)
        conn.execute(
            """INSERT INTO tax_computation_snapshots (year, quarter, model, payload_json, computed_at)
               VALUES (2026, 1, '303', ?, '2026-01-01T00:00:00')""",
            (_legacy_modelo303_payload(),),
        )
        conn.commit()

        updated = backfill_tax_snapshot_legacy_keys(conn)
        assert updated == 1

        rows = load_tax_snapshots_for_period(2026, 1, conn)
        payload = next(r["payload_json"] for r in rows if r["model"] == "303")
        data = json.loads(payload)
        assert "box_28_iva_soportado" not in data
        assert "box_29_base_soportado" not in data
        assert data["box_29_cuota_soportado"] == pytest.approx(15.0)
        assert data["box_28_base_soportado"] == pytest.approx(60.0)
        conn.close()

    def test_init_db_migrates_stale_rows_on_startup(self, tmp_db):
        init_db(tmp_db)
        conn = get_connection(tmp_db)
        conn.execute(
            """INSERT INTO tax_computation_snapshots (year, quarter, model, payload_json, computed_at)
               VALUES (2026, 1, '303', ?, '2026-01-01T00:00:00')""",
            (_legacy_modelo303_payload(),),
        )
        conn.commit()
        conn.close()

        # Re-running init_db (as the app does on every startup) must self-heal
        # the stale row so the default view decodes without a TypeError.
        init_db(tmp_db)

        conn = get_connection(tmp_db)
        rows = load_tax_snapshots_for_period(2026, 1, conn)
        payload = next(r["payload_json"] for r in rows if r["model"] == "303")
        result = decode_snapshot("303", payload)
        assert result.c28_base == pytest.approx(60.0)
        assert result.c29_cuota == pytest.approx(15.0)
        conn.close()

    def test_decode_snapshot_tolerates_unmigrated_legacy_keys(self):
        """Even without the startup migration, decode_snapshot alone must not crash."""
        result = decode_snapshot("303", _legacy_modelo303_payload())
        assert result.c28_base == pytest.approx(60.0)
        assert result.c29_cuota == pytest.approx(15.0)


class TestUpsertPaymentsColumns:
    """``upsert_payments`` is driven by one column tuple: every column is written and change-checked."""

    BASE = dict(
        id="ch_cols", created_date=datetime(2025, 2, 3, 10, 0, 0), converted_amount=100.0,
        converted_amount_refunded=0.0, description="d", fee=3.0, fee_stripe=1.0, fee_application=2.0,
        currency="eur", payment_type_meta="p", event_api_id_meta="e", email_meta="m@example.com",
        card_country="ES", billing_country="ES", amount_original=100.0, fx_rate=1.0,
        stripe_customer_id="cus_1", stripe_payment_intent_id="pi_1", stripe_balance_transaction_id="txn_1",
        stripe_invoice_id="in_1", raw_source={"a": 1}, raw_source_type="stripe_api",
    )
    CHANGES = dict(
        converted_amount=101.0, converted_amount_refunded=5.0, description="d2", fee=3.5, fee_stripe=1.5,
        fee_application=2.5, currency="usd", payment_type_meta="p2", event_api_id_meta="e2", email_meta="n@example.com",
        card_country="FR", billing_country="FR", amount_original=120.0, fx_rate=1.2, stripe_customer_id="cus_2",
        stripe_payment_intent_id="pi_2", stripe_balance_transaction_id="txn_2", stripe_invoice_id="in_2",
        raw_source={"a": 2}, raw_source_type="other",
    )

    def test_every_column_round_trips_and_triggers_an_update(self, tmp_db):
        init_db(tmp_db)
        assert upsert_payments([Payment(**self.BASE)], db_path=tmp_db) == (1, 0)
        assert upsert_payments([Payment(**self.BASE)], db_path=tmp_db) == (0, 0)
        conn = get_connection(tmp_db)
        try:
            row = conn.execute("SELECT * FROM transactions WHERE id = 'ch_cols'").fetchone()
        finally:
            conn.close()
        for col, value in self.BASE.items():
            if col in ("id", "created_date", "raw_source"):
                continue
            assert row[col] == value, col
        assert json.loads(row["raw_source_json"]) == {"a": 1}

        for field, new in self.CHANGES.items():
            payment = Payment(**{**self.BASE, field: new})
            assert upsert_payments([payment], db_path=tmp_db) == (0, 1), field
            # restore, so the next field is checked against the same baseline
            assert upsert_payments([Payment(**self.BASE)], db_path=tmp_db) == (0, 1), field
