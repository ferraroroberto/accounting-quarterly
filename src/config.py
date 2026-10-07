import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src._json_store import JsonCache
from src.exceptions import ConfigError

load_dotenv()

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"
_ENV_PATH = Path(__file__).parent.parent / ".env"


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


def save_stripe_api_key(key: str, env_path: str | Path | None = None) -> None:
    """Write ``STRIPE_API_KEY`` to ``.env`` and into the running process.

    Refuses an empty key. Only the exact ``STRIPE_API_KEY=`` line is replaced, so
    the ``STRIPE_API_KEY_RESTRICTED`` fallback is left alone. ``os.environ`` is
    updated too, because dotenv is only read once at import.
    """
    key = key.strip()
    if not key:
        raise ConfigError("Stripe API key is empty - nothing was saved.")
    path = Path(env_path) if env_path else _ENV_PATH
    lines: list[str] = []
    if path.exists():
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if not ln.startswith("STRIPE_API_KEY=")]
    lines.append(f"STRIPE_API_KEY={key}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["STRIPE_API_KEY"] = key
