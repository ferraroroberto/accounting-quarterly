"""Root layout of the structure ratchet: the shape presets, extra roots and second-level placement.

`scripts/structure_ratchet.py` loads this module for everything that depends on a repo's
declared `shape`. Three checks live here, all reading the repo's tracked paths:

- **Root layout** (`layout_findings`): a required root folder with no tracked file, or a
  tracked root folder neither the shape nor `extra_roots` lists. Never baselined.
- **Extra roots** (`extra_root_names`): a declared `extra_roots` folder the shape's preset does
  not already list is a deviation. The ratchet baselines it, so the list only shrinks.
- **Second-level placement** (`misplaced`): where a shape declares `homes`, a file under one of
  its roots that sits outside every home. The ratchet baselines the count per folder.

Vendor-verbatim (manifest key `structure_layout`), stdlib-only, no import of the ratchet.
The presets are mirrored in docs/structure-ratchet.md and docs/repo-layout.md; a test compares them.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import PurePosixPath

LAYOUT_DOC = "project-scaffolding docs/repo-layout.md"
#: Root folders every shape allows, besides hidden ones (`.github`, `.claude`, ...).
ALWAYS_ALLOWED = ("docs",)
DEFAULT_LAYERS = (("src", "app"),)


@dataclass(frozen=True)
class Shape:
    """One repo shape's root layout: folders it must have, folders it may have, and its layer pairs.

    `homes` is the second-level contract: path patterns where files under `home_roots` may live.
    A pattern ending in `/` is a folder and everything below it; `dir/*.py` is a file directly in `dir`.
    """

    required: tuple[str, ...]
    allowed: tuple[str, ...]
    layers: tuple[tuple[str, str], ...] = ()
    homes: tuple[str, ...] = ()

    @property
    def home_roots(self) -> frozenset[str]:
        return frozenset(home.split("/", 1)[0] for home in self.homes)


#: The `.fleet.toml` `shape` ids, mirroring the trees in docs/repo-layout.md. A required folder
#: cannot be waived (`extra_roots` only adds), so a set is required only where every repo of the
#: shape has it. docs/structure-ratchet.md carries the same table; a test compares the two.
SHAPES = {
    "webapp-tray": Shape(
        ("app", "src", "tests", "docs"), ("scripts", "data", "brand", "assets", "config", "webapp"), DEFAULT_LAYERS,
        ("app/*.py", "app/cli/", "app/tray/", "app/webapp/*.py", "app/webapp/routers/", "app/webapp/static/", "src/"),
    ),
    "streamlit": Shape(("app", "src", "tests"), ("scripts", "data", "brand", "config"), DEFAULT_LAYERS),
    "pipeline-cli": Shape(("tests",), ("src", "common", "scripts", "config", "data")),
    "skills-governance": Shape(("skills", "tests"), ("hooks", "scripts", "architecture")),
    "node": Shape(("src",), ("test", "scripts")),
    "static-site": Shape((), ("css", "js", "assets", "games")),
    "agent-workspace": Shape((), ("scripts",)),
    # No layer pair: the service in src/ mounts its admin app, so src imports app_web by design.
    "service": Shape(("src", "tests", "app_web", "tray"), ("scripts", "config", "launchers", "assets")),
}


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


def extra_root_names(paths: list[str], shape_id: str, extra_roots: tuple[str, ...]) -> list[str]:
    """Tracked `extra_roots` folders the shape's preset does not list: the deviations, sorted.

    A folder the preset already lists (promoted into it, or `docs`) is no deviation, whatever
    `.fleet.toml` still says.
    """
    shape = SHAPES[shape_id]
    preset = set(shape.required) | set(shape.allowed) | set(ALWAYS_ALLOWED)
    return sorted(name for name in set(extra_roots) & root_folders(paths) if name not in preset)


def _in_home(path: str, home: str) -> bool:
    if home.endswith("/"):
        return path.startswith(home)
    folder, _, pattern = home.rpartition("/")
    name = path[len(folder) + 1:]
    return path.startswith(folder + "/") and "/" not in name and fnmatch.fnmatchcase(name, pattern)


def misplaced(paths: list[str], shape_id: str) -> dict[str, int]:
    """Files under a home root that sit in no home, counted per folder (empty when the shape has no homes)."""
    shape = SHAPES[shape_id]
    counts: dict[str, int] = {}
    for path in paths:
        if path.split("/", 1)[0] in shape.home_roots and not any(_in_home(path, home) for home in shape.homes):
            folder = str(PurePosixPath(path).parent)
            counts[folder] = counts.get(folder, 0) + 1
    return counts


def placement_advice(shape_id: str) -> str:
    """Where a misplaced file goes: the shape's homes, spelled out."""
    homes = ", ".join(home if home.endswith("/") else home.replace("*.py", "<module>.py") for home in SHAPES[shape_id].homes)
    return f"move it into one of this shape's homes ({homes}), or into a domain subpackage of src/"
