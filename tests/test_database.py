"""Tests for the SQLite database layer."""
import pytest
from datetime import datetime

from src.database import (
    get_latest_transaction_date,
    get_transaction_count_db,
    get_uploaded_files,
    init_db,
    load_classified_payments,
    load_payments,
    record_upload,
    upsert_classified,
    upsert_payments,
)
from src.models import ClassifiedPayment, Payment


class TestDatabase:
    def test_init_db(self, tmp_db):
        init_db(tmp_db)
        assert tmp_db.exists()

    def test_upsert_and_load(self, tmp_db, sample_payments):
        init_db(tmp_db)
        inserted, updated = upsert_payments(sample_payments, db_path=tmp_db)
        assert inserted == 5
        assert updated == 0

        loaded = load_payments(db_path=tmp_db)
        assert len(loaded) == 5

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

    def test_load_with_date_filter(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)

        start = datetime(2025, 2, 1)
        end = datetime(2025, 2, 28, 23, 59, 59)
        loaded = load_payments(start, end, db_path=tmp_db)
        assert len(loaded) == 2  # ch_test_002 and ch_test_005

    def test_get_latest_date(self, tmp_db, sample_payments):
        init_db(tmp_db)
        upsert_payments(sample_payments, db_path=tmp_db)
        latest = get_latest_transaction_date(db_path=tmp_db)
        assert latest is not None
        assert latest.month == 3

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
