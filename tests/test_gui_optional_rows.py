"""GUI tests for Not-set dropdown rows on Settings > SSH options.

Unset is the first dropdown choice ("Not set"), never an off switch or a
bare selected OpenSSH keyword. Stored values are unchanged
(0 / \"\" = not sent).

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _int_row(**overrides):
    from sshpilot.optional_rows import OptionalIntRow

    options = dict(
        title="Connect Timeout (s)",
        lower=1,
        upper=600,
        step=1,
        initial=30,
        dont_set_label="Not set",
        custom_label="Set",
    )
    options.update(overrides)
    return OptionalIntRow(**options)


def _choice_row(**overrides):
    from sshpilot.optional_rows import OptionalChoiceRow

    options = dict(
        title="StrictHostKeyChecking",
        choices=("accept-new", "yes", "no", "ask"),
        dont_set_label="Not set",
    )
    options.update(overrides)
    return OptionalChoiceRow(**options)


def test_int_row_starts_unset_with_no_number_shown(gui):
    row = _int_row()
    assert row.get_value() == 0
    assert row.get_enabled() is False
    assert row.number_field_visible() is False


def test_int_row_custom_reveals_the_number_and_dont_set_unsets(gui):
    row = _int_row()
    seen = []
    row.connect("notify::value", lambda *_a: seen.append(row.get_value()))

    row.set_enabled(True)
    assert row.number_field_visible() is True
    assert row.get_value() == 30
    assert seen == [30]

    row.set_enabled(False)
    assert row.number_field_visible() is False
    assert row.get_value() == 0
    assert seen == [30, 0]


def test_int_row_saved_value_is_shown_and_zero_clears(gui):
    row = _int_row()
    row.set_value(45)
    assert (row.get_enabled(), row.get_value(), row.number_field_visible()) == (True, 45, True)
    row.set_value(0)
    assert (row.get_enabled(), row.get_value(), row.number_field_visible()) == (False, 0, False)
    row.set_enabled(True)
    assert row.get_value() == 45


def test_int_row_large_saved_value_is_not_clamped(gui):
    row = _int_row()
    row.set_value(600)
    assert row.get_value() == 600


def test_choice_row_dont_set_yes_and_no(gui):
    row = _choice_row(title="Compression", choices=("yes", "no"))
    assert row.get_value() == ""
    assert row.get_selected() == 0

    row.set_value("yes")
    assert row.get_value() == "yes"
    assert row.get_selected() == 1

    row.set_value("no")
    assert row.get_value() == "no"
    assert row.get_selected() == 2

    row.set_value("")
    assert row.get_value() == ""
    assert row.get_selected() == 0


def test_ssh_options_page_uses_dont_set_dropdown_rows(gui):
    from sshpilot.optional_rows import OptionalChoiceRow, OptionalIntRow

    gui.window.show_preferences("ssh-options")
    gui.pump(500)
    prefs = gui.window._preferences_window
    prefs._ensure_page_built("ssh-options")
    for name in (
        "connect_timeout_row",
        "verbosity_row",
    ):
        row = getattr(prefs, name)
        assert isinstance(row, OptionalIntRow), name
        if row.get_value() == 0:
            assert row.number_field_visible() is False
            assert row.get_enabled() is False

    for name in ("compression_row", "strict_host_row"):
        row = getattr(prefs, name)
        assert isinstance(row, OptionalChoiceRow), name
        if row.get_value() == "":
            assert row.get_selected() == 0

    assert not hasattr(prefs, "batch_mode_row")
    assert not hasattr(prefs, "connection_attempts_row")


def test_keepalive_is_one_switch_with_its_numbers_folded_in(gui):
    from sshpilot.optional_rows import SwitchExpanderRow

    gui.window.show_preferences("ssh-options")
    gui.pump(500)
    prefs = gui.window._preferences_window
    prefs._ensure_page_built("ssh-options")
    row = prefs.apply_default_keepalive_row
    assert isinstance(row, SwitchExpanderRow)

    prefs._suppress_advanced_ssh_autosave = True
    try:
        seen = []
        row.connect("notify::active", lambda *_a: seen.append(row.get_active()))
        row.set_active(False)
        assert row.get_enable_expansion() is False
        row.set_enable_expansion(True)
        assert row.get_active() is True
        assert seen == [False, True]

        prefs.keepalive_interval_row.set_value(40)
        prefs.keepalive_count_row.set_value(5)
        prefs._sync_keepalive_subtitle()
        assert row.get_subtitle() == "ServerAliveInterval 40, ServerAliveCountMax 5"
    finally:
        prefs._suppress_advanced_ssh_autosave = False
