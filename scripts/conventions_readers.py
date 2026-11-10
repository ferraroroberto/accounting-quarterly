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


def web_findings(source: bytes | str, rules: frozenset[str]) -> list[tuple[int, str]]:
    """(line, message) of every password input in one HTML or JS file, read line by line."""
    if "password-input-vendored" not in rules:
        return []
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    lines = text.splitlines()
    hits = []
    for number, line in enumerate(lines, start=1):
        if not _PASSWORD_INPUT.search(line):
            continue
        above = number - 1
        while above > 0 and _web_comment_line(lines[above - 1]):
            above -= 1
        # A web marker sits in `<!-- ... -->` or `/* ... */`: the closing punctuation is not a reason.
        if not _exempt("password-input-vendored", lines[above:number], " :#/*-<>!\t"):
            hits.append((number, f"password input outside the vendored sign-in component ({SIGN_IN_DIR}/)"))
    return hits


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
