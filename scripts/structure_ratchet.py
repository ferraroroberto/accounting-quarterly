"""Ratchet on file size, function length, flat directories, the layer contract and the root layout.

A file, a function or a directory that only ever grows is how a repo ends up with
a 20,000-line module that every agent then imitates. This stage stops that growth
deterministically, against a committed baseline: existing offenders never block
work, but they cannot grow, and a new offender fails by name. Where code goes
instead is the canonical layout in project-scaffolding's docs/repo-layout.md;
every failure message points at the section it breaks.

Baseline keys (`KINDS`): file size, function length (Python from `ast`, JS from a brace
matcher), flat directory, layer contract, and unmeasured JS functions. The near-limit band
(`BANDS`) is two shrink-only counts, files and functions at or above `near_limit_ratio` of
their limit, so a split that stops just under the limit fails like a new offender; a
baseline from before the band loads unchanged and `--update-baseline` records it. The root
layout (`shape`, `extra_roots`, ids in `SHAPES`) is never baselined.

Opt-in per adopter (`[structure_ratchet] enabled = true`; keys `_CONFIG_KEYS`, defaults
`DEFAULT_*`). Exit 0 pass or not enabled, 1 above the baseline or refused, 2 bad config,
baseline or arguments, 3 NOT CONFIRMED (never a pass). Vendor-verbatim (manifest key
`structure_ratchet`), stdlib-only. Modes, limits and the config table: docs/structure-ratchet.md.
"""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".structure-ratchet-baseline.json"
DEFAULT_MAX_FILE_LINES = 1000
DEFAULT_MAX_FUNCTION_LINES = 100
DEFAULT_MAX_DIR_MODULES = 40
DEFAULT_NEAR_LIMIT_RATIO = 0.9
#: Where a split should land: low enough that both halves start well clear of the near-limit band.
SPLIT_TARGET_RATIO = 0.75
#: Near-limit band -> (unit counted, failure title, docs/repo-layout.md anchor, advice).
BANDS = {
    "files": ("file(s)", "near-limit file", "file-size",
              "split it to about {target:,} lines, moving code into a new module beside it named for what it does"),
    "functions": ("function(s)", "near-limit function", "function-length",
                  "split it into named helpers of about {target:,} lines"),
}
DEFAULT_LAYERS = (("src", "app"),)

SIZE_SUFFIXES = frozenset({".py", ".js", ".mjs", ".ts", ".css", ".html", ".ps1", ".sql"})
JS_SUFFIXES = frozenset({".js", ".mjs"})
DIR_SUFFIXES = frozenset({".py", ".js", ".mjs", ".css"})
#: Always excluded, whatever the adopter's own `exclude` adds.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GENERATED_MARKERS = (b"DO NOT EDIT", b"@generated")  # case-sensitive, like the sibling ratchets
GENERATED_HEAD_LINES = 5
GIT_TIMEOUT_S = 120
LAYOUT_DOC = "project-scaffolding docs/repo-layout.md"
#: Root folders every shape allows, besides hidden ones (`.github`, `.claude`, ...).
ALWAYS_ALLOWED = ("docs",)

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {
    "enabled", "baseline", "max_file_lines", "max_function_lines", "max_dir_modules", "near_limit_ratio",
    "layers", "exclude",
}

#: Baseline key prefix -> (the field its count is stored under, rule title, docs/repo-layout.md anchor).
KINDS = {
    "size": ("lines", "file size", "file-size"),
    "fn": ("lines", "function length", "function-length"),
    "dir": ("modules", "flat directory", "flat-directory"),
    "layer": ("imports", "layer contract", "layer-contract"),
    "unmeasured": ("functions", "function length", "function-length"),
}


@dataclass(frozen=True)
class Shape:
    """One repo shape's root layout: folders it must have, folders it may have, and its layer pairs."""

    required: tuple[str, ...]
    allowed: tuple[str, ...]
    layers: tuple[tuple[str, str], ...] = ()


#: The `.fleet.toml` `shape` ids, mirroring the trees in docs/repo-layout.md. A required folder
#: cannot be waived (`extra_roots` only adds), so a set is required only where every repo of the
#: shape has it. docs/structure-ratchet.md carries the same table; a test compares the two.
SHAPES = {
    "webapp-tray": Shape(("app", "src", "tests", "docs"), ("scripts", "data", "brand", "assets", "config"), DEFAULT_LAYERS),
    "streamlit": Shape(("app", "src", "tests"), ("scripts", "data", "brand"), DEFAULT_LAYERS),
    "pipeline-cli": Shape(("tests",), ("src", "common", "scripts", "config", "data")),
    "skills-governance": Shape(("skills", "tests"), ("hooks", "scripts", "architecture")),
    "node": Shape(("src",), ("test", "scripts")),
    "static-site": Shape((), ("css", "js", "assets", "games")),
    "agent-workspace": Shape((), ("scripts",)),
    # No layer pair: the service in src/ mounts its admin app, so src imports app_web by design.
    "service": Shape(("src", "tests", "app_web", "tray"), ("scripts", "config", "launchers", "assets")),
}


