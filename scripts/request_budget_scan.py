"""The request budget ratchet's readers: the six rules, decided from one file's source alone.

`request_budget_ratchet.py` owns the configuration, the scope, git, the
baseline and the report. This module turns one file's text into findings,
`(line, rule, message)`, so the same source always gives the same answer:

  * browser code (JS, and inline `<script>` blocks in HTML) -- `fixed-rate-poll`
    and `poll-floor`, read from a masked copy of the text (strings, comments,
    template literals and regex literals blanked, offsets and lines kept);
  * server code (Python) -- `unwatched-loop`, `scan-per-request`,
    `default-pool` and the facts `timing-middleware` needs, read from `ast`;
  * any scanned file -- `request-budget-exempt` markers, and `bare-exempt` for
    a marker with no reason.

Vendor-verbatim (manifest key `request_budget_scan`) beside
`request_budget_ratchet.py`, stdlib-only. Rules and limits:
docs/request-budget-ratchet.md.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

RULES = ("fixed-rate-poll", "poll-floor", "unwatched-loop", "scan-per-request", "default-pool",
         "timing-middleware", "bare-exempt")
#: A browser poll faster than this needs the helper's `activeWhile` declaration.
POLL_FLOOR_MS = 2000.0
#: A server loop sleeping less than this needs a `[[request_budget.loops]]` declaration.
LOOP_FLOOR_S = 5.0
MODULE_SCOPE = "<module>"
MARKER = "request-budget-exempt"

Finding = tuple[int, str, str]  # (line, rule, message)


class ConfigError(RuntimeError):
    """The `[request_budget_ratchet]` / `[request_budget]` tables or the baseline are unusable."""


class NotConfirmed(RuntimeError):
    """A file could not be read through, so its count is unknown, not zero."""


# ------------------------------------------------------------------ markers

_MARKER_RE = re.compile(rf"(?://|#|/\*|<!--|\*)\s*{MARKER}\b(?P<rest>[^\n]*)")
_COMMENT_LINE = re.compile(r"\s*(?://|#|/\*|\*|<!--)")


@dataclass(frozen=True)
class Exemption:
    line: int  # the hit's line
    rule: str
    reason: str


def _marker_reason(line_text: str) -> str | None:
    """The marker's reason on this line; "" for a marker with none; None when the line has no marker."""
    m = _MARKER_RE.search(line_text)
    if m is None:
        return None
    rest = m.group("rest").strip()
    rest = re.sub(r"(?:\*/|-->)\s*$", "", rest).strip()
    if not rest.startswith(":"):
        return ""
    return rest[1:].strip()


def apply_exemptions(text: str, findings: list[Finding]) -> tuple[list[Finding], list[Exemption]]:
    """(findings left, exemptions applied). A marker counts on the hit's line or the comment lines right above it.

    A marker with no reason exempts nothing and is itself a `bare-exempt` finding.
    """
    lines = text.splitlines()
    kept: list[Finding] = []
    used: list[Exemption] = []
    for line, rule, message in findings:
        reason = _reason_for(lines, line)
        if reason:
            used.append(Exemption(line, rule, reason))
        else:
            kept.append((line, rule, message))
    for number, line_text in enumerate(lines, 1):
        if _marker_reason(line_text) == "":
            kept.append((number, "bare-exempt", f"`{MARKER}` with no reason exempts nothing; "
                                                f"write `{MARKER}: <why>`"))
    return kept, used


def _reason_for(lines: list[str], line: int) -> str:
    reason = _marker_reason(lines[line - 1]) if 0 < line <= len(lines) else None
    if reason:
        return reason
    above = line - 2
    while above >= 0 and _COMMENT_LINE.match(lines[above]):
        reason = _marker_reason(lines[above])
        if reason:
            return reason
        above -= 1
    return ""


# ------------------------------------------------------------- JS masking

