"""Tests for src/filed_returns.py — AEAT receipt import.

Every input here is SYNTHETIC: the PDFs are generated in the test by a tiny
PDF writer (reportlab is not a dependency) with fake amounts and fake tax ids.
Real receipts carry the taxpayer's and the presenter's NIF and must never be
committed as fixtures.

The synthetic receipts mimic the real layout that matters to the parser: a
first "Información de la presentación" page, box labels in the template font
(Helvetica) and declared values in a different font (Courier), values sitting
up to ~2pt off their label's baseline, pre-printed rate values in the 303
"Tipo %" boxes, and a template amount inside explanatory text.
"""
from __future__ import annotations

import io
import sqlite3

import pytest

from src.database import get_connection
from src.filed_returns import (
    FiledReturnParseError,
    detect_template_font,
    import_paths,
    import_pdf,
    load_filed_boxes,
    load_filings,
    main,
    pair_boxes,
    parse_amount,
    parse_pdf,
    period_to_quarter,
)
from src import tax_validator

T, V = "F1", "F2"  # template font (Helvetica), value font (Courier)
PAGE_H = 842


# ---------------------------------------------------------------------------
# Minimal PDF writer
# ---------------------------------------------------------------------------

def _pdf_str(text: str) -> bytes:
    raw = text.encode("cp1252")
    return b"(" + raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"


def make_pdf(pages: list[list[tuple[float, float, str, str]]]) -> bytes:
    """Build a PDF; each page is a list of (x, top, text, font) with font T or V.

    ``top`` is measured from the top of the page like pdfplumber reports it.
    """
    objs: list[bytes] = []
    font_t = len(objs) + 1; objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>")
    font_v = len(objs) + 1; objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>")
    pages_id = len(objs) + 1; objs.append(b"")  # placeholder for /Pages
    kids = []
    for items in pages:
        ops = []
        for x, top, text, font in items:
            size = 6.5 if font == T else 9
            baseline = PAGE_H - top - size
            ops.append(b"BT /%s %s Tf %.2f %.2f Td " % (font.encode(), str(size).encode(), x, baseline)
                       + _pdf_str(text) + b" Tj ET")
        stream = b"\n".join(ops)
        content_id = len(objs) + 1
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_id = len(objs) + 1
        objs.append(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 595 %d] " % (pages_id, PAGE_H)
            + b"/Resources << /Font << /F1 %d 0 R /F2 %d 0 R >> >> /Contents %d 0 R >>"
            % (font_t, font_v, content_id)
        )
        kids.append(page_id)
    objs[pages_id - 1] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids)
                          + b"] /Count %d >>" % len(kids))
    catalog_id = len(objs) + 1
    objs.append(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % i + body + b"\nendobj\n")
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1))
    for off in offsets:
        out.write(b"%010d 00000 n \n" % off)
    out.write(b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
              % (len(objs) + 1, catalog_id, xref))
    return out.getvalue()


def _words(line: str, x: float, top: float, font: str) -> list[tuple[float, float, str, str]]:
    """Split a phrase into positioned words (rough per-font advance)."""
    char_w, gap = (5, 4) if font == T else (6, 5)
    out = []
    for w in line.split():
        out.append((x, top, w, font))
        x += len(w) * char_w + gap
    return out


def receipt_page(model: str, justificante: str, presented: str = "17-07-2026 a las 09:20:53") -> list:
    """Fake 'Información de la presentación' first page."""
    return (
        _words(f"Modelo {model}", 304, 103, V)
        + _words(f"Presentación realizada el: {presented}", 62, 152, T)
        + _words("Código Seguro de Verificación: TESTCSV000000000", 62, 192, T)
        + _words(f"Número de justificante: {justificante}", 62, 212, T)
        + _words("NIF Presentador: 00000000T", 62, 282, T)
        + _words("Apellidos y Nombre / Razón social: GESTOR FICTICIO", 62, 302, T)
        + _words("En calidad de: Colaborador", 62, 322, T)
    )


