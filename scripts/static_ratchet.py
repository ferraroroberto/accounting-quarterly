"""Baseline-and-ratchet static analysis for the pre-ship gate (project-scaffolding#410).

Duplicated code and unused imports/variables regrow however often an audit
re-finds them. This stage turns that into structure: it computes the current
set of findings, compares it with a committed baseline, and fails only on a
finding that is *new* -- so existing debt never blocks work but cannot grow.

Three detectors, one key space:

  * **Duplicate blocks** (Python and JS together) -- `jscpd`, pinned to an exact
    version and run through `npx`. One tool covers both languages; a stdlib JS
    tokenizer would be a bug farm. Node is therefore a prerequisite for an
    adopter that opts in.
  * **Unused imports / variables** (Python only) -- `ruff --select F401,F841`,
    so the repo's own ruff config (excludes, per-file-ignores, `# noqa`) still
    applies. JS unused-code detection is deliberately out of scope.
  * **Likely bugs and security patterns** (Python only) -- a curated set of
    ruff `B` (flake8-bugbear) and `S` (flake8-bandit) codes, `DEFAULT_RULES`,
    run in the same ruff call. Each code is one a reviewer fixes rather than
    argues with; the codes left out, and why, are in docs/static-ratchet.md.

Findings are reduced to line-number-free keys so the baseline survives edits
above a finding: a duplicate is `dup:<sha1 of the whitespace-normalised
fragment>:<file>|<file>` (strict: editing the cloned block reads as one hit
gone plus one new), an unused name is `unused:<rule>:<file>:<symbol>`, a bug or
security finding is `lint:<rule>:<file>:<stripped source line>`; repeats of one
key in a file get a `#2`, `#3`... ordinal. jscpd reports a clone as an
ordered pair, and the fragment hashed is the first file's, so the key is stable
for as long as that order is -- it is deterministic for an unchanged tree.

Opt-in, per adopter, in its own `.fleet.toml` -- re-vendoring never breaks a
repo that has not opted in:

    [static_ratchet]
    enabled    = true
    baseline   = ".static-ratchet-baseline.json"   # default
    min_lines  = 10                                  # default
    min_tokens = 100                                 # default
    exclude    = ["**/_vendored/**"]                 # added to the built-in set
    ruff_rules = ["B006", "S113"]                    # replaces DEFAULT_RULES; [] = none

Every `dest` in the repo's own `[vendored]` table is excluded as well: a
vendor-verbatim copy is checked once, where it lives, and an adopter cannot edit
it to pay the debt down.

Exit codes (a state is never folded into a pass):

  0  pass, or the stage is not enabled (declared, not inferred)
  1  a new finding, or the baseline file is missing / a mode was refused
  2  bad configuration, baseline or arguments -- fail loud
  3  NOT CONFIRMED: a detector could not run (no Node, no network on first
     `npx`, ruff missing, unparseable output). The gate fails on it.

Modes: `check` (default; what the gate runs), `--init` (write the first
baseline, refuses to overwrite), `--update-baseline` (drop stale entries; exits 1
and writes nothing while any new finding exists), `--update-baseline
--allow-additions` (the explicit flag that adds entries -- shows in review).

Vendor-verbatim: copied byte-for-byte into adopters (manifest key
`static_ratchet`), stdlib-only. Full procedure and limits: docs/static-ratchet.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The exact jscpd release the gate runs. Never `latest`: a new release changes
#: what counts as a clone and would red-flag every adopter at once.
JSCPD_VERSION = "5.4.0"

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".static-ratchet-baseline.json"
DEFAULT_MIN_LINES = 10
DEFAULT_MIN_TOKENS = 100
#: Always excluded, whatever the adopter's own `exclude` adds.
BUILTIN_EXCLUDES = (
    "**/node_modules/**",
    "**/.venv/**",
    "**/venv/**",
    "**/.git/**",
    "**/__pycache__/**",
    "**/*.min.js",
)
JSCPD_FORMATS = "python,javascript"
#: Always selected: the unused-name detector, keyed `unused:`.
UNUSED_RULES = ("F401", "F841")
#: Bug (flake8-bugbear) and security (flake8-bandit) codes run unless the
#: adopter's `ruff_rules` replaces them, keyed `lint:`.
DEFAULT_RULES = (
    "B002",  # `++n` is two unary pluses, not an increment
    "B003",  # rebinding `os.environ` does not reach child processes
    "B004",  # `hasattr(x, "__call__")` misses callables; `callable()` does not
    "B005",  # `.strip("abc")` strips a character set, not a suffix
    "B006",  # a mutable default argument is shared by every call
    "B012",  # `return`/`break` in `finally` swallows the in-flight exception
    "B015",  # a comparison whose result is discarded is a missing `assert`
    "B016",  # raising a literal is a TypeError, not the intended error
    "B017",  # `pytest.raises(Exception)` passes on any error at all
    "B018",  # a statement with no effect is a typo or a missing call
    "B020",  # a loop variable that rebinds its own iterable
    "B021",  # an f-string docstring is not a docstring
    "B022",  # `contextlib.suppress()` with no exceptions suppresses nothing
    "B025",  # the second handler for the same exception is dead code
    "B026",  # `*args` after a keyword argument binds positionally first
    "B029",  # `except ():` catches nothing
    "B030",  # `except` on a non-exception class raises TypeError when reached
    "B031",  # a `groupby` group reused after the generator moved on is empty
    "B032",  # `x[k]: 1` is an annotation, not the intended assignment
    "B033",  # a set literal that repeats an item (often `0` and `False`)
    "B034",  # `re.sub` count/flags passed positionally are easy to swap
    "B035",  # a dict comprehension with a constant key keeps only the last item
    "B039",  # a mutable `ContextVar` default is shared by every context
    "S102",  # `exec` runs whatever string reaches it
    "S103",  # a world-writable `chmod`
    "S113",  # an HTTP call without a timeout hangs forever on a silent peer
    "S201",  # Flask `debug=True` serves the Werkzeug remote-code console
    "S307",  # `eval` of data is code execution; `ast.literal_eval` is not
    "S506",  # `yaml.load` without a safe loader builds arbitrary objects
    "S602",  # `shell=True` runs shell metacharacters inside an argument
    "S604",  # the same, through a non-subprocess call taking `shell=True`
    "S605",  # `os.system`/`os.popen` always go through a shell
    "S701",  # Jinja2 with autoescape off renders HTML injection
)
#: In tests, running code or a shell command is the test's own job on fixture
#: input, not an injection path, so these codes are not reported there.
TEST_GLOBS = ("**/tests/**", "**/test_*.py", "**/*_test.py", "**/conftest.py")
TEST_IGNORED_RULES = frozenset({"S102", "S307", "S602", "S604", "S605"})
_RULE_CODE = re.compile(r"[A-Z]+[0-9]+\Z")
TOOL_TIMEOUT_S = 600

EXIT_PASS, EXIT_NEW, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_FLEET_KEYS = {"enabled", "baseline", "min_lines", "min_tokens", "exclude", "ruff_rules"}


class ConfigError(RuntimeError):
    """The `[static_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A detector could not run, so the finding set is unknown."""


@dataclass(frozen=True)
class Config:
    baseline: str
    min_lines: int
    min_tokens: int
    exclude: tuple[str, ...]
    ruff_rules: tuple[str, ...] = DEFAULT_RULES


@dataclass(frozen=True)
class Finding:
    key: str
    kind: str  # "dup" | "unused" | "lint"
    files: tuple[str, ...]
    detail: str  # snippet or symbol, for the failure message only


# ----------------------------------------------------------------- config

def parse_config(toml_text: str) -> Config | None:
    """`[static_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key must not
    quietly run with defaults.
    """
    table = tomllib.loads(toml_text).get("static_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[static_ratchet] must be a table")
    unknown = sorted(set(table) - _FLEET_KEYS)
    if unknown:
        raise ConfigError(f"[static_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[static_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[static_ratchet] baseline must be a non-empty string")
    ints: dict[str, int] = {}
    for name, default in (("min_lines", DEFAULT_MIN_LINES), ("min_tokens", DEFAULT_MIN_TOKENS)):
        value = table.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"[static_ratchet] {name} must be a positive integer")
        ints[name] = value
    exclude = table.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(g, str) and g for g in exclude):
        raise ConfigError("[static_ratchet] exclude must be a list of non-empty glob strings")
    rules = table.get("ruff_rules", list(DEFAULT_RULES))
    if not isinstance(rules, list) or not all(isinstance(r, str) and _RULE_CODE.match(r) for r in rules):
        raise ConfigError('[static_ratchet] ruff_rules must be a list of ruff rule codes such as "S113"')
    return Config(baseline, ints["min_lines"], ints["min_tokens"], tuple(exclude), tuple(rules))


def vendored_globs(toml_text: str) -> tuple[str, ...]:
    """Exclusion globs for every `dest` in the repo's own `[vendored]` table.

    A dest may be a file or a folder, so each yields the path and everything under it.
    """
    table = tomllib.loads(toml_text).get("vendored")
    if not isinstance(table, dict):
        return ()
    globs: list[str] = []
    for entry in table.values():
        dest = entry.get("dest") if isinstance(entry, dict) else None
        clean = dest.replace("\\", "/").strip("/") if isinstance(dest, str) else ""
        if clean:
            globs += [clean, f"{clean}/**"]
    return tuple(sorted(globs))


def load_config(root: Path) -> Config | None:
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        cfg = parse_config(text)
        return cfg if cfg is None else replace(cfg, exclude=cfg.exclude + vendored_globs(text))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------- globs

def _glob_regex(glob: str) -> re.Pattern[str]:
    """Repo-root-anchored glob -> regex; `**/` is zero or more directories."""
    out: list[str] = []
    i = 0
    while i < len(glob):
        if glob.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def is_excluded(path: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_regex(g).match(path) for g in globs)


# --------------------------------------------------------------- findings

def _norm_path(name: str, root: Path) -> str:
    """A tool-reported path -> repo-relative, forward-slash."""
    p = Path(name)
    if p.is_absolute():
        try:
            p = p.resolve().relative_to(root.resolve())
        except ValueError:
            pass
    return p.as_posix().removeprefix("./")


def _ordinals(raw: list[tuple[str, int, Finding]]) -> list[Finding]:
    """Suffix repeats of one key (`#2`, `#3`...), ordered by source line."""
    seen: Counter[str] = Counter()
    out: list[Finding] = []
    for _, _, f in sorted(raw, key=lambda r: (r[0], r[1])):
        seen[f.key] += 1
        n = seen[f.key]
        key = f.key if n == 1 else f"{f.key}#{n}"
        out.append(Finding(key, f.kind, f.files, f.detail))
    return out


def parse_jscpd(report: object, root: Path, excludes: tuple[str, ...]) -> list[Finding]:
    """A jscpd JSON report -> duplicate findings."""
    if not isinstance(report, dict) or not isinstance(report.get("duplicates"), list):
        raise NotConfirmed("jscpd report has no `duplicates` list")
    raw: list[tuple[str, int, Finding]] = []
    for dup in report["duplicates"]:
        try:
            first, second = dup["firstFile"], dup["secondFile"]
            fragment = str(dup["fragment"])
            a = _norm_path(str(first["name"]), root)
            b = _norm_path(str(second["name"]), root)
            line = int(first["start"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NotConfirmed(f"jscpd report entry is malformed: {exc!r}") from exc
        if is_excluded(a, excludes) or is_excluded(b, excludes):
            continue
        digest = hashlib.sha1(" ".join(fragment.split()).encode("utf-8")).hexdigest()[:12]
        files = tuple(sorted((a, b)))
        snippet = " ".join(fragment.split())
        key = f"dup:{digest}:{files[0]}|{files[1]}"
        raw.append((key, line, Finding(key, "dup", files, snippet[:160])))
    return _ordinals(raw)


_SYMBOL = re.compile(r"`([^`]+)`")


def parse_ruff(items: object, root: Path, excludes: tuple[str, ...]) -> list[Finding]:
    """`ruff --output-format json` -> unused-name and bug/security findings."""
    if not isinstance(items, list):
        raise NotConfirmed("ruff output is not a JSON list")
    raw: list[tuple[str, int, Finding]] = []
    for item in items:
        try:
            rule = str(item["code"])
            filename = _norm_path(str(item["filename"]), root)
            message = str(item["message"])
            row = int(item["location"]["row"])
        except (KeyError, TypeError, ValueError) as exc:
            raise NotConfirmed(f"ruff output entry is malformed: {exc!r}") from exc
        if is_excluded(filename, excludes):
            continue
        if rule not in UNUSED_RULES:
            if rule in TEST_IGNORED_RULES and is_excluded(filename, TEST_GLOBS):
                continue
            # The source line, not the message: one message repeats for every hit.
            line = _source_line(root / filename, row) or message
            key = f"lint:{rule}:{filename}:{line}"
            raw.append((key, row, Finding(key, "lint", (filename,), f"{rule} {message}: {line}")))
            continue
        match = _SYMBOL.search(message)
        symbol = match.group(1) if match else _source_line(root / filename, row)
        key = f"unused:{rule}:{filename}:{symbol}"
        raw.append((key, row, Finding(key, "unused", (filename,), f"{rule} {message}")))
    return _ordinals(raw)


def _source_line(path: Path, row: int) -> str:
    """Fallback symbol when ruff's message names none: the stripped source line."""
    try:
        return path.read_text(encoding="utf-8").splitlines()[row - 1].strip()
    except (OSError, IndexError, UnicodeDecodeError):
        return ""


# --------------------------------------------------------------- detectors

def _run(cmd: list[str], cwd: Path, what: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW,
            timeout=TOOL_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"{what} timed out after {TOOL_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"{what} could not start: {exc}") from exc


def _tail(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " | ".join(lines[-3:]) if lines else "(no output)"


def run_jscpd(root: Path, cfg: Config) -> list[Finding]:
    npx = shutil.which("npx")
    if npx is None:
        raise NotConfirmed("npx not found -- Node is a prerequisite for the duplication check")
    excludes = BUILTIN_EXCLUDES + cfg.exclude
    with tempfile.TemporaryDirectory(prefix="static-ratchet-") as out_dir:
        proc = _run(
            [
                npx, "--yes", f"jscpd@{JSCPD_VERSION}", ".",
                "--reporters", "json", "--output", out_dir, "--silent",
                "--format", JSCPD_FORMATS,
                "--min-lines", str(cfg.min_lines), "--min-tokens", str(cfg.min_tokens),
                "--ignore", ",".join(excludes),
            ],
            root, "jscpd",
        )
        report_path = Path(out_dir) / "jscpd-report.json"
        if proc.returncode != 0 or not report_path.is_file():
            raise NotConfirmed(f"jscpd exited {proc.returncode} without a report: {_tail(proc.stderr or proc.stdout)}")
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise NotConfirmed(f"jscpd report unreadable: {exc}") from exc
    return parse_jscpd(report, root, excludes)


def run_ruff(root: Path, cfg: Config) -> list[Finding]:
    rules = ",".join(dict.fromkeys(UNUSED_RULES + cfg.ruff_rules))
    proc = _run(
        [sys.executable, "-m", "ruff", "check", "--select", rules,
         "--output-format", "json", "--no-cache", "."],
        root, "ruff",
    )
    # ruff exits 0 (clean) or 1 (findings); anything else, or output that is not
    # JSON (`No module named ruff` prints nothing on stdout), is an unknown.
    if proc.returncode not in (0, 1):
        raise NotConfirmed(f"ruff exited {proc.returncode}: {_tail(proc.stderr or proc.stdout)}")
    try:
        items = json.loads(proc.stdout)
    except ValueError as exc:
        raise NotConfirmed(f"ruff produced no JSON ({_tail(proc.stderr)})") from exc
    return parse_ruff(items, root, BUILTIN_EXCLUDES + cfg.exclude)


def collect(root: Path, cfg: Config) -> dict[str, Finding]:
    found = run_jscpd(root, cfg) + run_ruff(root, cfg)
    return {f.key: f for f in found}


def tool_versions() -> dict[str, str]:
    """What produced the baseline -- informational, never compared."""
    ruff = "unknown"
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "ruff", "--version"], capture_output=True, text=True,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=60, check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            ruff = proc.stdout.strip().removeprefix("ruff ")
    except (OSError, subprocess.TimeoutExpired):
        pass
    return {"jscpd": JSCPD_VERSION, "ruff": ruff}


# ---------------------------------------------------------------- baseline

def read_baseline(path: Path) -> dict[str, tuple[str, ...]]:
    """Baseline file -> ``{key: files}``. Raises `ConfigError` on anything off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise ConfigError(f"baseline {path}: `entries` must be a list")
    out: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("key"), str)
            or not isinstance(entry.get("files"), list)
            or not all(isinstance(f, str) for f in entry["files"])
        ):
            raise ConfigError(f"baseline {path}: malformed entry {entry!r}")
        out[entry["key"]] = tuple(entry["files"])
    return out


def render_baseline(findings: dict[str, Finding], tools: dict[str, str]) -> str:
    """Sorted, one entry per line, so a baseline diff reviews cleanly."""
    entries = [
        "    " + json.dumps({"key": f.key, "files": list(f.files)}, ensure_ascii=False)
        for f in sorted(findings.values(), key=lambda f: f.key)
    ]
    body = "[\n" + ",\n".join(entries) + "\n  ]" if entries else "[]"
    return (
        f'{{\n  "version": {BASELINE_VERSION},\n'
        f'  "tools": {json.dumps(tools, sort_keys=True)},\n'
        f'  "entries": {body}\n}}\n'
    )


def diff(current: dict[str, Finding], baseline: dict[str, tuple[str, ...]]) -> tuple[list[Finding], list[str]]:
    """(new findings, baselined keys no longer found), both sorted."""
    new = [current[k] for k in sorted(current.keys() - baseline.keys())]
    gone = sorted(baseline.keys() - current.keys())
    return new, gone


# -------------------------------------------------------------------- CLI

def _report_new(new: list[Finding]) -> None:
    print(f"static ratchet: {len(new)} NEW finding(s) not in the baseline:")
    for f in new:
        where = " <-> ".join(f.files)
        label = {"dup": "duplicate block", "unused": "unused", "lint": "bug/security"}[f.kind]
        print(f"  [{label}] {where}")
        print(f"      {f.detail}")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg = load_config(root)
    except ConfigError as exc:
        print(f"static ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("static ratchet: not enabled -- set [static_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("static ratchet: not enabled (no [static_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline

    baseline: dict[str, tuple[str, ...]] = {}
    if init:
        if baseline_path.exists():
            print(f"static ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
    elif baseline_path.is_file():
        try:
            baseline = read_baseline(baseline_path)
        except ConfigError as exc:
            print(f"static ratchet: BASELINE ERROR: {exc}")
            return EXIT_CONFIG
    else:
        print(f"static ratchet: FAIL: baseline {cfg.baseline} is missing -- run `python scripts/static_ratchet.py --init`")
        return EXIT_NEW

    try:
        current = collect(root, cfg)
    except NotConfirmed as exc:
        print(f"static ratchet: NOT CONFIRMED: {exc}")
        print("  (the check could not run, so it is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED

    if init:
        baseline_path.write_text(render_baseline(current, tool_versions()), encoding="utf-8", newline="\n")
        print(f"static ratchet: wrote {cfg.baseline} with {len(current)} baselined finding(s)")
        return EXIT_PASS

    new, gone = diff(current, baseline)
    if update:
        if new and not allow_additions:
            _report_new(new)
            print("static ratchet: nothing written -- fix them, or add them to the baseline with --allow-additions")
            return EXIT_NEW
        baseline_path.write_text(render_baseline(current, tool_versions()), encoding="utf-8", newline="\n")
        print(f"static ratchet: {cfg.baseline} now has {len(current)} entr(ies) "
              f"({len(gone)} dropped, {len(new)} added)")
        return EXIT_PASS

    if new:
        _report_new(new)
        print("  Remove the duplication / unused name / flagged pattern, or (reviewed) re-baseline with "
              "`--update-baseline --allow-additions`.")
        return EXIT_NEW
    print(f"static ratchet: PASS ({len(current)} baselined finding(s), none new)")
    if gone:
        print(f"  {len(gone)} baselined finding(s) are gone -- run "
              f"`python scripts/static_ratchet.py --update-baseline` to shrink the baseline")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop stale entries (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add new findings (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    sys.exit(main())