_REGEX_AFTER = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_KEYWORDS = {"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void", "throw",
                   "yield", "await"}


def mask_js(text: str, path: str) -> str:
    """`text` with every string, comment, template and regex literal blanked; offsets and newlines kept."""
    out = list(text)
    i, n = 0, len(text)
    last = ""  # the last significant code token's final character (or a keyword)

    def blank(start: int, end: int) -> None:
        for k in range(start, end):
            if out[k] != "\n":
                out[k] = " "

    while i < n:
        ch = text[i]
        if ch in " \t\r\n":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i)
            end = n if end < 0 else end
            blank(i, end)
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise NotConfirmed(f"{path}: a /* comment does not close")
            blank(i, end + 2)
            i = end + 2
        elif ch in "'\"`":
            end = _string_end(text, i, path)
            blank(i + 1, end - 1)
            i, last = end, "x"
        elif ch == "/" and (last == "" or last in _REGEX_AFTER or last in _REGEX_KEYWORDS):
            end = _regex_end(text, i, path)
            blank(i + 1, end - 1)
            i, last = end, "x"
        elif ch.isalnum() or ch in "_$":
            m = re.match(r"[\w$]+", text[i:])
            assert m is not None
            word = m.group(0)
            last = word if word in _REGEX_KEYWORDS else "x"
            i += len(word)
        else:
            last = ch
            i += 1
    return "".join(out)


def _string_end(text: str, start: int, path: str) -> int:
    quote, i = text[start], start + 1
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        if ch == "\n" and quote != "`":
            break
        i += 1
    line = text.count("\n", 0, start) + 1
    raise NotConfirmed(f"{path}:{line}: a {quote} string does not close")


def _regex_end(text: str, start: int, path: str) -> int:
    i, in_class = start + 1, False
    while i < len(text) and text[i] != "\n":
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            m = re.match(r"[a-z]*", text[i + 1:])
            return i + 1 + (len(m.group(0)) if m else 0)
        i += 1
    line = text.count("\n", 0, start) + 1
    raise NotConfirmed(f"{path}:{line}: a regex literal does not close")


