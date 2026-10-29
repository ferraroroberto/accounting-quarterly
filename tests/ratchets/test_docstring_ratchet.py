"""The pre-ship gate's docstring-ratchet stage fails on a new finding and on "not confirmed".

`scripts/docstring_ratchet.py` is repo-local, not vendored: it closes the vendored
comment ratchet's documented blind spot (docstrings). The gate's `Invoke-Stage` fails
on any non-zero exit, so exit 1 (a new finding) and exit 3 (a file that could not be
read or parsed) both stop the gate, and neither is a pass. The script runs against
throwaway git trees, never this repo's own.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# Windows-only flag; 0 elsewhere keeps the suite runnable on macOS and Linux.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
GATE = REPO_ROOT / "scripts" / "verify-before-ship.ps1"
SCRIPT = REPO_ROOT / "scripts" / "docstring_ratchet.py"


def test_the_gate_runs_the_script_directly_on_the_whole_tree():
    lines = [
        line
        for line in GATE.read_text(encoding="utf-8").splitlines()
        if line.lstrip().startswith("Invoke-Stage") and "docstring_ratchet.py" in line
    ]
    assert len(lines) == 1
    assert '"docstring ratchet"' in lines[0]
    assert re.search(r'&\s+\$py\s+"scripts/docstring_ratchet\.py"\s*}', lines[0])


def test_the_script_is_not_vendored():
    """Unlike the other ratchet stages, there is no project-scaffolding source for this one."""
    config = (REPO_ROOT / ".fleet.toml").read_text(encoding="utf-8")
    assert "docstring_ratchet" not in config.split("[vendored]")[1].split("\n\n")[0]


def _tree(tmp_path: Path, files: dict[str, str], entries: str = "[]") -> Path:
    """A git tree holding the script, a baseline and `files`."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "docstring_ratchet.py").write_bytes(SCRIPT.read_bytes())
    (tmp_path / ".docstring-ratchet-baseline.json").write_text(
        f'{{"version": 1, "entries": {entries}}}\n', encoding="utf-8"
    )
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, creationflags=NO_WINDOW)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True, creationflags=NO_WINDOW)
    return tmp_path


def _run(tree: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "scripts/docstring_ratchet.py"],
        cwd=tree,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1", **(env or {})},
        timeout=60,
        creationflags=NO_WINDOW,
    )


def test_a_seeded_docstring_reference_fails_by_name(tmp_path):
    tree = _tree(tmp_path, {"seeded.py": '"""Fixed the FX bug (#9999)."""\nx = 1\n'})
    result = _run(tree)
    assert result.returncode == 1
    assert "seeded.py" in result.stdout
    assert "#9999" in result.stdout


def test_a_plain_comment_reference_never_counts(tmp_path):
    """Only docstrings are in scope here -- `# comments` are the vendored stage's job."""
    tree = _tree(tmp_path, {"seeded.py": "# fixed in #9999\nx = 1\n"})
    result = _run(tree)
    assert result.returncode == 0


def test_a_single_digit_step_label_never_counts(tmp_path):
    tree = _tree(tmp_path, {"seeded.py": '"""Groups are numbered #1, #2 in the review."""\nx = 1\n'})
    result = _run(tree)
    assert result.returncode == 0


def test_a_baselined_reference_does_not_fail(tmp_path):
    tree = _tree(
        tmp_path,
        {"seeded.py": '"""Fixed the FX bug (#9999)."""\nx = 1\n'},
        entries='[{"path": "seeded.py", "count": 1}]',
    )
    result = _run(tree)
    assert result.returncode == 0


def test_an_unparseable_file_is_not_confirmed(tmp_path):
    tree = _tree(tmp_path, {"broken.py": "x = (\n"})
    result = _run(tree)
    assert result.returncode == 3
    assert "NOT CONFIRMED" in result.stdout


def test_a_missing_baseline_fails_not_crashes(tmp_path):
    tree = _tree(tmp_path, {"clean.py": "x = 1\n"})
    (tree / ".docstring-ratchet-baseline.json").unlink()
    result = _run(tree)
    assert result.returncode == 1
    assert "--init" in result.stdout
