"""Implicit-state-machine readers for the conventions ratchet: three suspect rules.

`scripts/conventions_ratchet.py` loads this sibling from beside itself (manifest key
`conventions_state`). An implicit state machine is a concept with real modes (stopped,
loading, ready, failed) that the code never declares as one type; the modes are re-derived
from status strings and flags spread across modules (docs/implicit-state-machines.md). The
rules flag suspects; an agent confirms each one before anything is declared:

  * `state-literal` -- a lifecycle string (`"running"`, `"failed"`) compared with `==`/`!=`/`in`
    against, or assigned to, a variable, attribute or key whose name says it holds a state
    (`status`, `state`, `phase`, `stage`, `lifecycle`);
  * `state-set-repeat` -- the same set of two or more lifecycle strings (a tuple, list or set
    literal, `Literal[...]`, or an `Enum`'s string values) spelled out in more than one module;
  * `state-write` -- a boolean or `None` written to a lifecycle-named attribute or key
    (`_loading`, `is_ready`, `*_pending`, `*_started`) from more than one module.

Python modules are read from their syntax tree; JavaScript and TypeScript modules (`.js` `.mjs`
`.cjs` `.ts` `.jsx` `.tsx`) from a stdlib token stream, with the same three patterns and the same
`[[state_machines]]` declaration, plus the JS spellings with no Python twin: `switch (x.status)`
cases, `[...].includes(x.status)`, all-string object and `enum` bodies, and TypeScript unions of
string literals. Both languages share one vocabulary and count toward the same cross-module sets.

Code in a module that `[[state_machines]]` declares is never reported: declaring the machine
is what clears its suspects. Its spellings still count toward the cross-module rules, so a
second module re-spelling the declared states or writing its flag is still a hit.

Nothing here reads a file, a baseline or git. `facts()` reads one parsed Python module and
`facts_js()` one JS/TS source; `cross_module()`
turns the facts of every module into hits; `Sink` carries both through one scan of the tree;
`parse_machines()` and `unresolved()` validate the declarations. Stdlib only.
"""

from __future__ import annotations

import ast
import re
import warnings
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

STATE_RULES = ("state-literal", "state-set-repeat", "state-write")

#: Words that name a lifecycle state. Generic outcome words (`error`, `success`, `ok`,
#: `active`, `unknown`, `none`) are left out: as values they are mostly log levels, HTTP
#: outcomes and toggles, and they made most of the noise when the rule was tuned.
LIFECYCLE = frozenset({
    "idle", "loading", "loaded", "ready", "starting", "started", "running", "stopping", "stopped",
    "pending", "queued", "waiting", "failed", "finished", "completed", "done", "cancelled",
    "canceled", "crashed", "retrying", "paused", "resuming", "connecting", "connected",
    "disconnected", "booting", "warming", "succeeded", "errored", "aborted", "expired",
    "draining", "terminated", "killed", "dead", "offline", "online", "in_progress", "scheduled",
})
#: A name that holds a state: its last word (after `_`) is one of these.
_STATE_HOLDER = re.compile(r"(?:^|_)(?:status|state|phase|stage|lifecycle)$", re.IGNORECASE)
#: A lifecycle-named flag: `loading`, `_is_ready`, `has_started`, `scan_pending`, `model_loaded`.
_FLAG_WORDS = ("loading|loaded|ready|starting|started|running|stopping|stopped|pending|busy|"
               "connected|connecting|retrying|paused|initialized|initialised|booted|warming|warm|"
               "finished|failed|draining|in_flight|inflight")
_FLAG = re.compile(rf"^_*(?:(?:is|has|was)_)?(?:\w+_)?(?:{_FLAG_WORDS})$", re.IGNORECASE)
_ENUM_BASES = frozenset({"Enum", "StrEnum", "IntEnum"})


@dataclass(frozen=True)
class Site:
    """One spelling: where it is, and the text it carries (a literal, a set key or a flag name)."""

    line: int
    end: int
    key: str


@dataclass
class Facts:
    """What one module spells: state-literal sites, lifecycle sets and lifecycle-flag writes."""

    literals: list[Site] = field(default_factory=list)
    sets: list[Site] = field(default_factory=list)
    writes: list[Site] = field(default_factory=list)


