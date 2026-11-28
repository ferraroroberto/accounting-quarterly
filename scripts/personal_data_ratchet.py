"""Ratchet on personal data in tracked files: its count may only go down.

A repository that is going to be shared must not carry its owner's machine
paths, email address, handle or private app names. This stage reads every
tracked text file, counts findings per file and rule against a committed
baseline, and fails only when a count rises. A finding is reported as file,
line and rule, never as the matched text: gate logs are copied and published.

Pattern rules (docs/personal-data-ratchet.md), which need no setup:

  * `user-path` -- an absolute local user or checkout path: a drive letter
    followed by a users or projects root, or its POSIX form (`/home/<you>`,
    `/Users/<you>`, a Git Bash mount). The segments `<root>`, `<you>`, `me`, `you`
    and `sample` are placeholders, not paths;
  * `personal-email` -- an email address not on a reserved example domain;
  * `handle-default` -- the owner's GitHub handle as a value in code or a
    config sample (not in a URL, an issue reference or a comment). The handle
    is the `origin` remote's owner plus the term list's `handles`;
  * `bare-exempt` -- a `<rule>-exempt` marker with no reason.

Term rules read a machine-local term list that is never committed:
`term-name`, `term-handle`, `term-email`, `term-domain` and `term-app`. The
list is the file `PERSONAL_DATA_TERMS` names, else
`~/.config/personal-data/terms.toml`, with one string list per rule:
`names`, `handles`, `emails`, `domains`, `apps`. A rule with no terms to read
is `unknown`, never a pass: the other rules still run, and the exit code is 3.

A single hit keeps its exception with `<rule>-exempt: <reason>` on its line or
on the comment line right above it. Exemptions are printed every run.

Opt-in, per adopter, in its own `.fleet.toml`:

    [personal_data_ratchet]
    enabled  = true
    baseline = ".ratchets/personal-data.json"  # default
    rules    = [...]                           # default: all
    exclude  = ["vendor/**"]                   # globs; never scanned

Exit codes: 0 pass or not enabled; 1 a count rose, or the baseline is missing /
a mode was refused; 2 bad configuration, term list, baseline or arguments;
3 NOT CONFIRMED (a rule `unknown`, `git` unusable, a file that cannot be read,
decoded or parsed) -- the gate fails on it. Modes: `check`, `--init`,
`--update-baseline` (shrink only), `--update-baseline --allow-additions`
(shows in review).

Vendor-verbatim (manifest key `personal_data_ratchet`), stdlib-only.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"personal data ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys personal_data_ratchet and ratchet_baseline)")
        sys.exit(3)


_baseline = _load_sibling("ratchet_baseline")  # where the baseline lives, and its move into .ratchets/

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Term rule -> the term list key that feeds it.
TERM_KEYS: dict[str, str] = {
    "term-name": "names", "term-handle": "handles", "term-email": "emails",
    "term-domain": "domains", "term-app": "apps",
}
PATTERN_RULES: tuple[str, ...] = ("user-path", "personal-email", "handle-default")
RULES: tuple[str, ...] = (*PATTERN_RULES, *TERM_KEYS, "bare-exempt")
MESSAGES: dict[str, str] = {
    "user-path": "an absolute local user or checkout path (placeholders: <root>, <you>, me, you, sample)",
    "personal-email": "an email address outside the reserved example domains",
    "handle-default": "the owner's GitHub handle used as a value",
    "term-name": "a name from the term list",
    "term-handle": "a handle from the term list",
    "term-email": "an email address from the term list",
    "term-domain": "a domain from the term list",
    "term-app": "an app name from the term list",
}
TERMS_ENV = "PERSONAL_DATA_TERMS"
DEFAULT_TERMS = (".config", "personal-data", "terms.toml")  # under the home directory
MIN_TERM = 3  # a shorter term matches ordinary words everywhere
BASELINE_VERSION = 1
DEFAULT_BASELINE = ".ratchets/personal-data.json"
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GIT_TIMEOUT_S = 120
EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3
LABEL = "personal data ratchet"

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "rules", "exclude"}

_PLACEHOLDERS = frozenset({"<root>", "<you>", "me", "you", "sample"})
_PROJECT_ROOTS = r"(?:projects?|automation|code|dev|repos?|git|github|src|work|workspace)"
_SEGMENT = r"([^\\/\s\"'`|:;,()\[\]{}*?]*(?:\{[^}\s]*\})?)"
_AFTER_ROOT = r"(?![\w.\-])(?:[\\/]+" + _SEGMENT + r")?"
# `<letter>:\Users\<name>` and `<letter>:\<projects root>`, with `\`, `\\` or `/` separators.
_DRIVE_PATH = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]+(?:(?P<users>(?i:users))|(?i:" + _PROJECT_ROOTS + r"))" + _AFTER_ROOT)
# The same through a Git Bash or WSL mount (`/c/...`, `/mnt/e/...`).
_MOUNT_PATH = re.compile(
    r"(?<![\w.\-~}\]:])(?:/mnt)?/[a-z]/(?:(?P<users>(?i:users))|" + _PROJECT_ROOTS + r")" + _AFTER_ROOT)
# `/home/<you>` and `/Users/<you>`; a URL path or `{base}/home/` is preceded by a character excluded here.
_HOME_PATH = re.compile(r"(?<![\w.\-~}\]:>])/(?P<users>home|Users)/" + _SEGMENT)

_EMAIL = re.compile(
    r"(?<![\w.+\-])([A-Za-z0-9][A-Za-z0-9._%+\-]*)@((?:[A-Za-z0-9](?:[A-Za-z0-9\-]*[A-Za-z0-9])?\.)+[A-Za-z]{2,})"
    r"(?![\w\-])")
_RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_TLDS = ("example", "test", "invalid", "localhost")

_ORIGIN_OWNER = re.compile(r"github\.com(?::\d+)?[:/]+([A-Za-z0-9\-]+)/")
_URL_BEFORE = re.compile(r"(?:://|github\.com[:/]|githubusercontent\.com/)[^\s\"'`<>()]*$")
_ISSUE_AFTER = re.compile(r"(?:/[\w.\-]+)?#\d")

#: Suffixes `handle-default` reads: code, and the config formats a value is assigned in.
_VALUE_SUFFIXES = (".py", ".ps1", ".psm1", ".bat", ".cmd", ".sh", ".js", ".mjs", ".cjs", ".ts",
                   ".json", ".toml", ".yml", ".yaml", ".ini", ".cfg", ".env")
_SAMPLE_MARKS = (".sample", ".example", ".template")
_HASH = ("#",)
_SLASH = ("//", "/*", "*")
_REM = ("rem ", "@rem ", "::")
#: Full-line comment openers per suffix; a line starting with one is prose to `handle-default`.
_COMMENT_OPENERS: dict[str, tuple[str, ...]] = {
    ".py": _HASH, ".sh": _HASH, ".toml": _HASH, ".yml": _HASH, ".yaml": _HASH, ".env": _HASH,
    ".ini": ("#", ";"), ".cfg": ("#", ";"), ".ps1": ("#", "<#"), ".psm1": ("#", "<#"),
    ".bat": _REM, ".cmd": _REM, ".js": _SLASH, ".mjs": _SLASH, ".cjs": _SLASH, ".ts": _SLASH,
}
_BLOCK_COMMENTS: dict[str, tuple[str, str]] = {
    ".ps1": ("<#", "#>"), ".psm1": ("<#", "#>"),
    ".js": ("/*", "*/"), ".mjs": ("/*", "*/"), ".cjs": ("/*", "*/"), ".ts": ("/*", "*/"),
}
#: Any line starting with one of these can carry the marker for the line below it, in any file type.
_MARKER_LINE = ("#", "//", "/*", "*", "<!--", "<#", "rem ", "@rem ", "::", ";")
_EXEMPTABLE = "|".join(rule for rule in RULES if rule != "bare-exempt")
_EXEMPT = re.compile(rf"(?<![\w\-])({_EXEMPTABLE})-exempt\b:?(.*?)(?=(?:{_EXEMPTABLE})-exempt\b|$)")


class ConfigError(RuntimeError):
    """The adopter's configuration, term list or baseline is unusable (exit 2)."""


