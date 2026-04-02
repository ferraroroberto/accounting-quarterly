"""Tests for the rules engine (load/save/reload)."""
import json

import pytest

from src.rules_engine import load_rules, reload_rules, save_rules


class TestRulesEngine:
    def test_load_rules_from_file(self, tmp_rules, sample_rules):
        rules = load_rules(tmp_rules)
        assert "activity_rules" in rules
        assert len(rules["activity_rules"]) == 5

    def test_save_and_reload(self, tmp_rules, sample_rules):
        rules = load_rules(tmp_rules)
        rules["activity_rules"].append({
            "priority": 6,
            "name": "test_rule",
            "activity_type": "COACHING",
            "match_type": "description_contains",
            "keywords": ["test"],
        })
        save_rules(rules, tmp_rules)

        reloaded = reload_rules(tmp_rules)
        assert len(reloaded["activity_rules"]) == 6

    def test_load_missing_file_returns_defaults(self, tmp_path):
        import src.rules_engine as re_mod
        re_mod._rules_cache = None  # clear cache from prior tests
        rules = load_rules(tmp_path / "nonexistent.json")
        assert rules["activity_rules"] == []
        re_mod._rules_cache = None  # reset for other tests

    def test_geographic_rules_structure(self, tmp_rules):
        rules = load_rules(tmp_rules)
        geo = rules["geographic_rules"]
        assert "defaults" in geo
        assert "geographic_overrides" in geo
        assert "email_overrides" in geo
