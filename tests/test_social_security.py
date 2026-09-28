"""Tests for the Social Security (Seguridad Social) bank-export importer.

All fixtures are synthetic — no real bank data, amounts, or account
identifiers appear here. The real-file format (two title rows, header
`Fecha | Fecha valor | Movimiento | Más datos | Importe`, negative debits,
a TGSS concept) is mimicked structurally only.
"""
import pandas as pd
import pytest

from src.database import init_db
from src.social_security import (
    DEFAULT_CONCEPT_PATTERNS,
    _matches_concept,
    _parse_date,
    add_manual_ss_entry,
    detect_header_row,
    get_ss_payments,
    get_ss_period_totals,
    load_bank_export,
    upsert_ss_payments,
)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

def _write_synthetic_export(path, with_title_rows: bool = True) -> None:
    """Write a synthetic .xlsx mimicking the legacy bank-export shape.

    Two title rows, then the header row, then mixed movement rows: TGSS
    debits (negative), a TGSS refund (positive), a non-TGSS row (should be
    filtered out when a concept filter is applied), and a duplicate row.
    """
    header = ["Fecha", "Fecha valor", "Movimiento", "Más datos", "Importe"]
    data_rows = [
        ["2026-01-31", "2026-01-31", "TGSS.COTIZACION 0", "REF0000000", -50.25],
        ["2026-02-28", "2026-02-28", "TGSS.COTIZACION 0", "REF0000000", -50.25],
        ["2026-02-28", "2026-02-28", "TGSS.COTIZACION 0", "REF0000000", -50.25],  # exact duplicate
        ["2026-03-15", "2026-03-15", "TGSS.DEVOLUCION PLURIACTIVIDAD", "REF123", 45.00],  # refund (credit)
        ["2026-03-31", "2026-03-31", "OTRO MOVIMIENTO BANCARIO", "N/A", -12.00],  # not SS — filtered
    ]
    rows: list[list] = []
    if with_title_rows:
        rows.append(["Movimientos de la cuenta ES00 0000 0000 0000 0000 0000", "", "", "", ""])
        rows.append(["Importes expresados en euros", "", "", "", ""])
    rows.append(header)
    rows.extend(data_rows)

    df = pd.DataFrame(rows)
    df.to_excel(path, header=False, index=False, engine="openpyxl")


@pytest.fixture
def synthetic_export(tmp_path):
    fp = tmp_path / "ss_export.xlsx"
    _write_synthetic_export(fp, with_title_rows=True)
    return fp


@pytest.fixture
def synthetic_export_no_titles(tmp_path):
    fp = tmp_path / "ss_export_plain.xlsx"
    _write_synthetic_export(fp, with_title_rows=False)
    return fp


# ---------------------------------------------------------------------------
# Header auto-detection
# ---------------------------------------------------------------------------

class TestHeaderDetection:
    def test_detects_header_below_title_rows(self):
        raw = pd.DataFrame([
            ["Movimientos de la cuenta ...", None, None],
            ["Importes expresados en euros", None, None],
            ["Fecha", "Importe", "Movimiento"],
            ["2026-01-31", -50.25, "TGSS.COTIZACION 0"],
        ])
        assert detect_header_row(raw, "Fecha", "Importe") == 2

    def test_detects_header_at_row_zero(self):
        raw = pd.DataFrame([
            ["Fecha", "Importe"],
            ["2026-01-31", -50.25],
        ])
        assert detect_header_row(raw, "Fecha", "Importe") == 0

    def test_raises_when_header_not_found(self):
        raw = pd.DataFrame([
            ["Something", "Else"],
            ["Other", "Row"],
        ])
        with pytest.raises(ValueError, match="Could not auto-detect"):
            detect_header_row(raw, "Fecha", "Importe")


# ---------------------------------------------------------------------------
# Excel serial date fallback (the legacy .xls edge case)
# ---------------------------------------------------------------------------

