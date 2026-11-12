"""A parameter no function body reads is generality nothing exercises.

Callers dutifully compute and pass it, and the signature promises a behaviour
that does not exist. Product code (``src/``, ``app/``, ``scripts/``) takes no
such parameter; a callback that must keep a fixed signature names the
parameter with a leading underscore.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRODUCT_DIRS = ("src", "app", "scripts")


def _is_stub(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A body of only a docstring, ``pass``, ``...`` or ``raise`` declares an interface."""
    for stmt in func.body:
        if isinstance(stmt, (ast.Pass, ast.Raise)):
            continue
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
            continue
        return False
    return True


def _is_override_or_overload(func: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    names = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "") for d in func.decorator_list}
    return bool(names & {"overload", "abstractmethod", "override"})


def _unused_parameters(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    args = func.args
    declared = [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]
    read = {n.id for stmt in func.body for n in ast.walk(stmt) if isinstance(n, ast.Name)}
    skip = {"self", "cls"}
    return [a for a in declared if a not in read and a not in skip and not a.startswith("_")]


def find_unused_parameters() -> list[str]:
    found: list[str] = []
    for folder in PRODUCT_DIRS:
        for path in sorted((ROOT / folder).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if _is_stub(node) or _is_override_or_overload(node):
                    continue
                for name in _unused_parameters(node):
                    found.append(f"{path.relative_to(ROOT).as_posix()}:{node.lineno} {node.name}({name})")
    return found


def test_no_function_takes_a_parameter_it_never_reads() -> None:
    assert find_unused_parameters() == []


def test_the_check_flags_an_unused_parameter() -> None:
    func = ast.parse("def f(a, b):\n    return a\n").body[0]
    assert _unused_parameters(func) == ["b"]


def test_the_check_leaves_a_used_or_underscored_parameter() -> None:
    func = ast.parse("def f(a, _b, *, c=1):\n    return a + c\n").body[0]
    assert _unused_parameters(func) == []
