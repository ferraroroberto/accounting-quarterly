import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.exceptions import ConfigError

load_dotenv()

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
_config_cache: dict | None = None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    global _config_cache
    if _config_cache is not None:
        return _config_cache

    cfg_path = Path(path) if path else _CONFIG_PATH
    if not cfg_path.exists():
        raise ConfigError(f"Config file not found: {cfg_path}")

    with open(cfg_path, encoding="utf-8") as f:
        _config_cache = json.load(f)

    return _config_cache


def reload_config(path: str | Path | None = None) -> dict[str, Any]:
    global _config_cache
    _config_cache = None
    return load_config(path)


def save_config(cfg: dict[str, Any], path: str | Path | None = None) -> None:
    cfg_path = Path(path) if path else _CONFIG_PATH
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    global _config_cache
    _config_cache = cfg


def get_stripe_api_key() -> str:
    key = os.getenv("STRIPE_API_KEY") or os.getenv("STRIPE_API_KEY_RESTRICTED")
    if not key:
        raise ConfigError("STRIPE_API_KEY not set in environment / .env file")
    return key