class TestParseDate:
    def test_parses_datetime_cell(self):
        import datetime
        assert _parse_date(datetime.datetime(2026, 8, 31)) == "2026-08-31"

    def test_parses_raw_excel_serial_number(self):
        # 44927 == 2023-01-01 under the Excel 1899-12-30 epoch.
        assert _parse_date(44927) == "2023-01-01"
        assert _parse_date(44927.0) == "2023-01-01"

    def test_parses_string_with_time_component(self):
        assert _parse_date("2026-08-31 00:00:00") == "2026-08-31"

    def test_parses_dd_mm_yyyy_string(self):
        assert _parse_date("31/08/2026") == "2026-08-31"

    def test_returns_none_for_unparseable(self):
        assert _parse_date("not a date") is None
        assert _parse_date(None) is None


# ---------------------------------------------------------------------------
# Concept matching
# ---------------------------------------------------------------------------

class TestConceptMatching:
    def test_matches_default_patterns(self):
        assert _matches_concept("TGSS.COTIZACION 0", DEFAULT_CONCEPT_PATTERNS)
        assert _matches_concept("SEGURIDAD SOCIAL AUTONOMOS", DEFAULT_CONCEPT_PATTERNS)

    def test_accent_insensitive(self):
        assert _matches_concept("cuota autónomos", ["AUTONOMOS"])

    def test_no_match(self):
        assert not _matches_concept("TRANSFERENCIA A TERCEROS", DEFAULT_CONCEPT_PATTERNS)


# ---------------------------------------------------------------------------
# load_bank_export — end to end against a synthetic file
# ---------------------------------------------------------------------------

class TestLoadBankExport:
    def test_auto_detects_header_with_title_rows(self, synthetic_export):
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            description_column="Más datos",
        )
        # 5 data rows total (no concept filter applied)
        assert len(rows) == 5

    def test_works_without_title_rows_too(self, synthetic_export_no_titles):
        rows = load_bank_export(
            file_path=synthetic_export_no_titles,
            date_column="Fecha",
            amount_column="Importe",
        )
        assert len(rows) == 5

    def test_concept_filter_keeps_only_matching_rows(self, synthetic_export):
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            description_column="Más datos",
            concept_column="Movimiento",
            concept_patterns=["TGSS"],
        )
        # The "OTRO MOVIMIENTO BANCARIO" row is filtered out.
        assert len(rows) == 4
        assert all("TGSS" not in "" for r in rows)  # sanity: no crash

    def test_debit_stored_as_positive_contribution(self, synthetic_export):
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            concept_column="Movimiento",
            concept_patterns=["TGSS"],
        )
        jan_row = next(r for r in rows if r["payment_date"] == "2026-01-31")
        assert jan_row["amount_eur"] == 50.25

    def test_refund_stored_as_negative_contribution(self, synthetic_export):
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            concept_column="Movimiento",
            concept_patterns=["TGSS"],
        )
        refund_row = next(r for r in rows if r["payment_date"] == "2026-03-15")
        assert refund_row["amount_eur"] == -45.00

    def test_explicit_skiprows_overrides_auto_detect(self, synthetic_export):
        # Header is at row index 2 (two title rows above it).
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            skiprows=2,
        )
        assert len(rows) == 5

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_bank_export(
                file_path=tmp_path / "does_not_exist.xlsx",
                date_column="Fecha",
                amount_column="Importe",
            )

    def test_unknown_column_fails_auto_detection(self, synthetic_export):
        # Header auto-detection requires both columns in the same row, so an
        # unknown amount column means no header row is found at all.
        with pytest.raises(ValueError, match="Could not auto-detect"):
            load_bank_export(
                file_path=synthetic_export,
                date_column="Fecha",
                amount_column="NoSuchColumn",
            )

    def test_unknown_column_raises_with_explicit_header_row(self, synthetic_export):
        with pytest.raises(ValueError, match="not found"):
            load_bank_export(
                file_path=synthetic_export,
                date_column="Fecha",
                amount_column="NoSuchColumn",
                skiprows=2,
            )


# ---------------------------------------------------------------------------
# Dedupe key: (date, amount, description)
# ---------------------------------------------------------------------------

