"""Headless tests for banner tip loading and platform gating."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from sshpilot import tips as tips_mod


SAMPLE = textwrap.dedent(
    """\
    # comment
    Press {primary}+F to search
    [file-manager] Open the file manager with {primary}+Shift+O
    [external-terminal] Choose your preferred terminal in Settings
    Middle-click a connection to open it in a new tab
    Middle-click a group to open all its connections in tabs
    Double-tap Shift to search hosts and jump to a connection
    """
).splitlines(keepends=True)


def test_parse_substitutes_primary_and_strips_comments():
    result = tips_mod.parse_tip_lines(
        SAMPLE,
        primary_modifier="Ctrl",
        include_file_manager=True,
        include_external_terminal=True,
    )
    assert result[0] == "Press Ctrl+F to search"
    assert "Open the file manager with Ctrl+Shift+O" in result
    assert "Choose your preferred terminal in Settings" in result
    assert "#" not in "".join(result)


def test_parse_gates_file_manager_and_external_terminal():
    result = tips_mod.parse_tip_lines(
        SAMPLE,
        primary_modifier="Ctrl",
        include_file_manager=False,
        include_external_terminal=False,
    )
    assert result == [
        "Press Ctrl+F to search",
        "Middle-click a connection to open it in a new tab",
        "Middle-click a group to open all its connections in tabs",
        "Double-tap Shift to search hosts and jump to a connection",
    ]


@pytest.mark.parametrize(
    "platform, lang, expected",
    [
        ("linux", None, "Ctrl"),
        ("linux", "de", "Strg"),
        ("linux", "de_DE", "Strg"),
        ("linux", "fr", "Ctrl"),
        ("darwin", None, "\u2318"),
        ("darwin", "de", "\u2318"),
    ],
)
def test_tips_primary_modifier(monkeypatch, platform, lang, expected):
    monkeypatch.setattr(tips_mod.sys, "platform", platform)
    assert tips_mod.tips_primary_modifier(lang) == expected


def test_load_window_tips_prefers_language_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(tips_mod.sys, "platform", "linux")
    (tmp_path / "tips.md").write_text(
        "Press {primary}+F to search\n", encoding="utf-8"
    )
    (tmp_path / "tips.de.md").write_text(
        "Drücken Sie {primary}+F, um zu suchen\n"
        "Mittlerer Klick auf eine Gruppe öffnet alle Verbindungen in Tabs\n",
        encoding="utf-8",
    )

    loaded = tips_mod.load_window_tips(str(tmp_path), ["de_DE", "de"])
    assert loaded == [
        "Drücken Sie Strg+F, um zu suchen",
        "Mittlerer Klick auf eine Gruppe öffnet alle Verbindungen in Tabs",
    ]


def test_bundled_tips_include_omnisearch_and_middle_click_group():
    resources = Path(tips_mod.__file__).resolve().parent / "resources"
    loaded = tips_mod.load_window_tips(
        str(resources),
        [],
        include_file_manager=True,
        include_external_terminal=True,
    )
    assert any("Double-tap Shift" in tip for tip in loaded)
    assert any("Middle-click a connection" in tip for tip in loaded)
    assert any("Middle-click a group" in tip for tip in loaded)
    assert all("{primary}" not in tip for tip in loaded)
    assert all(not tip.startswith("[") for tip in loaded)


def test_bundled_tips_drop_gated_features_when_hidden():
    resources = Path(tips_mod.__file__).resolve().parent / "resources"
    loaded = tips_mod.load_window_tips(
        str(resources),
        [],
        include_file_manager=False,
        include_external_terminal=False,
    )
    joined = "\n".join(loaded)
    assert "file manager" not in joined.lower()
    assert "Manage Files" not in joined
    assert "preferred terminal" not in joined.lower()
    assert "Double-tap Shift" in joined
    assert "Middle-click a group" in joined
