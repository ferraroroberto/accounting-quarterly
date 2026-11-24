"""Where every gate ratchet keeps its baseline: `.ratchets/<name>.json`, so the repo root holds no debt records.

Each ratchet loads this sibling to resolve its baseline path. A ratchet from before the folder kept
the file at the root as `.<name>-ratchet-baseline.json`; the first run of a newer copy moves it into
`.ratchets/` and says so, and the move must be committed. There is never a second copy: when both
exist, `resolve` names them and returns None, and the ratchet exits 2 with both left untouched.

Adopters wrote the old default out in `.fleet.toml` (`baseline = ".<name>-ratchet-baseline.json"`);
`configured` reads that value as the new default, so an existing table needs no edit. Any other
`baseline` value is a deliberate choice and is used as written, with no move.

Vendor-verbatim (manifest key `ratchet_baseline`), stdlib-only, no import of any ratchet.
Mechanism: docs/ratchet-catalog.md in project-scaffolding.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

FOLDER = ".ratchets"


def legacy_of(default: str) -> str:
    """The root path a ratchet kept its baseline at before `.ratchets/`: `.ratchets/x.json` -> `.x-ratchet-baseline.json`."""
    return f".{PurePosixPath(default).stem}-ratchet-baseline.json"


def configured(value: object, default: str) -> object:
    """The `baseline` value as the ratchet should read it: the old root default means the new default."""
    return default if value == legacy_of(default) else value


def resolve(root: Path, baseline: str, default: str, label: str) -> Path | None:
    """The baseline file, after moving an old root copy into `.ratchets/` once.

    Creates `.ratchets/` so `--init` can write into it. None, after printing why, when both copies exist:
    they could disagree, so neither is picked.
    """
    path, legacy = root / baseline, root / legacy_of(default)
    if baseline != default:
        return path
    # runtime-write-exempt: .ratchets/ holds the repo's baselines, never a code folder
    path.parent.mkdir(exist_ok=True)
    if not legacy.is_file():
        return path
    if path.exists():
        print(f"{label}: CONFIG ERROR: both {legacy.name} and {baseline} exist -- keep one, delete the other")
        return None
    # runtime-write-exempt: the repo's own baseline moving into .ratchets/; os.replace keeps one copy at every instant
    legacy.replace(path)
    print(f"{label}: moved {legacy.name} to {baseline} -- commit the move")
    return path
