"""Ratchet on fleet conventions that one AST or markup rule can check in every repo.

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
    repo's declared wrapper (`git_wrappers`);
  * `password-input-vendored` -- a password input (`type="password"`,
    `type: 'password'`, `.type = 'password'`, `setAttribute('type', 'password')`)
    in a web file (`.html`, `.htm`, `.js`, `.mjs`, `.cjs`, `.ts`, `.jsx`, `.tsx`,
    `.vue`, `.svelte`, `.jinja`, `.jinja2`, `.j2`) outside a `_vendored/sign-in/`
    folder: every app's sign-in goes through the one vendored component. It is
    the only rule that reads web files, and it reads no `.py`;
  * `login-lockout` -- a FastAPI password route (`@<x>.post(".../login")`) in a
    module that does not import the vendored `login_lockout`, or whose handler
    never calls `retry_after(...)`: every password route answers `429` with
    `Retry-After` after repeated wrong passwords;
  * `no-pwsh-stub` -- the `pwsh` stub run as a command (`pwsh` / `pwsh.exe` followed
    by a switch, or the first quoted item of an argv list) in a `.md`, `.ps1`, `.bat`, `.cmd`,
    `.toml`, `.json`, `.yml`, `.yaml` or `.py` file: on the fleet's main machine
    `pwsh` is a 0-byte alias, so a gate or restart command written with it never
    runs. An absolute path to a real install, and `shell: pwsh`, are not hits.
    It reads text lines, so a line that only names the stub to forbid it carries
    the marker on the same line or the line right above;
  * `must-use` -- a call to a dotted name (`sqlite3.connect`, `requests.get`) outside
    the file(s) the repo declares as the one home for that job. The repo lists one
    `[[conventions_ratchet.must_use]]` table per helper (`call`, `helper`, `why`,
    optional `paths`); listing an entry is what turns the rule on. The name is
    resolved through the module's imports (`import x as y`, `from x import f`), the
    way `secret-compare` and `git-wrapper` resolve theirs. Those two are the same
    check with the call set built in and the helper read from `secret_helpers` /
    `git_wrappers`. A method on an instance (`conn.replace`) is not seen.

Test modules (and every file under a `tests/` or `test/` folder) are skipped by
every rule except `no-window`. A single call keeps its exception with a
`# <rule>-exempt: <reason>` comment on its own lines or in the comment block
right above it (in a web file the comment is `<!-- ... -->`, `// ...` or
`/* ... */`); a marker with no reason exempts nothing.

Opt-in, per adopter, in its own `.fleet.toml`:

    [conventions_ratchet]
    enabled        = true
    baseline       = ".conventions-ratchet-baseline.json"  # default
    rules          = ["no-window", "no-drive-path"]          # required: the rules this repo enforces
    exclude        = ["src/generated/**"]                    # globs; never scanned
    print_paths    = ["app/", "src/"]                        # logging-not-print scope (default)
    secret_helpers = ["src/secret_compare.py"]               # required by secret-compare
    git_wrappers   = ["src/git_run.py"]                      # required by git-wrapper

    [[conventions_ratchet.must_use]]                         # one table per helper; enables must-use
    call   = "sqlite3.connect"                               # a dotted name, or a list of them
    helper = ["src/db.py"]                                   # the file(s) that may make the call
    why    = "CLAUDE.md: one get_db dependency owns the connection"
    paths  = ["src/", "app/"]                                # optional scope; default the whole repo

Exit codes: 0 pass or not enabled; 1 a count rose, or the baseline is missing /
a mode was refused; 2 bad configuration, baseline or arguments; 3 NOT CONFIRMED
(`git` unusable, a tracked `.py` file that does not parse, or a scanned file
that cannot be read) --
the gate fails on it. Modes: `check`, `--init`, `--update-baseline` (shrink
only), `--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `conventions_ratchet`), stdlib-only, with its sibling
`conventions_readers.py` (manifest key `conventions_readers`) loaded from beside it.
Procedure and limits: docs/conventions-ratchet.md.
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
import warnings
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        print(f"conventions ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys conventions_ratchet and conventions_readers)")
        sys.exit(3)


# The pure readers (syntax-tree predicates, the web and text line rules) live in the sibling.
_readers = _load_sibling("conventions_readers")
_Findings = Callable[[bytes | str, frozenset[str]], list[tuple[int, str]]]
SIGN_IN_DIR: str = _readers.SIGN_IN_DIR
_docstring_ids: Callable[[ast.Module], set[int]] = _readers._docstring_ids
_is_main_guard: Callable[[ast.If], bool] = _readers._is_main_guard
_branch_platform: Callable[[ast.expr], str | None] = _readers._branch_platform
is_cli_path: Callable[[str], bool] = _readers.is_cli_path
_dotted: Callable[[ast.expr], str] = _readers._dotted
_program: Callable[[ast.Call], str] = _readers._program
_is_login_route: Callable[[ast.expr], bool] = _readers._is_login_route
_calls_retry_after: Callable[[ast.FunctionDef | ast.AsyncFunctionDef], bool] = _readers._calls_retry_after
_writes_to_a_file: Callable[[ast.Call], bool] = _readers._writes_to_a_file
_exempt: Callable[..., bool] = _readers._exempt
_web_findings: _Findings = _readers.web_findings
_text_findings: _Findings = _readers.text_findings

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".conventions-ratchet-baseline.json"
DEFAULT_PRINT_PATHS = ("app/", "src/")
#: Always excluded, whatever the adopter's own `exclude` adds.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GIT_TIMEOUT_S = 120

RULES = ("no-window", "logging-not-print", "no-drive-path", "secret-compare", "git-wrapper",
         "password-input-vendored", "login-lockout", "no-pwsh-stub", "must-use")
#: The rules a test module is held to; fixtures legitimately spell drive paths, raw compares and raw git.
TEST_RULES = frozenset({"no-window"})
#: The rules read from web files rather than Python source.
WEB_RULES = frozenset({"password-input-vendored"})
WEB_SUFFIXES = (".html", ".htm", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".vue", ".svelte",
                ".jinja", ".jinja2", ".j2")
#: The rule that reads plain text lines, in `.py` files and in the non-Python files below.
TEXT_RULES = frozenset({"no-pwsh-stub"})
TEXT_SUFFIXES = (".md", ".ps1", ".bat", ".cmd", ".toml", ".json", ".yml", ".yaml")
#: The config key a rule cannot run without, because it names the repo's own helper.
REQUIRED_KEYS = {"secret-compare": "secret_helpers", "git-wrapper": "git_wrappers"}

SPAWNS = frozenset({
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call", "subprocess.check_output",
    "asyncio.create_subprocess_exec", "asyncio.create_subprocess_shell",
})
SECRET_COMPARES = frozenset({"hmac.compare_digest", "secrets.compare_digest"})
#: Modules whose imports are always followed; a `must_use` entry adds its own call's module.
_TRACKED_MODULES = frozenset({"subprocess", "asyncio", "hmac", "secrets"})
_DOTTED_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+\Z")
_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
#: Absolute system binaries the instructions themselves mandate (`C:/Windows/System32/.../powershell.exe`).
_SYSTEM_DRIVE = re.compile(
    r"[A-Za-z]:[\\/]+(?:Windows|Program Files(?: \(x86\))?|ProgramData)(?:[\\/]|\Z)", re.IGNORECASE
)
#: A bare drive root (`"C:\\"`) anchors a walk of the filesystem; it names no data location.
_DRIVE_ROOT = re.compile(r"[A-Za-z]:[\\/]?\Z")
#: A module that is a command-line entry point prints its output by design.
_CLI_MODULES = frozenset({"argparse", "click", "typer", "fire", "optparse"})
_GIT_PROGRAMS = frozenset({"git", "git.exe"})
#: The vendored lock-out module's name, wherever the app copied it.
LOCKOUT_MODULE = "login_lockout"

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "rules", "exclude", "print_paths", "secret_helpers", "git_wrappers", "must_use"}
_MUST_USE_KEYS = {"call", "helper", "why", "paths"}


class ConfigError(RuntimeError):
    """The `[conventions_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A tracked Python file could not be read or parsed, or git could not list the tree."""


@dataclass(frozen=True)
class MustUse:
    """One `[[conventions_ratchet.must_use]]` entry: these calls belong in the `helper` file(s) only."""

    calls: tuple[str, ...]
    helper: tuple[str, ...]
    why: str
    paths: tuple[str, ...] = ()  # empty: the whole repo


@dataclass(frozen=True)
class Config:
    baseline: str
    rules: tuple[str, ...]
    exclude: tuple[str, ...] = ()
    print_paths: tuple[str, ...] = DEFAULT_PRINT_PATHS
    secret_helpers: tuple[str, ...] = ()
    git_wrappers: tuple[str, ...] = ()
    must_use: tuple[MustUse, ...] = ()


@dataclass(frozen=True)
class _Policy:
    """A call that only the repo's helper may make: the rule it reports as, the names it matches, its text."""

    rule: str
    calls: frozenset[str]
    message: Callable[[str], str]
    programs: frozenset[str] = frozenset()  # non-empty: a spawn counts only when it runs one of these


#: The two older helper rules are built-in policies: the call set is fixed, the helper comes from the config.
BUILTIN_POLICIES = {
    "secret-compare": _Policy("secret-compare", SECRET_COMPARES,
                              lambda name: f"raw `{name}` outside the repo's secret-compare helper"),
    "git-wrapper": _Policy("git-wrapper", SPAWNS,
                           lambda name: f"`{name}` spawns git directly, not through the repo's wrapper",
                           _GIT_PROGRAMS),
}


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...],
              where: str = "[conventions_ratchet]") -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"{where} {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def _must_use_entry(item: object, number: int) -> MustUse:
    where = f"[[conventions_ratchet.must_use]] #{number}"
    if not isinstance(item, dict):
        raise ConfigError("[conventions_ratchet] must_use must be an array of tables")
    unknown = sorted(set(item) - _MUST_USE_KEYS)
    if unknown:
        raise ConfigError(f"{where}: unknown key(s) {', '.join(unknown)}; known: {', '.join(sorted(_MUST_USE_KEYS))}")
    call = item.get("call")
    calls = (call,) if isinstance(call, str) and call else _str_list(item, "call", (), where)
    if not calls:
        raise ConfigError(f"{where} needs call: the dotted name(s) only the helper may call, e.g. \"sqlite3.connect\"")
    bad = [c for c in calls if not _DOTTED_NAME.match(c)]
    if bad:
        raise ConfigError(f"{where}: call {bad[0]!r} must be a dotted name such as \"module.function\"")
    helper = _str_list(item, "helper", (), where)
    if not helper:
        raise ConfigError(f"{where} needs helper: the file(s) that may make the call")
    why = item.get("why")
    if not isinstance(why, str) or not why.strip():
        raise ConfigError(f"{where} needs why: the convention it enforces, as a non-empty string")
    return MustUse(calls, helper, why.strip(), _str_list(item, "paths", (), where))


def _must_use_entries(table: dict[str, object]) -> tuple[MustUse, ...]:
    raw = table.get("must_use", [])
    if not isinstance(raw, list):
        raise ConfigError("[conventions_ratchet] must_use must be an array of tables ([[conventions_ratchet.must_use]])")
    return tuple(_must_use_entry(item, number) for number, item in enumerate(raw, start=1))


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
    entries = _must_use_entries(table)
    if not rules and not entries:
        raise ConfigError(f"[conventions_ratchet] rules must name at least one of: {', '.join(RULES)}, "
                          "or declare a [[conventions_ratchet.must_use]] entry")
    bad = sorted(set(rules) - set(RULES))
    if bad:
        raise ConfigError(f"[conventions_ratchet] unknown rule(s) {', '.join(bad)}; known: {', '.join(RULES)}")
    if "must-use" in rules and not entries:
        raise ConfigError("[conventions_ratchet] rule must-use needs a [[conventions_ratchet.must_use]] entry")
    cfg = Config(
        baseline,
        tuple(r for r in RULES if r in rules or (r == "must-use" and entries)),
        _str_list(table, "exclude", ()),
        _str_list(table, "print_paths", DEFAULT_PRINT_PATHS),
        _str_list(table, "secret_helpers", ()),
        _str_list(table, "git_wrappers", ()),
        entries,
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
    """Is `path` a Python or web file this stage scans? A vendored copy's debt belongs to the repo it came from."""
    if not (path.endswith(".py") or is_web_file(path) or is_text_file(path, cfg)):
        return False
    if any(_under(path, d) for d in vendored):
        return False
    if path == cfg.baseline or path.endswith("-ratchet-baseline.json"):
        return False  # a baseline is data about hits, never a hit
    return not matches_any(path, BUILTIN_EXCLUDES + cfg.exclude)


def is_web_file(path: str) -> bool:
    return path.lower().endswith(WEB_SUFFIXES)


def is_text_file(path: str, cfg: Config) -> bool:
    """A non-Python text file, read only when the repo enabled a rule that reads text."""
    return path.lower().endswith(TEXT_SUFFIXES) and bool(TEXT_RULES & set(cfg.rules))


def in_sign_in_component(path: str) -> bool:
    return path.startswith(SIGN_IN_DIR + "/") or f"/{SIGN_IN_DIR}/" in path


def is_test_module(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def rules_for(path: str, cfg: Config) -> frozenset[str]:
    """The enabled rules that apply to one in-scope file."""
    rules = set(cfg.rules)
    if is_web_file(path):
        rules &= WEB_RULES
        if in_sign_in_component(path):
            rules.discard("password-input-vendored")
    elif path.endswith(".py"):
        rules -= WEB_RULES
    else:
        rules &= TEXT_RULES
    if is_test_module(path):
        rules &= TEST_RULES
    if not any(_under(path, p) for p in cfg.print_paths):
        rules.discard("logging-not-print")
    if any(_under(path, h) for h in cfg.secret_helpers):
        rules.discard("secret-compare")
    if any(_under(path, w) for w in cfg.git_wrappers):
        rules.discard("git-wrapper")
    if "must-use" in rules and not entries_for(path, cfg):
        rules.discard("must-use")
    return frozenset(rules)


def entries_for(path: str, cfg: Config) -> tuple[MustUse, ...]:
    """The `must_use` entries that bind one file: in the entry's `paths`, and not one of its helpers."""
    return tuple(
        e for e in cfg.must_use
        if (not e.paths or any(_under(path, p) for p in e.paths)) and not any(_under(path, h) for h in e.helper)
    )


# ---------------------------------------------------------------- scanning

def _login_lockout_hits(scanner: _Scanner) -> list[tuple[ast.AST, str, str]]:
    """A login route with no lock-out: the module never imports it, or the handler never checks it."""
    found: list[tuple[ast.AST, str, str]] = []
    for decorator, handler in scanner.login_routes:
        if not scanner.imports_lockout:
            message = f"login route `{handler.name}` in a module that does not import {LOCKOUT_MODULE}"
        elif not _calls_retry_after(handler):
            message = f"login route `{handler.name}` never calls retry_after() before the password check"
        else:
            continue
        found.append((decorator, "login-lockout", message))
    return found


def _entry_policy(entry: MustUse) -> _Policy:
    homes = ", ".join(entry.helper)
    return _Policy("must-use", frozenset(entry.calls),
                   lambda name: f"`{name}(...)` outside {homes}: {entry.why}")


def _module_prefixes(call: str) -> set[str]:
    """`urllib.request.urlopen` -> {`urllib`, `urllib.request`}: every module an import of it can name."""
    parts = call.split(".")[:-1]
    return {".".join(parts[:i]) for i in range(1, len(parts) + 1)}


class _Scanner(ast.NodeVisitor):
    def __init__(self, path: str, rules: frozenset[str], docstrings: set[int],
                 entries: tuple[MustUse, ...] = ()) -> None:
        self.path = path
        self.rules = rules
        self.docstrings = docstrings
        self.policies = [p for rule, p in BUILTIN_POLICIES.items() if rule in rules]
        self.tracked = set(_TRACKED_MODULES)
        if "must-use" in rules:
            self.policies.extend(_entry_policy(e) for e in entries)
            for entry in entries:
                for call in entry.calls:
                    self.tracked |= _module_prefixes(call)
        self.hits: list[tuple[ast.AST, str, str]] = []
        self.aliases: dict[str, str] = {}  # local name -> `module` or `module.attr`
        self.print_shadowed = False
        self.cli = False  # a `__main__` guard or a CLI-parser import: the module prints its output
        self.posix_depth = 0  # inside a branch that never runs on Windows
        self.imports_lockout = False
        self.login_routes: list[tuple[ast.expr, ast.FunctionDef | ast.AsyncFunctionDef]] = []

    def _resolve(self, func: ast.expr) -> str:
        dotted = _dotted(func)
        head, _, rest = dotted.partition(".")
        target = self.aliases.get(head)
        if target is None:
            return ""
        return f"{target}.{rest}" if rest else target

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname:
                if alias.name in self.tracked:
                    self.aliases[alias.asname] = alias.name
            elif alias.name.split(".")[0] in self.tracked:
                self.aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]  # `import a.b` binds `a`
            if alias.name.split(".")[0] in _CLI_MODULES:
                self.cli = True
            if alias.name.split(".")[-1] == LOCKOUT_MODULE:
                self.imports_lockout = True

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "").split(".")[0] in _CLI_MODULES:
            self.cli = True
        if (node.module or "").split(".")[-1] == LOCKOUT_MODULE:
            self.imports_lockout = True
        for alias in node.names:
            if alias.name == LOCKOUT_MODULE:
                self.imports_lockout = True
            if node.module in self.tracked and not node.level:
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
        if "login-lockout" in self.rules:
            self.login_routes.extend((d, node) for d in node.decorator_list if _is_login_route(d))
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
        elif (isinstance(node.func, ast.Name) and node.func.id == "print" and "logging-not-print" in self.rules
              and not _writes_to_a_file(node)):
            self.hits.append((node, "logging-not-print", "`print(...)` in app code; use `logging`"))
        for policy in self.policies:
            if name in policy.calls and (not policy.programs or _program(node) in policy.programs):
                self.hits.append((node, policy.rule, policy.message(name)))
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


def scan_web(source: bytes | str, path: str, rules: frozenset[str]) -> list[Hit]:
    """Hits of the web rules in one HTML or JS file, read line by line."""
    return [Hit(path, n, "password-input-vendored", m) for n, m in _web_findings(source, rules)]


def scan_text(source: bytes | str, path: str, rules: frozenset[str]) -> list[Hit]:
    """Hits of the text rules in one file, read line by line (any file type, comments and strings alike)."""
    return [Hit(path, n, "no-pwsh-stub", m) for n, m in _text_findings(source, rules)]


def scan_source(source: bytes | str, path: str, rules: frozenset[str],
                entries: tuple[MustUse, ...] = ()) -> list[Hit]:
    """Hits of the given rules in one module; raises `NotConfirmed` when it does not parse.

    `entries` are the `must_use` entries that bind this file (`entries_for`); `must-use` reports nothing without them.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape in the scanned code is not this stage's finding
            tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        where = f" (line {exc.lineno})" if isinstance(exc, SyntaxError) and exc.lineno else ""
        raise NotConfirmed(f"{path}: does not parse{where}: {exc.msg if isinstance(exc, SyntaxError) else exc}") from exc
    if not rules:
        return []
    scanner = _Scanner(path, rules, _docstring_ids(tree), entries)
    scanner.visit(tree)
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    lines = text.splitlines()
    hits = []
    for node, rule, message in [*scanner.hits, *_login_lockout_hits(scanner)]:
        if rule == "logging-not-print" and (scanner.print_shadowed or scanner.cli or is_cli_path(path)):
            continue
        if not _exempt(rule, _window(node, lines)):
            hits.append(Hit(path, getattr(node, "lineno", 0), rule, message))
    hits.extend(scan_text(source, path, rules))
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
    """Hits per in-scope tracked `.py`, web or (with a text rule on) text file; only files with a hit.

    A baseline, this stage's or any other ratchet's, is JSON and never counts as
    a hit or as evidence against one, even when `.json` files are read.
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
        rules = rules_for(path, cfg)
        if is_web_file(path):
            hits = scan_web(source, path, rules)
        elif path.endswith(".py"):
            hits = scan_source(source, path, rules, entries_for(path, cfg) if "must-use" in rules else ())
        else:
            hits = scan_text(source, path, rules)
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
    # Reports contain non-ASCII (e.g. "→"); a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
