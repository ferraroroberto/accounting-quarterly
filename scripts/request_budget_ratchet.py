"""Ratchet on request budget: polling and background waste may only go down.

A web app built and tested on a fast machine hides its waste: overlapping
browser polls, a server loop that wakes every second, a directory walk on every
request, one shared thread pool. This stage reads the source, counts each
rule's hits per file against a committed baseline, and fails only when a count
rises. Existing debt never blocks work, but it cannot grow.

The rules (docs/request-budget-ratchet.md; the reasons: docs/polling-budget.md):

  * `fixed-rate-poll` (RB1) -- a `setInterval` (or a declared `poll_wrappers`
    name built on one) in browser code whose callback reaches a server call, or
    a callback the file cannot show stays off the server. Polls go through the
    vendored poll helper (`startPoll`) instead;
  * `poll-floor` (RB2) -- such a poll, or a `startPoll` call, whose interval
    literal is under 2000 ms, unless the `startPoll` call declares `activeWhile`
    (it runs only while a job is in progress);
  * `unwatched-loop` (RB3) -- a server `while` loop paced by a sleep, or a timer
    that re-arms itself, that sleeps under 5 s, sleeps for an interval the
    source does not fix, or starts an outside program on each pass, unless
    `[[request_budget.loops]]` in `.fleet.toml` declares it;
  * `scan-per-request` (RB4) -- `os.walk`, `glob`/`rglob`, `iterdir`, `scandir`
    or `listdir` in a module under `handler_dirs` that does not import a
    `stat_cache` module;
  * `default-pool` (RB5) -- `asyncio.to_thread(` or `run_in_executor(None, ...)`;
  * `timing-middleware` (RB6) -- a web app (a `FastAPI(` or `Starlette(` call)
    whose tracked Python never imports the vendored `RequestTimingMiddleware`;
    counted once, on the file that builds the app;
  * `bare-exempt` -- a `request-budget-exempt` marker with no reason.

A single hit keeps its exception with `request-budget-exempt: <reason>` in a
comment on its line or in the comment lines right above it. Exemptions are
counted and printed on every run.

Opt-in, per adopter, in its own `.fleet.toml`:

    [request_budget_ratchet]
    enabled        = true
    baseline       = ".request-budget-ratchet-baseline.json"           # default
    rules          = [...]                                             # default: all
    browser_paths  = ["app/webapp/static/**", "app/webapp/templates/**"]  # default
    server_paths   = ["src/**", "app/**"]                              # default
    handler_dirs   = ["app/webapp/routers"]                            # default: none (RB4 OFF)
    stat_cache     = ["src.stat_cache"]                                # modules that count as the cache
    poll_wrappers  = ["createPoller"]                                  # repo wrappers around setInterval
    fetch_wrappers = ["apiGet"]                                        # repo wrappers around fetch
    exclude        = ["app/webapp/static/vendor/**"]                   # globs; never scanned

    [[request_budget.loops]]
    path       = "src/watcher.py"
    function   = "Watcher.run"          # the enclosing function; "<module>" at top level
    interval_s = 1
    why        = "reads the hardware's state while nobody is viewing"

Exit codes: 0 pass or not enabled; 1 a count rose, or the baseline is missing /
a mode was refused; 2 bad configuration, baseline or arguments; 3 NOT CONFIRMED
(`git` unusable, a file that cannot be read, decoded or parsed through) -- the
gate fails on it. Modes: `check`, `--init`, `--update-baseline` (shrink only),
`--update-baseline --allow-additions` (shows in review).

Vendor-verbatim (manifest key `request_budget_ratchet`) with its sibling reader
`request_budget_scan.py` (manifest key `request_budget_scan`), stdlib-only.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import ModuleType
from typing import Any


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)  # a dataclass resolves its module by name
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"request budget ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys request_budget_ratchet and request_budget_scan)")
        sys.exit(3)


scan = _load_sibling("request_budget_scan")
ConfigError: type[RuntimeError] = scan.ConfigError
NotConfirmed: type[RuntimeError] = scan.NotConfirmed
RULES: tuple[str, ...] = scan.RULES

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
DEFAULT_BASELINE = ".request-budget-ratchet-baseline.json"
DEFAULT_BROWSER_PATHS = ("app/webapp/static/**", "app/webapp/templates/**")
DEFAULT_SERVER_PATHS = ("src/**", "app/**")
#: Always excluded, whatever the adopter's own `exclude` adds; a minified file is third-party output.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**", "**/*.min.js")
JS_SUFFIXES = (".js", ".mjs", ".cjs")
HTML_SUFFIXES = (".html", ".htm", ".jinja", ".jinja2", ".j2")
GIT_TIMEOUT_S = 120
WIRED = "timing-middleware"

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "rules", "browser_paths", "server_paths", "handler_dirs", "stat_cache",
                "poll_wrappers", "fetch_wrappers", "exclude"}
_BUDGET_KEYS = {"loops", "visible_requests_per_minute"}
_LOOP_KEYS = {"path", "function", "interval_s", "why"}
_NAME = re.compile(r"[A-Za-z_$][\w$]*")


@dataclass(frozen=True)
class Config:
    baseline: str
    rules: tuple[str, ...]
    browser_paths: tuple[str, ...] = DEFAULT_BROWSER_PATHS
    server_paths: tuple[str, ...] = DEFAULT_SERVER_PATHS
    handler_dirs: tuple[str, ...] = ()
    stat_cache: tuple[str, ...] = ()
    poll_wrappers: tuple[str, ...] = ()
    fetch_wrappers: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    loops: dict[str, dict[str, float]] = field(default_factory=dict)  # path -> {scope: interval_s}


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    rule: str
    message: str


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], section: str, name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[{section}] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/") for v in value)


def _names(table: dict[str, object], name: str) -> tuple[str, ...]:
    values = _str_list(table, "request_budget_ratchet", name, ())
    bad = [v for v in values if not _NAME.fullmatch(v)]
    if bad:
        raise ConfigError(f"[request_budget_ratchet] {name}: {', '.join(bad)} is not a JS function name")
    return values


def parse_loops(data: dict[str, object]) -> dict[str, dict[str, float]]:
    """`[[request_budget.loops]]` -> `{path: {scope: interval_s}}`; every entry needs its interval and its why."""
    table = data.get("request_budget")
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise ConfigError("[request_budget] must be a table")
    unknown = sorted(set(table) - _BUDGET_KEYS)
    if unknown:
        raise ConfigError(f"[request_budget]: unknown key(s) {', '.join(unknown)}")
    loops: dict[str, dict[str, float]] = {}
    entries = table.get("loops", [])
    if not isinstance(entries, list):
        raise ConfigError("[request_budget] loops must be an array of tables ([[request_budget.loops]])")
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != _LOOP_KEYS:
            raise ConfigError(f"[[request_budget.loops]] entry {entry!r} needs exactly: {', '.join(sorted(_LOOP_KEYS))}")
        path, function, interval, why = entry["path"], entry["function"], entry["interval_s"], entry["why"]
        if not (isinstance(path, str) and path and isinstance(function, str) and function):
            raise ConfigError(f"[[request_budget.loops]] entry {entry!r}: path and function must be non-empty strings")
        if not isinstance(interval, (int, float)) or isinstance(interval, bool) or interval <= 0:
            raise ConfigError(f"[[request_budget.loops]] entry {entry!r}: interval_s must be a positive number")
        if not isinstance(why, str) or not why.strip():
            raise ConfigError(f"[[request_budget.loops]] entry {entry!r}: why must say why it cannot wait for a viewer")
        loops.setdefault(path.replace("\\", "/"), {})[function] = float(interval)
    return loops


def parse_config(toml_text: str) -> Config | None:
    """`[request_budget_ratchet]` -> `Config`, or None when the stage is not enabled.

    An absent table, or `enabled = false`, is the one correct skip: the adopter
    declared it. Anything malformed raises `ConfigError` -- a typo'd key or rule
    must not quietly run with less than the adopter asked for.
    """
    data = tomllib.loads(toml_text)
    table = data.get("request_budget_ratchet")
    if table is None:
        return None
    section = "request_budget_ratchet"
    if not isinstance(table, dict):
        raise ConfigError(f"[{section}] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[{section}]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError(f"[{section}] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError(f"[{section}] baseline must be a non-empty string")
    rules = _str_list(table, section, "rules", RULES)
    bad = sorted(set(rules) - set(RULES))
    if not rules or bad:
        raise ConfigError(f"[{section}] rules must name rules from: {', '.join(RULES)}"
                          + (f" (unknown: {', '.join(bad)})" if bad else ""))
    return Config(
        baseline, tuple(r for r in RULES if r in rules),
        _str_list(table, section, "browser_paths", DEFAULT_BROWSER_PATHS),
        _str_list(table, section, "server_paths", DEFAULT_SERVER_PATHS),
        tuple(d.strip("/") for d in _str_list(table, section, "handler_dirs", ())),
        _str_list(table, section, "stat_cache", ()),
        _names(table, "poll_wrappers"), _names(table, "fetch_wrappers"),
        _str_list(table, section, "exclude", ()), parse_loops(data),
    )


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get("vendored")
    found = []
    for entry in (table.values() if isinstance(table, dict) else ()):
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


def is_test_path(path: str) -> bool:
    return any(p in ("tests", "test") for p in PurePosixPath(path).parts[:-1])


def file_kind(path: str, cfg: Config, vendored: tuple[str, ...]) -> str | None:
    """`browser`, `server` or None: which rules read `path`. A vendored copy's debt belongs to the repo it came from."""
    if is_test_path(path) or any(_under(path, d) for d in vendored) or matches_any(path, BUILTIN_EXCLUDES + cfg.exclude):
        return None
    lower = path.lower()
    if lower.endswith(JS_SUFFIXES + HTML_SUFFIXES) and matches_any(path, cfg.browser_paths):
        return "browser"
    if lower.endswith(".py") and matches_any(path, cfg.server_paths):
        return "server"
    return None


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


