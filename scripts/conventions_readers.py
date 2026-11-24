"""Pure readers for the conventions ratchet: syntax-tree predicates and the line rules.

`scripts/conventions_ratchet.py` loads this sibling from beside itself (manifest key
`conventions_readers`). Nothing here reads a config, a baseline or git: each function answers
one question about one node, path or file's text, so the ratchet keeps the scanning, the
configuration and the baseline, and both files stay under the structure limits. Stdlib only.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import PurePosixPath

_PLATFORM_EXPRS = frozenset({"sys.platform", "os.name", "platform.system"})
_PASSWORD_INPUT = re.compile(
    r"""\btype\s*[=:]\s*(["'`]?)password\1(?![\w-])"""
    r"""|setAttribute\(\s*["'`]type["'`]\s*,\s*["'`]password["'`]""",
    re.IGNORECASE,
)
#: `pwsh` run as a command: followed by a switch, or the first quoted item of an argv list (a
#: shell name in the middle of a list is a name, not a command). A preceding path character
#: means an absolute path to a real install, which is not the stub.
_PWSH_STUB = re.compile(
    r"""(?<![\w./\\-])pwsh(?:\.exe)?(?=\s+-{1,2}[A-Za-z])"""
    r"""|[\[(]\s*(["'])pwsh(?:\.exe)?\1\s*,""",
    re.IGNORECASE,
)
#: The one folder a password input may live in: the vendored sign-in component, wherever it was copied.
SIGN_IN_DIR = "_vendored/sign-in"


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


def _is_login_route(decorator: ast.expr) -> bool:
    """`@<x>.post("/api/login")`, by position or `path=`: the route a password is posted to."""
    if not (isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "post"):
        return False
    paths = [*decorator.args[:1], *(kw.value for kw in decorator.keywords if kw.arg == "path")]
    return any(isinstance(p, ast.Constant) and isinstance(p.value, str)
               and p.value.rstrip("/").rsplit("/", 1)[-1] == "login" for p in paths)


def _calls_retry_after(handler: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in ast.walk(handler):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "retry_after") or (
                    isinstance(func, ast.Name) and func.id == "retry_after"):
                return True
    return False


#: Playwright's browser-type objects, and the pytest-playwright fixture that hands one out.
_BROWSER_TYPES = frozenset({"chromium", "firefox", "webkit", "browser_type"})


def _browser_open(call: ast.Call) -> str:
    """`x.new_context(...)`, `browser.new_page(...)` or `<browser type>.launch*(...)`: the spelling, else ''.

    Read by method and receiver name, because no type is known here: `new_context` is Playwright's
    alone, `launch` only counts on a browser type, and `new_page` only on a receiver named `browser`
    (on a `context` it adds a tab to the one context, which is the pattern the rule asks for).
    """
    func = call.func
    if not isinstance(func, ast.Attribute):
        return ""
    base = func.value
    receiver = base.attr if isinstance(base, ast.Attribute) else base.id if isinstance(base, ast.Name) else ""
    if func.attr == "new_context" or (func.attr == "new_page" and receiver == "browser") or (
            func.attr in ("launch", "launch_persistent_context") and receiver in _BROWSER_TYPES):
        return f"{receiver}.{func.attr}" if receiver else func.attr
    return ""


BROWSER_LOOP_RULE = "browser-per-iteration"


class _BrowserLoops(ast.NodeVisitor):
    """Collects the browser or context opens that sit inside a loop body or comprehension."""

    def __init__(self) -> None:
        self.depth = 0  # loops around the node being visited, within the current function
        self.hits: list[tuple[ast.AST, str, str]] = []

    def _looped(self, nodes: list[ast.AST], once: list[ast.AST] | None = None) -> None:
        """Visit what runs every pass one loop deeper, and what runs once (`once`) where it stands."""
        for child in once or []:
            self.visit(child)
        self.depth += 1
        for child in nodes:
            self.visit(child)
        self.depth -= 1

    def visit_For(self, node: ast.For) -> None:
        self._looped([node.target, *node.body], [node.iter, *node.orelse])

    visit_AsyncFor = visit_For  # type: ignore[assignment]

    def visit_While(self, node: ast.While) -> None:
        self._looped([node.test, *node.body], list(node.orelse))

    def _comprehension(self, node: ast.AST) -> None:
        self._looped(list(ast.iter_child_nodes(node)))

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def _def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        loops, self.depth = self.depth, 0  # a function defined in a loop does not run once per pass
        self.generic_visit(node)
        self.depth = loops

    visit_FunctionDef = visit_AsyncFunctionDef = _def

    def visit_Call(self, node: ast.Call) -> None:
        opened = _browser_open(node) if self.depth else ""
        if opened:
            self.hits.append((node, BROWSER_LOOP_RULE,
                              f"`{opened}(...)` inside a loop opens a fresh browser or context per pass; "
                              "open one before the loop and reuse it (tests/e2e/_paced_browser.py)"))
        self.generic_visit(node)


def browser_loop_hits(tree: ast.Module, rules: frozenset[str]) -> list[tuple[ast.AST, str, str]]:
    """`(node, rule, message)` for every browser or context opened per pass of a loop, when the rule is on."""
    walker = _BrowserLoops()
    if BROWSER_LOOP_RULE in rules:
        walker.visit(tree)
    return walker.hits


HOST_IDENTITY_RULE = "host-identity"
#: Calls that answer with the account running the process; no environment override reaches the last two.
_ACCOUNT_CALLS = frozenset({"getpass.getuser", "os.getlogin"})
#: The environment variables that name the account or its home folder, on Windows and POSIX.
HOST_IDENTITY_VARS = frozenset({"USERNAME", "USER", "LOGNAME", "LNAME", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH"})
_HOST_MODULES = frozenset({"getpass", "os", "os.path", "pwd", "pathlib"})
_HOST_FIX = "read it through the synthetic_host fixture (tests/host/_synthetic_host.py)"


def _str_arg(call: ast.Call) -> str | None:
    first = call.args[0] if call.args else None
    return first.value if isinstance(first, ast.Constant) and isinstance(first.value, str) else None


class _HostIdentity(ast.NodeVisitor):
    """Reads of the real account name or home folder: calls, `~` expansions and environment lookups."""

    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}  # local name -> `module` or `module.attr`
        self.hits: list[tuple[ast.AST, str, str]] = []

    def _resolve(self, node: ast.expr) -> str:
        head, _, rest = _dotted(node).partition(".")
        target = self.aliases.get(head)
        if target is None:
            return ""
        return f"{target}.{rest}" if rest else target

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.asname and alias.name in _HOST_MODULES:
                self.aliases[alias.asname] = alias.name
            elif not alias.asname and alias.name.split(".")[0] in _HOST_MODULES:
                self.aliases[alias.name.split(".")[0]] = alias.name.split(".")[0]

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module in _HOST_MODULES and not node.level:
            for alias in node.names:
                self.aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    def _hit(self, node: ast.AST, what: str) -> None:
        self.hits.append((node, HOST_IDENTITY_RULE, f"{what} reads the real host's identity; {_HOST_FIX}"))

    def _env_name(self, node: ast.expr) -> str | None:
        name = node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
        return name if name is not None and name.upper() in HOST_IDENTITY_VARS else None

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Load) and self._resolve(node.value) == "os.environ":
            name = self._env_name(node.slice)
            if name:
                self._hit(node, f"`os.environ[{name!r}]`")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = self._resolve(node.func)
        func = node.func
        env = node.args[0] if node.args else None
        if name in _ACCOUNT_CALLS or name.startswith("pwd.getpw"):
            self._hit(node, f"`{name}()`")
        elif name.startswith("pathlib.") and name.endswith(".home"):
            self._hit(node, f"`{name.split('.', 1)[1]}()`")
        elif name == "os.path.expanduser" and (_str_arg(node) or "").startswith("~"):
            self._hit(node, f"`os.path.expanduser({_str_arg(node)!r})`")
        elif (isinstance(func, ast.Attribute) and func.attr == "expanduser" and isinstance(func.value, ast.Call)
              and (_str_arg(func.value) or "").startswith("~")):
            self._hit(node, f"`.expanduser()` on {_str_arg(func.value)!r}")
        elif env is not None and (name == "os.getenv" or name == "os.environ.get") and self._env_name(env):
            self._hit(node, f"`{name}({self._env_name(env)!r})`")
        self.generic_visit(node)


def host_identity_hits(tree: ast.Module, rules: frozenset[str]) -> list[tuple[ast.AST, str, str]]:
    """`(node, rule, message)` for every direct read of the account name or home folder, when the rule is on.

    Writes are not reads: `monkeypatch.setenv("HOME", ...)` and `os.environ["HOME"] = ...` are how a test
    fakes the host. A value the code cannot see (a variable passed to `expanduser`, a copied `env` dict)
    is missed, never invented.
    """
    walker = _HostIdentity()
    if HOST_IDENTITY_RULE in rules:
        walker.visit(tree)
    return walker.hits


PATH_SEP_RULE = "path-sep"
#: Calls whose result is text a regex parsed out of another format: a `.bat` writes `scripts\run.py`. A
#: whole-file read is not one, because the files read that way are pointers the host writes (`gitdir:`).
_TEXT_SOURCES = frozenset({"group", "groups", "groupdict", "findall", "expand"})
#: `str` methods whose result keeps the receiver's separators.
_STR_KEEPS = frozenset({"strip", "lstrip", "rstrip", "split", "rsplit", "splitlines", "partition", "rpartition",
                        "removeprefix", "removesuffix", "lower", "upper", "casefold", "replace", "format"})
#: Path constructors that read a backslash as a file-name character on POSIX.
_PATH_TYPES = frozenset({"Path", "PurePath", "PurePosixPath", "PosixPath"})
_PATH_SEP_FIX = 'normalise it first (`.replace("\\\\", "/")` or `PureWindowsPath(...)`): POSIX keeps the backslash'


def _separator_fixed(call: ast.Call) -> bool:
    """`s.replace("\\\\", "/")` or `s.split("\\\\")`: the backslash cannot reach the path."""
    func = call.func
    if not isinstance(func, ast.Attribute) or not call.args:
        return False
    first = call.args[0]
    backslash = isinstance(first, ast.Constant) and isinstance(first.value, str) and "\\" in first.value
    if func.attr in ("split", "rsplit"):
        return backslash
    second = call.args[1] if len(call.args) > 1 else None
    return (func.attr == "replace" and backslash
            and isinstance(second, ast.Constant) and second.value == "/")


class _PathSep(ast.NodeVisitor):
    """Path joins whose right side is text a regex parsed in the same function, never normalised.

    Names are followed in statement order: an assignment from tainted text taints the name, one from a
    clean value clears it. A nested function starts clean; a value the walker cannot follow is clean.
    """

    def __init__(self) -> None:
        self.tainted: set[str] = set()
        self.hits: list[tuple[ast.AST, str, str]] = []

    def _text(self, node: ast.AST | None) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.tainted
        if isinstance(node, (ast.Subscript, ast.Starred, ast.NamedExpr)):
            return self._text(node.value)
        if isinstance(node, ast.JoinedStr):
            return any(self._text(v.value) for v in node.values if isinstance(v, ast.FormattedValue))
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            return self._text(node.left) or self._text(node.right)
        if isinstance(node, ast.IfExp):
            return self._text(node.body) or self._text(node.orelse)
        if isinstance(node, ast.BoolOp):
            return any(self._text(v) for v in node.values)
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        if isinstance(func, ast.Name):
            return func.id == "str" and any(self._text(a) for a in node.args)
        if not isinstance(func, ast.Attribute) or _separator_fixed(node):
            return False
        if func.attr in _TEXT_SOURCES:
            return True
        if func.attr in ("sub", "subn", "join"):  # a regex `.sub(repl, text)`, `"".join(parts)`
            return any(self._text(a) for a in node.args)
        return func.attr in _STR_KEEPS and self._text(func.value)

    def _bind(self, target: ast.AST, text: bool) -> None:
        if isinstance(target, ast.Name):
            (self.tainted.add if text else self.tainted.discard)(target.id)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind(elt.value if isinstance(elt, ast.Starred) else elt, text)

    def _hit(self, node: ast.AST, spelled: str) -> None:
        self.hits.append((node, PATH_SEP_RULE, f"{spelled} joins text a regex parsed onto a path; "
                                               f"{_PATH_SEP_FIX}"))

    def _def(self, node: ast.AST) -> None:
        outer, self.tainted = self.tainted, set()
        self.generic_visit(node)
        self.tainted = outer

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _def

    def visit_Assign(self, node: ast.Assign) -> None:
        self.visit(node.value)
        for target in node.targets:
            self._bind(target, self._text(node.value))

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self.visit(node.value)
            self._bind(node.target, self._text(node.value))

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self.visit(node.value)
        self._bind(node.target, self._text(node.value))

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self.visit(node.value)
        if isinstance(node.op, ast.Div) and self._text(node.value):
            self._hit(node, "`/=`")
        elif isinstance(node.op, ast.Add) and self._text(node.value):
            self._bind(node.target, True)

    def visit_For(self, node: ast.For) -> None:
        self.visit(node.iter)
        self._bind(node.target, self._text(node.iter))
        for child in [*node.body, *node.orelse]:
            self.visit(child)

    visit_AsyncFor = visit_For  # type: ignore[assignment]

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self.visit(node.iter)
        self._bind(node.target, self._text(node.iter))
        for cond in node.ifs:
            self.visit(cond)

    def _comprehension(self, node: ast.AST) -> None:
        for gen in getattr(node, "generators", []):
            self.visit(gen)
        for field in ("key", "value", "elt"):
            if getattr(node, field, None) is not None:
                self.visit(getattr(node, field))

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div) and self._text(node.right):
            self._hit(node, "`<path> / <text>`")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _dotted(func)
        last = func.attr if isinstance(func, ast.Attribute) else name
        joins = (last in _PATH_TYPES or last == "joinpath"
                 or name in ("os.path.join", "path.join"))  # `posixpath.join` joins URL paths on purpose
        if joins and any(self._text(a) for a in node.args):
            self._hit(node, f"`{name or last}(...)`")
        self.generic_visit(node)


