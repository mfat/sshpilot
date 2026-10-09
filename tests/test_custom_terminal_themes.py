"""Custom terminal color schemes: validation and the Config catalog (#1311)."""

import json

import pytest

from sshpilot.custom_terminal_themes import (
    load_custom_themes,
    new_custom_theme_key,
    normalize_color,
    normalize_terminal_theme,
)

PALETTE = [f"#{i:02X}{i:02X}{i:02X}" for i in range(16)]


def _theme(**overrides):
    theme = {
        "name": "Ocean",
        "foreground": "#FFFFFF",
        "background": "#003366",
        "palette": list(PALETTE),
    }
    theme.update(overrides)
    return theme


def test_normalize_color_accepts_hex_only():
    assert normalize_color("#0f0") == "#00FF00"
    assert normalize_color(" #a0b1c2 ") == "#A0B1C2"
    assert normalize_color("green") is None
    assert normalize_color("#12345") is None
    assert normalize_color(None) is None


def test_normalize_theme_fills_the_builtin_shape():
    theme = normalize_terminal_theme(_theme(name="  Ocean  "))
    assert theme == {
        "name": "Ocean",
        "foreground": "#FFFFFF",
        "background": "#003366",
        "cursor_color": "#FFFFFF",
        "highlight_background": "#4A90E2",
        "highlight_foreground": "#FFFFFF",
        "palette": PALETTE,
    }


def test_eight_color_palette_is_repeated_for_bright_colors():
    theme = normalize_terminal_theme(_theme(palette=PALETTE[:8]))
    assert theme["palette"] == PALETTE[:8] + PALETTE[:8]


@pytest.mark.parametrize(
    "broken",
    [
        {"name": ""},
        {"name": None},
        {"foreground": "white"},
        {"background": None},
        {"palette": PALETTE[:5]},
        {"palette": PALETTE[:15] + ["red"]},
    ],
)
def test_invalid_themes_are_rejected(broken):
    assert normalize_terminal_theme(_theme(**broken)) is None


def test_load_skips_invalid_entries_and_builtin_keys():
    stored = {
        "mine": _theme(),
        "broken": _theme(palette=[]),
        "dracula": _theme(name="Fake Dracula"),
    }
    assert list(load_custom_themes(stored, {"default", "dracula"})) == ["mine"]
    assert load_custom_themes("not a dict", set()) == {}


def test_new_keys_are_slugged_and_unique():
    assert new_custom_theme_key("Ocean Blue!", set()) == "custom_ocean_blue"
    taken = {"custom_ocean_blue", "custom_ocean_blue_2"}
    assert new_custom_theme_key("Ocean Blue", taken) == "custom_ocean_blue_3"
    assert new_custom_theme_key("***", set()) == "custom_theme"


# --- Config ---------------------------------------------------------------

@pytest.fixture
def make_config(tmp_path, monkeypatch):
    import sshpilot.config as config_mod

    monkeypatch.setattr(config_mod, "get_config_dir", lambda: str(tmp_path))
    monkeypatch.setattr(config_mod.Config, "_import_legacy_gsettings", lambda self: None)

    def _make(terminal=None):
        if terminal is not None:
            (tmp_path / "config.json").write_text(
                json.dumps(
                    {"config_version": config_mod.CONFIG_VERSION, "terminal": terminal}
                )
            )
        return config_mod.Config()

    return _make


def test_hand_written_theme_is_loaded_and_active(make_config):
    config = make_config({"theme": "mine", "custom_themes": {"mine": _theme()}})
    assert config.custom_theme_keys() == ["mine"]
    assert config.get_terminal_profile()["background"] == "#003366"


def test_saved_theme_survives_a_restart(make_config):
    config = make_config()
    key = config.save_custom_theme(_theme())
    assert key == "custom_ocean"
    assert config.save_custom_theme(_theme()) == "custom_ocean_2"

    edited = config.save_custom_theme(_theme(background="#000000"), key=key)
    assert edited == key

    reloaded = make_config()
    assert reloaded.custom_theme_keys() == ["custom_ocean", "custom_ocean_2"]
    assert reloaded.terminal_themes[key]["background"] == "#000000"


def test_builtin_themes_cannot_be_replaced(make_config):
    config = make_config()
    assert config.save_custom_theme(_theme(), key="nord") is None
    assert config.terminal_themes["nord"]["name"] == "Nord"
    assert not config.is_custom_theme("nord")


def test_removing_the_active_theme_switches_to_default_first(make_config):
    config = make_config({"theme": "mine", "custom_themes": {"mine": _theme()}})
    seen = []
    config.emit = lambda _signal, key, _value: seen.append(
        (key, "mine" in config.terminal_themes)
    )

    config.remove_custom_theme("mine")

    # The switch happens while the theme still exists, so no repaint looks
    # up a missing theme.
    assert seen == [("terminal.theme", True), ("terminal.custom_themes", False)]
    reloaded = make_config()
    assert reloaded.get_setting("terminal.theme") == "default"
    assert reloaded.custom_theme_keys() == []


def test_reset_to_defaults_drops_custom_themes(make_config):
    config = make_config({"custom_themes": {"mine": _theme()}})
    config.reset_to_defaults()
    assert config.custom_theme_keys() == []