def _match_close(masked: str, open_at: int) -> int:
    """The offset just past the bracket that closes the one at `open_at`; -1 when it never closes."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = []
    for i in range(open_at, len(masked)):
        ch = masked[i]
        if ch in pairs:
            stack.append(pairs[ch])
        elif ch in ")]}":
            if not stack or stack.pop() != ch:
                return -1
            if not stack:
                return i + 1
    return -1


def _split_args(masked: str, open_paren: int, path: str) -> list[tuple[int, int]]:
    close = _match_close(masked, open_paren)
    if close < 0:
        line = masked.count("\n", 0, open_paren) + 1
        raise NotConfirmed(f"{path}:{line}: a call's brackets do not close")
    spans, depth, start = [], 0, open_paren + 1
    for i in range(open_paren + 1, close - 1):
        ch = masked[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "," and depth == 0:
            spans.append((start, i))
            start = i + 1
    if masked[start:close - 1].strip():
        spans.append((start, close - 1))
    return spans


# ------------------------------------------------------- JS poll rules

_SERVER_CALL = re.compile(
    r"(?<![\w$.])fetch\s*\(|\bXMLHttpRequest\b|\bEventSource\s*\(|\bsendBeacon\s*\(|\baxios\b"
    r"|\$\.(?:ajax|get|post|getJSON)\s*\(")
_BARE_CALL = re.compile(r"(?<![\w$.])(?<!new )([A-Za-z_$][\w$]*)\s*\(")
_JS_NOT_A_CALL = {"if", "for", "while", "switch", "catch", "function", "return", "typeof", "await", "import",
                  "super", "yield", "async", "delete", "void", "throw", "in", "of"}
_JS_BUILTINS = {"String", "Number", "Boolean", "Array", "Object", "Symbol", "BigInt", "parseInt", "parseFloat",
                "isNaN", "isFinite", "encodeURIComponent", "decodeURIComponent", "encodeURI", "decodeURI",
                "setTimeout", "clearTimeout", "clearInterval", "requestAnimationFrame", "cancelAnimationFrame",
                "queueMicrotask", "structuredClone"}


def _js_functions(masked: str) -> dict[str, tuple[int, int]]:
    """Name -> (start, end) of each function the file defines by name: `function f`, `const f = ... =>`."""
    found: dict[str, tuple[int, int]] = {}
    for m in re.finditer(r"\bfunction\s*\*?\s*([A-Za-z_$][\w$]*)\s*\(", masked):
        brace = masked.find("{", _match_close(masked, m.end() - 1))
        end = _match_close(masked, brace) if brace >= 0 else -1
        if end > 0:
            found.setdefault(m.group(1), (m.start(), end))
    for m in re.finditer(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?"
                         r"(?:function\b|\([^()]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)", masked):
        found.setdefault(m.group(1), (m.start(), _statement_end(masked, m.end())))
    return found


def _statement_end(masked: str, i: int) -> int:
    depth = 0
    while i < len(masked):
        ch = masked[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return i
        elif depth == 0 and ch == ";":
            return i
        elif depth == 0 and ch == "\n":
            rest = masked[i + 1:].lstrip()
            if not rest[:1] or rest[:1] not in ".?:+-*&|,=":
                return i
        i += 1
    return i


@dataclass
class _JsFile:
    masked: str
    functions: dict[str, tuple[int, int]]
    fetch_wrappers: tuple[str, ...]


def _reaches_server(js: _JsFile, body: str, seen: set[str]) -> str | None:
    """Why `body` reaches a server call (a direct call, or one through a function), None when it provably does not."""
    if _SERVER_CALL.search(body):
        return "it makes a server call"
    for name in js.fetch_wrappers:
        if re.search(rf"(?<![\w$]){re.escape(name)}\s*\(", body):
            return f"it calls `{name}`, a declared fetch wrapper"
    for m in _BARE_CALL.finditer(body):
        name = m.group(1)
        if name in _JS_NOT_A_CALL or name in _JS_BUILTINS or name in seen:
            continue
        span = js.functions.get(name)
        if span is None:
            return f"it calls `{name}`, which this file does not define, so a server call cannot be ruled out"
        seen.add(name)
        why = _reaches_server(js, js.masked[span[0]:span[1]], seen)
        if why is not None:
            return f"it calls `{name}`, and {why}"
    return None


def _callback_reach(js: _JsFile, arg: str) -> str | None:
    text = arg.strip()
    if re.match(r"(?:async\s+)?function\b", text) or "=>" in text:
        return _reaches_server(js, text, set())
    if re.fullmatch(r"[A-Za-z_$][\w$]*", text):
        span = js.functions.get(text)
        if span is None:
            return f"its callback `{text}` is not defined in this file, so a server call cannot be ruled out"
        return _reaches_server(js, js.masked[span[0]:span[1]], {text})
    return f"its callback `{' '.join(text.split())[:40]}` cannot be read, so a server call cannot be ruled out"


def _js_number(js: _JsFile, text: str, depth: int = 0) -> float | None:
    """The interval's value when it is a numeric literal, a product of them, or a `const` bound to one."""
    factors = [f.strip() for f in text.strip().split("*")]
    if all(re.fullmatch(r"\d[\d_]*(?:\.\d+)?", f) for f in factors):
        value = 1.0
        for f in factors:
            value *= float(f.replace("_", ""))
        return value
    name = text.strip()
    if depth < 3 and re.fullmatch(r"[A-Za-z_$][\w$]*", name):
        m = re.search(rf"\bconst\s+{re.escape(name)}\s*=\s*([^;\n]+)", js.masked)
        return _js_number(js, m.group(1), depth + 1) if m else None
    return None


