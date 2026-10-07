"""Filing sheet and immutable filed snapshots (#101). Synthetic data only."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from src.database import (
    get_connection,
    init_db,
    load_tax_snapshot_versions,
    load_tax_snapshots_for_period,
)
from src.filing_sheet import (
    aeat_amount,
    build_filing_sheet,
    mark_filed,
    render_markdown,
)
from src.reconciliation import result_boxes
from src.tax_engine import compute_and_persist_tax_snapshots
from src.tax_snapshot_codec import decode_snapshot

YEAR, QUARTER = 2025, 1
CONFIG = {"tax": {}}


def _insert_tx(conn: sqlite3.Connection, tx_id: str, amount: float, geo: str = "SPAIN",
               buyer_vat_id: str | None = None, activity: str = "COACHING") -> None:
    conn.execute(
        """INSERT INTO transactions
               (id, created_date, converted_amount, converted_amount_refunded, description, fee,
                currency, activity_type, geo_region, buyer_vat_id, email_meta)
           VALUES (?, '2025-02-10T10:00:00', ?, 0, 'test', 0, 'eur', ?, ?, ?, ?)""",
        (tx_id, amount, activity, geo, buyer_vat_id, f"{tx_id}@example.com"),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path):
    db = tmp_path / "sheet.db"
    init_db(db)
    c = get_connection(db)
    _insert_tx(c, "t_es", 121.0)
    _insert_tx(c, "t_eu", 300.0, geo="EU_NOT_SPAIN", buyer_vat_id="DE999999999", activity="NEWSLETTER")
    yield c
    c.close()


def _compute(conn: sqlite3.Connection) -> None:
    compute_and_persist_tax_snapshots(YEAR, QUARTER, conn, CONFIG)


def _versions(conn: sqlite3.Connection, model: str = "303") -> list[dict]:
    return [dict(r) for r in load_tax_snapshot_versions(conn, YEAR, QUARTER, model)]


# ---------------------------------------------------------------------------
# Filing sheet content
# ---------------------------------------------------------------------------

def test_sheet_contains_every_non_zero_box_of_303_130_and_349(conn):
    _compute(conn)
    md = render_markdown(build_filing_sheet(YEAR, QUARTER, conn))
    stored = {r["model"]: decode_snapshot(r["model"], r["payload_json"])
              for r in load_tax_snapshots_for_period(YEAR, QUARTER, conn)}
    checked = 0
    for model in ("303", "130", "349"):
        boxes = result_boxes(model, stored[model])
        non_zero = {b: v for b, v in boxes.items() if abs(v) >= 0.005}
        assert non_zero, f"synthetic data should give Modelo {model} a non-zero box"
        section = md.split(f"## Modelo {model}", 1)[1].split("\n## ", 1)[0]
        for box, value in non_zero.items():
            typed = str(int(value)) if (model, box) == ("349", "01") else aeat_amount(value)
            assert f"| {box} |" in section and f"`{typed}`" in section, (model, box)
            checked += 1
    assert checked >= 5
    assert "| DE | 999999999 |" in md  # the 349 operator line


def test_zero_boxes_are_hidden_by_default_and_listed_on_request(conn):
    _compute(conn)
    sheet = build_filing_sheet(YEAR, QUARTER, conn)
    default = render_markdown(sheet).split("## Modelo 303", 1)[1].split("### Credit chain", 1)[0]
    full = render_markdown(sheet, include_zero=True).split("## Modelo 303", 1)[1].split("### Credit chain", 1)[0]
    assert "| 01 |" not in default          # the 4 % row is zero
    assert "| 01 |" in full and "| 07 |" in default


def test_303_boxes_follow_the_form_order_and_carry_the_credit_chain(conn):
    _compute(conn)
    s303 = build_filing_sheet(YEAR, QUARTER, conn).models["303"]
    order = [b.box for b in s303.boxes]
    assert order.index("07") < order.index("27") < order.index("46") < order.index("59") < order.index("110") \
        < order.index("78") < order.index("87") < order.index("71")
    assert [b.box for b in s303.credit_chain] == ["110", "78", "87", "71", "72", "73"]
    assert s303.result.startswith("To pay (box 71)")


def test_missing_snapshots_render_a_hint(conn):
    md = render_markdown(build_filing_sheet(YEAR, QUARTER, conn))
    assert md.count("No stored snapshot") == 3
    assert "## Deadlines" in md


# ---------------------------------------------------------------------------
# Mark filed / versioning
# ---------------------------------------------------------------------------

def test_mark_filed_freezes_a_new_version(conn):
    _compute(conn)
    version = mark_filed(conn, YEAR, QUARTER, "303", " 3031234567890 ", date(2025, 4, 18))

    assert version == 2
    v = _versions(conn)
    assert [(r["snapshot_version"], r["status"]) for r in v] == [(1, "COMPUTED"), (2, "FILED")]
    assert v[1]["payload_json"] == v[0]["payload_json"]
    assert v[1]["justificante"] == "3031234567890" and v[1]["presented_on"] == "2025-04-18"
    status = conn.execute("SELECT status, amount_eur, filed_at FROM tax_filing_status "
                          "WHERE year = ? AND quarter = ? AND model = '303'", (YEAR, QUARTER)).fetchone()
    box71 = result_boxes("303", decode_snapshot("303", v[1]["payload_json"]))["71"]
    assert status["status"] == "FILED" and status["amount_eur"] == pytest.approx(box71)
    assert status["filed_at"] == "2025-04-18"

    s303 = build_filing_sheet(YEAR, QUARTER, conn).models["303"]
    assert s303.status == "FILED" and s303.version == 2 and not s303.changes_since_filed


def test_recompute_after_filing_adds_a_computed_version_and_leaves_the_filed_one(conn):
    _compute(conn)
    mark_filed(conn, YEAR, QUARTER, "303", "J1", date(2025, 4, 18))
    filed_before = _versions(conn)[1]

    _compute(conn)  # nothing changed: no new draft
    assert len(_versions(conn)) == 2

    _insert_tx(conn, "t_es2", 242.0)
    _compute(conn)
    v = _versions(conn)
    assert [(r["snapshot_version"], r["status"]) for r in v] == [(1, "COMPUTED"), (2, "FILED"), (3, "COMPUTED")]
    assert v[1] == filed_before
    assert v[2]["payload_json"] != filed_before["payload_json"]

    _insert_tx(conn, "t_es3", 12.1)
    _compute(conn)  # the new draft is rewritten in place
    v = _versions(conn)
    assert len(v) == 3 and v[1] == filed_before

    latest = {r["model"]: r for r in load_tax_snapshots_for_period(YEAR, QUARTER, conn)}
    assert latest["303"]["snapshot_version"] == 3 and latest["303"]["status"] == "COMPUTED"
    s303 = build_filing_sheet(YEAR, QUARTER, conn).models["303"]
    assert s303.filed_version == 2
    assert {c.box for c in s303.changes_since_filed} >= {"07", "09"}
    assert "Recomputed after filing" in render_markdown(build_filing_sheet(YEAR, QUARTER, conn))


def test_mark_filed_rejects_bad_requests(conn):
    with pytest.raises(ValueError, match="no computed snapshot"):
        mark_filed(conn, YEAR, QUARTER, "303", "J1", date(2025, 4, 18))
    _compute(conn)
    with pytest.raises(ValueError, match="justificante"):
        mark_filed(conn, YEAR, QUARTER, "303", "  ", date(2025, 4, 18))
    with pytest.raises(ValueError, match="not filed from the filing sheet"):
        mark_filed(conn, YEAR, QUARTER, "OSS", "J1", date(2025, 4, 18))
    mark_filed(conn, YEAR, QUARTER, "349", "J349", date(2025, 4, 18))
    with pytest.raises(ValueError, match="already filed"):
        mark_filed(conn, YEAR, QUARTER, "349", "J349b", date(2025, 4, 19))
    assert len(_versions(conn, "349")) == 2


def test_triggers_block_update_and_delete_of_filed_rows(conn):
    _compute(conn)
    mark_filed(conn, YEAR, QUARTER, "130", "J130", date(2025, 4, 18))
    where = "WHERE year = 2025 AND quarter = 1 AND model = '130'"
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute(f"UPDATE tax_computation_snapshots SET payload_json = '{{}}' {where} AND status = 'FILED'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):
        conn.execute(f"DELETE FROM tax_computation_snapshots {where} AND status = 'FILED'")
    with pytest.raises(sqlite3.DatabaseError, match="immutable"):  # a draft cannot be flipped to FILED
        conn.execute(f"UPDATE tax_computation_snapshots SET status = 'FILED' {where} AND snapshot_version = 1")
    conn.rollback()
    conn.execute(f"DELETE FROM tax_computation_snapshots {where} AND status = 'COMPUTED'")  # drafts are not frozen
    assert [r["status"] for r in _versions(conn, "130")] == ["FILED"]


def test_init_db_migrates_an_unversioned_snapshot_table(tmp_path):
    db = tmp_path / "old.db"
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE tax_computation_snapshots (
            year INTEGER NOT NULL, quarter INTEGER NOT NULL, model TEXT NOT NULL,
            payload_json TEXT NOT NULL, computed_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (year, quarter, model));
        INSERT INTO tax_computation_snapshots VALUES (2025, 1, '349', '{"year": 2025, "quarter": 1}', '2025-04-01T00:00:00');
    """)
    c.close()

    init_db(db)
    init_db(db)  # idempotent
    conn = get_connection(db)
    try:
        rows = [dict(r) for r in load_tax_snapshot_versions(conn, 2025, 1, "349")]
        assert [(r["snapshot_version"], r["status"], r["computed_at"]) for r in rows] == \
            [(1, "COMPUTED", "2025-04-01T00:00:00")]
        triggers = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        assert {"tax_snapshots_filed_no_update", "tax_snapshots_filed_no_delete"} <= triggers
    finally:
        conn.close()


def test_aeat_amount_uses_a_decimal_comma_without_thousands():
    assert aeat_amount(1234.5) == "1234,50"
    assert aeat_amount(-0.4) == "-0,40"