def _name_of(node: ast.expr) -> str:
    """The identifier a value is stored under: `x`, `obj.x`, `d["x"]`, `d.get("x")`, a dict key `"x"`, else ""."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
        return node.slice.value
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get"
            and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
        return node.args[0].value
    return ""


def _lifecycle(node: ast.expr) -> str:
    return node.value if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                          and node.value.lower() in LIFECYCLE) else ""


def _holds_state(node: ast.expr) -> bool:
    return bool(_STATE_HOLDER.search(_name_of(node)))


def _members(node: ast.expr) -> list[ast.expr]:
    """The items of a tuple/list/set literal or a `frozenset({...})` / `set([...])` call."""
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return list(node.elts)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("frozenset", "set")
            and len(node.args) == 1):
        return _members(node.args[0])
    return []


def _set_key(items: list[ast.expr]) -> str:
    """A sorted, comma-joined key when every item is a string and two or more of them are lifecycle words."""
    if not items or not all(isinstance(i, ast.Constant) and isinstance(i.value, str) for i in items):
        return ""
    if sum(1 for i in items if _lifecycle(i)) < 2:
        return ""
    return ",".join(sorted({str(i.value).lower() for i in items if isinstance(i, ast.Constant)}))


def _site(node: ast.AST, key: str) -> Site:
    line = getattr(node, "lineno", 0)
    return Site(line, getattr(node, "end_lineno", line) or line, key)


def _flag_key(target: ast.expr) -> str:
    """`load.ready` for a write to a lifecycle flag on an object or a key, else "".

    The receiver's own name is part of the key, so `self.ready` in one class and `load.ready` in
    another are two flags; a bare local name is never shared state, so it is never a key.
    """
    name: object
    if isinstance(target, ast.Attribute):
        receiver, name = target.value, target.attr
    elif isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
        receiver, name = target.value, target.slice.value
    else:
        return ""
    if not (isinstance(name, str) and _FLAG.match(name)):
        return ""
    owner = _dotted_tail(receiver.func if isinstance(receiver, ast.Call) else receiver) or "?"
    return f"{owner}.{name.lstrip('_').lower()}"


def _is_flag_value(node: ast.expr | None) -> bool:
    if isinstance(node, ast.Constant):
        return node.value is None or isinstance(node.value, bool)
    return isinstance(node, (ast.Compare, ast.BoolOp)) or (
        isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not))


class _Reader(ast.NodeVisitor):
    def __init__(self, rules: frozenset[str]) -> None:
        self.rules = rules
        self.facts = Facts()
        self._in_set: set[int] = set()  # members already counted as one set

    def _literal(self, node: ast.AST, value: ast.expr) -> None:
        word = _lifecycle(value)
        if word and "state-literal" in self.rules:
            self.facts.literals.append(_site(node, word))

    def _set(self, node: ast.AST, items: list[ast.expr]) -> None:
        key = _set_key(items)
        if key and "state-set-repeat" in self.rules:
            self.facts.sets.append(_site(node, key))
            self._in_set.update(id(i) for i in items)

    def visit_Compare(self, node: ast.Compare) -> None:
        sides = [node.left, *node.comparators]
        for op, left, right in zip(node.ops, sides, sides[1:]):
            if isinstance(op, (ast.Eq, ast.NotEq)):
                for value, other in ((left, right), (right, left)):
                    if _holds_state(other):
                        self._literal(node, value)
            elif isinstance(op, (ast.In, ast.NotIn)) and _holds_state(left):
                for item in _members(right):
                    self._literal(node, item)
        self.generic_visit(node)

    def _assign(self, node: ast.AST, targets: list[ast.expr], value: ast.expr | None) -> None:
        if value is not None and any(_holds_state(t) for t in targets):
            self._literal(node, value)
        if _is_flag_value(value) and "state-write" in self.rules:
            for target in targets:
                key = _flag_key(target)
                if key:
                    self.facts.writes.append(_site(node, key))

    def visit_Assign(self, node: ast.Assign) -> None:
        self._assign(node, node.targets, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assign(node, [node.target], node.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values):
            if key is not None and _holds_state(key):
                self._literal(value, value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg and _STATE_HOLDER.search(kw.arg):
                self._literal(kw.value, kw.value)
        if (isinstance(node.func, ast.Name) and node.func.id == "setattr" and len(node.args) == 3
                and isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                and _is_flag_value(node.args[2]) and "state-write" in self.rules):
            key = _flag_key(ast.Attribute(value=node.args[0], attr=node.args[1].value))
            if key:
                self.facts.writes.append(_site(node, key))
        self._set(node, _members(node))
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _dotted_tail(node.value) == "Literal":
            items = list(node.slice.elts) if isinstance(node.slice, ast.Tuple) else [node.slice]
            self._set(node, items)
        self.generic_visit(node)

    def _visit_collection(self, node: ast.Tuple | ast.List | ast.Set) -> None:
        # `frozenset({...})` and `Literal[...]` already counted their items as one set.
        if not (node.elts and all(id(e) in self._in_set for e in node.elts)):
            self._set(node, list(node.elts))
        self.generic_visit(node)

    visit_Tuple = visit_List = visit_Set = _visit_collection  # ast.NodeVisitor dispatches on these names

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if any(_dotted_tail(b) in _ENUM_BASES for b in node.bases):
            values: list[ast.expr] = [s.value for s in node.body
                                      if isinstance(s, ast.Assign) and isinstance(s.value, ast.Constant)]
            self._set(node, values)
        self.generic_visit(node)


def _dotted_tail(node: ast.expr) -> str:
    return node.attr if isinstance(node, ast.Attribute) else node.id if isinstance(node, ast.Name) else ""


@dataclass(frozen=True)
class StateMachine:
    """One `[[state_machines]]` entry: the module that owns a declared state, its type and derive function."""

    name: str
    module: str
    type: str
    derive: str = ""  # empty: a stored state with no derive function


_MACHINE_KEYS = ("name", "module", "type", "derive")


def parse_machines(raw: object) -> tuple[StateMachine, ...]:
    """The parsed top-level `state_machines` value -> entries; raises `ValueError` on a malformed one."""
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ValueError("state_machines must be an array of tables ([[state_machines]])")
    machines = []
    for number, item in enumerate(raw, start=1):
        where = f"[[state_machines]] #{number}"
        unknown = sorted(set(item) - set(_MACHINE_KEYS))
        if unknown:
            raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)}; known: {', '.join(_MACHINE_KEYS)}")
        values = {k: item.get(k, "" if k == "derive" else None) for k in _MACHINE_KEYS}
        bad = [k for k, v in values.items() if not isinstance(v, str) or (k != "derive" and not v.strip())]
        if bad:
            raise ValueError(f"{where} needs {', '.join(bad)} as non-empty string(s)")
        name, module, kind, derive = (str(values[k]) for k in _MACHINE_KEYS)
        machines.append(StateMachine(name, module.replace("\\", "/").strip("/"), kind, derive))
    return tuple(machines)


def unresolved(machine: StateMachine, source: bytes | str) -> list[str]:
    """The declared `type` / `derive` names the machine's module does not define at its top level.

    The module is Python or, by its suffix, JavaScript/TypeScript; raises `SyntaxError` or
    `ValueError` when it cannot be read.
    """
    if is_js_source(machine.module):
        defined = js_module_names(source)
    else:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape in the declared module is not this check's finding
            defined = module_names(ast.parse(source))
    return [n for n in (machine.type, machine.derive) if n and n not in defined]


class Sink:
    """What the state rules collect while a tree is scanned: per-module spellings and the marked sites."""

    def __init__(self, rules: frozenset[str]) -> None:
        self.rules = rules & frozenset(STATE_RULES)
        self.facts: dict[str, Facts] = {}
        self.exempt: dict[str, set[tuple[int, str]]] = {}

    def collect(self, tree: ast.Module, path: str, marked: Callable[[str, int, int], bool]) -> None:
        """Read one Python module; `marked(rule, first_line, last_line)` says a site carries its exempt marker."""
        self._add(path, facts(tree, self.rules), marked)

    def collect_js(self, source: bytes | str, path: str, marked: Callable[[str, int, int], bool]) -> None:
        """Read one JS/TS module the same way; raises `ValueError` when it cannot be tokenized."""
        self._add(path, facts_js(source, self.rules), marked)

    def _add(self, path: str, found: Facts, marked: Callable[[str, int, int], bool]) -> None:
        self.facts[path] = found
        sites = (("state-literal", found.literals), ("state-set-repeat", found.sets), ("state-write", found.writes))
        self.exempt[path] = {(s.line, rule) for rule, group in sites for s in group if marked(rule, s.line, s.end)}

    def hits(self, reported: set[str]) -> dict[str, list[tuple[int, str, str]]]:
        """(line, rule, message) per `reported` module; a marked site is dropped but still counts as a home."""
        out: dict[str, list[tuple[int, str, str]]] = {}
        for path, found in cross_module(self.facts, reported).items():
            kept = [(s.line, rule, msg) for s, rule, msg in found
                    if rule in self.rules and (s.line, rule) not in self.exempt[path]]
            if kept:
                out[path] = kept
        return out


def module_names(tree: ast.Module) -> set[str]:
    """The names a module defines at its top level: classes, functions, type aliases and assigned names."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            names.add(node.name.id)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return names


