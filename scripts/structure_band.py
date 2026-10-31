"""Near-limit band of the structure ratchet: the band's definition and its warning lines.

`scripts/structure_ratchet.py` loads this module for everything that depends on the band, the
files, functions and directories at or above `near_limit_ratio` of their limit and not over it.

- **The ratcheted band** (`BANDS`, `SPLIT_TARGET_RATIO`, `band_low`): the ratchet keeps the count
  of files and of functions in the band shrink-only, and fails a split that lands in it.
- **The warning lines** (`warning_lines`): every file, function and directory in the band, listed on
  every check. They never change the exit code. A file whose functions could not all be measured
  has an unknown function band, and is listed as unknown, never as clear.

Vendor-verbatim (manifest key `structure_band`), stdlib-only, no import of the ratchet.
"""

from __future__ import annotations

import math
from pathlib import PurePosixPath

#: Where a split should land: low enough that both halves start well clear of the near-limit band.
SPLIT_TARGET_RATIO = 0.75
#: Ratcheted band -> (unit counted, failure title, docs/repo-layout.md anchor, advice).
BANDS = {
    "files": ("file(s)", "near-limit file", "file-size",
              "split it to about {target:,} lines, moving code into a new module beside it named for what it does"),
    "functions": ("function(s)", "near-limit function", "function-length",
                  "split it into named helpers of about {target:,} lines"),
}
#: Warning-only member lists, beside the `BANDS` keys: directories in the band, files with unmeasured functions.
WARN_ONLY = ("dirs", "unknown")


def band_low(limit: int, ratio: float) -> int:
    """The lowest count inside the band; the rounding keeps 0.9 * 100 at 90, not 91."""
    return math.ceil(round(limit * ratio, 9))


def empty_members() -> dict[str, list[str]]:
    """One empty member list per ratcheted band and per warning-only list."""
    return {key: [] for key in (*BANDS, *WARN_ONLY)}


def module_counts(paths: list[str], suffixes: frozenset[str]) -> dict[str, int]:
    """Modules directly in each directory (not counting subfolders), `__init__.py` aside."""
    counts: dict[str, int] = {}
    for path in paths:
        pure = PurePosixPath(path)
        if pure.suffix.lower() in suffixes and pure.name != "__init__.py":
            counts[str(pure.parent)] = counts.get(str(pure.parent), 0) + 1
    return counts


def warning_lines(members: dict[str, list[str]], ratio: float) -> list[str]:
    """The gate's warning output: a summary line, then one line per member. Informational, never a failure."""
    files, functions, dirs, unknown = (sorted(members[key]) for key in ("files", "functions", "dirs", "unknown"))
    head = (f"structure ratchet: warning band ({ratio:.0%} of a limit; reported, never failed): "
            f"{len(files)} file(s), {len(functions)} function(s), {len(dirs)} directory(s)")
    if unknown:
        head += f"; function band UNKNOWN in {len(unknown)} file(s)"
    out = [head]
    out += [f"  [warning] {member}" for member in (*files, *functions, *dirs)]
    out += [f"  [unknown] {member}: the end of a function was not found, so its band is unknown"
            for member in unknown]
    return out
