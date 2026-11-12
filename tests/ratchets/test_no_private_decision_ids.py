"""A comment states its rule in place; it never cites an id defined nowhere in the repo.

A tax-engine comment such as ``(D5)`` points the reader at a decision they cannot
resolve, where the article or treatment it stands for is exactly what they need on
the line. Comments and docstrings in product code (``src/``, ``app/``, ``scripts/``)
name the rule, or link the ``docs/tax-conventions.md`` section that states it.
Phrases that narrate how a module came to be belong in ``git log``, not in a docstring.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_DIRS = ("src", "app", "scripts")
# Vendored from project-scaffolding byte-for-byte; edited only by re-vendoring.
VENDORED_SUFFIX = "_ratchet.py"

# `(D5)`, `, D11)`, `decision D5`: a bare D<n> id in a comment or docstring.
DECISION_ID = re.compile(r"(?<![\w.])D\d{1,2}(?![\w.])")
HISTORY_PHRASES = re.compile(
    r"\beach\s+used\s+to\b|\bfactors\s+that\s+pattern\b|\bprivate\s+decision\b",
    re.IGNORECASE,
)


def _product_files() -> list[Path]:
    return sorted(
        path
        for name in PRODUCT_DIRS
        for path in (ROOT / name).rglob("*.py")
        if not path.name.endswith(VENDORED_SUFFIX)
    )


def _prose(path: Path) -> list[tuple[int, str]]:
    """(line, text) of every comment and docstring in the file."""
    source = path.read_text(encoding="utf-8")
    found: list[tuple[int, str]] = []
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            found.append((tok.start[0], tok.string))
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                found.append((node.body[0].lineno, doc))
    return found


def test_product_comments_cite_no_private_decision_id_or_history_phrase():
    offenders = [
        f"{path.relative_to(ROOT).as_posix()}:{line}"
        for path in _product_files()
        for line, text in _prose(path)
        if DECISION_ID.search(text) or HISTORY_PHRASES.search(text)
    ]
    assert not offenders, "state the rule in present tense instead: " + ", ".join(offenders)
