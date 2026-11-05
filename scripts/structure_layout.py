"""Repo-shape contract of the structure ratchet: the shape presets and every check that reads them.

`scripts/structure_ratchet.py` loads this module for everything that depends on a repo's
declared `shape`. The checks all read the repo's tracked paths:

- **Root layout** (`layout_findings`): a required root folder with no tracked file, or a
  tracked root folder neither the shape nor `extra_roots` lists. Never baselined.
- **Extra roots** (`extra_root_names`): a declared `extra_roots` folder the shape's preset does
  not already list is a deviation. The ratchet baselines it, so the list only shrinks.
- **Root files** (`loose_root_files`): a tracked root file neither the common set nor the shape
  lists. Baselined by name, declared or not; `extra_root_files` is where a kept one's reason lives.
- **Folder types** (`mistyped`): a file whose extension its folder's type does not take, counted
  per folder.
- **Second-level placement** (`misplaced`): where a shape declares `homes`, a file under one of
  its roots that sits outside every home. The ratchet baselines the count per folder.
- **Redundant exceptions** (`redundant_names`): an `extra_roots` or `extra_root_files` entry the
  preset already allows.

The baseline fields these are recorded in (`RECORD_GROUPS`) are read and written here too.
Vendor-verbatim (manifest key `structure_layout`), stdlib-only, no import of the ratchet.
The presets are mirrored in docs/structure-ratchet.md and docs/repo-layout.md; tests compare them.
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import PurePosixPath

LAYOUT_DOC = "project-scaffolding docs/repo-layout.md"
#: Root folders every shape allows, besides hidden ones (`.github`, `.claude`, ...).
ALWAYS_ALLOWED = ("docs",)
DEFAULT_LAYERS = (("src", "app"),)
#: The file that makes a folder a surface package: its own app and listener.
SURFACE_ENTRY = "server.py"
#: The folder name of a component channel (`app/webapp/static/_vendored/`): the components, their readme and gallery.
CHANNEL = "_vendored"

#: Root files every shape allows, as names or globs: instructions, licence, tool config, the ratchet baselines.
COMMON_ROOT_FILES = (
    "README.md", "CLAUDE.md", "AGENTS.md", "LICENSE", ".fleet.toml", ".gitignore", ".gitattributes", ".ignore",
    ".env.example", ".*-baseline.json", "requirements*.txt", "pyproject.toml", "pytest.ini", "package*.json",
)

_IMAGES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"})
_CODE = frozenset({".py", ".pyi", ".js", ".mjs", ".cjs", ".ts", ".sql"})
_SHELL = frozenset({".ps1", ".psm1", ".sh", ".bat", ".cmd"})
_CONFIG = frozenset({".json", ".yaml", ".yml", ".toml", ".ini", ".example", ".sample"})
#: A folder type -> the extensions it takes (compared lowercased); None takes anything.
TYPES: dict[str, frozenset[str] | None] = {
    "code": _CODE,
    "code+shell": _CODE | _SHELL,
    "shell": _SHELL,
    "config": _CONFIG,
    "doc": _IMAGES | {".md", ".mmd", ".html", ".json"},
    "web": _CODE | _IMAGES | {".html", ".css", ".json", ".webmanifest", ".woff", ".woff2", ".ttf", ".otf", ".txt", ".license"},
    "image": _IMAGES,
    "css": frozenset({".css"}),
    "js": frozenset({".js", ".mjs"}),
    "tooling": _CODE | _SHELL | _CONFIG | {".md", ".html", ".txt"},
    "any": None,
}
#: A file with this name is allowed in every typed folder: a package's own readme.
ANY_FOLDER_NAMES = ("README.md",)
#: Folder types every shape has; a shape's own entries add folders or override one. The longest folder wins.
#: `tests/e2e/shots` is the vendored `shot_baseline` default, where the screenshot baselines live.
COMMON_FOLDER_TYPES = (
    ("docs", "doc"), ("scripts", "code+shell"), ("src", "code"), ("tests", "code+shell"), ("tests/fixtures", "any"),
    ("tests/e2e/shots", "image"), ("tests/e2e/fixtures", "any"), ("config", "config"),
)


@dataclass(frozen=True)
class Shape:
    """One repo shape's contract: root folders, root files, folder types, layer pairs and homes.

    `root_files` adds names or globs to `COMMON_ROOT_FILES`; `folder_types` adds `(folder, type)` pairs to
    `COMMON_FOLDER_TYPES`. `homes` is the second-level contract: path patterns where files under `home_roots`
    may live. A pattern ending in `/` is a folder and everything below it (a segment may be a glob, so
    `skills/*/` is every subfolder of `skills/`); `dir/*.py` is a file directly in `dir`.
    `surface_parents` are folders whose direct subfolder holding a `SURFACE_ENTRY` file is a home too:
    a second, separately bound HTTP surface, whose isolation boundary is that package.
    """

    required: tuple[str, ...]
    allowed: tuple[str, ...]
    layers: tuple[tuple[str, str], ...] = ()
    homes: tuple[str, ...] = ()
    surface_parents: tuple[str, ...] = ()
    root_files: tuple[str, ...] = ()
    folder_types: tuple[tuple[str, str], ...] = ()

    @property
    def home_roots(self) -> frozenset[str]:
        return frozenset(home.split("/", 1)[0] for home in self.homes)

    @property
    def all_root_files(self) -> tuple[str, ...]:
        return COMMON_ROOT_FILES + self.root_files

    @property
    def all_folder_types(self) -> dict[str, str]:
        return dict(COMMON_FOLDER_TYPES + self.folder_types)


#: The `.fleet.toml` `shape` ids, mirroring the trees in docs/repo-layout.md. A required folder
#: cannot be waived (`extra_roots` only adds), so a set is required only where every repo of the
#: shape has it. Root files, folder types and homes enter a preset only where more than one adopter
#: of the shape needs them; one repo's need stays a recorded exception. The docs carry the same data.
SHAPES = {
    "webapp-tray": Shape(
        ("app", "src", "tests", "docs"), ("scripts", "data", "brand", "assets", "config", "webapp"), DEFAULT_LAYERS,
        ("app/*.py", "app/cli/", "app/tray/", "app/webapp/*.py", "app/webapp/routers/", "app/webapp/static/", "src/"),
        ("app/webapp",),
        ("tray.bat", "webapp.bat", "launcher.py", "setup.bat", "webapp_tunnel_named.bat", "start.bat", "launch_app.bat",
         "config.example.json"),
        (("app", "code"), ("app/webapp", "web")),
    ),
    "streamlit": Shape(
        ("app", "src", "tests"), ("scripts", "data", "brand", "config"), DEFAULT_LAYERS,
        ("app/*.py", "app/views/", "app/styles/", "app/.streamlit/"), (),
        ("launch_app.bat", "launch_app.sh", "launch_server.bat", "launch_server.sh", "config.json.example",
         "tray.bat.template"),
        (("app", "code"), ("app/styles", "css")),
    ),
    "pipeline-cli": Shape(
        ("tests",), ("src", "common", "scripts", "config", "data"),
        root_files=("main.py", "launch_*.bat"), folder_types=(("common", "code"),),
    ),
    "skills-governance": Shape(
        ("skills", "tests"), ("hooks", "scripts", "architecture"), homes=("skills/*/",),
        root_files=("install.ps1",), folder_types=(("skills", "tooling"), ("hooks", "tooling")),
    ),
    "node": Shape(("src",), ("test", "scripts"), folder_types=(("test", "code"),)),
    "static-site": Shape(
        (), ("css", "js", "assets", "games", "content", "site", "scripts"),
        root_files=("*.html",), folder_types=(("css", "css"), ("js", "js")),
    ),
    "agent-workspace": Shape((), ("scripts",)),
    # No layer pair: the service in src/ mounts its admin app, so src imports app_web by design.
    "service": Shape(
        ("src", "tests", "app_web", "tray"), ("scripts", "config", "launchers", "assets"),
        homes=("app_web/*.py", "app_web/routers/", "app_web/static/", "tray/*.py"),
        root_files=("tray.bat", "run_*.bat", "run_*.sh"),
        folder_types=(("app_web", "web"), ("tray", "code+shell"), ("launchers", "shell")),
    ),
}

#: Baseline fields that record layout findings -> the finding kinds each holds. A baseline written
#: before a field existed lacks it; the ratchet then takes today's findings of those kinds as the
#: record, so no adopter fails on the day it re-vendors.
RECORD_GROUPS = {
    "root_extras": ("root", "place"),
    "shape_contract": ("rootfile", "redundant", "type", "place"),
}
GROUP_TITLES = {
    "root_extras": "extra roots and placement",
    "shape_contract": "root files, folder types, redundant exceptions and the homes of every shape",
}
#: Kinds stored in those fields rather than in the baseline's `entries`.
RECORDED_KINDS = ("root", "rootfile", "redundant", "type")


# ---------------------------------------------------------------- config

def _names(value: object, error: str) -> tuple[str, ...]:
    """A list of non-empty single names (no `/` inside), normalised and sorted; ValueError otherwise."""
    if not isinstance(value, list):
        raise ValueError(error)
    names = set()
    for item in value:
        name = item.replace("\\", "/").strip("/") if isinstance(item, str) else ""
        if not name or "/" in name:
            raise ValueError(error)
        names.add(name)
    return tuple(sorted(names))


def declared(data: dict[str, object]) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """The top-level `shape`, `extra_roots` and `extra_root_files` of a parsed `.fleet.toml`; ValueError when malformed."""
    shape = data.get("shape")
    if shape is not None and not isinstance(shape, str):
        raise ValueError("shape in .fleet.toml must be a string")
    roots = _names(data.get("extra_roots", []), "extra_roots in .fleet.toml must be a list of top-level folder names")
    files = _names(data.get("extra_root_files", []), "extra_root_files in .fleet.toml must be a list of root file names")
    return shape, roots, files


def published(data: dict[str, object]) -> tuple[str, ...]:
    """Every `src` in the repo's own `[components]` catalog (the scaffold's), sorted; malformed rows are skipped."""
    table = data.get("components")
    if not isinstance(table, dict):
        return ()
    found = {
        row["src"].replace("\\", "/").strip("/")
        for row in table.values()
        if isinstance(row, dict) and isinstance(row.get("src"), str) and row["src"].strip("/")
    }
    return tuple(sorted(found))


def publisher_roots(published_srcs: tuple[str, ...]) -> tuple[str, ...]:
    """The trees a publisher's placement and folder types skip: each source, or the `CHANNEL` folder holding it.

    A component lives at its adopters' path, where that tree holds the copy. A channel folder is that path's
    too: its readme and gallery sit beside the components they document and exercise.
    """
    roots = set()
    for src in published_srcs:
        parts = src.split("/")
        cut = parts.index(CHANNEL) + 1 if CHANNEL in parts[:-1] else len(parts)
        roots.add("/".join(parts[:cut]))
    return tuple(sorted(roots))


# ---------------------------------------------------------------- checks

def root_folders(paths: list[str]) -> set[str]:
    return {path.split("/", 1)[0] for path in paths if "/" in path}


def layout_findings(paths: list[str], shape_id: str, extra_roots: tuple[str, ...]) -> list[str]:
    """Root-layout problems for the declared shape, one message each.

    Not baselined: a declared exception is data in `extra_roots`, and an undeclared one fails.
    """
    shape = SHAPES[shape_id]
    roots = root_folders(paths)
    listed = set(shape.required) | set(shape.allowed) | set(ALWAYS_ALLOWED) | set(extra_roots)
    see = f"See {LAYOUT_DOC}#{shape_id}"
    problems = [
        f"shape {shape_id}: required folder {name}/ has no tracked file; add it, or correct `shape` in "
        f".fleet.toml. {see}"
        for name in shape.required
        if name not in roots
    ]
    problems += [
        f"shape {shape_id}: {name}/ is not a folder this shape lists; move its code into one of "
        f"{', '.join(f'{n}/' for n in sorted(listed))}, or declare {name!r} in `extra_roots` in .fleet.toml "
        f"with a line in docs/layout.md. {see}"
        for name in sorted(roots)
        if not name.startswith(".") and name not in listed
    ]
    return problems


def _preset_folders(shape_id: str) -> set[str]:
    shape = SHAPES[shape_id]
    return set(shape.required) | set(shape.allowed) | set(ALWAYS_ALLOWED)


def extra_root_names(paths: list[str], shape_id: str, extra_roots: tuple[str, ...]) -> list[str]:
    """Tracked `extra_roots` folders the shape's preset does not list: the deviations, sorted.

    A folder the preset already lists (promoted into it, or `docs`) is no deviation, whatever
    `.fleet.toml` still says; `redundant_names` reports the stale entry.
    """
    preset = _preset_folders(shape_id)
    return sorted(name for name in set(extra_roots) & root_folders(paths) if name not in preset)


def _allowed_root_file(name: str, shape_id: str) -> bool:
    return any(fnmatch.fnmatchcase(name, pattern) for pattern in SHAPES[shape_id].all_root_files)


def loose_root_files(paths: list[str], shape_id: str) -> list[str]:
    """Tracked files directly at the root that neither the common set nor the shape lists, sorted."""
    return sorted(path for path in paths if "/" not in path and not _allowed_root_file(path, shape_id))


def redundant_names(shape_id: str, extra_roots: tuple[str, ...], extra_root_files: tuple[str, ...]) -> list[str]:
    """Declared exceptions the preset already allows: folders as `name/`, files by name, sorted."""
    preset = _preset_folders(shape_id)
    folders = [f"{name}/" for name in extra_roots if name in preset]
    return sorted(folders + [name for name in extra_root_files if _allowed_root_file(name, shape_id)])


def folder_type(path: str, shape_id: str) -> str | None:
    """The type of the longest typed folder holding `path`, or None when no typed folder holds it."""
    best: tuple[int, str] | None = None
    for folder, type_name in SHAPES[shape_id].all_folder_types.items():
        if path.startswith(folder + "/") and (best is None or len(folder) > best[0]):
            best = (len(folder), type_name)
    return best[1] if best else None


def mistyped(paths: list[str], shape_id: str) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Files whose extension their folder's type does not take: folder -> (count, the extensions found).

    A path with a hidden segment (`.gitkeep`, `app/.streamlit/`) is tool config, never typed; nor is a readme.
    """
    found: dict[str, tuple[int, set[str]]] = {}
    for path in paths:
        pure = PurePosixPath(path)
        type_name = folder_type(path, shape_id)
        takes = TYPES[type_name] if type_name else None
        if takes is None or pure.name in ANY_FOLDER_NAMES or any(part.startswith(".") for part in pure.parts):
            continue
        suffix = pure.suffix.lower()
        if suffix not in takes:
            count, suffixes = found.get(str(pure.parent), (0, set()))
            found[str(pure.parent)] = (count + 1, suffixes | {suffix or "(none)"})
    return {folder: (count, tuple(sorted(suffixes))) for folder, (count, suffixes) in found.items()}


