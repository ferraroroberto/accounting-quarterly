r"""Ratchet on #NNN issue references inside Python docstrings (repo-local, not vendored).

`scripts/comment_ratchet.py` (vendored byte-for-byte from project-scaffolding) counts
only `#` *comment* tokens -- its own header says "Docstrings ... are out of scope" by
design, because that rule's text is owned centrally and a change to it needs a
fleet-wide vendoring pass, not a per-repo patch. This repo's own audit (the recurring
`claude-md-drift` findings) found the identical decision-history smell leaking into
module/class/function docstrings instead, exactly where the vendored scanner never
looks. This stage closes that gap for this repo: it counts `#NNN` references (`N` is
2-5 digits, not preceded by another `#`) inside every docstring `ast.get_docstring`
returns, per tracked `.py` file, compares the count with a committed baseline, and
fails only when a file's count *rises*. Existing debt never blocks work, but it cannot
grow.

What is counted -- `ast.get_docstring` on the module, every class and every function
(sync or async); a 2-5 digit `#NNN` or `repo-name#NNN` reference. A single-digit marker
(`#1`, `#2`, as in "groups are numbered #1, #2") never counts -- this repo's real issue
numbers all run 2-5 digits, and a step/group list legitimately uses `#N` as a label.

Scope -- tracked Python files only (`git ls-files --cached --others --exclude-standard`,
so ignored and generated files never appear), minus `tests/`, `test_*.py`, `*_test.py`,
`conftest.py`, and every `dest` in this repo's own `.fleet.toml` `[vendored]` table
(those files quote the vendored rule's own text, not this repo's history).

Exit codes (a state is never folded into a pass):

  0  pass, or no tracked `.py` files found
  1  a file's count rose, a new file carries references, or the baseline is missing
  2  bad baseline or arguments -- fail loud
  3  NOT CONFIRMED: git is unusable, or a file could not be read or parsed. The gate
     fails on it, the same as a real finding.

Modes: `check` (default; what `scripts/verify-before-ship.ps1` runs), `--init` (write
the first baseline, refuses to overwrite), `--update-baseline` (lower or drop entries;
exits 1 and writes nothing while any count rose), `--update-baseline --allow-additions`
(the explicit flag that raises or adds a file -- shows up in review).
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_BASELINE = ".docstring-ratchet-baseline.json"
BASELINE_VERSION = 1
GIT_TIMEOUT_S = 30
# Windows-only flag; 0 elsewhere keeps the script runnable on macOS and Linux.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# `#` + 2-5 digits, not preceded by another `#` -- so a markdown-style `##` heading,
# and this repo's own single-digit step labels, never count.
_REF = re.compile(r"(?<!#)#\d{2,5}\b")

_EXCLUDED_NAMES = {"conftest.py"}


class NotConfirmed(RuntimeError):
    """git is unusable, or a tracked file could not be read or parsed."""


class ConfigError(RuntimeError):
    """The baseline file is unusable, or the arguments are bad."""


def _vendored_dests() -> set[str]:
    toml_path = REPO_ROOT / ".fleet.toml"
    if not toml_path.is_file():
        return set()
    config = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    return {entry["dest"] for entry in config.get("vendored", {}).values() if "dest" in entry}


def _is_excluded(rel_path: str, vendored: set[str]) -> bool:
    if rel_path in vendored:
        return True
    parts = Path(rel_path).parts
    if "tests" in parts:
        return True
    name = Path(rel_path).name
    if name in _EXCLUDED_NAMES or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return False


def tracked_python_files() -> list[str]:
    """Every tracked-or-untracked-but-not-ignored `.py` path this stage covers, sorted."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "*.py"],
            cwd=REPO_ROOT, capture_output=True, env=env,
            stdin=subprocess.DEVNULL, creationflags=NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git ls-files timed out after {GIT_TIMEOUT_S}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git ls-files exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    vendored = _vendored_dests()
    names = [n for n in proc.stdout.decode("utf-8", "replace").split("\0") if n]
    return sorted(n for n in names if not _is_excluded(n, vendored))