def pdf_303(justificante: str = "3030000000001", presented: str = "17-07-2026 a las 09:20:53",
            base_10: str = "111,11") -> bytes:
    page2 = (
        _words("Ejercicio", 416, 126, T) + [(452, 126, "2026", V)]
        + _words("Período", 505, 126, T) + [(534, 126, "2T", V)]
        # 21% row: box 08 is a 'Tipo %' box with a pre-printed rate in the value font.
        + [(297, 498, "07", T), (397, 498, "08", T), (427, 496, "21,00", V), (461, 498, "09", T)]
        # Two values on one row: nearest label to the left wins.
        + [(296, 508, "10", T), (364, 507, base_10, V), (461, 508, "11", T), (534, 507, "23,33", V)]
        # Formula text with box numbers on the row above the total's label.
        + _words("Total cuota devengada (152 + 167 + 03 + 11)", 60, 619, T)
        + [(460, 620, "27", T), (534, 619, "23,33", V)]
        + [(361, 654, "28", T), (420, 653, "1.000,00", V), (461, 654, "29", T), (521, 653, "210,00", V)]
        # Value 1pt above its label's row.
        + [(460, 807, "46", T), (518, 806, "-186,67", V)]
    )
    page3 = (
        [(463, 86, "60", T), (523, 85, "4.000,00", V)]
        + [(461, 340, "110", T), (530, 339, "50,00", V)]
        + [(463, 374, "87", T), (530, 373, "50,00", V)]
        + [(463, 448, "71", T), (520, 447, "-186,67", V)]
    )
    page4 = [(88, 97, "72", T), (124, 96, "186,67", V)]
    return make_pdf([receipt_page("303", justificante, presented), page2, page3, page4])


def pdf_130() -> bytes:
    page2 = (
        _words("Ejercicio", 375, 105, T) + _words("Período", 490, 105, T)
        + [(427, 106, "2026", V), (535, 106, "2T", V)]
        + [(464, 230, "01", T), (527, 232, "2.000,00", V)]
        + [(464, 242, "02", T), (527, 243, "1.000,00", V)]
        + [(109, 255, "01", T), (128, 255, "02", T), (464, 254, "03", T), (527, 255, "1.000,00", V)]
        + [(464, 266, "04", T), (534, 267, "200,00", V)]
        + [(464, 567, "19", T), (545, 567, "0,00", V)]
        # Template amounts inside explanatory text, in the template font, with a
        # box number to their left: they are not values of box 03 / 08.
        + _words("El 2 por 100 de 03 (máximo: 660,14 euros) o de 08 (máximo: 660,14 euros)", 86, 519, T)
        + [(464, 519, "16", T)]
    )
    return make_pdf([receipt_page("130", "1300000000001"), page2])


def pdf_349() -> bytes:
    page2 = (
        _words("Ejercicio (con 4 cifras)", 371, 162, T) + [(524, 162, "2026", V)]
        + _words("Período", 371, 186, T) + [(542, 186, "2T", V)]
        + [(458, 330, "01", T), (555, 331, "2", V)]
        + [(458, 354, "02", T), (533, 354, "150,00", V)]
    )
    header = [(36, "Código"), (62, "país"), (80, "NIF"), (96, "comunitario"), (158, "Apellidos"),
              (192, "y"), (200, "nombre"), (462, "Clave"), (483, "Base"), (505, "imponible")]

    def block(top: float, n: int, country: str, vat: str, name: str, key: str, base: str) -> list:
        rows = (
            _words(f"Operador {n}", 38, top - 14, T)
            + [(x, top, text, T) for x, text in header]
            + _words("A cumplimentar exclusivamente en caso de clave C", 36, top + 25, T)
        )
        if vat:
            rows += [(40, top + 11, country, V), (82, top + 11, vat, V),
                     (466, top + 10, key, V), (532, top + 11, base, V)]
            rows += _words(name, 161, top + 12, V)
        return rows

    page3 = (
        block(113, 1, "IE", "0000000XX", "ACME TEST LIMITED", "I", "100,00")
        + block(195, 2, "DE", "000000000", "BEISPIEL GMBH", "I", "50,00")
        + block(278, 3, "", "", "", "", "")  # empty slot
    )
    return make_pdf([receipt_page("349", "3490000000001"), page2, page3])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_parse_amount_and_period():
    assert parse_amount("-1.234,56") == pytest.approx(-1234.56)
    assert parse_amount("0,55") == pytest.approx(0.55)
    assert period_to_quarter("2T") == 2
    assert period_to_quarter("0A") is None
    assert period_to_quarter("07") is None


def _w(text: str, x0: float, top: float, font: str = "Courier", size: float = 9.0) -> dict:
    return {"text": text, "x0": x0, "x1": x0 + 5 * len(text), "top": top,
            "bottom": top + size, "fontname": font}


