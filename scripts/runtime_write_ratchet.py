"""Ratchet on runtime files written inside tracked code folders.

Runtime output (logs, run records, caches, databases) belongs under `data/` or
the machine's runtime root (`src/runtime_data.py`), never next to the code that
writes it (docs/repo-layout.md, `data/`). This stage finds every write target in
the repo's Python source and resolves its path statically:

  * resolved under a code folder (`src/`, `app/`, `scripts/`, `hooks/`,
    `skills/` by default) -> kind `code-folder`, a failure when the count rises;
  * resolved anywhere else (repo root, `data/`, an absolute path, a temp dir, the
    runtime root) -> clean;
  * not resolvable from the source (a parameter, an attribute, an env var) ->
    kind `unresolved`. That is "unknown", never clean: a new one exits 3 (NOT
    CONFIRMED) until the path is made resolvable, the call carries
    `# runtime-write-exempt: <reason>`, or the baseline is raised in review.

Write targets: `open()`/`io.open()`/`codecs.open()` and `<path>.open()` with a
writing mode, `<path>.write_text/write_bytes/touch/mkdir()`, `os.mkdir/makedirs`,
`shutil.copy*/move` (the destination), `logging.*FileHandler`,
`logging.basicConfig(filename=)` and `sqlite3.connect()`. A relative path is read
from the repo root, where the fleet runs every entry point. Test modules are not
scanned: they write under pytest's `tmp_path`, and the probe below covers a run.

The runtime probe: `--snapshot FILE` before the test run records the untracked
and ignored files under the code folders; `--probe FILE` after it fails on any
file the run added there (bytecode and tool caches aside).

Opt-in, per adopter, in its own `.fleet.toml`:

    [runtime_write_ratchet]
    enabled      = true
    baseline     = ".runtime-write-ratchet-baseline.json"  # default
    code_folders = ["src", "app", "scripts", "hooks", "skills"]  # default
    exclude      = ["src/generated/**"]                     # globs; never scanned
    safe_calls   = ["my_runtime_dir"]                       # calls returning a path outside the tree

Exit codes: 0 pass or not enabled; 1 a `code-folder` count rose, the probe found
a new file, or the baseline is missing / a mode was refused; 2 bad configuration,
baseline or arguments; 3 NOT CONFIRMED (a new `unresolved` target, `git`
unusable, a tracked `.py` file that does not parse, a snapshot that cannot be
read) -- the gate fails on it. Modes: `check`, `--init`, `--update-baseline`
(shrink only), `--update-baseline --allow-additions` (shows in review),
`--snapshot FILE`, `--probe FILE`.

Vendor-verbatim (manifest key `runtime_write_ratchet`, plus its path resolver, sibling
`runtime_write_resolve.py`), stdlib-only. Procedure and limits: docs/runtime-write-ratchet.md.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import ModuleType


def _load_sibling(name: str) -> ModuleType:
    """A sibling module, loaded by path so `python -P` (scripts/ off `sys.path`) finds it."""
    try:
        if __package__:  # imported as a package member (the tests)
            return importlib.import_module(f"{__package__}.{name}")
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(f"{name}.py"))
        assert spec is not None and spec.loader is not None
        module = sys.modules[spec.name] = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    except (ImportError, OSError, SyntaxError):
        print(f"runtime write ratchet: NOT CONFIRMED: {name}.py is missing beside this script -- vendor both "
              "(manifest keys runtime_write_ratchet and runtime_write_resolve)")
        sys.exit(3)


resolve = _load_sibling("runtime_write_resolve")

REPO_ROOT = Path(__file__).resolve().parent.parent

BASELINE_VERSION = 1
SNAPSHOT_VERSION = 1
DEFAULT_BASELINE = ".runtime-write-ratchet-baseline.json"
DEFAULT_CODE_FOLDERS = ("src", "app", "scripts", "hooks", "skills")
#: Always excluded, whatever the adopter's own `exclude` adds.
BUILTIN_EXCLUDES = ("**/node_modules/**", "**/.venv/**", "**/venv/**")
GIT_TIMEOUT_S = 120

KINDS = ("code-folder", "unresolved")
EXEMPT_MARKER = "runtime-write-exempt"

#: Untracked paths the interpreter and its tools write beside any code; never a runtime file of the app's.
_PROBE_NOISE = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"})

EXIT_PASS, EXIT_RISE, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

# Vendor-verbatim module: derives the flag locally (the shared `src/no_window.py`
# would not resolve in a consumer tree), the documented exception.
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

_CONFIG_KEYS = {"enabled", "baseline", "code_folders", "exclude", "safe_calls"}


class ConfigError(RuntimeError):
    """The `[runtime_write_ratchet]` table, the baseline or the snapshot is unusable."""


class NotConfirmed(RuntimeError):
    """A tracked Python file could not be read or parsed, or git could not list the tree."""


@dataclass(frozen=True)
class Config:
    baseline: str = DEFAULT_BASELINE
    code_folders: tuple[str, ...] = DEFAULT_CODE_FOLDERS
    exclude: tuple[str, ...] = ()
    safe_calls: tuple[str, ...] = ()


@dataclass(frozen=True)
class Hit:
    path: str
    line: int
    kind: str
    message: str


# ----------------------------------------------------------------- config

def _str_list(table: dict[str, object], name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    if name not in table:
        return default
    value = table[name]
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ConfigError(f"[runtime_write_ratchet] {name} must be a list of non-empty strings")
    return tuple(v.replace("\\", "/").strip("/") if name == "code_folders" else v.replace("\\", "/")
                 for v in value)


def parse_config(toml_text: str) -> Config | None:
    """`[runtime_write_ratchet]` -> `Config`, or None when the stage is not enabled."""
    table = tomllib.loads(toml_text).get("runtime_write_ratchet")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise ConfigError("[runtime_write_ratchet] must be a table")
    unknown = sorted(set(table) - _CONFIG_KEYS)
    if unknown:
        raise ConfigError(f"[runtime_write_ratchet]: unknown key(s) {', '.join(unknown)}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ConfigError("[runtime_write_ratchet] enabled must be true or false")
    if not enabled:
        return None
    baseline = table.get("baseline", DEFAULT_BASELINE)
    if not isinstance(baseline, str) or not baseline:
        raise ConfigError("[runtime_write_ratchet] baseline must be a non-empty string")
    folders = _str_list(table, "code_folders", DEFAULT_CODE_FOLDERS)
    if not folders or any("/" in f or f in (".", "..") for f in folders):
        raise ConfigError("[runtime_write_ratchet] code_folders must name top-level folders, at least one")
    return Config(baseline, folders, _str_list(table, "exclude", ()), _str_list(table, "safe_calls", ()))


def vendored_dests(toml_text: str) -> tuple[str, ...]:
    table = tomllib.loads(toml_text).get("vendored")
    if not isinstance(table, dict):
        return ()
    found = []
    for entry in table.values():
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


def is_test_module(path: str) -> bool:
    parts = PurePosixPath(path).parts
    name = parts[-1]
    if any(p in ("tests", "test") for p in parts[:-1]):
        return True
    return name == "conftest.py" or name.startswith("test_") or name.endswith("_test.py")


def in_scope(path: str, cfg: Config, vendored: tuple[str, ...]) -> bool:
    """Is `path` a Python file this stage scans? A vendored copy's debt belongs to the repo it came from."""
    if not path.endswith(".py") or is_test_module(path):
        return False
    if any(_under(path, d) for d in vendored):
        return False
    return not matches_any(path, BUILTIN_EXCLUDES + cfg.exclude)