def _read_text(root: Path, path: str) -> str | None:
    """A file's text, None when it is tracked but deleted in the working tree."""
    try:
        return (root / path).read_bytes().decode("utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise NotConfirmed(f"{path}: is not UTF-8 (byte {exc.start})") from exc


# -------------------------------------------------------------------- scan

@dataclass
class Tree:
    """One scan of the repo: hits per file (only files with one), the exemptions, and what was declared but not met."""

    found: dict[str, list[Hit]] = field(default_factory=dict)
    exempted: list[tuple[str, int, str, str]] = field(default_factory=list)  # (path, line, rule, reason)
    stale_loops: list[str] = field(default_factory=list)


def _scan_file(text: str, path: str, kind: str, cfg: Config) -> tuple[list[tuple[int, str, str]], Any]:
    """(findings, Python facts or None) for one in-scope file, before exemptions."""
    if kind == "browser":
        reader = scan.scan_html if path.lower().endswith(HTML_SUFFIXES) else scan.scan_js
        return list(reader(text, path, cfg.poll_wrappers, cfg.fetch_wrappers)), None
    handler = any(_under(path, d) for d in cfg.handler_dirs)
    facts = scan.scan_python(text, path, declared=cfg.loops.get(path, {}), handler=handler, stat_cache=cfg.stat_cache)
    return list(facts.findings), facts


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...]) -> Tree:
    """Every in-scope browser and server file, with exemptions applied and the repo-level middleware rule."""
    tree = Tree()
    seen_loops: set[tuple[str, str]] = set()
    app: tuple[str, int] | None = None
    uses_middleware = False
    for path in tracked_files(root):
        kind = file_kind(path, cfg, vendored)
        # The middleware import may live in any tracked Python file outside tests and vendored copies.
        if kind is None and not (path.endswith(".py") and not is_test_path(path)
                                 and not any(_under(path, d) for d in vendored)):
            continue
        text = _read_text(root, path)
        if text is None:
            continue
        if kind is None:
            uses_middleware |= scan.MIDDLEWARE in text and scan.scan_python(
                text, path, declared={}, handler=False, stat_cache=()).uses_middleware
            continue
        findings, facts = _scan_file(text, path, kind, cfg)
        if facts is not None:
            uses_middleware |= facts.uses_middleware
            seen_loops |= {(path, s) for s in facts.loops_seen}
            if app is None and facts.app_line is not None:
                app = (path, facts.app_line)
        _record(tree, path, text, findings, cfg)
    if app is not None and not uses_middleware and WIRED in cfg.rules:
        text = _read_text(root, app[0]) or ""
        _record(tree, app[0], text, [(app[1], WIRED, "this web app never imports the vendored "
                                       "RequestTimingMiddleware (src/request_timing.py)")], cfg, markers=False)
    tree.stale_loops = sorted(f"{p}: {s}" for p, scopes in cfg.loops.items() for s in scopes if (p, s) not in seen_loops)
    return tree


