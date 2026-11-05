from __future__ import annotations

from pathlib import Path

import pytest

from dyson_pi.config import AppConfig, ConfigError, ConfigManager, build_config, load_raw

ROOT = Path(__file__).resolve().parents[1]


def test_example_config_is_valid_and_matches_defaults():
    raw = load_raw(ROOT / "config.example.yaml")
    cfg = build_config(raw)
    assert cfg.model_dump() == AppConfig().model_dump()


def test_unknown_key_rejected():
    with pytest.raises(ConfigError):
        build_config({"press": {"pres_duration_s": 1}})


def test_bad_timezone_rejected():
    with pytest.raises(ConfigError):
        build_config({"scheduler": {"timezone": "Mars/Olympus"}})


def test_same_pins_rejected():
    with pytest.raises(ConfigError):
        build_config({"hardware": {"relay_pin": 27}})


def test_overrides_layer_and_persistence():
    store: dict = {}
    m = ConfigManager({})
    m.attach_override_store(lambda: dict(store), lambda v: (store.clear(), store.update(v)))
    m.set_overrides({"press.departure_timeout_s": 30})
    assert m.current.press.departure_timeout_s == 30
    assert store == {"press.departure_timeout_s": 30.0}
    with pytest.raises(ConfigError):
        m.set_overrides({"safety.daily_press_limit": 100})  # not a calibration field
    m2 = ConfigManager({})
    m2.attach_override_store(lambda: dict(store), lambda v: None)
    assert m2.current.press.departure_timeout_s == 30
    m.reset_overrides()
    assert store == {} and m.current.press.departure_timeout_s == 20


def test_invalid_stored_overrides_are_dropped():
    store = {"press.press_duration_s": 9.0}
    m = ConfigManager({})
    m.attach_override_store(lambda: dict(store), lambda v: (store.clear(), store.update(v)))
    assert m.current.press.press_duration_s == 1.0
    assert store == {}