def _poll_calls(masked: str, wrappers: tuple[str, ...]) -> list[tuple[int, str, int]]:
    """(call offset, callee, open-paren offset) of each `setInterval`, wrapper and `startPoll` call."""
    names = ["setInterval", "startPoll", *wrappers]
    pattern = re.compile(rf"(?<![\w$])(?:(?:window|globalThis|self)\.)?({'|'.join(map(re.escape, names))})\s*\(")
    calls = []
    for m in pattern.finditer(masked):
        if re.search(r"\bfunction\s*\*?\s*$", masked[max(0, m.start() - 20):m.start()]):
            continue  # the definition, not a call
        calls.append((m.start(), m.group(1), m.end() - 1))
    return calls


def scan_js(text: str, path: str, wrappers: tuple[str, ...] = (), fetch_wrappers: tuple[str, ...] = (),
            line_offset: int = 0) -> list[Finding]:
    """`fixed-rate-poll` and `poll-floor` findings in one JS source."""
    masked = mask_js(text, path)
    js = _JsFile(masked, _js_functions(masked), fetch_wrappers)
    findings: list[Finding] = []
    for offset, callee, paren in _poll_calls(masked, wrappers):
        line = masked.count("\n", 0, offset) + 1 + line_offset
        args = _split_args(masked, paren, path)
        if not args:
            continue
        interval = _js_number(js, masked[args[1][0]:args[1][1]]) if len(args) > 1 else None
        if callee == "startPoll":
            busy = len(args) > 2 and re.search(r"\bactiveWhile\s*:", masked[args[2][0]:args[2][1]])
            if interval is not None and interval < POLL_FLOOR_MS and not busy:
                findings.append((line, "poll-floor", f"startPoll every {interval:g} ms is under the "
                                 f"{POLL_FLOOR_MS:g} ms floor; declare `activeWhile` so it runs only while a job is in progress"))
            continue
        why = _callback_reach(js, masked[args[0][0]:args[0][1]])
        if why is None:
            continue
        findings.append((line, "fixed-rate-poll", f"`{callee}` polls on a fixed rate and {why}; "
                         "use the vendored poll helper's startPoll()"))
        if interval is not None and interval < POLL_FLOOR_MS:
            findings.append((line, "poll-floor", f"`{callee}` every {interval:g} ms is under the {POLL_FLOOR_MS:g} ms floor"))
    return findings


_SCRIPT_BLOCK = re.compile(r"<script\b(?P<attrs>[^>]*)>(?P<body>.*?)</script\s*>", re.IGNORECASE | re.DOTALL)


def scan_html(text: str, path: str, wrappers: tuple[str, ...] = (), fetch_wrappers: tuple[str, ...] = ()) -> list[Finding]:
    """The JS rules over each inline `<script>` block (one with a `src` loads a file scanned on its own)."""
    findings: list[Finding] = []
    for m in _SCRIPT_BLOCK.finditer(text):
        if re.search(r"\bsrc\s*=", m.group("attrs")) or re.search(r"type\s*=\s*[\"']?application/(?:ld\+)?json",
                                                                 m.group("attrs"), re.IGNORECASE):
            continue
        offset = text.count("\n", 0, m.start("body"))
        findings += scan_js(m.group("body"), path, wrappers, fetch_wrappers, line_offset=offset)
    return findings


# --------------------------------------------------------- Python rules

_SPAWNS = {("subprocess", "run"), ("subprocess", "Popen"), ("subprocess", "call"), ("subprocess", "check_call"),
           ("subprocess", "check_output"), ("asyncio", "create_subprocess_exec"),
           ("asyncio", "create_subprocess_shell"), ("os", "system"), ("os", "popen"), ("os", "startfile")}
