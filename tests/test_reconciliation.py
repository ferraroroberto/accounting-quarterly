"""Tests for src/reconciliation.py — filed-vs-app matching, catalogue, 349
operators, markdown export and the engine → AEAT box adapter (303, 130, 349
and the Modelo 390 engine, all through ``aeat_boxes()``).

All data is synthetic (fake VAT ids, round amounts)."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import src.reconciliation as rc
import src.tax_validator as tax_validator
from src.database import init_db, upsert_audit_entries_conn
from src.filed_returns import FiledReturn, Operator349, store_filed_return
from src.reconciliation import (
    STATUS_CATALOGUED,
    STATUS_EXACT,
    STATUS_MISSING,
    STATUS_UNCATALOGUED,
    CatalogueError,
    Divergence,
    Reconciliation,
    ReconLine,
    app_boxes,
    apply_catalogue,
    audit_entries_for_box,
    build_lines,
    classify,
    load_catalogue,
    load_logged_audit,
    operator_box,
    parse_entry,
    reconcile,
    result_boxes,
    save_catalogue,
    to_markdown,
)
from src.tax_engine import compute_modelo_130, compute_modelo_303
from src.tax_models import AuditEntry

ROOT = Path(__file__).parent.parent
CFG: dict = {"tax": {}}


def _div(**kw) -> Divergence:
    base = dict(model="303", year=2025, quarter=1, box="46", category="gestor_error",
                explanation="synthetic")
    base.update(kw)
    return Divergence(**base)


# ---------------------------------------------------------------------------
# Matching logic
# ---------------------------------------------------------------------------

class TestClassify:
    def test_exact_match_within_one_cent(self):
        assert classify(100.0, 100.01, []) == (STATUS_EXACT, None)
        assert classify(100.0, 99.99, [])[0] == STATUS_EXACT

    def test_two_cents_is_not_exact(self):
        assert classify(100.0, 100.02, [])[0] == STATUS_UNCATALOGUED

    def test_catalogued_expected_delta(self):
        entry = _div(expected_delta=3.21)
        status, hit = classify(10.0, 13.21, [entry])
        assert status == STATUS_CATALOGUED and hit is entry

    def test_expected_delta_outside_tolerance_is_uncatalogued(self):
        entry = _div(expected_delta=3.21, tolerance=0.01)
        assert classify(10.0, 13.24, [entry])[0] == STATUS_UNCATALOGUED

    def test_expected_delta_tolerance_widens_the_match(self):
        entry = _div(expected_delta=3.21, tolerance=0.5)
        assert classify(10.0, 13.60, [entry])[0] == STATUS_CATALOGUED

    def test_negative_expected_delta(self):
        assert classify(-1000.00, -600.00, [_div(expected_delta=400.00)])[0] == STATUS_CATALOGUED
        assert classify(500.0, 450.0, [_div(expected_delta=-50.0)])[0] == STATUS_CATALOGUED

    def test_rule_app_gte_filed(self):
        entry = _div(rule="app_gte_filed")
        assert classify(7.50, 500.0, [entry])[0] == STATUS_CATALOGUED
        assert classify(500.0, 7.50, [entry])[0] == STATUS_UNCATALOGUED

    def test_rule_app_lte_filed(self):
        entry = _div(rule="app_lte_filed")
        assert classify(500.0, 7.50, [entry])[0] == STATUS_CATALOGUED
        assert classify(7.50, 500.0, [entry])[0] == STATUS_UNCATALOGUED

    def test_rule_any(self):
        assert classify(1.0, 9999.0, [_div(rule="any")])[0] == STATUS_CATALOGUED

    def test_uncatalogued_diff(self):
        assert classify(100.0, 150.0, []) == (STATUS_UNCATALOGUED, None)

    def test_missing_side(self):
        assert classify(None, 100.0, [_div(rule="any")]) == (STATUS_MISSING, None)
        assert classify(100.0, None, [_div(rule="any")]) == (STATUS_MISSING, None)

    def test_first_fitting_entry_wins(self):
        a, b = _div(expected_delta=1.0), _div(rule="any")
        assert classify(0.0, 5.0, [a, b])[1] is b
        assert classify(0.0, 1.0, [a, b])[1] is a


class TestApplyCatalogue:
    def _rec(self, **kw) -> Reconciliation:
        return Reconciliation(model="303", year=2025, quarter=2, filed_found=True,
                              lines=[ReconLine("46", "", 10.0, 15.0)], **kw)

    def test_entry_for_other_period_or_box_does_not_apply(self):
        cat = [_div(quarter=1, expected_delta=5.0), _div(box="45", quarter=2, expected_delta=5.0),
               _div(model="130", quarter=2, expected_delta=5.0), _div(year=2024, quarter=2, expected_delta=5.0)]
        rec = apply_catalogue(self._rec(), cat)
        assert rec.lines[0].status == STATUS_UNCATALOGUED

    def test_null_year_and_quarter_mean_any(self):
        rec = apply_catalogue(self._rec(), [_div(year=None, quarter=None, expected_delta=5.0)])
        ln = rec.lines[0]
        assert ln.status == STATUS_CATALOGUED
        assert ln.tag == "gestor_error" and ln.explanation == "synthetic"

    def test_counts(self):
        rec = self._rec()
        rec.lines += [ReconLine("07", "", 1.0, 1.0), ReconLine("110", "", 5.0, None)]
        apply_catalogue(rec, [])
        assert rec.counts() == {STATUS_EXACT: 1, STATUS_CATALOGUED: 0,
                                STATUS_UNCATALOGUED: 1, STATUS_MISSING: 1}


class TestBuildLines:
    def test_blank_box_on_imported_return_counts_as_zero(self):
        lines = {ln.box: ln for ln in build_lines("303", {"46": 5.0}, {"07": 12.34, "46": 5.0})}
        assert lines["07"].filed == 0.0 and lines["07"].app == 12.34
        assert lines["46"].filed == 5.0

    def test_box_the_app_does_not_compute_is_missing(self):
        (ln,) = build_lines("303", {"110": 80.00}, {})
        assert ln.filed == 80.00 and ln.app is None
        assert classify(ln.filed, ln.app, [])[0] == STATUS_MISSING

    def test_no_filed_return_leaves_filed_side_empty(self):
        lines = build_lines("303", None, {"07": 100.0, "09": 21.0})
        assert [ln.filed for ln in lines] == [None, None]

    def test_yaml_filing_only_knows_its_listed_boxes(self):
        lines = {ln.box: ln for ln in build_lines("303", {"09": 21.0}, {"07": 100.0, "09": 21.0},
                                                   filed_complete=False)}
        assert lines["07"].filed is None and lines["09"].filed == 21.0

    def test_boxes_sort_numerically(self):
        lines = build_lines("303", {"110": 1.0, "07": 1.0, "46": 1.0}, {"09": 1.0})
        assert [ln.box for ln in lines] == ["07", "09", "46", "110"]


class TestModelo349Operators:
    FILED = [
        {"country": "IE", "vat_id": "1234567X", "name": "Cloud IE", "key": "I", "base": 100.0},
        {"country": "NL", "vat_id": "NL000000001B01", "name": "Hosting NL", "key": "I", "base": 50.0},
        {"country": "FR", "vat_id": "00111111111", "name": "Client FR", "key": "S", "base": 30.0},
    ]
    APP = [
        {"country": "IE", "vat_id": "IE 123.4567-X", "name": "Cloud IE", "key": "I", "base": 60.0},
        {"country": "IE", "vat_id": "IE1234567X", "name": "Cloud IE", "key": "I", "base": 40.0},
        {"country": "NL", "vat_id": "NL000000001B01", "name": "Hosting NL", "key": "I", "base": 55.0},
        {"country": "FR", "vat_id": "FR00111111111", "name": "Client FR", "key": "I", "base": 30.0},
        {"country": "IE", "vat_id": "IE9999999Z", "name": "Tool IE", "key": "I", "base": 7.50},
    ]

    def _ops(self) -> dict[str, ReconLine]:
        lines = build_lines("349", {"01": 3.0, "02": 180.0}, {"01": 4.0, "02": 189.13},
                            filed_operators=self.FILED, app_ops=self.APP)
        return {ln.box: ln for ln in lines if ln.box.startswith("op:")}

    def test_operator_key_normalises_country_prefix_and_separators(self):
        assert operator_box("IE", "1234567X", "i") == operator_box(None, "ie-123 4567x", "I") == "op:IE1234567X:I"

    def test_same_operator_matches_and_invoices_are_summed(self):
        ops = self._ops()
        ln = ops["op:IE1234567X:I"]
        assert (ln.filed, ln.app) == (100.0, 100.0)
        assert classify(ln.filed, ln.app, [])[0] == STATUS_EXACT

    def test_operator_amount_diff(self):
        ln = self._ops()["op:NL000000001B01:I"]
        assert ln.diff == 5.0

    def test_same_vat_id_with_different_key_is_a_different_row(self):
        ops = self._ops()
        assert ops["op:FR00111111111:S"].app == 0.0 and ops["op:FR00111111111:S"].filed == 30.0
        assert ops["op:FR00111111111:I"].filed == 0.0 and ops["op:FR00111111111:I"].app == 30.0

    def test_operator_only_in_app_can_be_catalogued(self):
        ln = self._ops()["op:IE9999999Z:I"]
        assert (ln.filed, ln.app) == (0.0, 7.50)
        entry = _div(model="349", box="op:IE9999999Z:I", expected_delta=7.50)
        assert classify(ln.filed, ln.app, [entry])[0] == STATUS_CATALOGUED

    def test_no_filed_349_leaves_operators_missing(self):
        lines = build_lines("349", None, {"01": 1.0}, app_ops=self.APP[:1])
        op = next(ln for ln in lines if ln.box.startswith("op:"))
        assert op.filed is None and classify(op.filed, op.app, [])[0] == STATUS_MISSING


# ---------------------------------------------------------------------------
# Catalogue file
# ---------------------------------------------------------------------------

class TestCatalogue:
    VALID = {"model": "303", "year": 2026, "quarter": 2, "box": "7", "expected_delta": 12.34,
             "rule": None, "tolerance": None, "category": "gestor_error", "explanation": "x"}

    def test_parse_normalises_box_and_defaults_tolerance(self):
        d = parse_entry(self.VALID)
        assert d.box == "07" and d.tolerance == 0.01 and d.quarter == 2

    def test_nan_cells_from_the_data_editor_count_as_empty(self):
        d = parse_entry({**self.VALID, "year": float("nan"), "quarter": float("nan"),
                         "rule": None, "tolerance": float("nan")})
        assert d.year is None and d.quarter is None

    @pytest.mark.parametrize("patch, message", [
        ({"model": "111"}, "model"),
        ({"expected_delta": None}, "exactly one"),
        ({"rule": "any"}, "exactly one"),
        ({"expected_delta": None, "rule": "app_bigger"}, "rule must be"),
        ({"category": "oops"}, "category"),
        ({"explanation": " "}, "explanation"),
        ({"quarter": 5}, "quarter"),
        ({"box": ""}, "box"),
        ({"tolerance": -1}, "tolerance"),
        ({"model": "390"}, "annual"),
    ])
    def test_invalid_entries_are_rejected(self, patch, message):
        with pytest.raises(CatalogueError, match=message):
            parse_entry({**self.VALID, **patch})

    def test_save_and_load_round_trip(self, isolated_divergence_catalogue):
        rows = [self.VALID, {**self.VALID, "box": "op:ie1234567x:i", "expected_delta": None,
                             "rule": "app_gte_filed", "year": None, "quarter": None}]
        save_catalogue(rows)
        data = json.loads(isolated_divergence_catalogue.read_text(encoding="utf-8"))
        assert len(data["divergences"]) == 2
        loaded = load_catalogue()
        assert [d.box for d in loaded] == ["07", "op:IE1234567X:I"]
        assert loaded[1].rule == "app_gte_filed" and loaded[1].expected_delta is None

    def test_invalid_save_writes_nothing_and_names_the_row(self, isolated_divergence_catalogue):
        with pytest.raises(CatalogueError, match="row 2"):
            save_catalogue([self.VALID, {**self.VALID, "category": "nope"}])
        assert not isolated_divergence_catalogue.exists()

    def test_missing_file_is_an_empty_catalogue(self):
        assert load_catalogue() == []

    def test_malformed_file_raises(self, isolated_divergence_catalogue):
        isolated_divergence_catalogue.write_text("{not json", encoding="utf-8")
        with pytest.raises(CatalogueError, match="not valid JSON"):
            load_catalogue()

    def test_shipped_example_is_valid(self):
        entries = load_catalogue(ROOT / "divergences.json.example")
        assert entries and {e.category for e in entries} <= {"gestor_error", "convention", "app_choice"}


# ---------------------------------------------------------------------------
# Markdown export
# ---------------------------------------------------------------------------

def test_markdown_export_contains_every_row_and_escapes_pipes():
    rec = Reconciliation(model="303", year=2025, quarter=2, filed_found=True,
                         filed_source="db", filed_date="2025-07-20",
                         lines=[ReconLine("07", "Base 21 %", 100.0, 100.0),
                                ReconLine("46", "Resultado", -10.0, -5.0),
                                ReconLine("110", "", 3.0, None)])
    apply_catalogue(rec, [_div(quarter=2, expected_delta=5.0, explanation="a | b")])
    md = to_markdown(rec)
    assert "## Modelo 303 — 2025 Q2" in md
    assert "| 07 | Base 21 % | 100.00 | 100.00 | +0.00 | ✅ exact |" in md
    assert "| 46 | Resultado | -10.00 | -5.00 | +5.00 | 🟡 catalogued | gestor_error | a \\| b |" in md
    assert "| 110 |  | 3.00 | — | — | ⚪ missing |" in md
    assert "✅ exact: 1 · 🟡 catalogued: 1 · 🔴 uncatalogued: 0 · ⚪ missing: 1" in md


def test_markdown_export_without_filed_return():
    rec = Reconciliation(model="130", year=2025, quarter=1, filed_found=False,
                         lines=[ReconLine("01", "", None, 10.0)])
    apply_catalogue(rec, [])
    assert "No filed return for this period" in to_markdown(rec)


# ---------------------------------------------------------------------------
# Engine adapter + end-to-end reconcile on a temp DB
# ---------------------------------------------------------------------------

@pytest.fixture
def db_conn(tmp_path, monkeypatch):
    monkeypatch.setattr(tax_validator, "_YAML_PATH", tmp_path / "no-validation.yaml")
    db_path = tmp_path / "recon.db"
    init_db(db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """INSERT INTO transactions
           (id, created_date, converted_amount, converted_amount_refunded, description, fee,
            currency, activity_type, geo_region)
           VALUES ('tx_1', '2025-02-10T10:00:00', 121.0, 0.0, 'coaching', 0.0, 'eur',
                   'COACHING', 'SPAIN')"""
    )
    conn.commit()
    yield conn
    conn.close()


def _file(conn, model: str, boxes: dict[str, float], operators=(), period: str = "1T") -> None:
    store_filed_return(conn, FiledReturn(
        model=model, year=2025, period=period, justificante=f"J{model}{period}",
        source_file="synthetic.pdf", presented_at="2025-04-18T10:00:00",
        boxes=boxes, operators=list(operators),
    ))


class TestAppBoxesAdapter:
    def test_303_uses_the_engines_aeat_boxes(self, db_conn):
        result = compute_modelo_303(2025, 1, db_conn, CFG)
        boxes = app_boxes("303", 2025, 1, db_conn, CFG)
        assert boxes == result.aeat_boxes()
        # 121 gross Spanish sale → 21 % row: base 100 in 07, 21 in 09 (01/03 is the 4 % row).
        assert (boxes["07"], boxes["09"], boxes["27"]) == (100.0, 21.0, 21.0)
        assert boxes["01"] == boxes["03"] == 0.0

    def test_130_uses_the_engines_aeat_boxes(self, db_conn):
        result = compute_modelo_130(2025, 1, db_conn, CFG)
        boxes = app_boxes("130", 2025, 1, db_conn, CFG)
        assert boxes == result.aeat_boxes()
        assert list(boxes) == [f"{n:02d}" for n in range(1, 20)]
        # Box 02 includes the 5 % allowance, as on the form.
        assert boxes["02"] == round(result.gastos_reales + result.gastos_dificil_justificacion, 2)

    def test_result_with_aeat_boxes_is_used_verbatim(self, db_conn, monkeypatch):
        class NewResult:
            audit: list = []

            def aeat_boxes(self):
                return {"07": 1.0, "10": 2.0, "110": 3.0}

        monkeypatch.setattr(rc, "compute_modelo_303", lambda *a, **k: NewResult())
        assert app_boxes("303", 2025, 1, db_conn, CFG) == {"07": 1.0, "10": 2.0, "110": 3.0}

    def test_result_with_operators_is_used_for_349(self, db_conn, monkeypatch):
        class New349:
            audit: list = []

            def aeat_boxes(self):
                return {"01": 1.0, "02": 9.99}

            def operators(self):
                return [{"country": "IE", "vat_id": "0000000XX", "name": "X", "key": "I", "base": 9.99}]

        monkeypatch.setattr(rc, "compute_modelo_349", lambda *a, **k: New349())
        rec = reconcile("349", 2025, 1, db_conn, CFG)
        assert [ln.box for ln in rec.lines] == ["01", "02", "op:IE0000000XX:I"]


class TestReconcile:
    def test_no_filed_return_shows_app_side_only(self, db_conn):
        rec = reconcile("303", 2025, 1, db_conn, CFG)
        assert rec.filed_found is False
        assert rec.lines and all(ln.status == STATUS_MISSING for ln in rec.lines)
        assert {"07", "27", "110", "71"} <= {ln.box for ln in rec.lines}

    def test_filed_303_exact_catalogued_uncatalogued_and_missing(self, db_conn):
        app = app_boxes("303", 2025, 1, db_conn, CFG)
        _file(db_conn, "303", {"07": app["07"], "09": app["09"], "27": app["27"],
                               "46": round(app["46"] - 3.21, 2), "110": 50.0, "33": 5.0})
        cat = [Divergence(model="303", year=2025, quarter=1, box="46", expected_delta=3.21,
                          category="gestor_error", explanation="synthetic")]
        rec = reconcile("303", 2025, 1, db_conn, CFG, catalogue=cat)
        by_box = {ln.box: ln for ln in rec.lines}
        assert rec.filed_found and rec.filed_source == "db"
        assert by_box["07"].status == STATUS_EXACT
        assert by_box["46"].status == STATUS_CATALOGUED
        assert by_box["110"].status == STATUS_UNCATALOGUED     # app chain has no earlier credit
        assert by_box["64"].status == STATUS_UNCATALOGUED      # blank on the return = 0
        assert by_box["33"].status == STATUS_MISSING           # imports: not computed by the app

    def test_filed_349_compared_operator_by_operator(self, db_conn):
        _file(db_conn, "349", {"01": 1.0, "02": 100.0},
              operators=[Operator349(1, "IE", "1234567X", "Cloud IE", "I", 100.0)])
        rec = reconcile("349", 2025, 1, db_conn, CFG)
        op = next(ln for ln in rec.lines if ln.box == "op:IE1234567X:I")
        assert (op.filed, op.app, op.status) == (100.0, 0.0, STATUS_UNCATALOGUED)

    def test_390_is_annual(self, db_conn):
        rec = reconcile("390", 2025, 3, db_conn, CFG)
        assert rec.quarter is None and rec.period == "2025 annual"
        assert "05" in {ln.box for ln in rec.lines}
        # The 390 engine's audit trail backs the drill-down (cells named after the box).
        cells = [e["cell"] for e in audit_entries_for_box(rec.live_audit, "390", "65")]
        assert cells == ["c65"]


# ---------------------------------------------------------------------------
# Drill-down
# ---------------------------------------------------------------------------

class TestAuditDrillDown:
    ENTRIES = [
        {"cell": "box_05_base", "value": 100.0}, {"cell": "box_03_cuota", "value": 21.0},
        {"cell": "c07_base", "value": 1.0}, {"cell": "c070_x", "value": 2.0},
        {"cell": "operator_IE1234567X", "value": 5.0}, {"cell": "total", "value": 5.0},
    ]

    def test_aeat_box_matches_cells_named_after_it(self):
        cells = [e["cell"] for e in audit_entries_for_box(self.ENTRIES, "303", "07")]
        assert cells == ["c07_base"]

    def test_operator_row_matches_its_vat_id(self):
        cells = [e["cell"] for e in audit_entries_for_box(self.ENTRIES, "349", "op:IE1234567X:I")]
        assert cells == ["operator_IE1234567X"]

    def test_logged_audit_reads_the_latest_run(self, db_conn):
        entry = AuditEntry.of("303", 2025, 1, "box_01_base", "Base", "f", 1.0)
        upsert_audit_entries_conn(db_conn, [entry], "2025-04-01T00:00:00")
        newer = AuditEntry.of("303", 2025, 1, "box_01_base", "Base", "f", 2.0)
        upsert_audit_entries_conn(db_conn, [newer], "2025-04-02T00:00:00")
        rows = load_logged_audit(db_conn, "303", 2025, 1)
        assert [r["value"] for r in rows] == [2.0]
        assert load_logged_audit(db_conn, "303", 2025, 2) == []

    def test_live_audit_is_carried_on_the_reconciliation(self, db_conn):
        rec = reconcile("303", 2025, 1, db_conn, CFG)
        hits = audit_entries_for_box(rec.live_audit, "303", "07")
        assert [h["cell"] for h in hits] == ["c07_base"]
        assert hits[0]["computed_at"] == "live"