class NotConfirmed(RuntimeError):
    """The tree could not be read through (exit 3)."""


@dataclass(frozen=True)
class Config:
    baseline: str
    rules: tuple[str, ...]
    exclude: tuple[str, ...] = ()


@dataclass(frozen=True)
class Terms:
    """What the term rules and `handle-default` match, and why a rule has nothing to match."""

    patterns: dict[str, re.Pattern[str]] = field(default_factory=dict)  # term rule -> pattern
    handle: re.Pattern[str] | None = None
    unknown: dict[str, str] = field(default_factory=dict)  # rule -> reason

    def redact(self, text: str) -> str:
        """`text` with every listed term masked, for anything about to be printed."""
        for pattern in self.patterns.values():
            text = pattern.sub("***", text)
        return text


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[personal_data_ratchet] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def parse_config(toml_text: str) -> Config | None:
    """`[personal_data_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip. Anything
    malformed raises `ConfigError`: a typo'd key or rule must not quietly run
    with less than the adopter asked for.
    """
    table = tomllib.loads(toml_text).get("personal_data_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[personal_data_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[personal_data_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[personal_data_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = _baseline.configured(table.get("baseline", DEFAULT_BASELINE), DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[personal_data_ratchet] baseline must be a non-empty string")
    rules = _str_list(table, "rules", RULES)
    bad = sorted(set(rules) - set(RULES))
    if not rules or bad:
        raise ConfigError(f"[personal_data_ratchet] rules must name rules from: {', '.join(RULES)}"
                          + (f" (unknown: {', '.join(bad)})" if bad else ""))
    return Config(baseline, tuple(r for r in RULES if r in rules), _str_list(table, "exclude", ()))


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get("vendored")
    found = []
    for entry in (table.values() if isinstance(table, dict) else ()):
        value = entry.get("dest") if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip("/"):
            found.append(value.replace("\\", "/").strip("/"))
    return tuple(sorted(found))


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...]]:
    """(config or None when not enabled, `[vendored]` dests)."""
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"cannot read .fleet.toml: {type(exc).__name__}") from exc
    try:
        return parse_config(text), vendored_dests(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f".fleet.toml is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------- terms

def terms_path() -> Path:
    named = os.environ.get(TERMS_ENV, "").strip()
    return Path(named) if named else Path.home().joinpath(*DEFAULT_TERMS)


def read_term_list(path: Path) -> dict[str, tuple[str, ...]] | None:
    """The list's keys and terms; None when there is no file. Errors never quote the file's content."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise NotConfirmed(f"the term list cannot be read ({type(exc).__name__})") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError("the term list is not valid TOML") from exc
    allowed = tuple(TERM_KEYS.values())
    if set(data) - set(allowed):
        raise ConfigError(f"the term list has a key other than: {', '.join(allowed)}")
    for key, value in data.items():
        if not isinstance(value, list) or not all(isinstance(v, str) and len(v.strip()) >= MIN_TERM for v in value):
            raise ConfigError(f"the term list's `{key}` must be a list of strings of {MIN_TERM} characters or more")
    return {key: tuple(v.strip() for v in value) for key, value in data.items()}


def _term_pattern(terms: tuple[str, ...]) -> re.Pattern[str]:
    """Any of `terms`, case-insensitive, not inside a longer word (`_` and `-` do separate words)."""
    body = "|".join(re.escape(t) for t in sorted(terms, key=len, reverse=True)) or r"(?!)"
    return re.compile(rf"(?<![^\W_])(?:{body})(?![^\W_])", re.IGNORECASE)


def _handle_pattern(handles: tuple[str, ...]) -> re.Pattern[str]:
    body = "|".join(re.escape(h) for h in sorted(handles, key=len, reverse=True))
    # A hyphen joins words only after a word character: shell's `${VAR:-handle}` default still counts.
    return re.compile(rf"(?<![\w.@])(?<!\w-)(?:{body})(?![\w\-@]|\.\w)", re.IGNORECASE)


def origin_owner(root: Path) -> str | None:
    """The GitHub owner of the `origin` remote; None when there is no GitHub origin to read."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "config", "--get", "remote.origin.url"], capture_output=True, text=True,
            errors="replace", stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = _ORIGIN_OWNER.search(proc.stdout) if proc.returncode == 0 else None
    return match.group(1) if match else None


def load_terms(root: Path) -> Terms:
    listed = read_term_list(terms_path())
    where = f"the file {TERMS_ENV} names" if os.environ.get(TERMS_ENV, "").strip() else "~/" + "/".join(DEFAULT_TERMS)
    patterns: dict[str, re.Pattern[str]] = {}
    unknown: dict[str, str] = {}
    for rule, key in TERM_KEYS.items():
        if listed is None:
            unknown[rule] = f"no term list at {where}"
        elif key not in listed:
            unknown[rule] = f"the term list has no `{key}` key (write `{key} = []` if there are none)"
        else:
            patterns[rule] = _term_pattern(listed[key])
    owner = origin_owner(root)
    handles = tuple(dict.fromkeys([*([owner] if owner else []), *((listed or {}).get("handles", ()))]))
    if not handles:
        unknown["handle-default"] = "no GitHub `origin` remote and no `handles` in a term list"
    return Terms(patterns, _handle_pattern(handles) if handles else None, unknown)


# ------------------------------------------------------------------- files

def _glob_regex(glob: str) -> re.Pattern[str]:
    """Repo-root-anchored glob -> regex; `**/` is zero or more directories."""
    tokens = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", glob)
    return re.compile("".join(tokens.get(p, re.escape(p)) for p in parts) + r"\Z")


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """A file the repo owns: not a vendored copy, not excluded, not this ratchet's own baseline."""
    if path == cfg.baseline or any(path == d or path.startswith(d + "/") for d in vendored):
        return False
    return not any(_glob_regex(g).match(path) for g in (*BUILTIN_EXCLUDES, *cfg.exclude))


def tracked_files(root: Path) -> list[str]:
    """Tracked and untracked-but-not-ignored files, so a new file counts before it is committed."""
    cmd = ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    try:
        proc = subprocess.run(
            cmd, capture_output=True, stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise NotConfirmed(f"git could not start ({type(exc).__name__})") from exc
    if proc.returncode != 0:
        raise NotConfirmed(f"git ls-files exited {proc.returncode}")
    try:
        return sorted(set(filter(None, proc.stdout.decode("utf-8").split("\0"))))
    except UnicodeDecodeError as exc:
        raise NotConfirmed("git ls-files returned a path that is not UTF-8") from exc


def read_text(root: Path, path: str) -> str | None:
    """The file's text; None for a binary file or one no longer on disk."""
    full = root / path
    try:
        if not full.is_file():
            return None
        data = full.read_bytes()
    except OSError as exc:
        raise NotConfirmed(f"{path}: cannot be read ({type(exc).__name__})") from exc
    wide = data.startswith((b"\xff\xfe", b"\xfe\xff"))  # UTF-16, as Windows PowerShell redirects write it
    if not wide and b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-16" if wide else "utf-8-sig")
    except UnicodeDecodeError as exc:
        raise NotConfirmed(f"{path}: is not UTF-8 (byte {exc.start}); list it in `exclude` if it is not text") from exc


# ------------------------------------------------------------------- rules

def _has_user_path(line: str) -> bool:
    for pattern in (_DRIVE_PATH, _MOUNT_PATH, _HOME_PATH):
        for match in pattern.finditer(line):
            segment = (match.group(match.re.groups) or "").rstrip(".")
            if segment.lower() in _PLACEHOLDERS:
                continue
            # Under a users root only a literal name is personal: `%USERNAME%`, `$USER`, `{user}` and `...`
            # name nobody. A projects root is itself the checkout path, whatever follows it.
            if match.group("users") is None or re.match(r"[\w<]", segment):
                return True
    return False


def _has_personal_email(line: str) -> bool:
    for match in _EMAIL.finditer(line):
        local, domain = match.group(1).lower(), match.group(2).lower()
        labels = domain.split(".")
        if labels[-1] in _RESERVED_TLDS or any(domain == d or domain.endswith("." + d) for d in _RESERVED_DOMAINS):
            continue
        # `git@host`, `user@host:path` (clone and copy targets) and `icon@2x.png` are not addresses, and
        # neither is the escape letter before a decorator written inside a string literal.
        escape = len(local) == 1 and line[:match.start()].endswith("\\")
        if escape or local == "git" or re.match(r":[\w~/.]", line[match.end():]) or re.fullmatch(r"\d+x", labels[0]):
            continue
        return True
    return False


def _has_handle(line: str, handle: re.Pattern[str]) -> bool:
    return any(not _URL_BEFORE.search(line[:m.start()]) and not _ISSUE_AFTER.match(line[m.end():])
               for m in handle.finditer(line))


def _value_suffix(path: str) -> str | None:
    """The comment syntax of a file `handle-default` reads (`tray.bat.template` -> `.bat`), else None."""
    name = PurePosixPath(path).name.lower()
    sample = any(mark in name for mark in _SAMPLE_MARKS)
    for mark in _SAMPLE_MARKS:
        name = name.replace(mark, "")
    suffix = ".env" if name == ".env" else PurePosixPath(name).suffix
    return suffix if sample or suffix in _VALUE_SUFFIXES else None


def prose_lines(lines: list[str], text: str, path: str, suffix: str) -> set[int]:
    """1-based comment and Python docstring lines; raises `NotConfirmed` on Python that does not parse."""
    openers = _COMMENT_OPENERS.get(suffix, ())
    prose = {n for n, line in enumerate(lines, 1) if openers and line.lstrip().lower().startswith(openers)}
    if suffix == ".py" and path.endswith(".py"):
        try:
            tree = ast.parse(text, filename=path)
        except (SyntaxError, ValueError) as exc:
            raise NotConfirmed(f"{path}: cannot be parsed ({type(exc).__name__})") from exc
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            first = node.body[0] if node.body else None
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                prose.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    elif suffix in _BLOCK_COMMENTS:
        opener, closer = _BLOCK_COMMENTS[suffix]
        open_block = False
        for number, line in enumerate(lines, 1):
            stripped = line.strip()
            if not open_block and stripped.startswith(opener):
                open_block = closer not in stripped[len(opener):]
                prose.add(number)
            elif open_block:
                prose.add(number)
                open_block = closer not in stripped
    return prose


def line_rules(line: str, terms: Terms, *, as_value: bool) -> list[str]:
    """The rules one line breaks, at most once each, before exemptions."""
    found = []
    if _has_user_path(line):
        found.append("user-path")
    if "@" in line and _has_personal_email(line):
        found.append("personal-email")
    if as_value and terms.handle is not None and _has_handle(line, terms.handle):
        found.append("handle-default")
    found += [rule for rule, pattern in terms.patterns.items() if pattern.search(line)]
    return found


def _exemptions(lines: list[str], index: int) -> dict[str, str]:
    """rule -> reason ('' when bare) from the markers on line `index` or the comment line right above."""
    marks: dict[str, str] = {}
    for probe in (index - 1, index):
        if probe < 0 or (probe != index and not lines[probe].lstrip().lower().startswith(_MARKER_LINE)):
            continue
        for match in _EXEMPT.finditer(lines[probe]):
            reason = re.sub(r"(?:-->|\*/|#>)\s*$", "", match.group(2)).strip(" ;,")
            marks[match.group(1)] = reason or marks.get(match.group(1), "")
    return marks


@dataclass
class FileScan:
    findings: list[tuple[int, str]] = field(default_factory=list)  # (line, rule)
    exempted: list[tuple[int, str, str]] = field(default_factory=list)  # (line, rule, reason)


def scan_source(text: str, path: str, terms: Terms) -> FileScan:
    """Findings in one file's text. `handle-default` skips prose; every other rule reads every line."""
    lines = text.splitlines()
    suffix = _value_suffix(path)
    prose = prose_lines(lines, text, path, suffix) if suffix is not None and terms.handle is not None else set()
    result = FileScan()
    for index, line in enumerate(lines):
        number = index + 1
        found = line_rules(line, terms, as_value=suffix is not None and number not in prose)
        if not found:
            continue
        marks = _exemptions(lines, index)
        bare = False
        for rule in found:
            reason = marks.get(rule)
            if reason:
                result.exempted.append((number, rule, reason))
            else:
                result.findings.append((number, rule))
                bare = bare or reason == ""
        if bare:
            result.findings.append((number, "bare-exempt"))
    return result


@dataclass
class Tree:
    found: dict[str, list[tuple[int, str]]] = field(default_factory=dict)
    exempted: list[tuple[str, int, str, str]] = field(default_factory=list)  # (path, line, rule, reason)
    scanned: int = 0
    skipped: int = 0  # binary files


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...], terms: Terms) -> Tree:
    files = tracked_files(root)
    listed = terms_path().resolve()
    if any((root / path).resolve() == listed for path in files):
        raise ConfigError("the term list is a file git would commit -- move it outside the repository")
    tree = Tree()
    for path in files:
        if not in_scope(path, cfg, vendored):
            continue
        text = read_text(root, path)
        if text is None:
            tree.skipped += 1
            continue
        tree.scanned += 1
        scanned = scan_source(text, path, terms)
        tree.exempted += [(path, line, rule, why) for line, rule, why in scanned.exempted if rule in cfg.rules]
        hits = sorted((line, rule) for line, rule in scanned.findings if rule in cfg.rules)
        if hits:
            tree.found[path] = hits
    return tree


