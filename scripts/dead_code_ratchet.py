"""Ratchet on Python functions, classes and methods nothing references.

A helper nobody calls, a class nothing builds and a handler that lost its route
still read as live code: the next reader (agent or human) maintains, tests and
imitates them. Unused imports and locals are the static ratchet's job; this
stage finds dead *definitions*, per file, against a committed baseline: it fails
only on a dead definition the baseline does not already record. Existing debt
never blocks work, but it cannot grow.

How it decides (a stdlib `ast` pass over every tracked `.py` file, no imports run):

  * a definition is a `def`, `async def` or `class`, at any depth;
  * it is **referenced** when its name is read anywhere in the tracked Python
    tree: a bare name (`f()`, `cb=f`, a type annotation), an attribute
    (`obj.f`, `self.f = ...`) or a `from m import f`. Matching is by name, so a
    dead `close` hides behind any other `.close`: misses, never false alarms;
  * an **entry point** is never reported: dunder names, a module's `__all__`,
    pytest tests, hooks and unittest set-ups, a definition whose decorator
    matches `entry_decorators` (route verbs, fixtures, validators, commands and
    `*.register` are built in), a public top-level name of a component this
    scaffold publishes (`[components]`), and the `allow` globs;
  * **confidence** is 100 when the name appears nowhere else, and 60 when it
    may be reached dynamically: the name sits in a string literal or a tracked
    non-Python file (a template, a `pyproject.toml` entry point, a script), its
    module calls `getattr` with a computed name or reads `globals()`, or it is a
    method of a class whose base comes from outside the repo (a framework may
    call `run`, `emit`, `do_GET`). Only findings at or above `min_confidence`
    count.

Opt-in, per adopter, in its own `.fleet.toml`:

    [dead_code_ratchet]
    enabled          = true
    baseline         = ".dead-code-ratchet-baseline.json"  # default
    include          = ["src/", "app/"]                    # path prefixes; default: the whole repo
    exclude          = ["src/generated/**"]                # globs; never reported, still read for references
    min_confidence   = 100                                 # default; 60 also reports possible dynamic use
    entry_decorators = ["bus.subscribe"]                   # globs over the decorator, added to the built-ins
    allow            = ["app/tray/menu.py:on_*"]           # globs over `<path>:<qualname>`

Exit codes: 0 pass or not enabled; 1 a new dead definition, or the baseline is
missing / a mode was refused; 2 bad configuration, baseline or arguments; 3 NOT
CONFIRMED (`git` unusable, a tracked `.py` file that does not parse or cannot be
read) -- the gate fails on it. Modes: `check`, `--init`, `--update-baseline`
(shrink only), `--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `dead_code_ratchet`), stdlib-only. Procedure and
limits: docs/dead-code-ratchet.md.
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
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".dead-code-ratchet-baseline.json"
DEFAULT_MIN_CONFIDENCE = 100
CONFIDENCE_DYNAMIC = 60
#: Always excluded, whatever the adopter's own `exclude` adds.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GIT_TIMEOUT_S = 120
#: Non-Python files whose words can name a Python definition: templates, scripts, config, entry points.
#: Markdown is left out on purpose, because docs describe dead code as readily as live code.
MENTION_SUFFIXES = frozenset({
    ".toml", ".cfg", ".ini", ".txt", ".html", ".htm", ".jinja", ".j2", ".js", ".mjs", ".ts", ".tsx",
    ".vue", ".bat", ".cmd", ".ps1", ".sh", ".yml", ".yaml", ".json",
})
MENTION_MAX_BYTES = 2_000_000  # a data dump that large is not where a callback gets named

#: Decorators that hand the definition to a framework, which then calls it by itself.
BUILTIN_ENTRY_DECORATORS = (
    "*.get", "*.post", "*.put", "*.patch", "*.delete", "*.head", "*.options", "*.trace",
    "*.route", "*.api_route", "*.websocket", "*.websocket_route", "*.on_event", "*.exception_handler",
    "*.middleware", "*.command", "*.group", "*.callback", "*.register", "*.hookimpl", "*.hookspec",
    "fixture", "*.fixture", "*.listens_for", "*.task", "*.receiver",
    "field_validator", "*.field_validator", "model_validator", "*.model_validator",
    "validator", "*.validator", "root_validator", "*.root_validator",
    "field_serializer", "*.field_serializer", "model_serializer", "*.model_serializer",
    "computed_field", "*.computed_field", "*.tool", "*.resource", "*.prompt",
)
#: Methods a test runner calls by name.
TEST_RUNNER_NAMES = frozenset({
    "setUp", "tearDown", "setUpClass", "tearDownClass", "setUpModule", "tearDownModule", "asyncSetUp",
    "asyncTearDown", "setup_method", "teardown_method", "setup_class", "teardown_class",
    "setup_module", "teardown_module", "setup_function", "teardown_function",
})
#: Bases that do not call a subclass's methods on their own, so they keep a method's confidence at 100.
INERT_BASES = frozenset({
    "object", "ABC", "Generic", "Protocol", "NamedTuple", "TypedDict", "Exception", "BaseException",
    "Enum", "IntEnum", "StrEnum", "Flag", "IntFlag",
})
#: Bases whose subclasses a test runner collects, whatever the class is called.
TEST_CASE_BASES = frozenset({"TestCase", "IsolatedAsyncioTestCase"})
_DYNAMIC_CALLS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
_DYNAMIC_SCOPE = frozenset({"globals", "locals", "vars"})

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "include", "exclude", "min_confidence", "entry_decorators", "allow"}
_WORD = re.compile(r"[A-Za-z_]\w*")
_SYMBOLIC_STRING = re.compile(r"[A-Za-z_][\w.:]*")
#: A call or an attribute inside any other string: JavaScript a webview runs (`api.set_theme(x)`), a code sample.
_CODE_WORD = re.compile(r"\.([A-Za-z_]\w*)|([A-Za-z_]\w*)\s*\(")


class ConfigError(RuntimeError):
    """The `[dead_code_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A tracked Python file could not be read or parsed, or git could not list the tree."""


@dataclass(frozen=True)
class Config:
    baseline: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    min_confidence: int
    entry_decorators: tuple[str, ...]
    allow: tuple[str, ...]


@dataclass(frozen=True)
class Definition:
    path: str
    line: int
    qualname: str  # `func`, `Class`, `Class.method`, `outer.inner`
    kind: str  # "function", "method" or "class"
    decorators: tuple[str, ...]
    bases: tuple[str, ...]  # a class's own base names; for a method, its class's

    @property
    def name(self) -> str:
        return self.qualname.rsplit(".", 1)[-1]


@dataclass
class FileScan:
    """What one parsed module defines and reads."""

    definitions: list[Definition] = field(default_factory=list)
    names: set[str] = field(default_factory=set)  # every name read as a name, attribute or import
    strings: set[str] = field(default_factory=set)  # words of identifier-shaped string literals
    exports: set[str] = field(default_factory=set)  # `__all__`
    classes: dict[str, tuple[str, ...]] = field(default_factory=dict)  # class name -> base names
    dynamic: bool = False  # computed getattr, globals(), locals(), vars() or a module `__getattr__`


@dataclass(frozen=True)
class Finding:
    definition: Definition
    confidence: int


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[dead_code_ratchet] {name} must be a list of non-empty strings")
    return tuple(value)


def parse_config(toml_text: str) -> Config | None:
    """`[dead_code_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key must not
    quietly run with defaults.
    """
    table = tomllib.loads(toml_text).get("dead_code_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[dead_code_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[dead_code_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[dead_code_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[dead_code_ratchet] baseline must be a non-empty string")
    floor = table.get("min_confidence", DEFAULT_MIN_CONFIDENCE)
    if not isinstance(floor, int) or isinstance(floor, bool) or not 0 <= floor <= 100:
        raise ConfigError("[dead_code_ratchet] min_confidence must be an integer from 0 to 100")
    return Config(
        baseline,
        _str_list(table, "include", ()),
        _str_list(table, "exclude", ()),
        floor,
        _str_list(table, "entry_decorators", ()),
        _str_list(table, "allow", ()),
    )


def _dests(toml_text: str, table_name: str, key: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get(table_name)
    if not isinstance(table, dict):
        return ()
    found = []
    for entry in table.values():
        value = entry.get(key) if isinstance(entry, dict) else None
        if isinstance(value, str) and value.strip("/"):
            found.append(value.replace("\\", "/").strip("/"))
    return tuple(sorted(found))


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...], tuple[str, ...]]:
    """(config or None when not enabled, `[vendored]` dests, `[components]` srcs)."""
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text), _dests(text, "vendored", "dest"), _dests(text, "components", "src")
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
    """Is `path` a Python file whose definitions this stage reports?"""
    if not path.endswith(".py"):
        return False
    if cfg.include and not any(_under(path, p) for p in cfg.include):
        return False
    if any(_under(path, d) for d in vendored):
        return False
    return not matches_any(path, BUILTIN_EXCLUDES + cfg.exclude)


def is_test_module(path: str) -> bool:
    name = PurePosixPath(path).name
    return name == "conftest.py" or name.startswith("test") or name.endswith("_test.py")


# ---------------------------------------------------------------- scanning

def dotted_name(node: ast.expr) -> str:
    """`app.get` for `@app.get("/x")` or `@app.get`; "" when the expression is not a plain dotted name."""
    if isinstance(node, ast.Call):
        node = node.func
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _base_name(node: ast.expr) -> str:
    """The last segment of a base class expression: `Thread` for `threading.Thread`, `Generic` for `Generic[T]`."""
    if isinstance(node, ast.Subscript):
        node = node.value
    return dotted_name(node).rsplit(".", 1)[-1]


def _docstring_ids(tree: ast.Module) -> set[int]:
    """`id()` of every docstring node, so prose never lowers a definition's confidence."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                ids.add(id(first.value))
    return ids


def _all_names(node: ast.Assign | ast.AugAssign | ast.AnnAssign) -> set[str]:
    """String items of a module-level `__all__ = [...]` / `+= [...]`."""
    value = node.value
    if not isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return set()
    return {e.value for e in value.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}


class _Collector(ast.NodeVisitor):
    def __init__(self, path: str, docstrings: set[int]) -> None:
        self.path = path
        self.docstrings = docstrings
        self.scan = FileScan()
        self.scope: list[tuple[str, ast.AST]] = []  # (name, def node) from the module down

    def _define(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, kind: str) -> None:
        parent = self.scope[-1][1] if self.scope else None
        bases = self.scan.classes.get(node.name, ()) if isinstance(node, ast.ClassDef) else ()
        if isinstance(parent, ast.ClassDef) and kind == "function":
            kind, bases = "method", self.scan.classes.get(parent.name, ())
        qualname = ".".join([n for n, _ in self.scope] + [node.name])
        decorators = tuple(d for d in (dotted_name(x) for x in node.decorator_list) if d)
        self.scan.definitions.append(
            Definition(self.path, node.lineno, qualname, kind, decorators, bases)
        )

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_def(node)

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._define(node, "function")
        if not self.scope and node.name == "__getattr__":
            self.scan.dynamic = True
        self.scope.append((node.name, node))
        self.generic_visit(node)
        self.scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.scan.classes[node.name] = tuple(b for b in (_base_name(x) for x in node.bases) if b)
        self._define(node, "class")
        self.scope.append((node.name, node))
        self.generic_visit(node)
        self.scope.pop()

    def visit_Name(self, node: ast.Name) -> None:
        if not isinstance(node.ctx, ast.Store):
            self.scan.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self.scan.names.add(node.attr)  # a store counts too: `obj.prop = v` runs a property setter
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            self.scan.names.add(alias.name)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            if func.id in _DYNAMIC_SCOPE:
                self.scan.dynamic = True
            elif func.id in _DYNAMIC_CALLS and len(node.args) >= 2 and not isinstance(node.args[1], ast.Constant):
                self.scan.dynamic = True
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and id(node) not in self.docstrings:
            text = node.value.strip()
            if _SYMBOLIC_STRING.fullmatch(text):
                self.scan.strings.update(_WORD.findall(text))
            else:
                self.scan.strings.update(a or b for a, b in _CODE_WORD.findall(text))

    def _visit_assign(self, node: ast.Assign | ast.AugAssign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not self.scope and any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            self.scan.exports |= _all_names(node)
        self.generic_visit(node)

    visit_Assign = _visit_assign
    visit_AugAssign = _visit_assign
    visit_AnnAssign = _visit_assign


def scan_source(source: bytes | str, path: str) -> FileScan:
    """Parse one module; raises `NotConfirmed` when it does not parse (a bad encoding included)."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape in the scanned code is not this stage's finding
            tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        where = f" (line {exc.lineno})" if isinstance(exc, SyntaxError) and exc.lineno else ""
        raise NotConfirmed(f"{path}: does not parse{where}: {exc.msg if isinstance(exc, SyntaxError) else exc}") from exc
    collector = _Collector(path, _docstring_ids(tree))
    collector.visit(tree)
    return collector.scan


# ------------------------------------------------------------------ judge

@dataclass(frozen=True)
class Usage:
    """What the whole tracked tree reads, merged across files."""

    names: frozenset[str]
    mentions: frozenset[str]  # words in string literals and tracked non-Python files
    classes: dict[str, tuple[str, ...]]


def _external_base(bases: tuple[str, ...], classes: dict[str, tuple[str, ...]], seen: frozenset[str]) -> bool:
    """Does any base, followed through the repo's own classes, come from outside the repo?"""
    for base in bases:
        if base in INERT_BASES or base in seen:
            continue
        if base not in classes:
            return True
        if _external_base(classes[base], classes, seen | {base}):
            return True
    return False


def _inherits(bases: tuple[str, ...], targets: frozenset[str], classes: dict[str, tuple[str, ...]],
              seen: frozenset[str] = frozenset()) -> bool:
    """Does any base, followed through the repo's own classes, have one of the `targets` names?"""
    for base in bases:
        if base in targets:
            return True
        if base in classes and base not in seen and _inherits(classes[base], targets, classes, seen | {base}):
            return True
    return False


def _is_test_class(name: str, bases: tuple[str, ...], classes: dict[str, tuple[str, ...]]) -> bool:
    return name.startswith("Test") or _inherits(bases, TEST_CASE_BASES, classes)


def is_entry_point(d: Definition, scan: FileScan, cfg: Config, component: bool,
                   classes: dict[str, tuple[str, ...]]) -> bool:
    """A definition something outside the Python call graph runs: never reported."""
    name = d.name
    if name.startswith("__") and name.endswith("__"):
        return True
    top_level = "." not in d.qualname
    if top_level and name in scan.exports:
        return True
    if component and top_level and not name.startswith("_"):
        return True  # the published API of a component adopters vendor
    if name in TEST_RUNNER_NAMES or (top_level and name.startswith("pytest_")):
        return True  # a pytest hook runs from any plugin module, not only conftest.py
    if is_test_module(d.path):
        if d.kind == "class" and top_level and _is_test_class(name, d.bases, classes):
            return True
        if name.startswith("test") and d.kind != "class":
            return True  # a test method on a mixin runs through the test classes that inherit it
    if any(matches_any(dec, BUILTIN_ENTRY_DECORATORS + cfg.entry_decorators) for dec in d.decorators):
        return True
    return matches_any(f"{d.path}:{d.qualname}", cfg.allow)


def confidence(d: Definition, scan: FileScan, usage: Usage) -> int:
    if d.name in usage.mentions or scan.dynamic:
        return CONFIDENCE_DYNAMIC
    if d.kind == "method" and _external_base(d.bases, usage.classes, frozenset()):
        return CONFIDENCE_DYNAMIC
    return 100


def judge(scans: dict[str, FileScan], usage: Usage, cfg: Config, reported: set[str],
          components: tuple[str, ...]) -> dict[str, list[Finding]]:
    """Dead definitions at or above the confidence floor, per reported file."""
    found: dict[str, list[Finding]] = {}
    for path in sorted(reported):
        scan = scans[path]
        component = path in components
        for d in scan.definitions:
            if d.name in usage.names or is_entry_point(d, scan, cfg, component, usage.classes):
                continue
            score = confidence(d, scan, usage)
            if score >= cfg.min_confidence:
                found.setdefault(path, []).append(Finding(d, score))
    return found


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
    """Repo-relative, forward-slash paths of every tracked file."""
    args = ["ls-files", "-z"]
    proc = _git(root, args)
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git {' '.join(args)} exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


def _mention_words(root: Path, path: str) -> set[str]:
    """Words of a tracked non-Python file; an unreadable one only lowers no confidence, so it is skipped."""
    try:
        target = root / path
        if target.stat().st_size > MENTION_MAX_BYTES:
            return set()
        return set(_WORD.findall(target.read_bytes().decode("utf-8", "replace")))
    except OSError:
        return set()


def dead_tree(root: Path, cfg: Config, vendored: tuple[str, ...],
              components: tuple[str, ...]) -> dict[str, list[Finding]]:
    """Dead, non-allowed definitions per in-scope file (only files with at least one)."""
    scans: dict[str, FileScan] = {}
    mentions: set[str] = set()
    for path in tracked_files(root):
        if matches_any(path, BUILTIN_EXCLUDES):
            continue
        if path.endswith(".py"):
            try:
                source = (root / path).read_bytes()
            except FileNotFoundError:
                continue  # tracked but deleted in the working tree: nothing to read
            except OSError as exc:
                raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
            scans[path] = scan_source(source, path)
        elif path != cfg.baseline and PurePosixPath(path).suffix.lower() in MENTION_SUFFIXES:
            # The baseline lists the dead names; reading it would make each one look referenced.
            mentions |= _mention_words(root, path)
    names: set[str] = set()
    classes: dict[str, tuple[str, ...]] = {}
    for scan in scans.values():
        names |= scan.names
        mentions |= scan.strings
        classes.update(scan.classes)
    usage = Usage(frozenset(names), frozenset(mentions), classes)
    reported = {p for p in scans if in_scope(p, cfg, vendored)}
    return judge(scans, usage, cfg, reported, components)


# ---------------------------------------------------------------- baseline

def current_entries(found: dict[str, list[Finding]]) -> dict[str, Counter[str]]:
    return {path: Counter(f.definition.qualname for f in findings) for path, findings in found.items()}


def read_baseline(path: Path) -> dict[str, Counter[str]]:
    """Baseline file -> ``{source path: Counter(qualname)}``. Raises `ConfigError` on anything off."""
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
    out: dict[str, Counter[str]] = {}
    for item in raw_entries:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            raise ConfigError(f"baseline {path}: malformed entry {item!r}")
        names = item.get("names")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
            raise ConfigError(f"baseline {path}: entry {item!r} needs a non-empty `names` list of strings")
        out[item["key"]] = Counter(names)
    return out


def render_baseline(entries: dict[str, Counter[str]]) -> str:
    """Sorted, one file per line, no line numbers, so a baseline diff reviews cleanly."""
    lines = []
    for key in sorted(entries):
        record = {"key": key, "names": sorted(entries[key].elements())}
        lines.append("    " + json.dumps(record, ensure_ascii=False))
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def diff(current: dict[str, Counter[str]], baseline: dict[str, Counter[str]]) -> tuple[dict[str, Counter[str]], int]:
    """(per file, the definitions beyond the baseline; how many baselined definitions are gone)."""
    added: dict[str, Counter[str]] = {}
    gone = 0
    for path in sorted(set(current) | set(baseline)):
        cur, base = current.get(path, Counter()), baseline.get(path, Counter())
        extra = cur - base
        if extra:
            added[path] = extra
        gone += sum((base - cur).values())
    return added, gone


# -------------------------------------------------------------------- CLI

def _total(entries: dict[str, Counter[str]]) -> int:
    return sum(sum(c.values()) for c in entries.values())


def _report_added(added: dict[str, Counter[str]], found: dict[str, list[Finding]]) -> None:
    print(f"dead code ratchet: {_total(added)} dead definition(s) not in the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for f in found[path]:
            d = f.definition
            if extra[d.qualname] > 0:
                extra[d.qualname] -= 1
                print(f"  {path}:{d.line}: {d.kind} `{d.qualname}` is never referenced (confidence {f.confidence})")


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored, components = load_config(root)
    except ConfigError as exc:
        print(f"dead code ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("dead code ratchet: not enabled -- set [dead_code_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("dead code ratchet: not enabled (no [dead_code_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline

    baseline: dict[str, Counter[str]] = {}
    if init:
        if baseline_path.exists():
            print(f"dead code ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
    elif baseline_path.is_file():
        try:
            baseline = read_baseline(baseline_path)
        except ConfigError as exc:
            print(f"dead code ratchet: BASELINE ERROR: {exc}")
            return EXIT_CONFIG
    else:
        print(f"dead code ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/dead_code_ratchet.py --init`")
        return EXIT_RISE

    try:
        found = dead_tree(root, cfg, vendored, components)
    except NotConfirmed as exc:
        print(f"dead code ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_entries(found)

    if init:
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"dead code ratchet: wrote {cfg.baseline} with {_total(current)} dead definition(s) "
              f"in {len(current)} file(s)")
        return EXIT_PASS

    added, gone = diff(current, baseline)
    if update:
        if added and not allow_additions:
            _report_added(added, found)
            print("dead code ratchet: nothing written -- delete the code, or raise the baseline with --allow-additions")
            return EXIT_RISE
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"dead code ratchet: {cfg.baseline} now has {_total(current)} definition(s) in {len(current)} file(s) "
              f"({gone} dropped, {_total(added)} added)")
        return EXIT_PASS

    if added:
        _report_added(added, found)
        print("  Delete the definition, or call it. An entry point something outside Python runs (a route, a "
              "callback named in a template) goes in [dead_code_ratchet] entry_decorators or allow; or (reviewed) "
              "raise the baseline with `--update-baseline --allow-additions`.")
        return EXIT_RISE
    print(f"dead code ratchet: PASS ({_total(current)} baselined dead definition(s) in {len(current)} file(s), "
          f"none new; min_confidence {cfg.min_confidence})")
    if gone:
        print(f"  {gone} baselined definition(s) are now referenced or gone -- run "
              "`python scripts/dead_code_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop definitions now referenced (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add definitions (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    sys.exit(main())