def _in_home(path: str, home: str) -> bool:
    if home.endswith("/"):
        parts, want = path.split("/"), home.rstrip("/").split("/")
        return len(parts) > len(want) and all(fnmatch.fnmatchcase(p, w) for p, w in zip(parts, want))
    folder, _, pattern = home.rpartition("/")
    name = path[len(folder) + 1:]
    return path.startswith(folder + "/") and "/" not in name and fnmatch.fnmatchcase(name, pattern)


def surface_packages(paths: list[str], shape_id: str) -> frozenset[str]:
    """Folders directly under a surface parent that track their own `SURFACE_ENTRY`."""
    parents = SHAPES[shape_id].surface_parents
    found = set()
    for path in paths:
        folder, _, name = path.rpartition("/")
        if name == SURFACE_ENTRY and str(PurePosixPath(folder).parent) in parents:
            found.add(folder)
    return frozenset(found)


def misplaced(paths: list[str], shape_id: str) -> dict[str, int]:
    """Files under a home root that sit in no home, counted per folder (empty when the shape has no homes)."""
    shape = SHAPES[shape_id]
    surfaces = tuple(package + "/" for package in surface_packages(paths, shape_id))
    counts: dict[str, int] = {}
    for path in paths:
        if path.split("/", 1)[0] not in shape.home_roots or path.startswith(surfaces):
            continue
        if not any(_in_home(path, home) for home in shape.homes):
            folder = str(PurePosixPath(path).parent)
            counts[folder] = counts.get(folder, 0) + 1
    return counts