def _docstrings(tree: ast.Module) -> list[str]:
    docs = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                docs.append(doc)
    return docs


def count_refs(rel_path: str) -> list[str]:
    """Every `#NNN`-shaped reference found across `rel_path`'s docstrings."""
    full = REPO_ROOT / rel_path
    try:
        text = full.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise NotConfirmed(f"{rel_path}: cannot read file: {exc}") from exc
    try:
        tree = ast.parse(text, filename=rel_path)
    except SyntaxError as exc:
        raise NotConfirmed(f"{rel_path}: cannot parse: {exc}") from exc
    matches: list[str] = []
    for doc in _docstrings(tree):
        matches.extend(_REF.findall(doc))
    return matches


def count_tree(paths: list[str]) -> dict[str, list[str]]:
    return {path: count_refs(path) for path in paths}


# ---------------------------------------------------------------- baseline

def read_baseline(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ConfigError(f"baseline {path}: `entries` must be a list")
    result: dict[str, int] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("count"), int)
        ):
            raise ConfigError(f"baseline {path}: malformed entry {entry!r}")
        result[entry["path"]] = entry["count"]
    return result


def render_baseline(counts: dict[str, int]) -> str:
    """Sorted, one entry per line, no match text, so a baseline diff reviews cleanly."""
    entries = [
        f'    {{"path": {json.dumps(path)}, "count": {count}}}'
        for path, count in sorted(counts.items()) if count
    ]
    body = "[\n" + ",\n".join(entries) + "\n  ]" if entries else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def diff(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    rose = sorted(p for p, c in current.items() if c > baseline.get(p, 0))
    shrank = sorted(p for p, b in baseline.items() if current.get(p, 0) < b)
    return rose, shrank


def _report_rose(rose: list[str], current: dict[str, list[str]], baseline: dict[str, int]) -> None:
    print(f"docstring ratchet: {len(rose)} file(s) with MORE #NNN docstring references than the baseline:")
    for path in rose:
        was = baseline.get(path)
        print(f"  {path}: {len(current[path])} (baseline {was if was is not None else 'none -- not baselined'})"
              f" -- {', '.join(current[path])}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=DEFAULT_BASELINE)
    parser.add_argument("--init", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--allow-additions", action="store_true")
    args = parser.parse_args(argv)

    baseline_path = REPO_ROOT / args.baseline
    try:
        paths = tracked_python_files()
        refs = count_tree(paths)
        current = {path: len(matches) for path, matches in refs.items()}

        if args.init:
            if baseline_path.exists():
                print(f"docstring ratchet: {args.baseline} already exists; --init never overwrites it")
                return 2
            baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
            print(f"docstring ratchet: wrote {args.baseline} with {sum(1 for c in current.values() if c)} "
                  f"file(s), {sum(current.values())} reference(s)")
            return 0

        if not baseline_path.is_file():
            print(f"docstring ratchet: FAIL: baseline {args.baseline} is missing "
                  f"-- run `python scripts/docstring_ratchet.py --init`")
            return 1
        baseline = read_baseline(baseline_path)

        if args.update_baseline:
            rose, _shrank = diff(current, baseline)
            if rose and not args.allow_additions:
                _report_rose(rose, refs, baseline)
                print("docstring ratchet: nothing written -- remove the references, "
                      "or raise the baseline with --allow-additions")
                return 1
            baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
            print(f"docstring ratchet: {args.baseline} now has {sum(1 for c in current.values() if c)} "
                  f"file(s), {sum(current.values())} reference(s)")
            return 0

        rose, _shrank = diff(current, baseline)
        if rose:
            _report_rose(rose, refs, baseline)
            print("docstring ratchet: reword the docstring instead of raising the baseline")
            return 1
        print(f"docstring ratchet: OK ({len(paths)} file(s) checked)")
        return 0
    except NotConfirmed as exc:
        print(f"docstring ratchet: NOT CONFIRMED: {exc}")
        return 3
    except ConfigError as exc:
        print(f"docstring ratchet: FAIL: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
