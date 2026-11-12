"""load_app_config falls back to defaults only for a missing file, never a malformed one."""
import json

import pytest

import src.config as config_mod
from src._json_store import JsonCache
from src.tax_data import load_app_config


def _cache_for(path):
    return JsonCache(path, on_missing=config_mod._missing_config)


def test_missing_config_runs_on_defaults_and_warns(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(config_mod, "_cache", _cache_for(tmp_path / "absent.json"))

    with caplog.at_level("WARNING"):
        assert load_app_config() == {}

    assert "config.json not found" in caplog.text


def test_malformed_config_raises(monkeypatch, tmp_path):
    bad = tmp_path / "config.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(config_mod, "_cache", _cache_for(bad))

    with pytest.raises(json.JSONDecodeError):
        load_app_config()


def test_valid_config_is_returned(monkeypatch, tmp_path):
    good = tmp_path / "config.json"
    good.write_text('{"tax": {"activity_start_date": "2024-01-01"}}', encoding="utf-8")
    monkeypatch.setattr(config_mod, "_cache", _cache_for(good))

    assert load_app_config() == {"tax": {"activity_start_date": "2024-01-01"}}