# ---------------------------------------------------------------- baseline

Counts = dict[str, Counter[str]]  # path -> Counter(rule)


def read_baseline(path: Path) -> Counts:
    """Baseline file -> ``{file path: Counter(rule)}``. Raises `ConfigError` on anything off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data["version"] != BASELINE_VERSION:
            raise ValueError(f"version is not {BASELINE_VERSION}")
        out: Counts = {}
        for item in data["entries"]:
            counts = item["counts"]
            if not counts or not all(r in RULES and type(n) is int and n > 0 for r, n in counts.items()):
                raise ValueError("`counts` must map rules to positive integers")
            out[str(item["key"])] = Counter(counts)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ConfigError(f"baseline {path.name} is unusable ({type(exc).__name__}: {exc})") from exc
    return out


def write_baseline(path: Path, counts: Counts) -> None:
    """Sorted, one file per line, no line numbers and no matched text, so a diff reviews cleanly."""
    lines = []
    for key in sorted(counts):
        record = {"key": key, "counts": {r: counts[key][r] for r in RULES if counts[key][r]}}
        lines.append("    " + json.dumps(record, ensure_ascii=False))
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    text = f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    path.write_text(text, encoding="utf-8", newline="\n")


def _total(counts: Counts) -> int:
    return sum(sum(c.values()) for c in counts.values())


# -------------------------------------------------------------------- CLI

@dataclass
class Run:
    """One scan compared with the baseline, over the rules that could be measured."""

    cfg: Config
    tree: Tree
    terms: Terms
    baseline: Counts

    @property
    def unknown(self) -> dict[str, str]:
        return {rule: why for rule, why in self.terms.unknown.items() if rule in self.cfg.rules}

    @property
    def current(self) -> Counts:
        return {path: Counter(rule for _, rule in hits) for path, hits in self.tree.found.items()}

    def diff(self) -> tuple[Counts, int]:
        """(per file, the counts beyond the baseline; how many baselined hits are gone)."""
        added: Counts = {}
        gone = 0
        current = self.current
        for path in sorted(set(current) | set(self.baseline)):
            cur = current.get(path, Counter())
            base = Counter({r: n for r, n in self.baseline.get(path, Counter()).items() if r not in self.unknown})
            if cur - base:
                added[path] = cur - base
            gone += sum((base - cur).values())
        return added, gone

    def kept(self) -> Counts:
        """What a rewrite records: today's counts, plus the baseline's for each rule that was not measured."""
        merged = self.current
        for path, counts in self.baseline.items():
            held = Counter({r: n for r, n in counts.items() if r in self.unknown})
            if held:
                merged[path] = merged.get(path, Counter()) + held
        return merged

    def report_added(self, added: Counts) -> None:
        print(f"{LABEL}: {_total(added)} hit(s) beyond the baseline, in {len(added)} file(s):")
        for path, extra in added.items():
            shown = self.terms.redact(path)
            for rule in RULES:
                if not extra[rule]:
                    continue
                had = self.baseline.get(path, Counter())[rule]
                print(f"  {shown}: [{rule}] {had + extra[rule]} hit(s), baseline {had}:")
                for line, found in self.tree.found[path]:
                    if found == rule:
                        print(f"    {shown}:{line}: [{rule}] {MESSAGES.get(rule, 'a marker with no reason')}")

    def report_context(self) -> None:
        for rule, why in self.unknown.items():
            print(f"  {rule}: unknown -- {why}")
        off = [r for r in RULES if r not in self.cfg.rules]
        if off:
            print(f"  OFF (not listed in `rules`): {', '.join(off)}")
        print(f"  {self.tree.scanned} text file(s) read, {self.tree.skipped} binary or missing not read; "
              f"{len(self.tree.exempted)} exemption(s)" + (":" if self.tree.exempted else ""))
        for path, line, rule, why in sorted(self.tree.exempted):
            print(f"    {self.terms.redact(path)}:{line}: [{rule}] {self.terms.redact(why)}")

    def verdict(self) -> int:
        """Exit code once nothing rose: a rule that could not be measured is never a pass."""
        if self.unknown:
            print(f"{LABEL}: NOT CONFIRMED: {len(self.unknown)} rule(s) unknown, so this is not a pass")
            return EXIT_NOT_CONFIRMED
        return EXIT_PASS


def _check(run: Run) -> int:
    added, gone = run.diff()
    if added:
        run.report_added(added)
        print("  Replace the value with a placeholder or move it to ignored machine-local config "
              "(docs/personal-data-ratchet.md). A hit that must stay carries `<rule>-exempt: <reason>`; or "
              "(reviewed) raise the baseline with `--update-baseline --allow-additions`.")
        run.report_context()
        return EXIT_RISE
    state = "no new hit in the rules measured" if run.unknown else "PASS"
    print(f"{LABEL}: {state} ({_total(run.current)} hit(s) found, none beyond the baseline)")
    run.report_context()
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/personal_data_ratchet.py --update-baseline` to lower it")
    return run.verdict()


def _write(run: Run, path: Path, *, init: bool, allow_additions: bool) -> int:
    added, gone = run.diff()
    if added and not init and not allow_additions:
        run.report_added(added)
        print(f"{LABEL}: nothing written -- remove the data, or raise the baseline with --allow-additions")
        return EXIT_RISE
    counts = run.kept()
    write_baseline(path, counts)
    change = "" if init else f" ({gone} dropped, {_total(added)} added)"
    print(f"{LABEL}: {run.cfg.baseline} now has {_total(counts)} hit(s) in {len(counts)} file(s){change}")
    run.report_context()
    return run.verdict()


def _load_baseline(baseline_path: Path, cfg: Config, init: bool) -> Counts | int:
    """The committed baseline (empty for `--init`), or the exit code that refuses the run."""
    if init:
        if baseline_path.exists():
            print(f"{LABEL}: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return {}
    if not baseline_path.is_file():
        print(f"{LABEL}: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/personal_data_ratchet.py --init`")
        return EXIT_RISE
    return read_baseline(baseline_path)


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    terms = Terms()  # nothing to mask until the list is read
    try:
        cfg, vendored = load_config(root)
        if cfg is None:
            if init or update:
                print(f"{LABEL}: not enabled -- set [personal_data_ratchet] enabled = true in .fleet.toml first")
                return EXIT_CONFIG
            print(f"{LABEL}: not enabled (no [personal_data_ratchet] enabled = true in .fleet.toml)")
            return EXIT_PASS
        baseline_path = _baseline.resolve(root, cfg.baseline, DEFAULT_BASELINE, LABEL)
        if baseline_path is None:
            return EXIT_CONFIG
        baseline = _load_baseline(baseline_path, cfg, init)
        if isinstance(baseline, int):
            return baseline
        terms = load_terms(root)
        state = Run(cfg, scan_tree(root, cfg, vendored, terms), terms, baseline)
    except ConfigError as exc:
        print(f"{LABEL}: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    except NotConfirmed as exc:
        print(f"{LABEL}: NOT CONFIRMED: {terms.redact(str(exc))}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    if init or update:
        return _write(state, baseline_path, init=init, allow_additions=allow_additions)
    return _check(state)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop hits now gone (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add hits (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    # Paths can be non-ASCII; a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
