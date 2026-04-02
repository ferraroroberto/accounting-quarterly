"""Classification rules engine that reads from classification_rules.json."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from src.logger import get_logger

log = get_logger(__name__)

_RULES_PATH = Path(__file__).parent.parent / "classification_rules.json"
_rules_cache: dict | None = None


def load_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    global _rules_cache
    if _rules_cache is not None:
        return _rules_cache
    rules_path = Path(path) if path else _RULES_PATH
    if not rules_path.exists():
        log.warning("⚠️ classification_rules.json not found, using empty rules")
        return {"activity_rules": [], "geographic_rules": {"defaults": {}, "geographic_overrides": {}, "email_overrides": {}}}
    with open(rules_path, encoding="utf-8") as f:
        _rules_cache = json.load(f)
    return _rules_cache


def reload_rules(path: Optional[str | Path] = None) -> dict[str, Any]:
    global _rules_cache
    _rules_cache = None
    return load_rules(path)


def save_rules(rules: dict[str, Any], path: Optional[str | Path] = None) -> None:
    rules_path = Path(path) if path else _RULES_PATH
    with open(rules_path, "w", encoding="utf-8") as f:
        json.dump(rules, f, indent=2, ensure_ascii=False)
    global _rules_cache
    _rules_cache = rules
    log.info("ℹ️ Classification rules saved to %s", rules_path)
