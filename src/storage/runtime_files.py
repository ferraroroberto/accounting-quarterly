"""Writers for runtime files whose path a parameter, a config value or the operator names.

The close output root, the archive folder, the invoice folders, the database path and
the logs folder are chosen by the caller or by ``config.json``, so the runtime-write
ratchet cannot follow them statically. These writers take the path, refuse it at run
time when it resolves under a code folder the ratchet guards, and then write. The
guarantee a per-call marker only claimed is enforced here.

Stdlib-only and import-free of the rest of the repo, so any module can use it.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path
from typing import IO, Any, Union

PathLike = Union[str, "os.PathLike[str]"]

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

#: The folders `[runtime_write_ratchet] code_folders` guards (the ratchet's default set).
CODE_FOLDERS = ("src", "app", "scripts", "hooks", "skills")


def checked(path: PathLike) -> Path:
    """``path`` resolved, or ``ValueError`` when it sits under a code folder."""
    resolved = Path(path).expanduser().resolve()
    for name in CODE_FOLDERS:
        folder = REPO_ROOT / name
        if resolved == folder or folder in resolved.parents:
            raise ValueError(
                f"Refusing to write under the code folder {folder}: {resolved}. "
                "Runtime output belongs in data/, tmp/ or a configured folder outside the code tree."
            )
    return resolved


def make_dirs(path: PathLike, *, parents: bool = True, exist_ok: bool = True) -> Path:
    """Create the folder ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named folder; checked() above raises when it is under a code folder
    target.mkdir(parents=parents, exist_ok=exist_ok)
    return target


def open_for_write(path: PathLike, mode: str = "w", **kwargs: Any) -> IO[Any]:
    """Open ``path`` with ``mode`` (creating no folders); refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named file; checked() above raises when it is under a code folder
    return target.open(mode, **kwargs)


def save_text(path: PathLike, text: str, encoding: str = "utf-8") -> Path:
    """Write ``text`` to ``path`` in one call; refuses a path under a code folder."""
    with open_for_write(path, "w", encoding=encoding) as handle:
        handle.write(text)
    return Path(path)


def copy_file(src: PathLike, dst: PathLike) -> Path:
    """Copy ``src`` to ``dst`` with its metadata; refuses a destination under a code folder."""
    target = checked(dst)
    # runtime-write-exempt: caller-named destination; checked() above raises when it is under a code folder
    shutil.copy2(src, target)
    return target


def connect_sqlite(path: PathLike) -> sqlite3.Connection:
    """Open the SQLite file ``path``; refuses a path under a code folder."""
    target = checked(path)
    # runtime-write-exempt: caller-named database; checked() above raises when it is under a code folder
    return sqlite3.connect(target)


def backup_sqlite(source: sqlite3.Connection, dest: PathLike) -> Path:
    """Copy the database behind ``source`` into the file ``dest`` with the backup API."""
    target = checked(dest)
    with connect_sqlite(target) as out:
        source.backup(out)
    return target