class TestUpsertDedupe:
    def test_exact_duplicate_skipped(self, tmp_db, synthetic_export):
        init_db(tmp_db)
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            description_column="Más datos",
            concept_column="Movimiento",
            concept_patterns=["TGSS"],
        )
        inserted, skipped = upsert_ss_payments(rows, source_file="test", db_path=tmp_db)
        # The Feb-28 row is repeated twice in the fixture with identical
        # date/amount/description — the second occurrence dedupes within
        # this same batch's DB-lookup logic on the first pass.
        assert inserted == 3  # Jan debit, Feb debit, Mar refund
        assert skipped == 1

    def test_reimport_is_idempotent(self, tmp_db, synthetic_export):
        init_db(tmp_db)
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            description_column="Más datos",
            concept_column="Movimiento",
            concept_patterns=["TGSS"],
        )
        upsert_ss_payments(rows, source_file="test", db_path=tmp_db)
        inserted_again, skipped_again = upsert_ss_payments(rows, source_file="test", db_path=tmp_db)
        assert inserted_again == 0
        assert skipped_again == 4

    def test_same_date_amount_different_description_both_kept(self, tmp_db):
        init_db(tmp_db)
        rows = [
            {"payment_date": "2026-05-31", "amount_eur": 50.25, "description": "TGSS cuota mensual"},
            {"payment_date": "2026-05-31", "amount_eur": 50.25, "description": "TGSS recargo"},
        ]
        inserted, skipped = upsert_ss_payments(rows, source_file="test", db_path=tmp_db)
        assert inserted == 2
        assert skipped == 0


# ---------------------------------------------------------------------------
# Manual entry fallback
# ---------------------------------------------------------------------------

class TestManualEntry:
    def test_add_manual_entry(self, tmp_db):
        init_db(tmp_db)
        inserted = add_manual_ss_entry("2026-06-30", 50.25, "Manual — missing month", db_path=tmp_db)
        assert inserted == 1
        rows = get_ss_payments(db_path=tmp_db)
        assert len(rows) == 1
        assert rows[0]["source_file"] == "manual"

    def test_duplicate_manual_entry_skipped(self, tmp_db):
        init_db(tmp_db)
        add_manual_ss_entry("2026-06-30", 50.25, "Manual entry", db_path=tmp_db)
        inserted = add_manual_ss_entry("2026-06-30", 50.25, "Manual entry", db_path=tmp_db)
        assert inserted == 0


# ---------------------------------------------------------------------------
# Quarterly / yearly totals helper
# ---------------------------------------------------------------------------

class TestPeriodTotals:
    def test_quarterly_and_yearly_totals(self, tmp_db):
        init_db(tmp_db)
        rows = [
            {"payment_date": "2026-01-31", "amount_eur": 100.0, "description": "Q1 cuota"},
            {"payment_date": "2026-02-28", "amount_eur": 100.0, "description": "Q1 cuota"},
            {"payment_date": "2026-04-30", "amount_eur": 50.0, "description": "Q2 cuota"},
            {"payment_date": "2026-05-31", "amount_eur": -20.0, "description": "Q2 refund"},
        ]
        upsert_ss_payments(rows, source_file="test", db_path=tmp_db)
        totals = get_ss_period_totals(2026, db_path=tmp_db)
        assert totals["quarters"][1] == 200.0
        assert totals["quarters"][2] == 30.0
        assert totals["quarters"][3] == 0.0
        assert totals["quarters"][4] == 0.0
        assert totals["yearly_total"] == 230.0

    def test_empty_year_returns_zeroes(self, tmp_db):
        init_db(tmp_db)
        totals = get_ss_period_totals(2099, db_path=tmp_db)
        assert totals["yearly_total"] == 0.0
        assert all(v == 0.0 for v in totals["quarters"].values())


# ---------------------------------------------------------------------------
# Modelo 130 integration: contributions net of refunds land in box 02
# ---------------------------------------------------------------------------

class TestModelo130Integration:
    def test_box_02_includes_ytd_contributions_net_of_refunds(self, tmp_db, synthetic_export):
        from src.database import get_connection
        from src.tax_engine import compute_modelo_130

        init_db(tmp_db)
        rows = load_bank_export(
            file_path=synthetic_export,
            date_column="Fecha",
            amount_column="Importe",
            description_column="Más datos",
            concept_column="Movimiento",
            concept_patterns=["TGSS"],
        )
        upsert_ss_payments(rows, source_file="test", db_path=tmp_db)

        conn = get_connection(tmp_db)
        try:
            result = compute_modelo_130(2026, 1, conn)
        finally:
            conn.close()
        # Jan + Feb debits (duplicate dropped) minus the March refund.
        assert result.box_02_gastos == pytest.approx(50.25 + 50.25 - 45.00)
