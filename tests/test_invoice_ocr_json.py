"""Characterization tests for ``src.invoice_ocr._parse_json_object`` (#192).

Pins how a model reply is turned into a dict — fences, surrounding prose,
nested braces, braces/escapes inside strings, and the failure modes — so the
hand-rolled scanner can be swapped for ``json.JSONDecoder.raw_decode``
without changing behaviour. Invented replies only; no hub or OCR call.
"""
from __future__ import annotations

import pytest

from src.invoice_ocr import _parse_json_object


@pytest.mark.parametrize(
    "reply, expected",
    [
        pytest.param('{"vendor": "Acme", "total_eur": 12.5}',
                     {"vendor": "Acme", "total_eur": 12.5}, id="plain"),
        pytest.param('  \n{"a": 1}\n  ', {"a": 1}, id="surrounding-whitespace"),
        pytest.param('```json\n{"a": 1, "b": "x"}\n```',
                     {"a": 1, "b": "x"}, id="fenced-json"),
        pytest.param('```\n{"a": 1}\n```', {"a": 1}, id="fenced-bare"),
        pytest.param('Here is the data:\n{"a": 1}', {"a": 1}, id="prose-before"),
        pytest.param('{"a": 1}\nHope that helps!', {"a": 1}, id="prose-after"),
        pytest.param('Sure.\n```json\n{"a": 1}\n```\nDone.',
                     {"a": 1}, id="prose-around-fence"),
        pytest.param('{"a": {"b": {"c": [1, {"d": 2}]}}, "e": 3}',
                     {"a": {"b": {"c": [1, {"d": 2}]}}, "e": 3}, id="nested"),
        pytest.param('Result: {"a": {"b": 1}} trailing {not json}',
                     {"a": {"b": 1}}, id="nested-with-trailing-braces"),
        pytest.param('Note {"a": "has } and { inside"} end',
                     {"a": "has } and { inside"}, id="braces-in-string"),
        pytest.param('Note {"a": "say \\"hi\\" }"} end',
                     {"a": 'say "hi" }'}, id="escaped-quotes"),
        pytest.param('Note {"a": "back\\\\"} end',
                     {"a": "back\\"}, id="escaped-backslash-before-quote"),
        pytest.param('{"a": 1}{"b": 2}', {"a": 1}, id="first-of-two-objects"),
    ],
)
def test_parses_first_object(reply, expected):
    assert _parse_json_object(reply) == expected


@pytest.mark.parametrize("reply", ["", "   ", "no json here"])
def test_no_object_raises_value_error(reply):
    with pytest.raises(ValueError, match="no JSON object found"):
        _parse_json_object(reply)


def test_bare_array_reply_raises_value_error():
    # Deliberate change (#192): a top-level array used to be returned as-is
    # and crash the caller's ``data.get`` with AttributeError; it now fails
    # through the handled "non-JSON" path.
    with pytest.raises(ValueError, match="no JSON object found"):
        _parse_json_object("[1, 2, 3]")


@pytest.mark.parametrize("reply", ['{"a": 1','prose {"a": {"b": 1}'])
def test_unterminated_object_raises_value_error(reply):
    with pytest.raises(ValueError):
        _parse_json_object(reply)


def test_malformed_balanced_object_raises_value_error():
    with pytest.raises(ValueError):
        _parse_json_object('prose {not: valid} more')
