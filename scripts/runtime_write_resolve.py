"""Path resolution for the runtime write ratchet: find write targets and say where they point.

`scripts/runtime_write_ratchet.py` loads this module for the static half of its check. Given one
parsed module, `find_writes` returns every write target (an open for writing, a `Path.write_*`, a
`mkdir`, a log `FileHandler`, `sqlite3.connect`, a copy destination) with its kind:

- `code-folder` -- the path resolves under one of the code folders;
- `unresolved` -- the source does not spell where it points (a parameter, an attribute, an env var);
- a target that resolves anywhere else (repo root, `data/`, an absolute path, a temp dir, the
  runtime root) is not returned.

The resolver follows literals, `__file__`, `.parent`, `.parents[n]`, `/`, `os.path.join`,
`os.path.dirname`, `Path(...)`, `.resolve()`, `.joinpath()`, an f-string with a path head, and any
name assigned exactly once in the enclosing function or module. A relative literal is read from the
repo root.

Vendor-verbatim (manifest key `runtime_write_resolve`), stdlib-only, no import of the ratchet.
"""

from __future__ import annotations

import ast
import re
from pathlib import PurePosixPath

#: Calls whose result is a location outside the checkout: temp dirs and the home folder.
BUILTIN_SAFE_CALLS = frozenset({
    "tempfile.mkdtemp", "tempfile.mkstemp", "tempfile.gettempdir", "tempfile.TemporaryDirectory",
    "tempfile.NamedTemporaryFile", "tempfile.TemporaryFile", "tempfile.SpooledTemporaryFile",
    "pathlib.Path.home",
})
#: The vendored runtime-root helpers (`src/runtime_data.py`), matched by name wherever they are imported from.
BUILTIN_SAFE_NAMES = frozenset({"runtime_db_path", "runtime_data_dir", "fleet_data_root"})
#: Calls that hand back the path they were given (or the joined parts).
_PASS_THROUGH = frozenset({
    "pathlib.Path", "pathlib.PurePath", "pathlib.PurePosixPath", "pathlib.PureWindowsPath",
    "pathlib.WindowsPath", "pathlib.PosixPath", "str", "os.fspath", "os.path.abspath", "os.path.realpath",
    "os.path.normpath", "os.path.join", "os.path.expanduser",
})
_SAME_PATH_METHODS = frozenset({"resolve", "absolute", "expanduser", "with_suffix", "with_name", "with_stem"})
_PATH_WRITE_METHODS = frozenset({"write_text", "write_bytes", "touch", "mkdir"})
_FILE_HANDLERS = frozenset({"FileHandler", "RotatingFileHandler", "TimedRotatingFileHandler", "WatchedFileHandler"})
_OPENERS = frozenset({"open", "io.open", "codecs.open"})
_DIR_MAKERS = frozenset({"os.mkdir", "os.makedirs"})
_COPIERS = frozenset({"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.move"})
_MODE = re.compile(r"[rwxabtU+]{1,4}\Z")
_ABSOLUTE = re.compile(r"(?:[A-Za-z]:)?[\\/]|~")
_MEMORY_DBS = (":memory:", "file::memory:")
#: How many names deep the resolver follows; deeper chains are unknown rather than slow.
_MAX_DEPTH = 12


#: A path the resolver knows: a tuple of parts is repo-relative; OUTSIDE is off the checkout; None is unknown.
#: A `?` part stands for a name the source does not spell: it stays under its parent but cannot be popped.
OUTSIDE = "outside"
Resolved = tuple[str, ...] | str | None


def _dotted(node: ast.expr) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return ""
    parts.append(node.id)
    return ".".join(reversed(parts))


def _split_literal(text: str) -> Resolved:
    """A string path, read from the repo root when relative."""
    if _ABSOLUTE.match(text):
        return OUTSIDE
    parts: list[str] = []
    for part in text.replace("\\", "/").split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return OUTSIDE
            parts.pop()
        else:
            parts.append(part)
    return tuple(parts)


def _join(left: Resolved, right: Resolved) -> Resolved:
    if left is None or left == OUTSIDE:
        return left
    if right == OUTSIDE:
        return OUTSIDE  # an absolute right-hand part replaces the left
    assert isinstance(left, tuple)
    if right is None:
        return left + ("?",)
    assert isinstance(right, tuple)
    return left + right


def _parent(value: Resolved, levels: int = 1) -> Resolved:
    if not isinstance(value, tuple):
        return value
    for _ in range(levels):
        if not value:
            return OUTSIDE
        if value[-1] == "?":
            return None
        value = value[:-1]
    return value