def facts(tree: ast.Module, rules: frozenset[str]) -> Facts:
    """Every state spelling in one parsed module, for the enabled state rules."""
    reader = _Reader(rules & frozenset(STATE_RULES))
    if reader.rules:
        reader.visit(tree)
    return reader.facts


def cross_module(per_path: dict[str, Facts], reported: set[str]) -> dict[str, list[tuple[Site, str, str]]]:
    """(site, rule, message) per path in `reported`: literals always; sets and writes seen in 2+ modules.

    `per_path` holds every scanned module, a declared machine's module included, so its spellings
    count toward repetition; only paths in `reported` (not declared, not test modules) get hits.
    """
    homes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path, f in per_path.items():
        for site in f.sets:
            homes[("state-set-repeat", site.key)].add(path)
        for site in f.writes:
            homes[("state-write", site.key)].add(path)
    out: dict[str, list[tuple[Site, str, str]]] = {}
    for path in sorted(reported & set(per_path)):
        f = per_path[path]
        found = [(s, "state-literal", f"lifecycle string {s.key!r} compared or stored as a state outside a "
                                      "declared machine") for s in f.literals]
        # A JS site counts Python homes too (a frontend re-spelling its backend's states); a Python site
        # counts Python homes only, so reading JS never changes a Python module's hits.
        python = not is_js_source(path)
        for rule, sites, what in (("state-set-repeat", f.sets, "the state set"), ("state-write", f.writes, "flag")):
            for s in sites:
                others = sorted(p for p in homes[(rule, s.key)] - {path} if not (python and is_js_source(p)))
                if others:
                    found.append((s, rule, f"{what} `{s.key}` is also spelled in {others[0]}"
                                           + (f" and {len(others) - 1} more" if len(others) > 1 else "")))
        if found:
            out[path] = found
    return out