def path_sep_hits(tree: ast.Module, rules: frozenset[str]) -> list[tuple[ast.AST, str, str]]:
    """`(node, rule, message)` for every path joined from un-normalised regex-parsed text, when the rule is on."""
    walker = _PathSep()
    if PATH_SEP_RULE in rules:
        walker.visit(tree)
    return walker.hits


def tree_hits(tree: ast.Module, rules: frozenset[str]) -> list[tuple[ast.AST, str, str]]:
    """The hits of every rule here that reads a whole module's syntax tree."""
    return [*browser_loop_hits(tree, rules), *host_identity_hits(tree, rules), *path_sep_hits(tree, rules)]


#: The vendored lock-out module's name, wherever the app copied it.
LOCKOUT_MODULE = "login_lockout"


def login_lockout_hits(
    routes: list[tuple[ast.expr, ast.FunctionDef | ast.AsyncFunctionDef]], imports_lockout: bool,
) -> list[tuple[ast.AST, str, str]]:
    """A login route with no lock-out: the module never imports it, or the handler never checks it."""
    found: list[tuple[ast.AST, str, str]] = []
    for decorator, handler in routes:
        if not imports_lockout:
            message = f"login route `{handler.name}` in a module that does not import {LOCKOUT_MODULE}"
        elif not _calls_retry_after(handler):
            message = f"login route `{handler.name}` never calls retry_after() before the password check"
        else:
            continue
        found.append((decorator, "login-lockout", message))
    return found