def placement_advice(shape_id: str) -> str:
    """Where a misplaced file goes: the shape's homes, spelled out."""
    shape = SHAPES[shape_id]
    homes = ", ".join(home.replace("*.py", "<module>.py").replace("/*/", "/<name>/") for home in shape.homes)
    surfaces = "".join(f", {parent}/<package>/ holding its own {SURFACE_ENTRY}" for parent in shape.surface_parents)
    domain = ", or into a domain subpackage of src/" if "src/" in shape.homes else ""
    return f"move it into one of this shape's homes ({homes}{surfaces}){domain}"


def baselined(
    paths: list[str], held: list[str], typed: list[str], shape_id: str, extra_roots: tuple[str, ...],
    extra_root_files: tuple[str, ...],
) -> dict[str, tuple[int, str, str]]:
    """Every baselined layout finding: key -> (count, what was found, where it goes instead).

    The root checks read every tracked path. Placement reads `held`: `paths` less the sources the repo
    publishes, which belong at their adopters' path. Folder types read `typed`: `held` less the
    ratchet's excludes and vendored dests, so a vendored component's files are its publisher's call.
    """
    tree = f"the {shape_id} tree"
    found = {
        f"root:{name}": (1, f"{name}/ is an extra root, outside {tree}",
                         "move its code under src/ or app/ (or promote the folder into the shape's preset); a justified "
                         "one is declared in `extra_roots` with its reason in docs/layout.md, and is recorded with "
                         "--update-baseline --allow-additions")
        for name in extra_root_names(paths, shape_id, extra_roots)
    }
    for name in loose_root_files(paths, shape_id):
        how = "is declared in `extra_root_files`" if name in extra_root_files else "is not declared"
        found[f"rootfile:{name}"] = (
            1, f"{name} is a root file outside {tree} ({how})",
            "move it into a folder the shape lists (scripts/ for a tool, docs/ for a document, config/ for a sample); "
            "a justified one is declared in `extra_root_files` with its reason, and is recorded with "
            "--update-baseline --allow-additions",
        )
    for name in redundant_names(shape_id, extra_roots, extra_root_files):
        field = "extra_roots" if name.endswith("/") else "extra_root_files"
        found[f"redundant:{name}"] = (
            1, f"{name} is declared in `{field}`, but the {shape_id} preset already allows it",
            f"delete it from `{field}` in .fleet.toml (and its line in docs/layout.md)",
        )
    for folder, (count, suffixes) in mistyped(typed, shape_id).items():
        kind = folder_type(folder + "/x", shape_id)
        found[f"type:{folder}"] = (
            count, f"{folder}/ holds {count} file(s) of a type its folder ({kind}) does not take: {', '.join(suffixes)}",
            "move each to a folder whose type takes it (docs/ for a document, tests/fixtures/ for test data, "
            "config/ for a sample, or a data folder the shape lists)",
        )
    advice = placement_advice(shape_id)
    for folder, count in misplaced(held, shape_id).items():
        found[f"place:{folder}"] = (count, f"{folder}/ holds {count} file(s) outside the homes of {tree}", advice)
    return found