# ------------------------------------------------------- JavaScript and TypeScript
#
# The same three rules over `.js` `.mjs` `.cjs` `.ts` `.jsx` `.tsx`, read from a token stream
# rather than a syntax tree: a stdlib tokenizer (comments, strings, template literals with their
# `${...}` code, regex literals) plus bracket matching is enough for these patterns, and keeps the
# vendored scripts free of Node. What a token stream cannot tell apart is listed in
# docs/conventions-ratchet.md, "State rules in JavaScript and TypeScript".

JS_SUFFIXES = (".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx")


@dataclass(frozen=True)
class Tok:
    """One token: `name`, `str` (its text unquoted), `tmpl` (a template with `${}`), `num`, `regex` or `punct`."""

    kind: str
    text: str
    line: int


_PUNCT = sorted(""">>>= ... === !== **= <<= >>= >>> &&= ||= ??= => == != <= >= && || ?? ?. ++ -- += -= *= /= %=
&= |= ^= ** << >> { } ( ) [ ] ; , < > + - * / % & | ^ ! ~ ? : = . @ #""".split(), key=len, reverse=True)
_JS_NAME = re.compile(r"[A-Za-z_$\u0080-￿][\w$\u0080-￿]*")
_JS_NUM = re.compile(r"\.?\d[\w.]*")
#: After these a `/` starts a regex literal; after a value (a name, `)`, `]`, `}`) it divides.
_REGEX_AFTER = frozenset({"return", "typeof", "case", "do", "else", "in", "of", "new", "delete", "void",
                          "throw", "instanceof", "yield", "await"})
_JS_CMP = frozenset({"==", "===", "!=", "!=="})
_JS_FLAG_CMP = frozenset({"==", "===", "!=", "!==", "<=", ">="})  # `<` `>` are TypeScript generics too
_JS_CONTINUES = frozenset({".", "?.", "?", ":", "&&", "||", "??", "+", "-", "*", "/", "%", "|", "&",
                           *_JS_FLAG_CMP})


def _js_string(text: str, i: int, quote: str) -> int:
    """Index just past the closing quote, or -1 when the line ends first (a JSX apostrophe, not a string)."""
    j = i + 1
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == quote:
            return j + 1
        if c == "\n":
            return -1
        j += 1
    return -1


def _js_regex(text: str, i: int) -> int:
    """Index just past a regex literal's flags, or -1 when the line ends first (then `/` divides)."""
    j, in_class = i + 1, False
    while j < len(text):
        c = text[j]
        if c == "\\":
            j += 2
            continue
        if c == "\n":
            return -1
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < len(text) and (text[j].isalnum() or text[j] == "_"):
                j += 1
            return j
        j += 1
    return -1


