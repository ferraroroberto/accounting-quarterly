"""Configuration tab: Save Tax Settings keeps the tax keys the form doesn't show (#187).

Config and rules loaders are stubbed, so the live ``config.json`` is never read
or written. All values are synthetic.
"""
from __future__ import annotations

import copy

from streamlit.testing.v1 import AppTest


def _configuration_page() -> None:
    from app.views import configuration

    configuration.render()


def test_save_tax_settings_keeps_undisplayed_tax_keys(monkeypatch, sample_rules):
    import app.views.configuration as tab

    prorrata = {"provisional_pct": 80, "definitive_pct_by_year": {"2025": 75}}
    cfg = {
        "app": {},
        "tax": {
            "regime": "estimacion_directa_simplificada",
            "vat_registered": True,
            "oss_registered": False,
            "vat_proration_percentage": 100,
            "activity_start_date": "2023-01-01",
            "prorrata": copy.deepcopy(prorrata),
            "previous_year_net_yield": 1000.0,
        },
    }
    saved: list[dict] = []
    monkeypatch.setattr(tab, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(tab, "save_config", lambda c, *a, **k: saved.append(copy.deepcopy(c)))
    monkeypatch.setattr(tab, "load_rules", lambda *a, **k: copy.deepcopy(sample_rules))

    at = AppTest.from_function(_configuration_page).run()
    assert not at.exception, at.exception
    at.number_input(key="tax_vat_prorrata").set_value(90).run()
    at.button(key="save_tax_settings").click().run()
    assert not at.exception, at.exception

    [tax] = [c["tax"] for c in saved]
    assert tax["vat_proration_percentage"] == 90  # the edited key is saved
    assert tax["activity_start_date"] == "2023-01-01"
    assert tax["prorrata"] == prorrata
    assert tax["previous_year_net_yield"] == 1000.0


def test_raw_json_editor_follows_rules_saved_elsewhere(monkeypatch, sample_rules):
    """A rule saved by another form shows in the Raw JSON editor, so saving it cannot delete the rule."""
    import json

    import app.views.configuration as tab

    stored = copy.deepcopy(sample_rules)
    monkeypatch.setattr(tab, "load_config", lambda *a, **k: {"app": {}, "tax": {}})
    monkeypatch.setattr(tab, "load_rules", lambda *a, **k: copy.deepcopy(stored))

    def _raw_text(at) -> str:
        [area] = [t for t in at.text_area if t.key.startswith("raw_rules_json")]
        return area.value

    at = AppTest.from_function(_configuration_page).run()
    assert not at.exception, at.exception
    assert "new_geo_override_rule" not in _raw_text(at)

    stored["added_elsewhere"] = "new_geo_override_rule"
    at.run()
    assert not at.exception, at.exception
    assert json.loads(_raw_text(at))["added_elsewhere"] == "new_geo_override_rule"
