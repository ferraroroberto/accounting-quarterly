"""The gate's docs-ratchet stage maps the vendored script's exit codes.

`scripts/docs_ratchet.py` exits 3 when it could not establish the count (git
unusable, a non-UTF-8 file, a fence or comment that never closes): nothing was
checked. These tests pin that 3 fails as NOT CONFIRMED -- never a pass, never a
skip -- and that the pre-ship gate calls the wrapper that guarantees it rather
than the script. They run fake scripts in a temp directory.
"""
import subprocess
import tomllib
from pathlib import Path

import pytest

from scripts import docs_ratchet_gate as gate

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def exits_with(tmp_path):
    """Build a script that prints a finding and exits with the given code."""

    def build(code: int) -> Path:
        script = tmp_path / "fake_docs_ratchet.py"
        script.write_text(
            f"print('a finding from the script')\nraise SystemExit({code})\n",
            encoding="utf-8",
        )
        return script

    return build


def test_only_exit_zero_passes():
    assert gate.classify_exit(0)[0] is True
    assert not any(gate.classify_exit(code)[0] for code in (1, 2, 3, 4, -1))


def test_exit_three_is_not_confirmed_and_never_a_skip():
    ok, label = gate.classify_exit(3)
    assert not ok
    assert "NOT CONFIRMED" in label
    assert "skip" not in label.lower()


@pytest.mark.parametrize(("code", "expected"), [(0, 0), (1, 1), (2, 1), (3, 1)])
def test_the_stage_fails_on_every_non_zero_script_exit(exits_with, capsys, code, expected):
    assert gate.run(exits_with(code)) == expected
    out = capsys.readouterr().out
    assert "a finding from the script" in out  # the script's own message is echoed
    verdict = out.rstrip().splitlines()[-1]
    assert verdict.startswith("OK: docs ratchet" if code == 0 else "FAIL: docs ratchet")


def test_a_script_that_hangs_is_not_confirmed(exits_with, capsys, monkeypatch):
    def hang(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docs_ratchet.py", 1)

    monkeypatch.setattr(subprocess, "run", hang)
    assert gate.run(exits_with(0)) == 1
    assert "NOT CONFIRMED" in capsys.readouterr().out.rstrip().splitlines()[-1]


def test_the_pre_ship_gate_calls_the_wrapper_not_the_script():
    gate_ps1 = (REPO_ROOT / "scripts" / "verify-before-ship.ps1").read_text(encoding="utf-8")
    stage_lines = [
        line for line in gate_ps1.splitlines() if line.lstrip().startswith("Invoke-Stage")
    ]
    ratchet = [line for line in stage_lines if "docs_ratchet" in line]
    assert len(ratchet) == 1
    assert "-m scripts.docs_ratchet_gate" in ratchet[0]
    assert "docs_ratchet.py" not in ratchet[0]


def test_the_ratchet_is_declared_and_its_script_is_vendored():
    fleet = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    assert fleet["docs_ratchet"]["enabled"] is True
    assert (REPO_ROOT / fleet["docs_ratchet"]["baseline"]).is_file()
    entry = fleet["vendored"]["docs_ratchet"]
    assert entry["dest"] == "scripts/docs_ratchet.py"
    assert (REPO_ROOT / entry["dest"]).is_file()
