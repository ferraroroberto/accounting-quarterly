"""The pre-ship gate ends every run with a `GATE-RESULT: PASS|FAIL` stdout line.

A pipeline reports its last command's status, so `verify-before-ship.ps1 | tail`
hands the caller `tail`'s 0 and a red gate reads green. The verdict line is the
part a pipe cannot rewrite. The helper block in the gate (between the
`gate-verdict` markers) is run for real, over a captured stdout (the same
anonymous pipe `|` creates), for a pass, a failed stage and an early terminating
error; the real script is run for the paths that need no stage to execute.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# Windows-only flag; 0 elsewhere keeps the suite importable on macOS and Linux.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GATE = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
BOX_LOCK = REPO_ROOT / "scripts" / "box_lock_gate.ps1"
POWERSHELL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="the gate is a Windows PowerShell script")


def _verdict_block() -> str:
    text = GATE.read_text(encoding="utf-8")
    match = re.search(r"# gate-verdict:begin\n(.*?)# gate-verdict:end", text.replace("\r\n", "\n"), re.DOTALL)
    assert match, "the gate lost its gate-verdict block"
    return match.group(1)


def _run(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=120, env=env,
    )


def _run_scenario(scenario: str) -> subprocess.CompletedProcess[str]:
    script = f'$ErrorActionPreference = "Stop"\n{_verdict_block()}\n{scenario}\n'
    return _run(["-Command", script])


def _last_line(run: subprocess.CompletedProcess[str]) -> str:
    lines = [line.rstrip() for line in run.stdout.splitlines() if line.strip()]
    assert lines, f"nothing on stdout; stderr={run.stderr!r}"
    return lines[-1]


@windows_only
def test_pass_ends_with_pass_and_exits_zero() -> None:
    run = _run_scenario('Invoke-Stage "ok" { cmd /c exit 0 }\nComplete-Gate 0')
    assert run.returncode == 0, run.stdout
    assert _last_line(run) == "GATE-RESULT: PASS"


@windows_only
def test_failed_stage_ends_with_fail_and_exits_one() -> None:
    run = _run_scenario('Invoke-Stage "bad" { cmd /c exit 7 }\nComplete-Gate 0')
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert "[FAIL] bad (exit 7)" in run.stdout


@windows_only
def test_early_terminating_error_reports_on_stdout_then_fails() -> None:
    run = _run_scenario('throw "boom before any stage"')
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert "boom before any stage" in run.stdout, "the error must reach stdout, not only stderr"


def _gate_copy(root: Path) -> Path:
    scripts = root / "scripts"
    scripts.mkdir()
    shutil.copy2(GATE, scripts / GATE.name)
    shutil.copy2(BOX_LOCK, scripts / BOX_LOCK.name)
    return scripts / GATE.name


def _isolated_env(root: Path) -> dict[str, str]:
    # An empty profile has no box_lock.py, so the gate runs unlocked and never reaches the real helper.
    return {**os.environ, "USERPROFILE": str(root)}


@windows_only
def test_missing_venv_ends_with_fail_and_exits_one(tmp_path: Path) -> None:
    run = _run(["-File", str(_gate_copy(tmp_path))], env=_isolated_env(tmp_path))
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"
    assert ".venv not found" in run.stdout


@windows_only
def test_unrunnable_interpreter_is_reported_as_fail_not_a_silent_run(tmp_path: Path) -> None:
    """A terminating error in the real script: the interpreter path exists but is not an executable."""
    gate = _gate_copy(tmp_path)
    interpreter = tmp_path / ".venv" / "Scripts" / "python.exe"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("not an executable", encoding="utf-8")
    run = _run(["-File", str(gate)], env=_isolated_env(tmp_path))
    assert run.returncode == 1, run.stdout
    assert _last_line(run) == "GATE-RESULT: FAIL"


def test_every_exit_goes_through_the_verdict() -> None:
    """A new `exit` that skips `Complete-Gate` would end a run with no verdict."""
    text = GATE.read_text(encoding="utf-8").replace("\r\n", "\n")
    outside = text.replace(_verdict_block(), "")
    assert not re.search(r"^\s*exit\s+\d", outside, re.MULTILINE), "exit through Complete-Gate, not directly"
    assert text.rstrip().endswith("Complete-Gate 0"), "the pass path must end on its verdict"


def test_verdict_block_is_ascii_for_windows_powershell_5_1() -> None:
    """Windows PowerShell 5.1 reads a BOM-less file as ANSI, so the block stays ASCII."""
    block = _verdict_block()
    block.encode("ascii")
