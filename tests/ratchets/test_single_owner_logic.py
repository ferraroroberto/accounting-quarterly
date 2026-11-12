"""Logic that feeds a declared figure has one owner; a second copy is a finding.

Each rule names the pattern that marks a second implementation and the modules
allowed to hold it. The five duplication findings of the audit each had two
copies that drifted apart without a test noticing, so the copies are banned
here instead of being kept aligned by hand.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

# (rule, directories scanned, regex of the second copy, files allowed to hold it)
RULES = [
    ("RETA / FX-difference rows are read through src.tax_data",
     ("src/returns", "src/pl_by_activity.py", "app"),
     r"SELECT\b[^;]{0,300}?\bFROM\s+(social_security_payments|fx_exchange_differences)\b", ()),
    ("a return identifies an expense vendor through expense_vendor_identity",
     ("src/returns",), r"\.match_invoice\(", ()),
    ("the UI shows filing deadlines from src.tax_deadlines, not typed dates",
     ("app",), r"[\"'](April|July|October|January) \d{1,2}\b", ()),
    ("the CLI renders the classification review, it does not recompute it",
     ("scripts/close_quarter.py",),
     r"\b(validate_classifications|eur_default_foreign_warning|compute_eu_b2c_threshold)\(", ()),
]


def _python_files(targets: tuple[str, ...]) -> list[Path]:
    files: list[Path] = []
    for target in targets:
        path = ROOT / target
        files.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
    return files


@pytest.mark.parametrize("rule,targets,pattern,allowed", RULES, ids=[r[0] for r in RULES])
def test_logic_has_one_owner(rule: str, targets: tuple[str, ...], pattern: str, allowed: tuple[str, ...]) -> None:
    regex = re.compile(pattern, re.DOTALL)
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _python_files(targets)
        if path.relative_to(ROOT).as_posix() not in allowed and regex.search(path.read_text(encoding="utf-8"))
    ]
    assert not offenders, f"{rule}: second copy in {offenders}"