class ConfigError(RuntimeError):
    """The `[structure_ratchet]` table or the baseline file is unusable."""


class NotConfirmed(RuntimeError):
    """A file could not be measured, so its findings are unknown."""


@dataclass(frozen=True)
class Config:
    baseline: str
    max_file_lines: int
    max_function_lines: int
    max_dir_modules: int
    layers: tuple[tuple[str, str], ...]
    exclude: tuple[str, ...]
    shape: str | None = None  # top-level `shape` in .fleet.toml; validated against SHAPES in run()
    extra_roots: tuple[str, ...] = ()
    near_limit_ratio: float = DEFAULT_NEAR_LIMIT_RATIO

    def band_range(self, band: str) -> tuple[int, int]:
        """(lowest, highest) line count inside `band`; the rounding keeps 0.9 * 100 at 90, not 91."""
        limit = self.max_file_lines if band == "files" else self.max_function_lines
        return math.ceil(round(limit * self.near_limit_ratio, 9)), limit


@dataclass(frozen=True)
class Span:
    """One function: its dotted name and its length in lines (None when its end was not found)."""

    qualname: str
    lines: int | None


@dataclass(frozen=True)
class Finding:
    key: str
    value: int
    detail: str  # what was measured, for the failure message
    advice: str  # where the code goes instead
    members: tuple[str, ...] = ()  # a near-limit band's files or functions, listed under the failure


# ----------------------------------------------------------------- config

def _positive_int(table: dict[str, object], name: str, default: int) -> int:
    value = table.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"[structure_ratchet] {name} must be a positive integer")
    return value


def _layers(table: dict[str, object], shape: str | None) -> tuple[tuple[str, str], ...]:
    if "layers" not in table:
        return SHAPES[shape].layers if shape in SHAPES else DEFAULT_LAYERS
    value = table["layers"]
    error = ConfigError('[structure_ratchet] layers must be a list of ["lower", "upper"] root pairs')
    if not isinstance(value, list):
        raise error
    pairs: list[tuple[str, str]] = []
    for pair in value:
        if not (isinstance(pair, list) and len(pair) == 2 and all(isinstance(p, str) and p.strip("/") for p in pair)):
            raise error
        pairs.append((pair[0].replace("\\", "/").strip("/"), pair[1].replace("\\", "/").strip("/")))
    return tuple(pairs)


def _shape(data: dict[str, object]) -> str | None:
    value = data.get("shape")
    if value is not None and not isinstance(value, str):
        raise ConfigError("shape in .fleet.toml must be a string")
    return value


def _extra_roots(data: dict[str, object]) -> tuple[str, ...]:
    value = data.get("extra_roots", [])
    error = ConfigError("extra_roots in .fleet.toml must be a list of top-level folder names")
    if not isinstance(value, list):
        raise error
    names = set()
    for item in value:
        name = item.replace("\\", "/").strip("/") if isinstance(item, str) else ""
        if not name or "/" in name:
            raise error
        names.add(name)
    return tuple(sorted(names))


def parse_config(toml_text: str) -> Config | None:
    """`[structure_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key must not
    quietly run with defaults.
    """
    data = tomllib.loads(toml_text)
    table = data.get("structure_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[structure_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[structure_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[structure_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[structure_ratchet] baseline must be a non-empty string")
    exclude = table.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(g, str) and g for g in exclude):
        raise ConfigError("[structure_ratchet] exclude must be a list of non-empty strings")
    ratio = table.get("near_limit_ratio", DEFAULT_NEAR_LIMIT_RATIO)
    if isinstance(ratio, bool) or not isinstance(ratio, (int, float)) or not 0 < ratio < 1:
        raise ConfigError("[structure_ratchet] near_limit_ratio must be a number between 0 and 1")
    shape = _shape(data)
    return Config(
        baseline,
        _positive_int(table, "max_file_lines", DEFAULT_MAX_FILE_LINES),
        _positive_int(table, "max_function_lines", DEFAULT_MAX_FUNCTION_LINES),
        _positive_int(table, "max_dir_modules", DEFAULT_MAX_DIR_MODULES),
        _layers(table, shape),
        tuple(exclude),
        shape,
        _extra_roots(data),
        float(ratio),
    )


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    """Every `dest` in the adopter's own `[vendored]` table (files or folders)."""
    table = tomllib.loads(toml_text).get("vendored")
    if not isinstance(table, dict):
        return ()
    found = {
        entry["dest"].replace("\\", "/").strip("/")
        for entry in table.values()
        if isinstance(entry, dict) and isinstance(entry.get("dest"), str) and entry["dest"].strip("/")
    }
    return tuple(sorted(found))


