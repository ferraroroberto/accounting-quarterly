"""Classification rules engine that reads from classification_rules.json."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from src._json_store import JsonCache
from src.logger import get_logger

log = get_logger(__name__)

_RULES_PATH = Path(__file__).parent.parent / "classification_rules.json"


def _missing_rules(path: Path) -> dict[str, Any]:
    log.warning("⚠️ classification_rules.json not found, using empty rules")
    return {
        "activity_rules": [],
        "geographic_rules": {"defaults": {}, "geographic_overrides": {}, "email_overrides": {}},
    }


_cache = JsonCache(_RULES_PATH, on_missing=_missing_rules)


def load_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    return _cache.load(path)


def reload_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    return _cache.reload(path)


def save_rules(rules: dict[str, Any], path: Optional[str | Path] = None) -> None:
    rules_path = Path(path) if path else _RULES_PATH
    _cache.save(rules, path)
    log.info("ℹ️ Classification rules saved to %s", rules_path)
