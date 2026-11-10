"""GUI tests for the "may be left unset" number rows on Settings > SSH options.

A number row used to show a bare ``0`` for "this option is not sent". It is now
a switch that is off ("Do not configure") with the number field hidden; the
stored value is unchanged (0 = not sent).

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _row(**overrides):
    from sshpilot.optional_int_row import OptionalIntRow

    options = dict(
        title="Connect Timeout (s)", lower=1, upper=600, step=1, initial=30,
        unset_subtitle="Do not configure",
    )
    options.update(overrides)
    return OptionalIntRow(**options)


def test_row_starts_unset_with_no_number_shown(gui):
    row = _row()
    assert row.get_value() == 0
    assert row.get_enabled() is False
    assert row.number_field_visible() is False
    assert row.get_subtitle() == "Do not configure"


def test_switching_on_reveals_the_number_and_reports_it(gui):
    row = _row()
    seen = []
    row.connect("notify::value", lambda *_a: seen.append(row.get_value()))

    row.set_enabled(True)
    assert row.number_field_visible() is True
    assert row.get_subtitle() == ""
    assert row.get_value() == 30
    assert seen == [30]

    row.set_enabled(False)
    assert row.number_field_visible() is False
    assert row.get_value() == 0
    assert seen == [30, 0]


def test_a_saved_value_is_shown_and_zero_switches_it_off(gui):
    row = _row()
    row.set_value(45)
    assert (row.get_enabled(), row.get_value(), row.number_field_visible()) == (True, 45, True)
    row.set_value(0)
    assert (row.get_enabled(), row.get_value(), row.number_field_visible()) == (False, 0, False)
    # Switching back on returns to the last number, not to a bare minimum.
    row.set_enabled(True)
    assert row.get_value() == 45


def test_a_large_saved_value_is_not_clamped(gui):
    row = _row()
    row.set_value(600)
    assert row.get_value() == 600


def test_ssh_options_page_uses_unset_rows_for_every_number(gui):
    from sshpilot.optional_int_row import OptionalIntRow

    gui.window.show_preferences("ssh-options")
    gui.pump(500)
    prefs = gui.window._preferences_window
    prefs._ensure_page_built("ssh-options")
    for name in (
        "connect_timeout_row",
        "connection_attempts_row",
        "keepalive_interval_row",
        "keepalive_count_row",
        "verbosity_row",
    ):
        row = getattr(prefs, name)
        assert isinstance(row, OptionalIntRow), name
        if row.get_value() == 0:
            assert row.number_field_visible() is False
            assert row.get_subtitle() != ""