class _Scopes(ast.NodeVisitor):
    """Single-assignment names per scope (module, each function): the only names the resolver follows."""

    def __init__(self, tree: ast.Module) -> None:
        self.tables: dict[int, dict[str, ast.expr | None]] = {id(tree): {}}
        self.stack: list[int] = [id(tree)]
        self.visit(tree)

    def _bind(self, name: str, value: ast.expr | None) -> None:
        table = self.tables[self.stack[-1]]
        table[name] = None if name in table else value  # rebound -> ambiguous

    def _poison(self, name: str) -> None:
        """A name bound by anything but one plain assignment (a parameter, a loop, a `with`): never followed."""
        self.tables[self.stack[-1]][name] = None

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        self.tables[id(node)] = {}
        self.stack.append(id(node))
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                self._poison(arg.arg)
        body = node.body if isinstance(node.body, list) else [node.body]
        for child in body:
            self.visit(child)
        self.stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._poison(node.name)
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._poison(node.name)
        self._visit_def(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_def(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """A class body gets its own table, which methods never see: attributes come through `self`."""
        self._poison(node.name)
        self.tables[id(node)] = {}
        self.stack.append(id(node))
        for child in node.body:
            self.visit(child)
        self.stack.pop()

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name):
                    self._bind(name.id, node.value if isinstance(target, ast.Name) and len(node.targets) == 1
                               else None)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._bind(node.target.id, node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):  # for/with/augassign/walrus targets
            self._poison(node.id)


class _Resolver:
    def __init__(self, path: str, aliases: dict[str, str], scopes: _Scopes, safe_calls: frozenset[str]) -> None:
        self.file_parts = tuple(PurePosixPath(path).parts)
        self.aliases = aliases
        self.scopes = scopes
        self.safe_calls = safe_calls

    def canonical(self, func: ast.expr) -> str:
        dotted = _dotted(func)
        head, _, rest = dotted.partition(".")
        target = self.aliases.get(head, head)
        return f"{target}.{rest}" if rest else target

    def _lookup(self, name: str, chain: list[int]) -> ast.expr | None:
        for scope in reversed(chain):
            table = self.scopes.tables.get(scope, {})
            if name in table:
                return table[name]
        return None

    def resolve(self, node: ast.expr, chain: list[int], depth: int = 0) -> Resolved:
        if depth > _MAX_DEPTH:
            return None
        nxt = depth + 1
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return _split_literal(node.value)
        if isinstance(node, ast.Name):
            if node.id == "__file__":
                return self.file_parts
            bound = self._lookup(node.id, chain)
            return None if bound is None else self.resolve(bound, chain, nxt)
        if isinstance(node, ast.JoinedStr):
            return self._fstring(node, chain, nxt)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            return _join(self.resolve(node.left, chain, nxt), self.resolve(node.right, chain, nxt))
        if isinstance(node, ast.Attribute):
            if self.canonical(node) == "os.devnull":
                return OUTSIDE
            if node.attr == "parent":
                return _parent(self.resolve(node.value, chain, nxt))
            return None
        if (isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
                and node.value.attr == "parents" and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, int)):
            return _parent(self.resolve(node.value.value, chain, nxt), node.slice.value + 1)
        if isinstance(node, ast.Call):
            return self._call(node, chain, nxt)
        return None

    def _fstring(self, node: ast.JoinedStr, chain: list[int], depth: int) -> Resolved:
        """`f"{LOG_DIR}/x.log"`: a resolvable head followed by literal text."""
        values = node.values
        if not values or not all(isinstance(v, ast.Constant) for v in values[1:]):
            return None
        tail = "".join(str(v.value) for v in values[1:] if isinstance(v, ast.Constant))
        if isinstance(values[0], ast.Constant):
            return _split_literal(str(values[0].value) + tail)
        if not isinstance(values[0], ast.FormattedValue):
            return None
        head = self.resolve(values[0].value, chain, depth)
        return _join(head, _split_literal(tail.lstrip("/\\"))) if tail else head

    def _call(self, node: ast.Call, chain: list[int], depth: int) -> Resolved:
        name = self.canonical(node.func)
        last = name.rsplit(".", 1)[-1]
        if name in BUILTIN_SAFE_CALLS or last in BUILTIN_SAFE_NAMES or self.safe_calls & {name, last}:
            return OUTSIDE
        if name in ("pathlib.Path.cwd", "os.getcwd"):
            return ()
        if name == "os.path.dirname" and len(node.args) == 1:
            return _parent(self.resolve(node.args[0], chain, depth))
        if name in _PASS_THROUGH and node.args and not node.keywords:
            out = self.resolve(node.args[0], chain, depth)
            for arg in node.args[1:]:
                out = _join(out, self.resolve(arg, chain, depth))
            return out
        if isinstance(node.func, ast.Attribute):
            receiver = node.func.value
            if node.func.attr in _SAME_PATH_METHODS:
                return self.resolve(receiver, chain, depth)
            if node.func.attr == "joinpath":
                out = self.resolve(receiver, chain, depth)
                for arg in node.args:
                    out = _join(out, self.resolve(arg, chain, depth))
                return out
        return None


