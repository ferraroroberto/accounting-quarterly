"""Repeated-text finder for the instruction ratchet's S-04 rule.

A CLAUDE.md that restates a doc or the global instructions word for word pays for the same text in
every session, then drifts from the original. This finds the runs of `n` or more consecutive words
that a document shares with a source, comparing lowercase alphanumeric words so wrapping, markdown
marks and punctuation do not hide a copy. Overlapping windows merge into one run, so a pasted
paragraph is one hit however long it is.

Loaded by `instruction_ratchet.py` only when `repeated_text_words` is set (manifest key
`instruction_repeats`), stdlib-only. Mechanism and limits: docs/instruction-ratchet.md.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

_WORD = re.compile(r"[^\W_]+")

Lines = list[tuple[int, str]]  # (1-based line number, text): the prose a caller chose to compare


@dataclass(frozen=True)
class Run:
    line: int  # first line of the run in the compared document
    words: int
    source: str  # the source the run was found in


def _words(lines: Lines) -> list[tuple[str, int]]:
    return [(w, no) for no, text in lines for w in _WORD.findall(text.lower())]


def _key(window: list[str]) -> bytes:
    return hashlib.blake2b(" ".join(window).encode("utf-8"), digest_size=8).digest()


def index_sources(sources: dict[str, Lines], n: int) -> dict[bytes, str]:
    """Every `n`-word window of every source -> the first source (by path) holding it."""
    found: dict[bytes, str] = {}
    for path in sorted(sources):
        words = [w for w, _ in _words(sources[path])]
        for i in range(len(words) - n + 1):
            found.setdefault(_key(words[i:i + n]), path)
    return found


def repeated_runs(lines: Lines, index: dict[bytes, str], n: int) -> list[Run]:
    """The maximal runs of `lines` whose every `n`-word window is in `index`, in document order."""
    words = _words(lines)
    runs: list[Run] = []
    start: int | None = None
    source = ""
    for i in range(len(words) - n + 1):
        hit = index.get(_key([w for w, _ in words[i:i + n]]))
        if hit is not None and start is None:
            start, source = i, hit
        elif hit is None and start is not None:
            runs.append(Run(words[start][1], i - 1 - start + n, source))
            start = None
    if start is not None:
        runs.append(Run(words[start][1], len(words) - start, source))
    return runs
