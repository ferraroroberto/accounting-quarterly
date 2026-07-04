import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src._json_store import JsonCache
from src.exceptions import ConfigError

load_dotenv()

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def _missing_config(path: Path) -> dict[str, Any]:
    raise ConfigError(f"Config file not found: {path}")


_cache = JsonCache(_CONFIG_PATH, on_missing=_missing_config)


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    return _cache.load(path)


def reload_config(path: str | Path | None = None) -> dict[str, Any]:
    return _cache.reload(path)


def save_config(cfg: dict[str, Any], path: str | Path | None = None) -> None:
    _cache.save(cfg, path)


def get_stripe_api_key() -> str:
    key = os.getenv("STRIPE_API_KEY") or os.getenv("STRIPE_API_KEY_RESTRICTED")
    if not key:
        raise ConfigError("STRIPE_API_KEY not set in environment / .env file")
    return key