def _writes_to_a_file(call: ast.Call) -> bool:
    """`print(x, file=fh)` writes data, not a log line; `file=sys.stdout/stderr` is still console output."""
    for kw in call.keywords:
        if kw.arg == "file":
            return _dotted(kw.value) not in ("sys.stdout", "sys.stderr")
    return False


def _exempt(rule: str, window: list[str], punct: str = " :#\t") -> bool:
    """A `# <rule>-exempt: <reason>` marker in the window; a bare marker exempts nothing."""
    marker = f"{rule}-exempt"
    return any(line.split(marker, 1)[1].strip(punct) for line in window if marker in line)


def window(node: ast.AST, lines: list[str]) -> list[str]:
    """A Python node's own lines plus the contiguous `#` comment block right above it."""
    start = getattr(node, "lineno", 1)
    return window_at(start, getattr(node, "end_lineno", start) or start, lines)


def window_at(start: int, end: int, lines: list[str]) -> list[str]:
    above = start - 1
    while above > 0 and lines[above - 1].lstrip().startswith("#"):
        above -= 1
    return lines[above:end]


def js_marked(lines: list[str]) -> Callable[[str, int, int], bool]:
    """`marked(rule, first, last)` for a JS/TS file: a reasoned marker on those lines or the comment block above."""
    def marked(rule: str, start: int, end: int) -> bool:
        above = start - 1
        while above > 0 and _web_comment_line(lines[above - 1]):
            above -= 1
        return _exempt(rule, lines[above:end], " :#/*-<>!\t")
    return marked