# ---------------------------------------------------------------- scanning

def _arg(node: ast.Call, index: int | None, keyword: str) -> ast.expr | None:
    """The argument passed as `keyword=`, else positionally at `index` (None: keyword-only)."""
    for kw in node.keywords:
        if kw.arg == keyword:
            return kw.value
    return node.args[index] if index is not None and len(node.args) > index else None


def _writing_mode(mode: ast.expr | None, *, default_write: bool) -> bool:
    """A mode that writes; a mode the source does not spell counts as writing only when `default_write`."""
    if mode is None:
        return False
    if isinstance(mode, ast.Constant) and isinstance(mode.value, str):
        return bool(_MODE.match(mode.value)) and any(c in mode.value for c in "wax+")
    return default_write


def _str_first(node: ast.Call) -> bool:
    """`ftp.mkdir("dir")`: `Path.mkdir/touch` never take a string first, so that receiver is not a path."""
    return bool(node.args) and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)


class _Scanner(ast.NodeVisitor):
    """Collects (call node, target expression, label, scope chain) for every write site."""

    def __init__(self, resolver: _Resolver, tree: ast.Module) -> None:
        self.resolver = resolver
        self.chain: list[int] = [id(tree)]
        self.sites: list[tuple[ast.Call, ast.expr, str, list[int]]] = []

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        self.chain.append(id(node))
        self.generic_visit(node)
        self.chain.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_def(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_def(node)

    def _site(self, node: ast.Call, target: ast.expr | None, label: str) -> None:
        if target is not None:
            self.sites.append((node, target, label, list(self.chain)))

    def visit_Call(self, node: ast.Call) -> None:
        name = self.resolver.canonical(node.func)
        last = name.rsplit(".", 1)[-1]
        attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
        if name in _OPENERS:
            if _writing_mode(_arg(node, 1, "mode"), default_write=True):
                self._site(node, _arg(node, 0, "file"), f"{name}(..., write)")
        elif name in _DIR_MAKERS:
            self._site(node, _arg(node, 0, "name") or _arg(node, 0, "path"), f"{name}(...)")
        elif name in _COPIERS:
            self._site(node, _arg(node, 1, "dst"), f"{name}(..., dst)")
        elif name.startswith("logging") and last in _FILE_HANDLERS:
            self._site(node, _arg(node, 0, "filename"), f"logging.{last}(...)")
        elif name == "logging.basicConfig":
            self._site(node, _arg(node, None, "filename"), "logging.basicConfig(filename=...)")
        elif name == "sqlite3.connect":
            db = _arg(node, 0, "database")
            if not (isinstance(db, ast.Constant) and db.value in _MEMORY_DBS):
                self._site(node, db, "sqlite3.connect(...)")
        elif (attr in _PATH_WRITE_METHODS and isinstance(node.func, ast.Attribute)
              and not (attr in ("mkdir", "touch") and _str_first(node))):
            self._site(node, node.func.value, f".{attr}()")
        elif (attr == "open" and isinstance(node.func, ast.Attribute)
              and _writing_mode(_arg(node, 0, "mode"), default_write=False)):
            self._site(node, node.func.value, ".open(write)")
        self.generic_visit(node)


def _aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> canonical dotted name, for every import in the module."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                out[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                out[alias.asname or alias.name] = f"{node.module}.{alias.name}"
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name  # relative import: the bare name
    return out


def classify(value: Resolved, code_folders: tuple[str, ...]) -> str | None:
    """`code-folder`, `unresolved`, or None for a target off the code folders."""
    if value is None:
        return "unresolved"
    if value == OUTSIDE or not value:
        return None
    assert isinstance(value, tuple)
    if value[0] == "?":
        return "unresolved"
    return "code-folder" if value[0] in code_folders else None


def find_writes(tree: ast.Module, path: str, code_folders: tuple[str, ...],
                safe_calls: tuple[str, ...]) -> list[tuple[ast.Call, str, str]]:
    """(call node, kind, message) for every write target in one module that is not clean."""
    resolver = _Resolver(path, _aliases(tree), _Scopes(tree), frozenset(safe_calls))
    scanner = _Scanner(resolver, tree)
    scanner.visit(tree)
    found = []
    for node, target, label, chain in scanner.sites:
        value = resolver.resolve(target, chain)
        kind = classify(value, code_folders)
        if kind is None:
            continue
        if kind == "code-folder":
            assert isinstance(value, tuple)
            where = "/".join("<name>" if p == "?" else p for p in value)
            message = f"`{label}` writes under the code folder `{value[0]}/` ({where})"
        else:
            message = f"`{label}` writes to a path this check cannot resolve"
        found.append((node, kind, message))
    return found
