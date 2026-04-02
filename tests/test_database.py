"""Tests for the SQLite database layer."""
import pytest
from datetime import datetime

from src.database import (
    get_latest_transaction_date,
    get_transaction_count_db,
    get_uploaded_files,
    init_db,
    load_payments,
    record_upload,
    upsert_payments,
)
from src.models import Payment


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