def _web_comment_line(text: str) -> bool:
    """A line that opens a comment, or ends one it did not open (`... -->`): code with a trailing comment is not."""
    text = text.strip()
    if text.startswith(("//", "/*", "*", "<!--")):
        return True
    return text.endswith(("-->", "*/")) and "<!--" not in text and "/*" not in text


def web_findings(source: bytes | str, rules: frozenset[str]) -> list[tuple[int, str, str]]:
    """(line, rule, message) of every web-rule hit in one HTML or JS file."""
    return sorted([*((n, "password-input-vendored", m) for n, m in password_findings(source, rules)),
                   *((n, NAV_HEAD_RULE, m) for n, m in nav_head_findings(source, rules))])


def password_findings(source: bytes | str, rules: frozenset[str]) -> list[tuple[int, str]]:
    """(line, message) of every password input in one HTML or JS file, read line by line."""
    if "password-input-vendored" not in rules:
        return []
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    lines = text.splitlines()
    hits = []
    for number, line in enumerate(lines, start=1):
        if _PASSWORD_INPUT.search(line) and not _web_marked("password-input-vendored", lines, number):
            hits.append((number, f"password input outside the vendored sign-in component ({SIGN_IN_DIR}/)"))
    return hits


NAV_HEAD_RULE = "nav-standalone-head"
#: A page that links the vendored nav's stylesheet: only such a page owes the nav its page head.
_NAV_STYLESHEET = re.compile(r"""<link\b[^>]*\bhref\s*=\s*["'][^"']*nav-tabs\.css""", re.IGNORECASE)
_HEAD_META = re.compile(
    r"""<meta\b[^>]*\bname\s*=\s*["'](viewport|apple-mobile-web-app-status-bar-style)["'][^>]*>""", re.IGNORECASE
)
_META_CONTENT = re.compile(r"""\bcontent\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def _web_marked(rule: str, lines: list[str], number: int) -> bool:
    """A `<rule>-exempt: <reason>` marker on the line or in the comment block right above it.

    A web marker sits in `<!-- ... -->` or `/* ... */`: the closing punctuation is not a reason.
    """
    above = number - 1
    while above > 0 and _web_comment_line(lines[above - 1]):
        above -= 1
    return _exempt(rule, lines[above:number], " :#/*-<>!\t")


def nav_head_findings(source: bytes | str, rules: frozenset[str]) -> list[tuple[int, str]]:
    """(line, message) of every page-head miss in a page that links the vendored nav's stylesheet.

    The nav's pill offset and top glass are written for `viewport-fit=auto` plus the
    `black-translucent` status bar; under `viewport-fit=cover` the installed iPhone app's web view
    runs under the home indicator and the pill lands on the screen's bottom edge.
    """
    if NAV_HEAD_RULE not in rules:
        return []
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    link = _NAV_STYLESHEET.search(text)
    if not link:
        return []
    lines = text.splitlines()
    link_line = text.count("\n", 0, link.start()) + 1
    found: list[tuple[int, str]] = []
    seen = {"viewport": False, "apple-mobile-web-app-status-bar-style": False}
    for meta in _HEAD_META.finditer(text):
        name = meta.group(1).lower()
        seen[name] = True
        content_match = _META_CONTENT.search(meta.group(0))
        content = (content_match.group(1) if content_match else "").replace(" ", "").lower()
        number = text.count("\n", 0, meta.start()) + 1
        if name == "viewport" and "viewport-fit=auto" not in content.split(","):
            found.append((number, "the vendored nav needs `viewport-fit=auto` in the viewport meta"))
        elif name != "viewport" and content != "black-translucent":
            found.append((number, "the vendored nav needs the `black-translucent` status-bar meta"))
    if not seen["viewport"]:
        found.append((link_line, "page links the vendored nav but has no viewport meta (`viewport-fit=auto`)"))
    if not seen["apple-mobile-web-app-status-bar-style"]:
        found.append((link_line, "page links the vendored nav but has no `black-translucent` status-bar meta"))
    return sorted(hit for hit in found if not _web_marked(NAV_HEAD_RULE, lines, hit[0]))


def text_findings(source: bytes | str, rules: frozenset[str]) -> list[tuple[int, str]]:
    """(line, message) of every `pwsh` stub run as a command in one text file, read line by line."""
    if "no-pwsh-stub" not in rules:
        return []
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    lines = text.splitlines()
    hits = []
    for number, line in enumerate(lines, start=1):
        if not _PWSH_STUB.search(line):
            continue
        # The marker sits on the line or the line right above it, unless that line is a hit of its own.
        window = lines[number - 1:number]
        if number > 1 and not _PWSH_STUB.search(lines[number - 2]):
            window = lines[number - 2:number]
        if not _exempt("no-pwsh-stub", window, " :#/*-<>!\t"):
            hits.append((number, "`pwsh` is a 0-byte stub on the fleet's main machine; "
                                 "use C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"))
    return hits


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


def parse_exclude_by_rule(raw: object, enabled: tuple[str, ...]) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """`exclude_by_rule = { rule = [globs] }` -> (rule, globs) pairs; a typo'd or unlisted rule is a `ValueError`."""
    where = "[conventions_ratchet] exclude_by_rule"
    if not isinstance(raw, dict):
        raise ValueError(f"{where} must be a table of rule = [globs]")
    for rule, globs in raw.items():
        if rule not in enabled:
            raise ValueError(f"{where}: {rule!r} is not a rule listed in rules ({', '.join(enabled)})")
        if not isinstance(globs, list) or not all(isinstance(g, str) and g for g in globs):
            raise ValueError(f"{where}: {rule} must be a list of non-empty strings")
    return tuple((rule, tuple(g.replace("\\", "/") for g in globs)) for rule, globs in raw.items())


def rules_skipped(path: str, by_rule: tuple[tuple[str, tuple[str, ...]], ...]) -> set[str]:
    """The rules whose `exclude_by_rule` globs match `path`."""
    return {rule for rule, globs in by_rule if matches_any(path, globs)}
