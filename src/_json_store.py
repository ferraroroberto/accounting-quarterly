"""Shared in-memory-cached JSON file store.

``src/config.py`` and ``src/rules_engine.py`` both persisted a single JSON
file behind a module-level ``_cache | None`` with matching
``load_/reload_/save_`` semantics — identical shape, different file path and
missing-file handling. This module factors that pattern into one class so
the load/reload/save semantics live in exactly one place.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Optional


class JsonCache:
    """In-memory cache over a single JSON file.

    ``load``/``reload`` only actually read from disk when the in-memory
    cache is empty (or on ``reload``, unconditionally); ``save`` writes
    through and refreshes the cache. A ``path`` override is honoured only
    while the cache is empty — matching the pre-existing per-module
    behaviour where a cached value short-circuits any explicit path.

    ``on_missing(resolved_path)``, if given, is called when the resolved
    file doesn't exist and must return the dict to use in its place. If not
    given, a missing file raises ``FileNotFoundError``.
    """

    def __init__(
        self,
        default_path: Path,
        *,
        on_missing: Optional[Callable[[Path], dict[str, Any]]] = None,
    ) -> None:
        self._default_path = default_path
        self._on_missing = on_missing
        self._cache: dict[str, Any] | None = None

    def load(self, path: str | Path | None = None) -> dict[str, Any]:
        if self._cache is not None:
            return self._cache

        resolved = Path(path) if path else self._default_path
        if not resolved.exists():
            if self._on_missing is not None:
                return self._on_missing(resolved)
            raise FileNotFoundError(f"File not found: {resolved}")

        with open(resolved, encoding="utf-8") as f:
            self._cache = json.load(f)
        return self._cache

    def reload(self, path: str | Path | None = None) -> dict[str, Any]:
        self._cache = None
        return self.load(path)

    def save(self, data: dict[str, Any], path: str | Path | None = None) -> None:
        resolved = Path(path) if path else self._default_path
        with open(resolved, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        self._cache = data

    def clear(self) -> None:
        """Drop the in-memory cache without touching the file (test helper)."""
        self._cache = None