# ---------------------------------------------------------------- baseline records

def _kind_names(counts: dict[str, int], kind: str) -> list[str]:
    return sorted(key.split(":", 1)[1] for key in counts if key.split(":", 1)[0] == kind)


def read_records(data: dict[str, object]) -> tuple[dict[str, int], frozenset[str]]:
    """The layout fields of a parsed baseline -> (``{key: count}``, the `RECORD_GROUPS` fields present).

    Raises ValueError, naming the field, on anything malformed.
    """
    counts: dict[str, int] = {}
    if data.get("root_extras") is not None:
        names = _names(data["root_extras"], "`root_extras` must be a list of root folder names")
        counts.update((f"root:{name}", 1) for name in names)
    contract = data.get("shape_contract")
    if contract is not None:
        shape_error = "`shape_contract` must hold root_files and redundant (lists of names) and folder_types (counts)"
        if not (isinstance(contract, dict) and set(contract) == {"root_files", "redundant", "folder_types"}):
            raise ValueError(shape_error)
        counts.update((f"rootfile:{name}", 1) for name in _names(contract["root_files"], shape_error))
        redundant = contract["redundant"]  # kept verbatim: a folder entry ends in `/`
        if not (isinstance(redundant, list) and all(isinstance(n, str) and n for n in redundant)):
            raise ValueError(shape_error)
        counts.update((f"redundant:{name}", 1) for name in redundant)
        types = contract["folder_types"]
        if not (isinstance(types, dict) and all(
                isinstance(n, int) and not isinstance(n, bool) and n >= 1 for n in types.values())):
            raise ValueError(shape_error)
        counts.update((f"type:{folder}", n) for folder, n in types.items())
    return counts, frozenset(group for group in RECORD_GROUPS if data.get(group) is not None)


def render_records(counts: dict[str, int]) -> str:
    """The `RECORD_GROUPS` fields for a baseline file, sorted: a list per line, a folder count per line."""
    def names(kind: str) -> str:
        return json.dumps(_kind_names(counts, kind), ensure_ascii=False)

    types = ",".join(f"\n      {json.dumps(folder, ensure_ascii=False)}: {counts[f'type:{folder}']}"
                     for folder in _kind_names(counts, "type"))
    return (
        f'\n  "root_extras": {names("root")},'
        '\n  "shape_contract": {'
        f'\n    "root_files": {names("rootfile")},'
        f'\n    "redundant": {names("redundant")},'
        f'\n    "folder_types": {{{types}{chr(10) + "    " if types else ""}}}'
        "\n  },"
    )