def _regex_may_start(prev: Tok | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "name":
        return prev.text in _REGEX_AFTER
    return prev.kind == "punct" and prev.text not in (")", "]", "}")


def tokenize_js(text: str, comments: list[tuple[int, int, str]] | None = None) -> list[Tok]:
    """The tokens of one JS/TS source; raises `ValueError` on an unterminated comment or template.

    Comments are not tokens; a caller that needs them passes a list and gets `(first line, last line, text)`
    for each, in source order.

    A template literal's `${...}` code is tokenized in place; the literal itself is one `str` token
    when it has no `${}`, else a `tmpl` token (it is never a fixed state string).
    """
    toks: list[Tok] = []
    modes: list[int] = []  # one brace depth per open `${`: when it closes, the template resumes
    line, i, n = 1, 0, len(text)
    if text.startswith("#!"):
        i = text.find("\n") if "\n" in text else n

    def template(start: int, first_line: int) -> int:
        """Scan a template chunk from `start` (just past a backtick or `}`); return where code resumes."""
        nonlocal line
        j = start
        while j < n:
            c = text[j]
            if c == "\\":
                j += 2
                continue
            if c == "`":
                return j + 1
            if c == "$" and text.startswith("${", j):
                modes.append(0)
                return j + 2
            if c == "\n":
                line += 1
            j += 1
        raise ValueError(f"unterminated template literal from line {first_line}")

    while i < n:
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
        elif c in " \t\r\f\v﻿":
            i += 1
        elif text.startswith("//", i):
            end = text.find("\n", i) if "\n" in text[i:] else n
            if comments is not None:
                comments.append((line, line, text[i:end]))
            i = end
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            if end < 0:
                raise ValueError(f"unterminated block comment from line {line}")
            if comments is not None:
                comments.append((line, line + text.count("\n", i, end), text[i:end + 2]))
            line += text.count("\n", i, end)
            i = end + 2
        elif c in "'\"":
            end = _js_string(text, i, c)
            if end < 0:  # an apostrophe in JSX text
                toks.append(Tok("punct", c, line))
                i += 1
            else:
                toks.append(Tok("str", text[i + 1:end - 1], line))
                i = end
        elif c == "`":
            start_line, start = line, i
            resume = template(i + 1, line)
            plain = text[resume - 1] == "`"
            toks.append(Tok("str" if plain else "tmpl", text[start + 1:resume - 1] if plain else "", start_line))
            i = resume
        elif c == "}" and modes and modes[-1] == 0:
            modes.pop()
            i = template(i + 1, line)
        elif c == "/" and _regex_may_start(toks[-1] if toks else None) and (end := _js_regex(text, i)) > 0:
            toks.append(Tok("regex", text[i:end], line))
            i = end
        elif m := _JS_NAME.match(text, i):
            toks.append(Tok("name", m.group(), line))
            i = m.end()
        elif m := _JS_NUM.match(text, i):
            toks.append(Tok("num", m.group(), line))
            i = m.end()
        else:
            p = next((p for p in _PUNCT if text.startswith(p, i)), c)
            if modes and p in ("{", "}"):
                modes[-1] += 1 if p == "{" else -1
            toks.append(Tok("punct", p, line))
            i += len(p)
    if modes:
        raise ValueError("unterminated template literal interpolation")
    return toks


def _js_pairs(toks: list[Tok]) -> dict[int, int]:
    """Index of each bracket -> its partner; raises `ValueError` when they do not balance."""
    pairs: dict[int, int] = {}
    stack: list[int] = []
    closer = {")": "(", "]": "[", "}": "{"}
    for k, t in enumerate(toks):
        if t.kind != "punct":
            continue
        if t.text in ("(", "[", "{"):
            stack.append(k)
        elif t.text in closer:
            if not stack or toks[stack[-1]].text != closer[t.text]:
                raise ValueError(f"unbalanced {t.text!r} on line {t.line}")
            opener = stack.pop()
            pairs[opener], pairs[k] = k, opener
    if stack:
        raise ValueError(f"unclosed {toks[stack[-1]].text!r} from line {toks[stack[-1]].line}")
    return pairs


_NO_TOK = Tok("", "", 0)
_JS_FLAG_LITERALS = frozenset({"true", "false", "null", "undefined"})
_UPPER = re.compile(r"^[A-Z][A-Z0-9_]*$")
_JS_DECLARES = frozenset({"const", "let", "var", "function", "class", "enum", "interface", "type"})


class _JsReader:
    """The state spellings in one tokenized JS/TS module, mirroring `_Reader`'s Python patterns."""

    def __init__(self, toks: list[Tok], pairs: dict[int, int], rules: frozenset[str]) -> None:
        self.t, self.pairs, self.rules = toks, pairs, rules
        self.facts = Facts()

    def at(self, k: int) -> Tok:
        return self.t[k] if 0 <= k < len(self.t) else _NO_TOK

    def punct(self, k: int, *texts: str) -> bool:
        tok = self.at(k)
        return tok.kind == "punct" and tok.text in texts

    def name_before(self, k: int) -> str:
        """The name the operand ending just before `k` is stored under: `x`, `o.x`, `o['x']`, `m.get('x')`."""
        tok = self.at(k - 1)
        if tok.kind == "name":
            return tok.text
        if self.punct(k - 1, "]", ")"):
            opener = self.pairs[k - 1]
            inner = self.at(opener + 1)
            single = opener + 2 == k - 1 and inner.kind == "str"
            if single and (tok.text == "]" or self.at(opener - 1).text == "get"):
                return inner.text
        return ""

    def chain_after(self, k: int) -> tuple[str, int]:
        """(stored-under name, index past the operand) for the member chain starting at `k`."""
        if self.at(k).kind != "name":
            return "", k
        name, j = self.at(k).text, k + 1
        while True:
            if self.punct(j, ".", "?.") and self.at(j + 1).kind == "name":
                name, j = self.at(j + 1).text, j + 2
            elif self.punct(j, "[", "("):
                close = self.pairs[j]
                inner = self.at(j + 1)
                single = close == j + 2 and inner.kind == "str"
                name = inner.text if single and (self.at(j).text == "[" or name == "get") else ""
                j = close + 1
            else:
                return name, j

    def lone_str(self, k: int) -> Tok | None:
        """The string token at `k` when it is a whole operand (not `'x'.length`, not `'a' + b`)."""
        tok = self.at(k)
        if tok.kind != "str" or self.punct(k + 1, ".", "?.", "[", "(", "+") or self.punct(k - 1, ".", "+"):
            return None
        return tok

    def literal(self, line: int, tok: Tok | None) -> None:
        if tok and tok.text.lower() in LIFECYCLE and "state-literal" in self.rules:
            self.facts.literals.append(Site(line, max(line, tok.line), tok.text))

    def strings(self, opener: int) -> list[Tok]:
        """The items between a bracket and its partner when every one is a single string, else []."""
        close, items, j = self.pairs[opener], [], opener + 1
        while j < close:
            if self.at(j).kind != "str" or not (j + 1 == close or self.punct(j + 1, ",")):
                return []
            items.append(self.at(j))
            j += 2
        return items

    def entries(self, opener: int) -> list[Tok]:
        """The values of an enum-shaped body: `enum S { A = 'v' }`, or `{ K: 'v', ... }` keyed in UPPER_CASE or by its values.

        A record such as `{ answer: 'done', label: 'Done' }` is data, not a state set, so its values never count.
        """
        close, keys, values, j = self.pairs[opener], [], [], opener + 1
        while j < close:
            key, value = self.at(j), self.at(j + 2)
            if (key.kind not in ("name", "str") or not self.punct(j + 1, ":", "=")
                    or value.kind != "str" or not (j + 3 == close or self.punct(j + 3, ","))):
                return []
            keys.append(key.text)
            values.append(value)
            j += 4
        is_enum = self.at(opener - 2).text == "enum" and self.at(opener - 1).kind == "name"
        enum_keyed = all(_UPPER.match(k) for k in keys) or all(
            k.lower() == v.text.lower() for k, v in zip(keys, values))
        return values if is_enum or enum_keyed else []

    def add_set(self, line: int, items: list[Tok]) -> None:
        if "state-set-repeat" not in self.rules or sum(1 for i in items if i.text.lower() in LIFECYCLE) < 2:
            return
        key = ",".join(sorted({i.text.lower() for i in items}))
        self.facts.sets.append(Site(line, max(i.line for i in items), key))

    def is_flag_value(self, start: int) -> bool:
        """`true`/`false`/`null`/`undefined`, a `!x`, or a comparison: what Python's `_is_flag_value` accepts."""
        if self.punct(start, "!"):
            return True
        j, compared = start, False
        while j < len(self.t):
            tok = self.t[j]
            if tok.kind == "punct" and tok.text in (";", ",", ")", "]", "}"):
                break
            if j > start and tok.line > self.t[j - 1].line and not (
                    self.punct(j - 1, *_JS_CONTINUES) or self.punct(j, *_JS_CONTINUES)):
                break  # a new line that does not continue the expression: automatic semicolon insertion
            compared |= tok.kind == "punct" and tok.text in _JS_FLAG_CMP
            j = self.pairs[j] + 1 if self.punct(j, "(", "[", "{") else j + 1
        first = self.at(start)
        return compared or (j == start + 1 and first.kind == "name" and first.text in _JS_FLAG_LITERALS)

    def write(self, k: int) -> None:
        """A lifecycle flag on an object or key assigned a flag value at the `=` at `k`."""
        target = self.at(k - 1)
        if target.kind == "name" and self.punct(k - 2, "."):
            flag, receiver = target.text, k - 3
        elif self.punct(k - 1, "]") and self.pairs[k - 1] + 2 == k - 1 and self.at(k - 2).kind == "str":
            flag, receiver = self.at(k - 2).text, self.pairs[k - 1] - 1
        else:
            return
        if not _FLAG.match(flag) or not self.is_flag_value(k + 1):
            return
        owner = self.at(self.pairs[receiver] - 1) if self.punct(receiver, ")") else self.at(receiver)
        name = owner.text if owner.kind == "name" else "?"
        self.facts.writes.append(Site(target.line, target.line, f"{name}.{flag.lstrip('_').lower()}"))

    def run(self) -> Facts:
        braces: list[int] = []  # the open `{` around the current token
        switch_bodies: dict[int, bool] = {}  # a `switch (...)` body's `{` -> its subject holds a state
        k = 0
        while k < len(self.t):
            tok = self.t[k]
            if tok.kind == "punct":
                self._punct(k, tok, braces)
            elif tok.kind == "name":
                self._name(k, tok, braces, switch_bodies)
            elif tok.kind == "str" and self.punct(k + 1, "|"):
                k = self._union(k)
                continue
            k += 1
        return self.facts

    def _punct(self, k: int, tok: Tok, braces: list[int]) -> None:
        text = tok.text
        if text == "{":
            braces.append(k)
            self.add_set(tok.line, self.entries(k))
        elif text == "}" and braces:
            braces.pop()
        elif text == "[" and _regex_may_start(self.at(k - 1) if k else None):
            self.add_set(tok.line, self.strings(k))  # an array literal, not an index: no value before it
        elif text in _JS_CMP:
            if _STATE_HOLDER.search(self.name_before(k)):
                self.literal(tok.line, self.lone_str(k + 1))
            if _STATE_HOLDER.search(self.chain_after(k + 1)[0]):
                self.literal(tok.line, self.lone_str(k - 1))
        elif text == "=":
            if _STATE_HOLDER.search(self.name_before(k)):
                self.literal(tok.line, self.lone_str(k + 1))
            if "state-write" in self.rules:
                self.write(k)
        elif text == ":":
            key = k - 2 if self.punct(k - 1, "?") else k - 1
            is_key = self.at(key).kind in ("name", "str") and self.punct(key - 1, "{", ",")
            if is_key and _STATE_HOLDER.search(self.at(key).text) and not self.punct(k + 2, "|"):
                self.literal(tok.line, self.lone_str(k + 1))

    def _name(self, k: int, tok: Tok, braces: list[int], switch_bodies: dict[int, bool]) -> None:
        if tok.text == "switch" and self.punct(k + 1, "("):
            close = self.pairs[k + 1]
            if self.punct(close + 1, "{"):
                switch_bodies[close + 1] = bool(_STATE_HOLDER.search(self.name_before(close)))
        elif tok.text == "case" and braces and switch_bodies.get(braces[-1]) and self.punct(k + 2, ":"):
            self.literal(tok.line, self.at(k + 1) if self.at(k + 1).kind == "str" else None)
        elif tok.text in ("includes", "has") and self.punct(k - 1, ".", "?.") and self.punct(k + 1, "("):
            name, end = self.chain_after(k + 2)
            if end != self.pairs[k + 1] or not _STATE_HOLDER.search(name):
                return
            receiver = k - 2
            if self.punct(receiver, ")"):  # `new Set([...]).has(x.status)`
                opener = self.pairs[receiver]
                receiver = receiver - 1 if self.at(opener - 1).text == "Set" and self.punct(receiver - 1, "]") else -1
            if receiver >= 0 and self.punct(receiver, "]"):
                for item in self.strings(self.pairs[receiver]):
                    self.literal(tok.line, item)

    def _union(self, k: int) -> int:
        """A TypeScript union of string literals (`'idle' | 'running'`) is a state set; returns the index past it."""
        items, j = [self.t[k]], k
        while self.punct(j + 1, "|") and self.at(j + 2).kind == "str":
            j += 2
            items.append(self.t[j])
        if len(items) > 1:
            self.add_set(self.t[k].line, items)
        return j + 1


def js_parse(source: bytes | str, comments: list[tuple[int, int, str]] | None = None) -> tuple[list[Tok], dict[int, int]]:
    """Tokens and bracket pairs of one JS/TS module; raises `ValueError` when it cannot be read.

    `comments`, when given, is filled as `tokenize_js` documents.
    """
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    toks = tokenize_js(text, comments)
    return toks, _js_pairs(toks)


def facts_js(source: bytes | str, rules: frozenset[str]) -> Facts:
    """Every state spelling in one JS/TS module, for the enabled state rules; raises `ValueError` when unreadable."""
    toks, pairs = js_parse(source)
    enabled = rules & frozenset(STATE_RULES)
    return _JsReader(toks, pairs, enabled).run() if enabled else Facts()


def js_module_names(source: bytes | str) -> set[str]:
    """The names a JS/TS module declares at its top level: `const`, `function`, `class`, `enum`, `type`, `interface`."""
    toks, _ = js_parse(source)
    names: set[str] = set()
    depth = 0
    for k, tok in enumerate(toks):
        if tok.kind == "punct" and tok.text in ("(", "[", "{"):
            depth += 1
        elif tok.kind == "punct" and tok.text in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and tok.kind == "name" and tok.text in _JS_DECLARES:
            nxt = k + 2 if k + 1 < len(toks) and toks[k + 1].text == "*" else k + 1
            if nxt < len(toks) and toks[nxt].kind == "name":
                names.add(toks[nxt].text)
    return names


def _js_body_names(toks: list[Tok], pairs: dict[int, int], opener: int) -> set[str]:
    """The member names and string values directly inside one `{...}` or `[...]`: enum, object, class body or array."""
    close, found, j = pairs[opener], set[str](), opener + 1
    while j < close:
        tok, nxt = toks[j], toks[j + 1]
        if tok.kind == "punct" and tok.text in ("(", "[", "{"):
            j = pairs[j] + 1  # a method, a nested value: not a member of this body
            continue
        if tok.kind == "str" and toks[opener].text == "[":
            found.add(tok.text)
        elif tok.kind in ("name", "str") and nxt.kind == "punct" and nxt.text in (":", "="):
            found.add(tok.text)
            if toks[j + 2].kind == "str":
                found.add(toks[j + 2].text)
        elif tok.kind == "name" and toks[opener].text == "{" and toks[j - 1].text in ("{", ",") and (
                j + 1 == close or nxt.text == ","):
            found.add(tok.text)  # a bare enum member: `enum S { A, B }`
        j += 1
    return found


def _js_declared_values(toks: list[Tok], pairs: dict[int, int], k: int) -> set[str]:
    """The state names of the declaration whose keyword is at `k` and whose name follows it."""
    keyword, j = toks[k].text, k + 2
    if keyword == "type":  # `type S = 'a' | 'b'`: the string literals of one union
        found = set[str]()
        while j < len(toks) and (toks[j].kind == "str" or toks[j].text in ("=", "|")):
            if toks[j].kind == "str":
                found.add(toks[j].text)
            j += 1
        return found
    while j < len(toks) and toks[j].text not in ("{", "[", ";"):
        if toks[j].text in ("(", "=", ".", "new", "extends", "implements") or toks[j].kind == "name":
            j += 1
        else:
            return set()  # `const S = compute(...)`: no literal body to read
    return _js_body_names(toks, pairs, j) if j < len(toks) and toks[j].text != ";" else set()


def js_state_names(source: bytes | str, type_name: str) -> list[str]:
    """The lower-cased state names a JS/TS owner module gives its declared type; empty when unreadable.

    Mirrors the Python reader: an `enum`, a `class`, an object or array bound by `const`, `let` or `var`
    contributes its member names and string values, a `type` alias its string-literal union.
    """
    try:
        toks, pairs = js_parse(source)
    except ValueError:
        return []
    toks = [*toks, _NO_TOK, _NO_TOK, _NO_TOK]
    found: set[str] = set()
    depth = 0
    for k, tok in enumerate(toks):
        if tok.kind == "punct" and tok.text in ("(", "[", "{"):
            depth += 1
        elif tok.kind == "punct" and tok.text in (")", "]", "}"):
            depth -= 1
        elif depth == 0 and tok.kind == "name" and tok.text in _JS_DECLARES - {"function", "interface"} \
                and toks[k + 1].kind == "name" and toks[k + 1].text == type_name:
            found |= _js_declared_values(toks, pairs, k)
    return sorted({s.lower() for s in found if s.isidentifier() and not s.startswith("_")})


def is_js_source(path: str) -> bool:
    """A JS/TS module the state rules read; a minified bundle is build output, not authored code."""
    lower = path.lower()
    return lower.endswith(JS_SUFFIXES) and not lower.endswith((".min.js", ".min.mjs", ".min.cjs"))