class TestPairBoxes:
    def test_nearest_label_to_the_left_on_the_same_row(self):
        words = [
            _w("10", 296, 508, "ABCDEF+Tmpl", 6.5), _w("111,11", 364, 507),
            _w("11", 461, 508, "ABCDEF+Tmpl", 6.5), _w("23,33", 534, 507),
        ]
        assert pair_boxes(words) == {"10": 111.11, "11": 23.33}

    def test_row_tolerance_rejects_labels_on_other_rows(self):
        words = [_w("01", 464, 230, "Tmpl", 6.5), _w("02", 464, 242, "Tmpl", 6.5),
                 _w("5.000,00", 527, 243)]
        assert pair_boxes(words) == {"02": 5000.0}

    def test_template_font_amounts_and_skip_boxes_are_ignored(self):
        words = [
            _w("03", 137, 519, "XYZABC+Tmpl", 7), _w("660,14", 178, 519, "QWERTY+Tmpl", 7),
            _w("08", 397, 498, "XYZABC+Tmpl", 6.5), _w("21,00", 427, 496),
        ]
        assert detect_template_font(words) == "Tmpl"
        assert pair_boxes(words, template_font="Tmpl", skip_boxes={"08"}) == {}

    def test_single_font_documents_are_not_filtered(self):
        words = [_w("27", 460, 620, "Same"), _w("12,34", 534, 620, "Same")]
        assert detect_template_font(words) is None
        assert pair_boxes(words, template_font=None) == {"27": 12.34}

    def test_integer_values_only_for_int_boxes(self):
        words = [_w("01", 458, 330, "Tmpl", 7), _w("3", 555, 331),
                 _w("05", 100, 400, "Tmpl", 7), _w("7", 200, 400)]
        assert pair_boxes(words, template_font="Tmpl", int_boxes={"01"}) == {"01": 3.0}


# ---------------------------------------------------------------------------
# Whole-PDF parsing (synthetic receipts)
# ---------------------------------------------------------------------------

def test_parse_303_header_and_every_box():
    r = parse_pdf(io.BytesIO(pdf_303()), "synthetic_303.pdf")
    assert (r.model, r.year, r.period, r.quarter) == ("303", 2026, "2T", 2)
    assert r.justificante == "3030000000001"
    assert r.csv == "TESTCSV000000000"
    assert r.presented_at == "2026-07-17T09:20:53"
    assert r.presenter_role == "Colaborador"
    assert r.presenter == "00000000T GESTOR FICTICIO"
    assert r.boxes == {
        "10": 111.11, "11": 23.33, "27": 23.33, "28": 1000.0, "29": 210.0,
        "46": -186.67, "60": 4000.0, "110": 50.0, "87": 50.0, "71": -186.67, "72": 186.67,
    }


def test_parse_130_ignores_template_amounts():
    r = parse_pdf(io.BytesIO(pdf_130()), "synthetic_130.pdf")
    assert (r.model, r.year, r.period) == ("130", 2026, "2T")
    assert r.boxes == {"01": 2000.0, "02": 1000.0, "03": 1000.0, "04": 200.0, "19": 0.0}


def test_parse_349_operators_and_count():
    r = parse_pdf(io.BytesIO(pdf_349()), "synthetic_349.pdf")
    assert (r.model, r.year, r.period) == ("349", 2026, "2T")
    assert r.boxes == {"01": 2.0, "02": 150.0}
    assert [(o.seq, o.country, o.vat_id, o.name, o.key, o.base) for o in r.operators] == [
        (1, "IE", "0000000XX", "ACME TEST LIMITED", "I", 100.0),
        (2, "DE", "000000000", "BEISPIEL GMBH", "I", 50.0),
    ]


def test_non_receipt_and_unsupported_model_are_rejected():
    invoice = make_pdf([_words("Factura 2026-001 Total 100,00", 50, 100, T)])
    with pytest.raises(FiledReturnParseError, match="not an AEAT presentation receipt"):
        parse_pdf(io.BytesIO(invoice), "invoice.pdf")
    other = make_pdf([receipt_page("200", "2000000000001")])
    with pytest.raises(FiledReturnParseError, match="unsupported model"):
        parse_pdf(io.BytesIO(other), "m200.pdf")