_SPAWN_NAMES = {"Popen", "check_output", "check_call", "create_subprocess_exec", "create_subprocess_shell"}
_SCANS_ANY = {"rglob", "iterdir", "scandir", "listdir"}
_SCANS_ON = {("os", "walk"), ("glob", "glob"), ("glob", "iglob")}
_SCAN_NAMES = {"scandir", "listdir", "iglob", "walk"}
_APP_CLASSES = {"FastAPI", "Starlette"}
MIDDLEWARE = "RequestTimingMiddleware"


@dataclass
class PyFacts:
    """One Python file's findings, plus the repo-level facts `timing-middleware` combines."""

    findings: list[Finding] = field(default_factory=list)
    app_line: int | None = None  # the first FastAPI()/Starlette() call
    uses_middleware: bool = False
    loops_seen: set[str] = field(default_factory=set)  # declared loop scopes this file matched


def _callee(node: ast.Call) -> tuple[str | None, str]:
    """(the object a method is called on, by name, or None; the called name)."""
    func = node.func
    if isinstance(func, ast.Attribute):
        owner = func.value.id if isinstance(func.value, ast.Name) else None
        return owner, func.attr
    if isinstance(func, ast.Name):
        return None, func.id
    return None, ""


def _module_constants(tree: ast.Module) -> dict[str, float]:
    consts: dict[str, float] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            value = _py_number(node.value, consts)
            if value is not None:
                consts[node.targets[0].id] = value
    return consts