def _record(tree: Tree, path: str, text: str, findings: list[tuple[int, str, str]], cfg: Config,
            markers: bool = True) -> None:
    """Apply the file's exemption markers and keep the enabled rules' hits."""
    kept, used = scan.apply_exemptions(text, findings)
    if not markers:  # the file was already marker-scanned; only its new findings count
        kept = [k for k in kept if k[1] != "bare-exempt"]
    tree.exempted += [(path, e.line, e.rule, e.reason) for e in used]
    hits = [Hit(path, line, rule, message) for line, rule, message in kept if rule in cfg.rules]
    if hits:
        tree.found.setdefault(path, []).extend(hits)


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
            raise ConfigError(f"baseline {path}: entry {item!r} needs `counts` of rules to positive integers")
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
    print(f"request budget ratchet: {_total(added)} hit(s) beyond the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for rule in RULES:
            if not extra[rule]:
                continue
            had = baseline.get(path, Counter())[rule]
            print(f"  {path}: [{rule}] {had + extra[rule]} hit(s), baseline {had}:")
            for h in found[path]:
                if h.rule == rule:
                    print(f"    {path}:{h.line}: [{rule}] {h.message}")


def _report_context(cfg: Config, tree: Tree) -> None:
    """What every run prints: the exemptions in force, switched-off checks, and declarations nothing matched."""
    print(f"  {len(tree.exempted)} exemption(s)" + (":" if tree.exempted else ""))
    for path, line, rule, reason in sorted(tree.exempted):
        print(f"    {path}:{line}: [{rule}] {reason}")
    if "scan-per-request" in cfg.rules and not cfg.handler_dirs:
        print("  scan-per-request: OFF (no handler_dirs declared)")
    for entry in tree.stale_loops:
        print(f"  [[request_budget.loops]] {entry} matches no loop any more -- remove the declaration")


def _check(cfg: Config, tree: Tree, current: Counts, baseline: Counts) -> int:
    added, gone = diff(current, baseline)
    if added:
        _report_added(added, tree.found, baseline)
        print("  Poll through the vendored startPoll, declare a background loop, read through the stat cache, use a "
              "named pool (docs/polling-budget.md). A hit that must stay carries `request-budget-exempt: <reason>`; "
              "or (reviewed) raise the baseline with `--update-baseline --allow-additions`.")
        _report_context(cfg, tree)
        return EXIT_RISE
    print(f"request budget ratchet: PASS ({_total(current)} baselined hit(s) in {len(current)} file(s), none new; "
          f"rules: {', '.join(cfg.rules)})")
    _report_context(cfg, tree)
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/request_budget_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def _load_baseline(baseline_path: Path, cfg: Config, init: bool) -> Counts | int:
    """The committed baseline (empty for `--init`), or the exit code that refuses the run."""
    if init:
        if baseline_path.exists():
            print(f"request budget ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return {}
    if not baseline_path.is_file():
        print(f"request budget ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/request_budget_ratchet.py --init`")
        return EXIT_RISE
    try:
        return read_baseline(baseline_path)
    except ConfigError as exc:
        print(f"request budget ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"request budget ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("request budget ratchet: not enabled -- set [request_budget_ratchet] enabled = true in .fleet.toml first")
            return EXIT_CONFIG
        print("request budget ratchet: not enabled (no [request_budget_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline
    baseline = _load_baseline(baseline_path, cfg, init)
    if isinstance(baseline, int):
        return baseline
    try:
        tree = scan_tree(root, cfg, vendored)
    except NotConfirmed as exc:
        print(f"request budget ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_counts(tree.found)

    if init:
        # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"request budget ratchet: wrote {cfg.baseline} with {_total(current)} hit(s) in {len(current)} file(s)")
        return EXIT_PASS
    if not update:
        return _check(cfg, tree, current, baseline)
    added, gone = diff(current, baseline)
    if added and not allow_additions:
        _report_added(added, tree.found, baseline)
        print("request budget ratchet: nothing written -- fix the code, or raise the baseline with --allow-additions")
        return EXIT_RISE
    # runtime-write-exempt: the repo's own baseline file, named by the config, never a code folder
    baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
    print(f"request budget ratchet: {cfg.baseline} now has {_total(current)} hit(s) in {len(current)} file(s) "
          f"({gone} dropped, {_total(added)} added)")
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