def load_config(root: Path) -> tuple[Config | None, tuple[str, ...]]:
    """(config or None when not enabled, vendored dests)."""
    path = root / ".fleet.toml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    try:
        return parse_config(text), vendored_dests(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc


# ------------------------------------------------------------------- scope

def _glob_regex(glob: str) -> re.Pattern[str]:
    """Repo-root-anchored glob -> regex; `**/` is zero or more directories."""
    parts = re.split(r"(\*\*/|\*\*|\*|\?)", glob)
    wild = {"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*", "?": "[^/]"}
    return re.compile("".join(wild.get(p, re.escape(p)) for p in parts) + r"\Z")


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """Is `path` a file this stage measures (before the generated-file check)?"""
    if PurePosixPath(path).suffix.lower() not in SIZE_SUFFIXES:
        return False
    if any(_under(path, d) for d in vendored):
        return False
    return not any(_glob_regex(g).match(path) for g in BUILTIN_EXCLUDES + cfg.exclude)


def is_generated(raw: bytes) -> bool:
    head = b"\n".join(raw.split(b"\n", GENERATED_HEAD_LINES)[:GENERATED_HEAD_LINES])
    return any(marker in head for marker in GENERATED_MARKERS)


def count_lines(raw: bytes) -> int:
    """Lines as an editor shows them; a missing final newline still ends a line."""
    return raw.count(b"\n") + (1 if raw and not raw.endswith(b"\n") else 0)


def tracked_files(root: Path) -> list[str]:
    """Repo-relative, forward-slash paths of every tracked or untracked-but-not-ignored file."""
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=root, capture_output=True, env=env,
            stdin=subprocess.DEVNULL, creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git ls-files timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git ls-files exited {proc.returncode}: {detail[-1] if detail else '(no output)'}")
    return sorted(p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p)


# --------------------------------------------------------------- Python

#: The only nodes a `def`, `class` or `import` can sit in; skipping expressions halves the walk.
_STATEMENT_NODES = (ast.stmt, ast.excepthandler, ast.match_case)


def _statements(node: ast.AST) -> list[ast.AST]:
    return [child for child in ast.iter_child_nodes(node) if isinstance(child, _STATEMENT_NODES)]


def python_spans(tree: ast.Module) -> list[Span]:
    """Every function and method, nested ones included, as `Outer.inner` qualnames."""
    spans: list[Span] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in _statements(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = prefix + child.name
                spans.append(Span(name, (child.end_lineno or child.lineno) - child.lineno + 1))
                visit(child, name + ".")
            elif isinstance(child, ast.ClassDef):
                visit(child, prefix + child.name + ".")
            else:
                visit(child, prefix)

    visit(tree, "")
    return spans


def python_imports(tree: ast.Module) -> list[str]:
    """Absolute module names imported anywhere in the file (relative imports never leave the package)."""
    names: list[str] = []
    pending: list[ast.AST] = [tree]
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module)
        pending.extend(reversed(_statements(node)))  # popped in source order
    return names


def parse_python(raw: bytes, path: str) -> ast.Module:
    try:
        return ast.parse(raw, filename=path)
    except (SyntaxError, ValueError) as exc:
        raise NotConfirmed(f"{path}: does not parse as Python: {exc}") from exc


# -------------------------------------------------------------------- JS

_JS_TOKEN = re.compile(
    r"""(?P<space>[^\S\n]+)
      | (?P<newline>\n)
      | (?P<comment>//[^\n]*|/\*[\s\S]*?\*/)
      | (?P<string>"(?:[^"\\\n]|\\[\s\S])*"|'(?:[^'\\\n]|\\[\s\S])*')
      | (?P<name>[A-Za-z_$\u0080-￿][\w$\u0080-￿]*)
      | (?P<number>\.?\d[\w.]*)
      | (?P<arrow>=>)
      | (?P<punct>[\s\S])""",
    re.VERBOSE,
)
_JS_TEMPLATE_TEXT = re.compile(r"(?:[^`\\$]|\\[\s\S]|\$(?!\{))*")
_JS_REGEX = re.compile(r"/(?![*/])(?:[^/\\\[\n]|\\.|\[(?:[^\]\\\n]|\\.)*\])+/[A-Za-z]*")
#: After these words a `/` opens a regex literal, not a division.
_JS_REGEX_AFTER_WORD = frozenset(
    {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case", "do", "else",
     "yield", "await"}
)
#: `word(...) {` with one of these words is a statement, not a function.
_JS_CONTROL = frozenset({"if", "for", "while", "switch", "catch", "with"})
#: What may stand between a method name and the `{`, `}`, `;` or `,` before it.
_JS_METHOD_MODIFIERS = frozenset({"static", "async", "get", "set", "*", "#"})
#: Punctuation a method definition follows: the start of a class or object body, or the end of a member.
_JS_MEMBER_START = frozenset({"{", "}", ";", ","})
_JS_ANONYMOUS = "<anonymous>"


@dataclass(frozen=True)
class _Tok:
    kind: str  # "name", "value" (string, number, template, regex) or "punct"
    text: str
    line: int


@dataclass(frozen=True)
class _Frame:
    kind: str  # "fn", "class", "block" or "template" (the `${` of a template literal)
    qualname: str
    line: int


class JsScanner:
    """Function spans in one JS file, from one tokenizing pass with a brace stack.

    Strings, template literals (with nested `${}` code), comments and regex
    literals never move the stack. A `{` opens a function body when it follows
    `function name(...)`, `name(...)` in a class or object body, or `=>`. A
    function still open at the end of the file is returned unmeasured.
    """

    def __init__(self, text: str) -> None:
        self.text = text
        self.toks: list[_Tok] = []
        self.opener_of: dict[int, int] = {}  # index of a `)` or `]` token -> index of its `(` or `[`
        self.openers: list[int] = []
        self.frames: list[_Frame] = []
        self.spans: list[Span] = []
        self.pending_class: int | None = None  # index of a `class` token whose body has not opened yet
        self.line = 1

    def scan(self) -> list[Span]:
        pos = 0
        while pos < len(self.text):
            pos = self._step(pos)
        self.spans.extend(Span(f.qualname, None) for f in self.frames if f.kind == "fn")
        return self.spans

    def _step(self, pos: int) -> int:
        """Consume one token at `pos`; return the next position (the end, on an unterminated construct)."""
        text, char = self.text, self.text[pos]
        if char == "`":
            return self._template(pos + 1)
        if char == "}" and self.frames and self.frames[-1].kind == "template":
            self.frames.pop()
            return self._template(pos + 1)
        if char == "/":
            if text.startswith("/*", pos) and text.find("*/", pos + 2) < 0:
                return len(text)  # an unterminated comment hides the rest of the file
            if not text.startswith(("//", "/*"), pos) and self._regex_allowed():
                regex = _JS_REGEX.match(text, pos)
                if regex:
                    self._push("value", regex.group())
                    return regex.end()
        m = _JS_TOKEN.match(text, pos)
        assert m is not None  # the last alternative matches any character
        kind, value = m.lastgroup, m.group()
        if kind == "name":
            self._name(value)
        elif kind in ("string", "number"):
            self._push("value", value)
        elif kind in ("arrow", "punct"):
            self._punct(value)
        self.line += value.count("\n")
        return m.end()

    def _template(self, pos: int) -> int:
        """Template text from `pos`: stop after the closing backtick, or after a `${`."""
        m = _JS_TEMPLATE_TEXT.match(self.text, pos)
        assert m is not None  # the pattern matches the empty string
        start_line = self.line
        self.line += m.group().count("\n")
        end = m.end()
        if end >= len(self.text):
            return end  # unterminated template literal
        if self.text[end] == "`":
            self.toks.append(_Tok("value", "`", start_line))
            return end + 1
        self.frames.append(_Frame("template", "", self.line))
        self._push("punct", "${")
        return end + 2

    def _push(self, kind: str, text: str) -> None:
        self.toks.append(_Tok(kind, text, self.line))

    def _regex_allowed(self) -> bool:
        if not self.toks:
            return True
        prev = self.toks[-1]
        if prev.kind == "name":
            return prev.text in _JS_REGEX_AFTER_WORD
        return prev.kind == "punct" and prev.text not in (")", "]")

    def _name(self, value: str) -> None:
        if value == "class" and not (self.toks and self.toks[-1].text == "."):
            self.pending_class = len(self.toks)
        self._push("name", value)

    def _punct(self, value: str) -> None:
        index = len(self.toks)
        if value in ("(", "["):
            self.openers.append(index)
        elif value in (")", "]") and self.openers:
            self.opener_of[index] = self.openers.pop()
        elif value == "{":
            self.frames.append(self._open_frame(index))
        elif value == "}" and self.frames:
            frame = self.frames.pop()
            if frame.kind == "fn":
                self.spans.append(Span(frame.qualname, self.line - frame.line + 1))
        elif value == ";" or (value == ":" and self.pending_class == index - 1):
            self.pending_class = None  # `class` was a property key, or never got a body
        self._push("punct", value)

    def _open_frame(self, brace: int) -> _Frame:
        if self.pending_class is not None:
            name = self._class_name(self.pending_class)
            self.pending_class = None
            return _Frame("class", self._qualify(name), self.line)
        header = self._function_header(brace)
        if header is None:
            return _Frame("block", "", self.line)
        return _Frame("fn", self._qualify(header[0]), header[1])

    def _qualify(self, name: str) -> str:
        for frame in reversed(self.frames):
            if frame.kind in ("fn", "class"):
                return f"{frame.qualname}.{name}"
        return name

    def _class_name(self, index: int) -> str:
        after = self.toks[index + 1] if index + 1 < len(self.toks) else None
        if after is not None and after.kind == "name" and after.text != "extends":
            return after.text
        return self._assigned_name(index)

    def _function_header(self, brace: int) -> tuple[str, int] | None:
        """(name, first line) when the `{` at token index `brace` opens a function body."""
        prev = self.toks[brace - 1] if brace else None
        if prev is None or prev.kind != "punct":
            return None
        if prev.text == "=>":
            return self._arrow_header(brace - 1)
        if prev.text != ")" or brace - 1 not in self.opener_of:
            return None
        head = self.opener_of[brace - 1] - 1  # the token before the parameter list
        if head < 0:
            return None
        tok = self.toks[head]
        if tok.text == "*" and head and self.toks[head - 1].text == "function":
            head, tok = head - 1, self.toks[head - 1]
        if tok.kind == "name" and tok.text == "function":
            start = self._skip_back(head, "async")
            return self._assigned_name(start), self.toks[start].line
        if tok.text == "]" and head in self.opener_of:
            return self._method(self.opener_of[head], "<computed>")
        if tok.kind != "name" or tok.text in _JS_CONTROL:
            return None
        keyword = head - (2 if head >= 2 and self.toks[head - 1].text == "*" else 1)
        if keyword >= 0 and self.toks[keyword].text == "function":
            start = self._skip_back(keyword, "async")
            return tok.text, self.toks[start].line
        return self._method(head, tok.text)

    def _arrow_header(self, arrow: int) -> tuple[str, int] | None:
        last = arrow - 1
        if last < 0:
            return None
        if self.toks[last].text == ")" and last in self.opener_of:
            start = self.opener_of[last]
        elif self.toks[last].kind == "name":
            start = last
        else:
            return None
        start = self._skip_back(start, "async")
        return self._assigned_name(start), self.toks[start].line

    def _method(self, head: int, name: str) -> tuple[str, int] | None:
        """`name(...) {` is a method when only modifiers stand between it and the start of a member."""
        if head and self.toks[head - 1].text == "#":
            name = "#" + name
        start = head
        while start and self.toks[start - 1].text in _JS_METHOD_MODIFIERS:
            start -= 1
        before = self.toks[start - 1] if start else None
        if before is not None and not (before.kind == "punct" and before.text in _JS_MEMBER_START):
            return None
        return name, self.toks[start].line

    def _skip_back(self, index: int, word: str) -> int:
        while index and self.toks[index - 1].kind == "name" and self.toks[index - 1].text == word:
            index -= 1
        return index

    def _assigned_name(self, start: int) -> str:
        """The name a function expression is bound to: `name = ...`, `name: ...` or `export default ...`."""
        if start >= 2 and self.toks[start - 1].text in ("=", ":"):
            target = self.toks[start - 2]
            if target.kind == "name":
                return target.text
            if target.kind == "value" and target.text[:1] in ("'", '"'):
                return target.text[1:-1] or _JS_ANONYMOUS
        if start and self.toks[start - 1].text == "default":
            return "default"
        return _JS_ANONYMOUS


def js_spans(text: str) -> list[Span]:
    return JsScanner(text).scan()


# ---------------------------------------------------------------- findings

_ADVICE = {
    "size": "a file over the limit does not grow: put new code in a new module beside it, named for what it does",
    "fn": "split it into named helpers; a view's helpers move out of its mount closure into the view's folder",
    "dir": "put the new module in a domain subpackage, {where}<domain>/",
    "layer": "{lower}/ never imports {upper}/: move what both need into {lower}/",
    "unmeasured": "the scanner could not find where it ends (an unbalanced brace, or syntax it does not read): "
                  "fix the syntax, or record it with --update-baseline --allow-additions",
}


def function_findings(path: str, spans: list[Span], limit: int) -> list[Finding]:
    """Functions over `limit`, plus one `unmeasured` entry per file that has any.

    Same-named functions rank by length (`name`, `name~2`, ...), so adding a short
    one, or reordering, never renames a recorded one.
    """
    over: dict[str, list[int]] = {}
    unmeasured: list[str] = []
    for span in spans:
        if span.lines is None:
            unmeasured.append(span.qualname)
        elif span.lines > limit:
            over.setdefault(span.qualname, []).append(span.lines)
    findings = []
    for name, lengths in over.items():
        for rank, lines in enumerate(sorted(lengths, reverse=True), 1):
            ranked = name if rank == 1 else f"{name}~{rank}"
            findings.append(Finding(
                f"fn:{path}:{ranked}", lines, f"{path}: {ranked}() is {lines:,} lines (limit {limit:,})",
                _ADVICE["fn"],
            ))
    if unmeasured:
        findings.append(Finding(
            f"unmeasured:{path}", len(unmeasured),
            f"{path}: the end of {', '.join(f'{n}()' for n in unmeasured)} was not found", _ADVICE["unmeasured"],
        ))
    return findings


def layer_findings(path: str, imports: list[str], layers: tuple[tuple[str, str], ...]) -> list[Finding]:
    """Imports of an upper root's package from a file under its lower root, counted per module."""
    counts: dict[str, int] = {}
    pair_of: dict[str, tuple[str, str]] = {}
    for lower, upper in layers:
        if not _under(path, lower):
            continue
        package = upper.replace("/", ".")
        for module in imports:
            if module == package or module.startswith(package + "."):
                counts[module] = counts.get(module, 0) + 1
                pair_of.setdefault(module, (lower, upper))
    return [
        Finding(
            f"layer:{path}:{module}", n, f"{path}: imports {module} (none allowed under {pair_of[module][0]}/)",
            _ADVICE["layer"].format(lower=pair_of[module][0], upper=pair_of[module][1]),
        )
        for module, n in sorted(counts.items())
    ]


def dir_findings(paths: list[str], limit: int) -> list[Finding]:
    """Directories holding more than `limit` modules directly (not counting subfolders)."""
    counts: dict[str, int] = {}
    for path in paths:
        pure = PurePosixPath(path)
        if pure.suffix.lower() in DIR_SUFFIXES and pure.name != "__init__.py":
            counts[str(pure.parent)] = counts.get(str(pure.parent), 0) + 1
    return [
        Finding(
            f"dir:{folder}", n, f"{folder}/ has {n} modules (limit {limit})",
            _ADVICE["dir"].format(where="" if folder == "." else folder + "/"),
        )
        for folder, n in sorted(counts.items())
        if n > limit
    ]


def layout_findings(paths: list[str], cfg: Config) -> list[str]:
    """Root-layout problems for the declared shape, one message each (none when there is no shape).

    Not baselined: a declared exception is data in `extra_roots`, and an undeclared one fails.
    """
    if cfg.shape is None:
        return []
    shape = SHAPES[cfg.shape]
    roots = {path.split("/", 1)[0] for path in paths if "/" in path}
    listed = set(shape.required) | set(shape.allowed) | set(ALWAYS_ALLOWED) | set(cfg.extra_roots)
    see = f"See {LAYOUT_DOC}#{cfg.shape}"
    problems = [
        f"shape {cfg.shape}: required folder {name}/ has no tracked file; add it, or correct `shape` in "
        f".fleet.toml. {see}"
        for name in shape.required
        if name not in roots
    ]
    problems += [
        f"shape {cfg.shape}: {name}/ is not a folder this shape lists; move its code into one of "
        f"{', '.join(f'{n}/' for n in sorted(listed))}, or declare {name!r} in `extra_roots` in .fleet.toml "
        f"with a line in docs/layout.md. {see}"
        for name in sorted(roots)
        if not name.startswith(".") and name not in listed
    ]
    return problems


def _in_band(lines: int | None, cfg: Config, band: str) -> bool:
    low, high = cfg.band_range(band)
    return lines is not None and low <= lines <= high


def measure_file(path: str, raw: bytes, cfg: Config, band: dict[str, list[str]] | None = None) -> list[Finding]:
    """File-size, function-length and layer findings for one in-scope file; its near-limit members into `band`."""
    findings = []
    lines = count_lines(raw)
    if lines > cfg.max_file_lines:
        findings.append(Finding(
            f"size:{path}", lines, f"{path}: {lines:,} lines (limit {cfg.max_file_lines:,})", _ADVICE["size"],
        ))
    spans: list[Span] = []
    suffix = PurePosixPath(path).suffix.lower()
    if suffix == ".py":
        tree = parse_python(raw, path)
        spans = python_spans(tree)
        if any(_under(path, lower) for lower, _upper in cfg.layers):
            findings += layer_findings(path, python_imports(tree), cfg.layers)
    elif suffix in JS_SUFFIXES:
        spans = js_spans(raw.decode("utf-8-sig", "replace"))
    findings += function_findings(path, spans, cfg.max_function_lines)
    if band is not None:
        if _in_band(lines, cfg, "files"):
            band["files"].append(f"{path}: {lines:,} lines")
        band["functions"] += [f"{path}: {s.qualname}() is {s.lines:,} lines"
                              for s in spans if _in_band(s.lines, cfg, "functions")]
    return findings


def measure_tree(
    root: Path, paths: list[str], cfg: Config, vendored: tuple[str, ...], band: dict[str, list[str]] | None = None,
) -> dict[str, Finding]:
    """Every finding among the tracked `paths`, keyed by its baseline key; band members into `band`."""
    found: dict[str, Finding] = {}
    measured: list[str] = []
    for path in paths:
        if not in_scope(path, cfg, vendored):
            continue
        try:
            raw = (root / path).read_bytes()
        except FileNotFoundError:
            continue  # tracked but deleted in the working tree: nothing to measure
        except OSError as exc:
            raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
        if is_generated(raw):
            continue
        measured.append(path)
        found.update((f.key, f) for f in measure_file(path, raw, cfg, band))
    found.update((f.key, f) for f in dir_findings(measured, cfg.max_dir_modules))
    return found


# ---------------------------------------------------------------- baseline

def _kind(key: str) -> str:
    return key.split(":", 1)[0]


def read_baseline(path: Path) -> dict[str, int]:
    """Baseline file -> ``{key: count}``. Raises `ConfigError` on anything off."""
    return read_baseline_file(path)[0]


def read_baseline_file(path: Path) -> tuple[dict[str, int], dict[str, int] | None]:
    """Baseline file -> (``{key: count}``, band counts, or None when written before the band existed)."""
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
    out: dict[str, int] = {}
    for item in raw_entries:
        key = item.get("key") if isinstance(item, dict) else None
        if not isinstance(key, str) or _kind(key) not in KINDS:
            raise ConfigError(f"baseline {path}: malformed entry {item!r}")
        field = KINDS[_kind(key)][0]
        value = item.get(field)
        if set(item) != {"key", field} or isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfigError(f"baseline {path}: malformed entry {item!r} (expected key and a positive {field})")
        out[key] = value
    band = data.get("band")
    if band is not None and not (isinstance(band, dict) and set(band) == set(BANDS) and all(
            isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in band.values())):
        raise ConfigError(f"baseline {path}: `band` must hold a count of zero or more for {' and '.join(BANDS)}")
    return out, band


def render_baseline(counts: dict[str, int]) -> str:
    """Sorted, one entry per line, no line numbers, so a baseline diff reviews cleanly; `band:` counts in `band`."""
    entries = {key: n for key, n in counts.items() if _kind(key) != "band"}
    lines = [
        "    " + json.dumps({"key": key, KINDS[_kind(key)][0]: entries[key]}, ensure_ascii=False)
        for key in sorted(entries)
    ]
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    band = {b: counts[f"band:{b}"] for b in BANDS if f"band:{b}" in counts}
    head = f'\n  "band": {json.dumps(band)},' if band else ""
    return f'{{\n  "version": {BASELINE_VERSION},{head}\n  "entries": {body}\n}}\n'


def diff(current: dict[str, int], baseline: dict[str, int]) -> tuple[list[str], list[str]]:
    """(keys that rose or are new, keys that fell or are gone), sorted."""
    keys = sorted(set(current) | set(baseline))
    rose = [k for k in keys if current.get(k, 0) > baseline.get(k, 0)]
    shrank = [k for k in keys if current.get(k, 0) < baseline.get(k, 0)]
    return rose, shrank


# -------------------------------------------------------------------- CLI

def summarize(counts: dict[str, int]) -> str:
    kinds = {kind: sum(1 for k in counts if _kind(k) == kind) for kind in KINDS}
    near = (f", near-limit band {counts['band:files']} file(s) and {counts['band:functions']} function(s)"
            if "band:files" in counts else "")
    return (f"{kinds['size']} file(s), {kinds['fn']} function(s), {kinds['dir']} directory(s), "
            f"{kinds['layer']} layer import(s), {kinds['unmeasured']} unmeasured file(s){near}")


def band_findings(members: dict[str, list[str]], cfg: Config) -> dict[str, Finding]:
    """One `band:<band>` finding per near-limit band, its value the count, its members listed."""
    found = {}
    for band, (unit, _title, _anchor, advice) in BANDS.items():
        low, high = cfg.band_range(band)
        found[f"band:{band}"] = Finding(
            f"band:{band}", len(members[band]),
            f"{len(members[band])} {unit} at {low:,}-{high:,} lines, the near-limit band "
            f"({cfg.near_limit_ratio:.0%} of the limit)",
            advice.format(target=round(high * SPLIT_TARGET_RATIO)) + ", so it lands clear of the band",
            tuple(sorted(members[band])),
        )
    return found


def _report_rose(rose: list[str], found: dict[str, Finding], baseline: dict[str, int]) -> None:
    print(f"structure ratchet: {len(rose)} finding(s) above the baseline:")
    for key in rose:
        finding = found[key]
        title, anchor = BANDS[key[5:]][1:3] if _kind(key) == "band" else KINDS[_kind(key)][1:]
        was = f"baseline {baseline[key]:,}" if key in baseline else "not baselined"
        print(f"  [{title}] {finding.detail}; {was}")
        for member in finding.members:
            print(f"      - {member}")
        print(f"      {finding.advice}. See {LAYOUT_DOC}#{anchor}")


def _read_or_refuse(path: Path, cfg: Config, init: bool) -> tuple[dict[str, int], dict[str, int] | None] | int:
    """The baseline (empty for --init), or the exit code that refuses the run."""
    if init:
        if path.exists():
            print(f"structure ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return {}, None
    if not path.is_file():
        print(f"structure ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/structure_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline_file(path)
    except ConfigError as exc:
        print(f"structure ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def _check(paths: list[str], cfg: Config, found: dict[str, Finding], baseline: dict[str, int]) -> int:
    """The gate's mode: the layout, then every finding and band count against the baseline."""
    current = {key: f.value for key, f in found.items()}
    rose, shrank = diff(current, baseline)
    layout = layout_findings(paths, cfg)
    if cfg.shape is None:
        print("structure ratchet: layout check not applicable (no `shape` in .fleet.toml)")
    if layout:
        print(f"structure ratchet: {len(layout)} layout finding(s):")
        for message in layout:
            print(f"  [root layout] {message}")
    if rose:
        _report_rose(rose, found, baseline)
        print("  Existing offenders may not grow and new ones may not appear. Move the code where the layout "
              "puts it; or (reviewed) raise the baseline with `--update-baseline --allow-additions`.")
    if rose or layout:
        return EXIT_RISE
    matched = f"; layout matches {cfg.shape}" if cfg.shape else ""
    print(f"structure ratchet: PASS ({summarize(current)} baselined, none above the baseline{matched})")
    if shrank:
        print(f"  {len(shrank)} finding(s) are below the baseline -- run "
              "`python scripts/structure_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"structure ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("structure ratchet: not enabled -- set [structure_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("structure ratchet: not enabled (no [structure_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    if cfg.shape is not None and cfg.shape not in SHAPES:
        print(f"structure ratchet: NOT CONFIRMED: shape {cfg.shape!r} in .fleet.toml is not one of "
              f"{', '.join(SHAPES)}")
        print("  (the layout cannot be held to a tree that does not exist, so it is neither a pass nor a skip)")
        return EXIT_NOT_CONFIRMED
    baseline_path = root / cfg.baseline
    loaded = _read_or_refuse(baseline_path, cfg, init)
    if isinstance(loaded, int):
        return loaded
    baseline, band_base = loaded

    members: dict[str, list[str]] = {band: [] for band in BANDS}
    try:
        paths = tracked_files(root)
        found = measure_tree(root, paths, cfg, vendored, members)
    except NotConfirmed as exc:
        print(f"structure ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be measured, so it is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    found.update(band_findings(members, cfg))
    current = {key: f.value for key, f in found.items()}

    if init:
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"structure ratchet: wrote {cfg.baseline}: {summarize(current)}")
        return EXIT_PASS

    # A baseline from before the band existed takes today's band as its record, so no adopter fails on re-vendor.
    if band_base is None:
        band_base = {b: current[f"band:{b}"] for b in BANDS}
        print(f"structure ratchet: the near-limit band is not recorded in {cfg.baseline} yet -- "
              "`--update-baseline` records it, and from then on it may only shrink")
    baseline.update((f"band:{b}", n) for b, n in band_base.items())
    if not update:
        return _check(paths, cfg, found, baseline)
    rose, shrank = diff(current, baseline)
    if rose and not allow_additions:
        _report_rose(rose, found, baseline)
        print("structure ratchet: nothing written -- fix the findings, "
              "or raise the baseline with --allow-additions")
        return EXIT_RISE
    baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
    print(f"structure ratchet: {cfg.baseline} now records {summarize(current)} "
          f"({len(shrank)} lowered or dropped, {len(rose)} raised or added)")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="lower or drop entries (shrink only)")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also raise or add entries (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    return run(args.root.resolve(), init=args.init, update=args.update_baseline,
               allow_additions=args.allow_additions)


if __name__ == "__main__":
    sys.exit(main())
