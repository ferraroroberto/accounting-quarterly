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

Code in a module that `[[state_machines]]` declares is never reported: declaring the machine
is what clears its suspects. Its spellings still count toward the cross-module rules, so a
second module re-spelling the declared states or writing its flag is still a hit.

Nothing here reads a file, a baseline or git. `facts()` reads one parsed module; `cross_module()`
turns the facts of every module into hits; `Sink` carries both through one scan of the tree;
`parse_machines()` and `unresolved()` validate the declarations. Stdlib only.
"""

from __future__ import annotations

import ast
import re
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


def unresolved(machine: StateMachine, tree: ast.Module) -> list[str]:
    """The declared `type` / `derive` names the machine's parsed module does not define at its top level."""
    defined = module_names(tree)
    return [n for n in (machine.type, machine.derive) if n and n not in defined]


class Sink:
    """What the state rules collect while a tree is scanned: per-module spellings and the marked sites."""

    def __init__(self, rules: frozenset[str]) -> None:
        self.rules = rules & frozenset(STATE_RULES)
        self.facts: dict[str, Facts] = {}
        self.exempt: dict[str, set[tuple[int, str]]] = {}

    def collect(self, tree: ast.Module, path: str, marked: Callable[[str, int, int], bool]) -> None:
        """Read one module; `marked(rule, first_line, last_line)` says a site carries its exempt marker."""
        found = self.facts[path] = facts(tree, self.rules)
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
        for rule, sites, what in (("state-set-repeat", f.sets, "the state set"), ("state-write", f.writes, "flag")):
            for s in sites:
                others = sorted(homes[(rule, s.key)] - {path})
                if others:
                    found.append((s, rule, f"{what} `{s.key}` is also spelled in {others[0]}"
                                           + (f" and {len(others) - 1} more" if len(others) > 1 else "")))
        if found:
            out[path] = found
    return out