# ---------------------------------------------------------------- scanning

def _window(node: ast.AST, lines: list[str]) -> list[str]:
    """The node's own lines plus the contiguous comment block right above it."""
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", start) or start
    above = start - 1
    while above > 0 and lines[above - 1].lstrip().startswith("#"):
        above -= 1
    return lines[above:end]


def _exempt(window: list[str]) -> bool:
    """A `# runtime-write-exempt: <reason>` marker in the window; a bare marker exempts nothing."""
    return any(line.split(EXEMPT_MARKER, 1)[1].strip(" :#\t") for line in window if EXEMPT_MARKER in line)


def scan_source(source: bytes | str, path: str, cfg: Config) -> list[Hit]:
    """Hits in one module; raises `NotConfirmed` when it does not parse."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # an invalid escape in the scanned code is not this stage's finding
            tree = ast.parse(source, filename=path)
    except (SyntaxError, ValueError) as exc:
        where = f" (line {exc.lineno})" if isinstance(exc, SyntaxError) and exc.lineno else ""
        raise NotConfirmed(f"{path}: does not parse{where}: {exc.msg if isinstance(exc, SyntaxError) else exc}") from exc
    text = source.decode("utf-8", "replace") if isinstance(source, bytes) else source
    lines = text.splitlines()
    hits = [Hit(path, getattr(node, "lineno", 0), kind, message)
            for node, kind, message in resolve.find_writes(tree, path, cfg.code_folders, cfg.safe_calls)
            if not _exempt(_window(node, lines))]
    return sorted(hits, key=lambda h: (h.line, h.kind))



# --------------------------------------------------------------------- git

def _git(root: Path, args: list[str]) -> bytes:
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0")  # a read-only sweep must not leave index locks
    try:
        proc = subprocess.run(
            ["git", *args], cwd=root, capture_output=True, env=env, stdin=subprocess.DEVNULL,
            creationflags=_NO_WINDOW, timeout=GIT_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise NotConfirmed(f"git {args[0]} timed out after {GIT_TIMEOUT_S}s") from exc
    except OSError as exc:
        raise NotConfirmed(f"git could not start: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise NotConfirmed(f"git {' '.join(args[:2])} exited {proc.returncode}: "
                           f"{detail[-1] if detail else '(no output)'}")
    return proc.stdout


def tracked_files(root: Path) -> list[str]:
    """Repo-relative, forward-slash paths of every tracked or untracked-but-not-ignored file."""
    out = _git(root, ["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
    return sorted(p for p in out.decode("utf-8", "replace").split("\0") if p)


def scan_tree(root: Path, cfg: Config, vendored: tuple[str, ...]) -> dict[str, list[Hit]]:
    """Hits per in-scope `.py` file (only files with at least one)."""
    found: dict[str, list[Hit]] = {}
    for path in tracked_files(root):
        if not in_scope(path, cfg, vendored):
            continue
        try:
            source = (root / path).read_bytes()
        except FileNotFoundError:
            continue  # tracked but deleted in the working tree: nothing to read
        except OSError as exc:
            raise NotConfirmed(f"{path}: cannot be read: {exc}") from exc
        hits = scan_source(source, path, cfg)
        if hits:
            found[path] = hits
    return found


def runtime_files(root: Path, cfg: Config) -> list[str]:
    """Untracked and ignored files under the code folders, tool caches and bytecode aside."""
    out = _git(root, ["status", "--porcelain=v1", "-z", "--ignored", "--untracked-files=all", "--",
                      *cfg.code_folders])
    found = []
    for entry in out.decode("utf-8", "replace").split("\0"):
        if entry[:3] not in ("?? ", "!! "):
            continue
        path = entry[3:].rstrip("/")
        parts = PurePosixPath(path).parts
        if any(p in _PROBE_NOISE for p in parts) or path.endswith((".pyc", ".pyo")):
            continue
        if not matches_any(path, cfg.exclude):
            found.append(path)
    return sorted(found)


# ---------------------------------------------------------------- baseline

Counts = dict[str, Counter[str]]  # path -> Counter(kind)


def current_counts(found: dict[str, list[Hit]]) -> Counts:
    return {path: Counter(h.kind for h in hits) for path, hits in found.items()}


def read_baseline(path: Path) -> Counts:
    """Baseline file -> ``{source path: Counter(kind)}``. Raises `ConfigError` on anything off."""
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
                or not all(k in KINDS and isinstance(n, int) and not isinstance(n, bool) and n > 0
                           for k, n in counts.items())):
            raise ConfigError(f"baseline {path}: entry {item!r} needs `counts` of known kinds to positive integers")
        out[item["key"]] = Counter(counts)
    return out


def render_baseline(counts: Counts) -> str:
    """Sorted, one file per line, no line numbers, so a baseline diff reviews cleanly."""
    lines = []
    for key in sorted(counts):
        record = {"key": key, "counts": {k: counts[key][k] for k in KINDS if counts[key][k]}}
        lines.append("    " + json.dumps(record, ensure_ascii=False))
    body = "[\n" + ",\n".join(lines) + "\n  ]" if lines else "[]"
    return f'{{\n  "version": {BASELINE_VERSION},\n  "entries": {body}\n}}\n'


def diff(current: Counts, baseline: Counts) -> tuple[Counts, int]:
    """(per file, the kind counts beyond the baseline; how many baselined hits are gone)."""
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

def _total(counts: Counts, kind: str | None = None) -> int:
    return sum(c[kind] if kind else sum(c.values()) for c in counts.values())


def _report_added(added: Counts, found: dict[str, list[Hit]], baseline: Counts) -> None:
    print(f"runtime write ratchet: {_total(added)} write target(s) beyond the baseline, in {len(added)} file(s):")
    for path, extra in added.items():
        for kind in KINDS:
            if not extra[kind]:
                continue
            had = baseline.get(path, Counter())[kind]
            print(f"  {path}: [{kind}] {had + extra[kind]} hit(s), baseline {had}:")
            for h in found[path]:
                if h.kind == kind:
                    print(f"    {path}:{h.line}: [{kind}] {h.message}")


def _load(root: Path, *, init: bool, update: bool) -> tuple[Config, Counts, tuple[str, ...]] | int:
    """Config, baseline and vendored dests, or the exit code that ends the run here."""
    try:
        cfg, vendored = load_config(root)
    except ConfigError as exc:
        print(f"runtime write ratchet: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        if init or update:
            print("runtime write ratchet: not enabled -- set [runtime_write_ratchet] enabled = true "
                  "in .fleet.toml first")
            return EXIT_CONFIG
        print("runtime write ratchet: not enabled (no [runtime_write_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    baseline_path = root / cfg.baseline
    if init:
        if baseline_path.exists():
            print(f"runtime write ratchet: {cfg.baseline} already exists; --init never overwrites it")
            return EXIT_CONFIG
        return cfg, {}, vendored
    if not baseline_path.is_file():
        print(f"runtime write ratchet: FAIL: baseline {cfg.baseline} is missing -- "
              "run `python scripts/runtime_write_ratchet.py --init`")
        return EXIT_RISE
    try:
        return cfg, read_baseline(baseline_path), vendored
    except ConfigError as exc:
        print(f"runtime write ratchet: BASELINE ERROR: {exc}")
        return EXIT_CONFIG


def run(root: Path, *, init: bool, update: bool, allow_additions: bool) -> int:
    loaded = _load(root, init=init, update=update)
    if isinstance(loaded, int):
        return loaded
    cfg, baseline, vendored = loaded
    baseline_path = root / cfg.baseline
    try:
        found = scan_tree(root, cfg, vendored)
    except NotConfirmed as exc:
        print(f"runtime write ratchet: NOT CONFIRMED: {exc}")
        print("  (the tree could not be read through, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    current = current_counts(found)

    if init:
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"runtime write ratchet: wrote {cfg.baseline} with {_total(current)} hit(s) in {len(current)} file(s)")
        return EXIT_PASS

    added, gone = diff(current, baseline)
    if update:
        if added and not allow_additions:
            _report_added(added, found, baseline)
            print("runtime write ratchet: nothing written -- fix the code, or raise the baseline with "
                  "--allow-additions")
            return EXIT_RISE
        baseline_path.write_text(render_baseline(current), encoding="utf-8", newline="\n")
        print(f"runtime write ratchet: {cfg.baseline} now has {_total(current)} hit(s) in {len(current)} file(s) "
              f"({gone} dropped, {_total(added)} added)")
        return EXIT_PASS

    if added:
        _report_added(added, found, baseline)
        print("  Write runtime output under data/ or the runtime root (src/runtime_data.py), make the path "
              "resolvable, or mark a deliberate write `# runtime-write-exempt: <reason>`; or (reviewed) raise "
              "the baseline with `--update-baseline --allow-additions`.")
        if _total(added, "code-folder"):
            return EXIT_RISE
        print("runtime write ratchet: NOT CONFIRMED: the new target(s) above are unresolved -- unknown, not clean")
        return EXIT_NOT_CONFIRMED
    unresolved = _total(current, "unresolved")
    print(f"runtime write ratchet: PASS ({_total(current, 'code-folder')} baselined write target(s) in code "
          f"folders, none new; code folders: {', '.join(cfg.code_folders)})")
    if unresolved:
        print(f"  {unresolved} baselined write target(s) stay unresolved: unknown, not clean")
    if gone:
        print(f"  {gone} baselined hit(s) are gone -- run "
              "`python scripts/runtime_write_ratchet.py --update-baseline` to lower it")
    return EXIT_PASS


def snapshot(root: Path, out: Path) -> int:
    """Record the untracked/ignored files under the code folders before a test run."""
    try:
        cfg, _ = load_config(root)
    except ConfigError as exc:
        print(f"runtime write probe: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        print("runtime write probe: not enabled (no [runtime_write_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    try:
        files = runtime_files(root, cfg)
    except NotConfirmed as exc:
        print(f"runtime write probe: NOT CONFIRMED: {exc}")
        return EXIT_NOT_CONFIRMED
    out.write_text(json.dumps({"version": SNAPSHOT_VERSION, "paths": files}), encoding="utf-8")
    print(f"runtime write probe: snapshot of {len(files)} untracked/ignored file(s) under "
          f"{', '.join(cfg.code_folders)}")
    return EXIT_PASS


def _read_snapshot(path: Path) -> set[str]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"snapshot {path} cannot be read: {exc}") from exc
    paths = data.get("paths") if isinstance(data, dict) and data.get("version") == SNAPSHOT_VERSION else None
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        raise ConfigError(f"snapshot {path}: expected an object with version {SNAPSHOT_VERSION} and `paths`")
    return set(paths)


def probe(root: Path, before_file: Path) -> int:
    """Fail on any file the run added under the code folders since `--snapshot`."""
    try:
        cfg, _ = load_config(root)
    except ConfigError as exc:
        print(f"runtime write probe: CONFIG ERROR: {exc}")
        return EXIT_CONFIG
    if cfg is None:
        print("runtime write probe: not enabled (no [runtime_write_ratchet] enabled = true in .fleet.toml)")
        return EXIT_PASS
    try:
        before = _read_snapshot(before_file)
        after = runtime_files(root, cfg)
    except (ConfigError, NotConfirmed) as exc:
        print(f"runtime write probe: NOT CONFIRMED: {exc}")
        print("  (no before/after comparison was possible, so this is neither a pass nor a clean tree)")
        return EXIT_NOT_CONFIRMED
    new = [p for p in after if p not in before]
    if new:
        print(f"runtime write probe: FAIL: the run wrote {len(new)} file(s) under the code folders:")
        for path in new:
            print(f"  {path}")
        print("  Runtime output belongs under data/ or the runtime root (src/runtime_data.py).")
        return EXIT_RISE
    before_file.unlink(missing_ok=True)
    print(f"runtime write probe: PASS (no new untracked or ignored file under {', '.join(cfg.code_folders)})")
    return EXIT_PASS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="repo root (default: this repo)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--init", action="store_true", help="write the first baseline; never overwrites")
    mode.add_argument("--update-baseline", action="store_true", help="drop hits now gone (shrink only)")
    mode.add_argument("--snapshot", type=Path, metavar="FILE", help="record runtime files before a test run")
    mode.add_argument("--probe", type=Path, metavar="FILE", help="fail on runtime files the run added")
    parser.add_argument("--allow-additions", action="store_true",
                        help="with --update-baseline: also add hits (shows in review)")
    args = parser.parse_args(argv)
    if args.allow_additions and not args.update_baseline:
        parser.error("--allow-additions only applies with --update-baseline")
    root = args.root.resolve()
    if args.snapshot:
        return snapshot(root, args.snapshot)
    if args.probe:
        return probe(root, args.probe)
    return run(root, init=args.init, update=args.update_baseline, allow_additions=args.allow_additions)


if __name__ == "__main__":
    # Reports contain non-ASCII (e.g. "→"); a cp1252 console would otherwise crash after the verdict.
    for _stream in (sys.stdout, sys.stderr):
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]  # real console streams are TextIOWrapper
    sys.exit(main())
