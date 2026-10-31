"""Ratchet on fleet conventions that one AST rule can check in every repo.

The global instructions carry a handful of rules that each repo used to guard
with its own one-off test (no-window spawns, one constant-time secret compare,
git through the repo's wrapper). This stage holds them in one vendored place:
a small set of stdlib `ast` rules, each opted into per repo, counted per file
and rule against a committed baseline. It fails only when a count rises.
Existing debt never blocks work, but it cannot grow.

The rules (each one quotes its global-instruction line in docs/conventions-ratchet.md):

  * `no-window` -- a `subprocess.run/Popen/call/check_call/check_output` or
    `asyncio.create_subprocess_*` call with no `creationflags=` keyword;
  * `logging-not-print` -- a builtin `print(...)` in app code (`print_paths`),
    outside a command-line module (an `if __name__ == "__main__":` block anywhere,
    an `argparse`/`click`/`typer`/`fire`/`optparse` import, a `cli.py` or
    `__main__.py` name, or a `cli/` folder), not writing to a file, and not a
    `print` the module binds itself;
  * `no-drive-path` -- a string literal that starts with a Windows drive letter
    (`E:\\data`, `C:/x`); the system folders `?:\\Windows`, `?:\\Program Files`,
    `?:\\Program Files (x86)` and `?:\\ProgramData` are never reported, because the
    instructions mandate absolute system binaries;
  * `secret-compare` -- a `hmac.compare_digest` / `secrets.compare_digest` call
    outside the repo's declared helper (`secret_helpers`);
  * `git-wrapper` -- a spawn whose program is literally `git` outside the
    repo's declared wrapper (`git_wrappers`).

Test modules are skipped by every rule except `no-window`. A single call keeps
its exception with a `# <rule>-exempt: <reason>` comment on its own lines or in
the comment block right above it; a marker with no reason exempts nothing.

Opt-in, per adopter, in its own `.fleet.toml`:

    [conventions_ratchet]
    enabled        = true
    baseline       = ".conventions-ratchet-baseline.json"  # default
    rules          = ["no-window", "no-drive-path"]          # required: the rules this repo enforces
    exclude        = ["src/generated/**"]                    # globs; never scanned
    print_paths    = ["app/", "src/"]                        # logging-not-print scope (default)
    secret_helpers = ["src/secret_compare.py"]               # required by secret-compare
    git_wrappers   = ["src/git_run.py"]                      # required by git-wrapper

Exit codes: 0 pass or not enabled; 1 a count rose, or the baseline is missing /
a mode was refused; 2 bad configuration, baseline or arguments; 3 NOT CONFIRMED
(`git` unusable, a tracked `.py` file that does not parse or cannot be read) --
the gate fails on it. Modes: `check`, `--init`, `--update-baseline` (shrink
only), `--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `conventions_ratchet`), stdlib-only. Procedure and
limits: docs/conventions-ratchet.md.
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
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".conventions-ratchet-baseline.json"
DEFAULT_PRINT_PATHS = ("app/", "src/")
#: Always excluded, whatever the adopter's own `exclude` adds.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GIT_TIMEOUT_S = 120

RULES = ("no-window", "logging-not-print", "no-drive-path", "secret-compare", "git-wrapper")
#: The rules a test module is held to; fixtures legitimately spell drive paths, raw compares and raw git.
TEST_RULES = frozenset({"no-window"})
#: The config key a rule cannot run without, because it names the repo's own helper.
REQUIRED_KEYS = {"secret-compare": "secret_helpers", "git-wrapper": "git_wrappers"}

SPAWNS = frozenset({
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
})
SECRET_COMPARES = frozenset({"hmac.compare_digest", "secrets.compare_digest"})
_TRACKED_MODULES = frozenset({"subprocess", "asyncio", "hmac", "secrets"})
_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
#: Absolute system binaries the instructions themselves mandate (`C:/Windows/System32/.../powershell.exe`).
_SYSTEM_DRIVE = re.compile(
    r"[A-Za-z]:[\\/]+(?:Windows|Program Files(?: \(x86\))?|ProgramData)(?:[\\/]|\Z)", re.IGNORECASE
)
#: A bare drive root (`"C:\\"`) anchors a walk of the filesystem; it names no data location.
_DRIVE_ROOT = re.compile(r"[A-Za-z]:[\\/]?\Z")
#: A module that is a command-line entry point prints its output by design.
_CLI_MODULES = frozenset({"argparse", "click", "typer", "fire", "optparse"})
_PLATFORM_EXPRS = frozenset({"sys.platform", "os.name", "platform.system"})
_GIT_PROGRAMS = frozenset({"git", "git.exe"})

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "rules", "exclude", "print_paths", "secret_helpers", "git_wrappers"}


class ConfigError(RuntimeError):
    """The `[conventions_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A tracked Python file could not be read or parsed, or git could not list the tree."""


@dataclass(frozen=True)
class Config:
    baseline: str
    rules: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    print_paths: tuple[str, ...] = DEFAULT_PRINT_PATHS
    secret_helpers: tuple[str, ...] = ()
    git_wrappers: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[conventions_ratchet] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def parse_config(toml_text: str) -> Config | None:
    """`[conventions_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key or rule
    must not quietly run with less than the adopter asked for.
    """
    table = tomllib.loads(toml_text).get("conventions_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[conventions_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[conventions_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[conventions_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[conventions_ratchet] baseline must be a non-empty string")
    rules = _str_list(table, "rules", ())
    if not rules:
        raise ConfigError(f"[conventions_ratchet] rules must name at least one of: {', '.join(RULES)}")
    bad = sorted(set(rules) - set(RULES))
    if bad:
        raise ConfigError(f"[conventions_ratchet] unknown rule(s) {', '.join(bad)}; known: {', '.join(RULES)}")
    cfg = Config(
        baseline,
        tuple(r for r in RULES if r in rules),
        _str_list(table, "exclude", ()),
        _str_list(table, "print_paths", DEFAULT_PRINT_PATHS),
        _str_list(table, "secret_helpers", ()),
        _str_list(table, "git_wrappers", ()),
    )
    for rule, key in REQUIRED_KEYS.items():
        if rule in cfg.rules and not getattr(cfg, key):
            raise ConfigError(f"[conventions_ratchet] rule {rule} needs {key}: the file(s) that may do it")
    return cfg


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get("vendored")
    if not isinstance(table, dict):
        return ()
    found = []
    for entry in table.values():
        value = entry.get("dest") if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip("/"):
            found.append(value.replace("\\", "/").strip("/"))
    return tuple(sorted(found))


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...]]:
    """(config or None when not enabled, `[vendored]` dests)."""
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text), vendored_dests(text)
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


def matches_any(text: str, globs: tuple[str, ...]) -> bool:
    return any(_glob_regex(g).match(text) for g in globs)


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.replace("\\", "/").strip("/")
    return path == prefix or path.startswith(prefix + "/")


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """Is `path` a Python file this stage scans? A vendored copy's debt belongs to the repo it came from."""
    if not path.endswith(".py"):
        return False
    if any(_under(path, d) for d in vendored):
        return False
    return not matches_any(path, BUILTIN_EXCLUDES + cfg.exclude)


def is_test_module(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def rules_for(path: str, cfg: Config) -> frozenset[str]:
    """The enabled rules that apply to one in-scope file."""
    rules = set(cfg.rules)
    if is_test_module(path):
        rules &= TEST_RULES
    if not any(_under(path, p) for p in cfg.print_paths):
        rules.discard("logging-not-print")
    if any(_under(path, h) for h in cfg.secret_helpers):
        rules.discard("secret-compare")
    if any(_under(path, w) for w in cfg.git_wrappers):
        rules.discard("git-wrapper")
    return frozenset(rules)


# ---------------------------------------------------------------- scanning

def _docstring_ids(tree: ast.Module) -> set[int]:
    """`id()` of every docstring node: prose about `C:\\` is not a hardcoded path."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _is_main_guard(node: ast.If) -> bool:
    test = node.test
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = [test.left, test.comparators[0]]
    return (any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
            and any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides))


def _branch_platform(test: ast.expr) -> str | None:
    """"win" when an `if` test holds only on Windows, "posix" when only off it, None when it says neither."""
    negate = False
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        negate, test = True, test.operand
    value: object = None
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], (ast.Eq, ast.NotEq)):
        left, right = test.left, test.comparators[0]
        if isinstance(left, ast.Call):
            left = left.func  # `platform.system()`
        if _dotted(left) in _PLATFORM_EXPRS and isinstance(right, ast.Constant):
            value = right.value
            negate ^= isinstance(test.ops[0], ast.NotEq)
    elif (isinstance(test, ast.Call) and isinstance(test.func, ast.Attribute) and test.func.attr == "startswith"
          and _dotted(test.func.value) == "sys.platform" and test.args and isinstance(test.args[0], ast.Constant)):
        value = test.args[0].value
    if not isinstance(value, str):
        return None
    windows = value.lower().startswith("win") or value == "nt"
    if negate:
        return "posix" if windows else None  # "not darwin" may still be Windows
    return "win" if windows else "posix"


def is_cli_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return parts[-1] in ("__main__.py", "cli.py") or "cli" in parts[:-1]


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _program(call: ast.Call) -> str:
    """The literal program a spawn runs (`git` for `["git", "status"]` or `"git status"`), else ""."""
    if not call.args:
        return ""
    first = call.args[0]
    if isinstance(first, (ast.List, ast.Tuple)) and first.elts:
        first = first.elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return PurePosixPath(first.value.replace("\\", "/")).name.lower()
        return ""
    if isinstance(first, ast.Constant) and isinstance(first.value, str):
        words = first.value.split()
        return PurePosixPath(words[0].replace("\\", "/")).name.lower() if words else ""
    return ""


def _writes_to_a_file(call: ast.Call) -> bool:
    """`print(x, file=fh)` writes data, not a log line; `file=sys.stdout/stderr` is still console output."""
    for kw in call.keywords:
        if kw.arg == "file":
            return _dotted(kw.value) not in ("sys.stdout", "sys.stderr")
    return False


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, rules: frozenset[str], docstrings: set[int]) -> None:
        self.path = path
        self.rules = rules
        self.docstrings = docstrings
        self.hits: list[tuple[ast.AST, str, str]] = []
        self.aliases: dict[str, str] = {}  # local name -> `module` or `module.attr`
        self.print_shadowed = False
        self.cli = False  # a `__main__` guard or a CLI-parser import: the module prints its output
        self.posix_depth = 0  # inside a branch that never runs on Windows

    def _resolve(self, func: ast.expr) -> str:
        dotted = _dotted(func)
        head, _, rest = dotted.partition(".")
        target = self.aliases.get(head)
        if target is None:
            return ""
        return f"{target}.{rest}" if rest else target

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in _TRACKED_MODULES:
                self.aliases[alias.asname or alias.name] = alias.name
            if alias.name.split(".")[0] in _CLI_MODULES:
                self.cli = True

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "").split(".")[0] in _CLI_MODULES:
            self.cli = True
        for alias in node.names:
            if node.module in _TRACKED_MODULES:
                self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
            if (alias.asname or alias.name) == "print":
                self.print_shadowed = True

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        if node.name == "print":
            self.print_shadowed = True
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_def(node)

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for expr in [*node.decorator_list, node.args, *([node.returns] if node.returns else [])]:
            self.visit(expr)
        self._visit_block(node.body, posix=False)

    def visit_If(self, node: ast.If) -> None:
        self.visit(node.test)
        self.cli |= _is_main_guard(node)
        platform = _branch_platform(node.test)
        self._visit_block(node.body, posix=platform == "posix")
        self._visit_block(node.orelse, posix=platform == "win")

    def _visit_block(self, body: list[ast.stmt], *, posix: bool) -> None:
        """Visit a statement list; after `if <windows>: ... return`, the rest of the block never runs on Windows."""
        depth = self.posix_depth
        self.posix_depth += posix
        for child in body:
            self.visit(child)
            if (isinstance(child, ast.If) and not child.orelse and _branch_platform(child.test) == "win"
                    and child.body and isinstance(child.body[-1], (ast.Return, ast.Raise))):
                self.posix_depth += 1
        self.posix_depth = depth

    def visit_Call(self, node: ast.Call) -> None:
        name = self._resolve(node.func)
        if name in SPAWNS:
            splat = any(kw.arg is None for kw in node.keywords)  # `**kw` may carry the flag: miss, never invent
            flagged = splat or any(kw.arg == "creationflags" for kw in node.keywords)
            if "no-window" in self.rules and not flagged and not self.posix_depth:
                self.hits.append((node, "no-window", f"`{name}(...)` passes no creationflags="))
            if "git-wrapper" in self.rules and _program(node) in _GIT_PROGRAMS:
                self.hits.append((node, "git-wrapper", f"`{name}` spawns git directly, not through the repo's wrapper"))
        elif name in SECRET_COMPARES and "secret-compare" in self.rules:
            self.hits.append((node, "secret-compare", f"raw `{name}` outside the repo's secret-compare helper"))
        elif (isinstance(node.func, ast.Name) and node.func.id == "print" and "logging-not-print" in self.rules
              and not _writes_to_a_file(node)):
            self.hits.append((node, "logging-not-print", "`print(...)` in app code; use `logging`"))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if ("no-drive-path" in self.rules and isinstance(node.value, str) and id(node) not in self.docstrings
                and _DRIVE.match(node.value) and not _SYSTEM_DRIVE.match(node.value)
                and not _DRIVE_ROOT.match(node.value)):
            shown = node.value if len(node.value) <= 40 else node.value[:37] + "..."
            self.hits.append((node, "no-drive-path", f"hardcoded drive path {shown!r}"))


def _window(node: ast.AST, lines: list[str]) -> list[str]:
    """The node's own lines plus the contiguous comment block right above it."""
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    above = start - 1
    while above > 0 and lines[above - 1].lstrip().startswith("#"):
        above -= 1
    return lines[above:end]


def _exempt(rule: str, window: list[str]) -> bool:
    """A `# <rule>-exempt: <reason>` marker in the window; a bare marker exempts nothing."""
    marker = f"{rule}-exempt"
    return any(line.split(marker, 1)[1].strip(" :#\t") for line in window if marker in line)


def scan_source(source: bytes | str, path: str, rules: frozenset[str]) -> list[Hit]:
    """Hits of the given rules in one module; raises `NotConfirmed` when it does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape in the scanned code is not this stage's finding
            tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        where = f" (line {exc.lineno})" if isinstance(exc, SyntaxError) and exc.lineno else ""
        raise NotConfirmed(f"{path}: does not parse{where}: {exc.msg if isinstance(exc, SyntaxError) else exc}") from exc
    if not rules:
        return []
    scanner = _Scanner(path, rules, _docstring_ids(tree))
    scanner.visit(tree)
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    lines = text.splitlines()
    hits = []
    for node, rule, message in scanner.hits:
        if rule == "logging-not-print" and (scanner.print_shadowed or scanner.cli or is_cli_path(path)):
            continue
        if not _exempt(rule, _window(node, lines)):
            hits.append(Hit(path, getattr(node, "lineno", 0), rule, message))
    return sorted(hits, key=lambda h: (h.line, h.rule))


# --------------------------------------------------------------------- git

def _git(root: Path, args: list[str]) -> subprocess.CompletedProcess[bytes]:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, env=env, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc


def tracked_files(root: Path) -> list[str]:
    """Repo-relative, forward-slash paths of every tracked or untracked-but-not-ignored file."""
    args = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    proc = _git(root, args)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git {' '.join(args)} exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...]) -> dict[str, list[Hit]]:
    """Hits per in-scope tracked `.py` file (only files with at least one).

    Only Python source is read: a baseline, this stage's or any other ratchet's,
    is JSON and never counts as a hit or as evidence against one.
    """
    found: dict[str, list[Hit]] = {}
    for path in tracked_files(root):
        if not in_scope(path, cfg, vendored):
            continue
        try:
            source = (root / path).read_bytes()
        except FileNotFoundError:
            continue  # tracked but deleted in the working tree: nothing to read
        except OSError as exc:
            raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
        hits = scan_source(source, path, rules_for(path, cfg))
        if hits:
            found[path] = hits
    return found


# ---------------------------------------------------------------- baseline

Counts = dict[str, Counter[str]]  # path -> Counter(rule)


def current_counts(found: dict[str, list[Hit]]) -> Counts:
    return {path: Counter(h.rule for h in hits) for path, hits in found.items()}


def read_baseline(path: Path) -> Counts:
    """Baseline file -> ``{source path: Counter(rule)}``. Raises `ConfigError` on anything off."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read baseline {path}: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"baseline {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict) or data.get("version") != BASELINE_VERSION:
        raise ConfigError(f"baseline {path}: expected an object with version {BASELINE_VERSION}")
    raw_entries = data.get("entries")
    if not isinstance(raw_entries, list):
        raise ConfigError(f"baseline {path}: `entries` must be a list")
    out: Counts = {}
    for item in raw_entries:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ConfigError(f"baseline {path}: malformed entry {item!r}")
        counts = item.get("counts")
        if (not isinstance(counts, dict) or not counts
                or not all(r in RULES and isinstance(n, int) and not isinstance(n, bool) and n > 0
                           for r, n in counts.items())):
            raise ConfigError(f"baseline {path}: entry {item!r} needs `counts` of known rules to positive integers")
        out[item["key"]] = Counter(counts)
    return out


def render_baseline(counts: Counts) -> str:
    """Sorted, one file per line, no line numbers, so a baseline diff reviews cleanly."""
    lines = []
    for key in sorted(counts):
        record = {"key": key, "counts": {r: counts[key][r] for r in RULES if counts[key][r]}}
        lines.append("    " + json.dumps(record, ensure_ascii=False))
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def diff(current: Counts, baseline: Counts) -> tuple[Counts, int]:
    """(per file, the rule counts beyond the baseline; how many baselined hits are gone)."""
    added: Counts = {}
    gone = 0
    for path in sorted(set(current) | set(baseline)):
        cur, base = current.get(path, Counter()), baseline.get(path, Counter())
        extra = cur - base
        if extra:
            added[path] = extra
        gone += sum((base - cur).values())
    return added, gone


# -------------------------------------------------------------------- CLI

def _total(counts: Counts) -> int:
    return sum(sum(c.values()) for c in counts.values())


def _report_added(added: Counts, found: dict[str, list[Hit]], baseline: Counts) -> None:
    print(f"conventions ratchet: {_total(added)} hit(s) beyond the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for rule in RULES:
            if not extra[rule]:
                continue
            had = baseline.get(path, Counter())[rule]
            print(f"  {path}: [{rule}] {had + extra[rule]} hit(s), baseline {had}:")
            for h in found[path]:
                if h.rule == rule:
                    print(f"    {path}:{h.line}: [{rule}] {h.message}")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"conventions ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("conventions ratchet: not enabled -- set [conventions_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("conventions ratchet: not enabled (no [conventions_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline

    baseline: Counts = {}
    if init:
        if baseline_path.exists():
            print(f"conventions ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
    elif baseline_path.is_file():
        try:
            baseline = read_baseline(baseline_path)
        except ConfigError as exc:
            print(f"conventions ratchet: BASELINE ERROR: {exc}")
            return EXIT_CONFIG
    else:
        print(f"conventions ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/conventions_ratchet.py --init`")
        return EXIT_RISE

    try:
        found = scan_tree(root, cfg, vendored)
    except NotConfirmed as exc:
        print(f"conventions ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_counts(found)

    if init:
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"conventions ratchet: wrote {cfg.baseline} with {_total(current)} hit(s) in {len(current)} file(s)")
        return EXIT_PASS

    added, gone = diff(current, baseline)
    if update:
        if added and not allow_additions:
            _report_added(added, found, baseline)
            print("conventions ratchet: nothing written -- fix the code, or raise the baseline with --allow-additions")
            return EXIT_RISE
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"conventions ratchet: {cfg.baseline} now has {_total(current)} hit(s) in {len(current)} file(s) "
              f"({gone} dropped, {_total(added)} added)")
        return EXIT_PASS

    if added:
        _report_added(added, found, baseline)
        print("  Follow the convention (docs/conventions-ratchet.md quotes each rule). A call that must break it "
              "carries `# <rule>-exempt: <reason>`; or (reviewed) raise the baseline with "
              "`--update-baseline --allow-additions`.")
        return EXIT_RISE
    print(f"conventions ratchet: PASS ({_total(current)} baselined hit(s) in {len(current)} file(s), none new; "
          f"rules: {', '.join(cfg.rules)})")
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/conventions_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


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
    sys.exit(main())
