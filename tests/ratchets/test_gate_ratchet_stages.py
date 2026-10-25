"""The pre-ship gate's vendored ratchet stages fail on a new finding and on "not confirmed".

Each stage runs the vendored script directly: `Invoke-Stage` fails on any non-zero
exit, so exit 1 (a new finding) and exit 3 (a detector that could not run) both stop
the gate, and neither is a pass. The scripts run against throwaway git trees, never
this repo's own.
"""
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / "scripts" / "verify-before-ship.ps1"


def _gate_stage_lines() -> list[str]:
    return [
        line
        for line in GATE.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("Invoke-Stage")
    ]


@pytest.mark.parametrize("name", ["static", "comment"])
def test_the_gate_runs_the_script_directly_on_the_whole_tree(name):
    script = f"{name}_ratchet.py"
    stages = [line for line in _gate_stage_lines() if script in line]
    assert len(stages) == 1
    assert f'"{name} ratchet"' in stages[0]
    assert re.search(rf'&\s+\$py\s+"scripts/{script}"\s*}}', stages[0])


def test_invoke_stage_fails_on_any_non_zero_exit():
    gate = GATE.read_text(encoding="utf-8")
    assert "$LASTEXITCODE -ne 0" in gate
    assert not re.search(r"\$LASTEXITCODE\s+-eq\s+[123]\b", gate)


@pytest.mark.parametrize("name", ["static", "comment"])
def test_the_script_is_vendored_and_enabled(name):
    config = tomllib.loads((REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8"))
    entry = config["vendored"][f"{name}_ratchet"]
    assert entry["dest"] == f"scripts/{name}_ratchet.py"
    assert re.fullmatch(r"[0-9a-f]{40}", entry["sha"])
    assert (REPO_ROOT / entry["dest"]).is_file()
    assert config[f"{name}_ratchet"]["enabled"] is True


def _tree(tmp_path: Path, name: str, files: dict[str, str]) -> Path:
    """A git tree holding the vendored script, an empty baseline and `files`."""
    (tmp_path / "scripts").mkdir()
    script = f"scripts/{name}_ratchet.py"
    (tmp_path / script).write_bytes((REPO_ROOT / script).read_bytes())
    (tmp_path / ".fleet.toml").write_text(f"[{name}_ratchet]\nenabled = true\n", encoding="utf-8")
    baseline = f".{name}-ratchet-baseline.json"
    (tmp_path / baseline).write_text('{"version": 1, "entries": []}\n', encoding="utf-8")
    for rel, text in files.items():
        (tmp_path / rel).write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def _run(tree: Path, name: str, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, f"scripts/{name}_ratchet.py"],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1", **(env or {})},
        timeout=300,
    )


def test_a_seeded_unused_import_fails_static_by_name(tmp_path):
    tree = _tree(tmp_path, "static", {"seeded.py": "import os\n"})
    result = _run(tree, "static")
    assert result.returncode == 1
    assert "seeded.py" in result.stdout
    assert "F401" in result.stdout


def test_a_missing_detector_is_not_confirmed_never_a_pass(tmp_path):
    tree = _tree(tmp_path, "static", {"clean.py": "x = 1\n"})
    system32 = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32")
    result = _run(tree, "static", {"PATH": system32})
    assert result.returncode == 3
    assert "NOT CONFIRMED" in result.stdout


def test_a_seeded_issue_reference_fails_comment_by_name(tmp_path):
    tree = _tree(tmp_path, "comment", {"seeded.py": "# fixed in #123\nx = 1\n"})
    result = _run(tree, "comment")
    assert result.returncode == 1
    assert "seeded.py" in result.stdout
    assert "#123" in result.stdout


def test_an_unscannable_file_is_not_confirmed_for_comment(tmp_path):
    tree = _tree(tmp_path, "comment", {"broken.py": "x = (\n"})
    result = _run(tree, "comment")
    assert result.returncode == 3
    assert "NOT CONFIRMED" in result.stdout