def _py_number(node: ast.expr | None, consts: dict[str, float]) -> float | None:
    """A numeric literal, a module-level constant bound to one, or arithmetic over them; None otherwise."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return float(node.value)
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.BinOp):
        left, right = _py_number(node.left, consts), _py_number(node.right, consts)
        if left is None or right is None:
            return None
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Div) and right:
            return left / right
    return None


def _first_arg(node: ast.Call) -> ast.expr | None:
    if node.args:
        return node.args[0]
    return next((kw.value for kw in node.keywords if kw.arg in ("timeout", "seconds", "interval", "delay")), None)


def _own_calls(nodes: list[ast.AST]) -> list[ast.Call]:
    """Every call in `nodes`, not descending into a nested function, lambda or class."""
    calls: list[ast.Call] = []
    stack: list[ast.AST] = list(nodes)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            calls.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return calls


def _is_spawn(call: ast.Call) -> bool:
    owner, name = _callee(call)
    return (owner, name) in _SPAWNS or (owner is None and name in _SPAWN_NAMES)


def _waits(calls: list[ast.Call]) -> list[ast.Call]:
    """The calls that pace a loop: any `sleep(...)`, and a `wait(...)` given a timeout (`stop.wait(1)`)."""
    return [c for c in calls if _callee(c)[1] == "sleep" or (_callee(c)[1] == "wait" and _first_arg(c) is not None)]


@dataclass
class _PyContext:
    path: str
    consts: dict[str, float]
    declared: dict[str, float]  # loop scope -> declared interval_s, for this file
    handler: bool
    cached: bool
    facts: PyFacts


def _loop_verdict(ctx: _PyContext, scope: str, intervals: list[ast.expr | None], spawns: bool, line: int,
                  what: str) -> None:
    if scope in ctx.declared:
        ctx.facts.loops_seen.add(scope)
        return
    values = [_py_number(arg, ctx.consts) for arg in intervals]
    known = [v for v in values if v is not None]
    if spawns:
        why = "starts an outside program on each pass"
    elif known and min(known) < LOOP_FLOOR_S:
        why = f"sleeps {min(known):g} s, under the {LOOP_FLOOR_S:g} s floor"
    elif len(known) < len(values):
        why = "sleeps for an interval that is not a literal, so it cannot be held to the floor"
    else:
        return
    ctx.facts.findings.append((line, "unwatched-loop", f"{what} in `{scope}` {why}; declare it under "
                               "[[request_budget.loops]] in .fleet.toml with its interval and why it cannot wait "
                               "for a viewer"))


def _check_loops(ctx: _PyContext, body: list[ast.stmt], scope: str) -> None:
    for node in _walk_scope(body):
        if isinstance(node, ast.While):
            calls = _own_calls([node.test, *node.body])
            waits = _waits(calls)
            if waits:
                _loop_verdict(ctx, scope, [_first_arg(c) for c in waits], any(_is_spawn(c) for c in calls),
                              node.lineno, "a `while` loop")
        elif isinstance(node, ast.Call) and _rearms(node, scope):
            _loop_verdict(ctx, scope, [node.args[0]], False, node.lineno, "a re-arming timer")


def _rearms(call: ast.Call, scope: str) -> bool:
    """A `Timer(delay, f)` / `call_later(delay, f)` whose callback is the function it is created in."""
    _owner, name = _callee(call)
    if name not in ("Timer", "call_later") or len(call.args) < 2:
        return False
    target = call.args[1]
    own = scope.rsplit(".", 1)[-1]
    return (isinstance(target, ast.Name) and target.id == own) or (
        isinstance(target, ast.Attribute) and target.attr == own)


def _walk_scope(body: list[ast.stmt]) -> list[ast.AST]:
    """Every node in `body`, not descending into nested functions or classes (they are scopes of their own)."""
    out: list[ast.AST] = []
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        out.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return sorted(out, key=lambda n: getattr(n, "lineno", 0))


def _scopes(tree: ast.Module) -> list[tuple[str, list[ast.stmt]]]:
    """(qualified name, body) of the module and of every function and method in it."""
    found: list[tuple[str, list[ast.stmt]]] = [(MODULE_SCOPE, tree.body)]

    def visit(body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.append((prefix + node.name, node.body))
                visit(node.body, prefix + node.name + ".")
            elif isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + ".")
            else:
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, ast.stmt):
                        visit([child], prefix)

    visit(tree.body, "")
    return found


def _check_calls(ctx: _PyContext, tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == MIDDLEWARE for a in node.names):
            ctx.facts.uses_middleware = True
        if isinstance(node, ast.Attribute) and node.attr == MIDDLEWARE:
            ctx.facts.uses_middleware = True
        if not isinstance(node, ast.Call):
            continue
        owner, name = _callee(node)
        if name in _APP_CLASSES and ctx.facts.app_line is None:
            ctx.facts.app_line = node.lineno
        if name == "to_thread" or (name == "run_in_executor" and node.args and isinstance(node.args[0], ast.Constant)
                                   and node.args[0].value is None):
            ctx.facts.findings.append((node.lineno, "default-pool", f"`{name}` runs on the shared default pool; "
                                       "submit to a named pool for this class of work"))
        if ctx.handler and not ctx.cached and (
                name in _SCANS_ANY or (owner, name) in _SCANS_ON or (owner is None and name in _SCAN_NAMES)
                or (name == "glob" and owner != "glob")):
            ctx.facts.findings.append((node.lineno, "scan-per-request", f"`{name}` walks the disk inside a request "
                                       "handler module; read through the shared stat cache"))


def _imports_any(tree: ast.Module, modules: tuple[str, ...]) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name in modules for a in node.names):
            return True
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module in modules or any(f"{node.module}.{a.name}" in modules for a in node.names):
                return True
    return False


def scan_python(text: str, path: str, *, declared: dict[str, float], handler: bool,
                stat_cache: tuple[str, ...]) -> PyFacts:
    """One Python file's `unwatched-loop`, `scan-per-request` and `default-pool` findings, and its app facts."""
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise NotConfirmed(f"{path}:{exc.lineno}: does not parse: {exc.msg}") from exc
    facts = PyFacts()
    ctx = _PyContext(path, _module_constants(tree), declared, handler,
                     bool(stat_cache) and _imports_any(tree, stat_cache), facts)
    for scope, body in _scopes(tree):
        _check_loops(ctx, body, scope)
    _check_calls(ctx, tree)
    return facts