# ---------------------------------------------------------------------------
# Storage, idempotency, validator integration, CLI
# ---------------------------------------------------------------------------

@pytest.fixture
def conn(tmp_path):
    c = get_connection(tmp_path / "filed.db")
    yield c
    c.close()


def _count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_reimport_is_a_noop_and_later_receipt_replaces(conn):
    assert import_pdf(conn, io.BytesIO(pdf_303()), "a.pdf").status == "imported"
    rows = _count(conn, "filed_returns")
    assert rows == 11
    assert import_pdf(conn, io.BytesIO(pdf_303()), "a.pdf").status == "unchanged"
    assert _count(conn, "filed_returns") == rows

    # A rectifying receipt presented later replaces the period's values…
    newer = pdf_303("3030000000002", "20-07-2026 a las 10:00:00", base_10="222,22")
    assert import_pdf(conn, io.BytesIO(newer), "b.pdf").status == "replaced"
    assert load_filed_boxes(conn, "303", 2026, "2T")["10"] == pytest.approx(222.22)
    assert _count(conn, "filed_returns") == rows
    # …and an older one never overwrites a newer one.
    older = pdf_303("3030000000000", "01-07-2026 a las 10:00:00", base_10="999,99")
    assert import_pdf(conn, io.BytesIO(older), "c.pdf").status == "superseded"
    assert load_filed_boxes(conn, "303", 2026, "2T")["10"] == pytest.approx(222.22)


def test_349_operators_are_stored_and_replaced_per_period(conn):
    import_pdf(conn, io.BytesIO(pdf_349()), "m349.pdf")
    assert _count(conn, "filed_349_operators") == 2
    assert import_pdf(conn, io.BytesIO(pdf_349()), "m349.pdf").status == "unchanged"
    assert _count(conn, "filed_349_operators") == 2
    (filing,) = load_filings(conn)
    assert filing["values"] == {"01_total_operators": 2.0, "02_total_amount": 150.0}
    assert [o["vat_id"] for o in filing["operators"]] == ["0000000XX", "000000000"]


def test_validator_prefers_db_and_falls_back_to_yaml(conn, monkeypatch):
    import_pdf(conn, io.BytesIO(pdf_130()), "m130.pdf")
    yaml_filings = [
        {"model": "130", "year": 2026, "quarter": 2, "filed_date": "2026-07-17",
         "values": {"01_ingresos_ytd": 1.0}},
        {"model": "130", "year": 2026, "quarter": 1, "filed_date": "2026-04-20",
         "values": {"01_ingresos_ytd": 5.0}},
    ]
    monkeypatch.setattr(tax_validator, "_load_yaml_filings", lambda: yaml_filings)
    filings = tax_validator._load_filings(conn)
    by_q = {f["quarter"]: f for f in filings if f["model"] == "130"}
    assert by_q[2]["source"] == "db"
    assert by_q[2]["values"]["01_ingresos_ytd"] == pytest.approx(2000.0)
    # Boxes left blank on the filed form read as zero.
    assert by_q[2]["values"]["05_trimestres_anteriores"] == 0.0
    assert by_q[2]["filed_date"] == "2026-07-17"
    assert by_q[1]["source"] == "yaml"
    assert len(filings) == 2


def test_cli_imports_a_folder_recursively(tmp_path, capsys):
    folder = tmp_path / "impuestos" / "2026T2"
    folder.mkdir(parents=True)
    (folder / "303.PDF").write_bytes(pdf_303())
    (folder / "349.pdf").write_bytes(pdf_349())
    (folder / "notes.txt").write_text("not a pdf")
    (tmp_path / "impuestos" / "invoice.pdf").write_bytes(
        make_pdf([_words("Factura 2026-001", 50, 100, T)])
    )
    db = tmp_path / "cli.db"
    assert main(["import", str(tmp_path / "impuestos"), "--db", str(db)]) == 0
    out = capsys.readouterr().out
    assert "imported   Modelo 303 2026 2T: 11 boxes" in out
    assert "imported   Modelo 349 2026 2T: 2 boxes, 2 operators" in out
    assert "skipped" in out and "invoice.pdf" in out
    assert "00000000T" not in out  # presenter is stored, never printed

    c = get_connection(db)
    try:
        assert len(import_paths(c, [tmp_path / "impuestos"])) == 3
        assert {f["model"] for f in load_filings(c)} == {"303", "349"}
    finally:
        c.close()
