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

    for name in ("compression_row", "strict_host_row", "address_family_row"):
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


def _row_labels(row):
    """Text of every label drawn on the row itself, popup excluded."""
    from gi.repository import Gtk

    found = []

    def walk(widget):
        if isinstance(widget, Gtk.Popover):
            return
        if isinstance(widget, Gtk.Label) and widget.get_mapped():
            found.append(widget.get_label())
        child = widget.get_first_child()
        while child is not None:
            walk(child)
            child = child.get_next_sibling()

    walk(row)
    return found


def test_int_row_shows_no_set_label_once_set(gui):
    """The number says it is set; the word "Set" stays in the popup only."""
    from gi.repository import Gtk

    row = _int_row()
    box = Gtk.ListBox()
    box.append(row)
    window = Gtk.Window(child=box)
    window.present()
    try:
        gui.pump(300)
        assert "Not set" in _row_labels(row)

        row.set_enabled(True)
        gui.pump(300)
        labels = _row_labels(row)
        assert "Set" not in labels and "Not set" not in labels
        assert row.shown_choice_text() == ""
        # Both choices are still offered.
        model = row.get_model()
        assert [model.get_string(i) for i in range(2)] == ["Not set", "Set"]

        row.set_enabled(False)
        gui.pump(300)
        assert "Not set" in _row_labels(row)
    finally:
        window.destroy()


def test_additional_options_apply_validates_then_saves(gui):
    gui.window.show_preferences("ssh-options")
    gui.pump(500)
    prefs = gui.window._preferences_window
    prefs._ensure_page_built("ssh-options")
    if prefs.ssh_overrides_controller is None:
        pytest.skip("no daemon overrides service in this session")
    buffer = prefs.extra_options_view.get_buffer()
    apply_button = prefs.extra_options_apply_button
    error = prefs.extra_options_error
    try:
        assert apply_button.get_sensitive() is False

        # An option that has its own row is refused, and nothing is saved.
        buffer.set_text("IPQoS none\nConnectTimeout 5")
        assert apply_button.get_sensitive() is True
        apply_button.emit("clicked")
        gui.pump(200)
        assert error.get_visible() and "ConnectTimeout" in error.get_label()
        assert "2" in error.get_label()
        assert prefs.ssh_overrides_controller.load().extra_options == ()

        # OpenSSH itself rejects a keyword that does not exist.
        buffer.set_text("NoSuchOptionAtAll yes")
        apply_button.emit("clicked")
        gui.pump(200)
        assert error.get_visible() and "NoSuchOptionAtAll".lower() in error.get_label().lower()
        assert prefs.ssh_overrides_controller.load().extra_options == ()

        # Valid options are saved through the daemon, in canonical form.
        buffer.set_text("# proxy\nIPQoS none\nSetEnv=LANG=C")
        apply_button.emit("clicked")
        gui.pump(300)
        assert error.get_visible() is False
        assert prefs.ssh_overrides_controller.load().extra_options == (
            "IPQoS=none", "SetEnv=LANG=C",
        )
        assert apply_button.get_sensitive() is False

        # Editing away from the saved value offers Apply again; back hides it.
        buffer.set_text("IPQoS none")
        assert apply_button.get_sensitive() is True
        buffer.set_text("IPQoS none\nSetEnv LANG=C")
        assert apply_button.get_sensitive() is False
    finally:
        buffer.set_text("")
        apply_button.emit("clicked")
        gui.pump(300)
    assert prefs.ssh_overrides_controller.load().extra_options == ()


def test_address_family_row_shows_labels_and_stores_keywords(gui):
    from sshpilot.optional_rows import OptionalChoiceRow

    row = OptionalChoiceRow(
        title="AddressFamily",
        choices=("inet", "inet6"),
        labels=("IPv4 only", "IPv6 only"),
        dont_set_label="Not set",
    )
    model = row.get_model()
    assert [model.get_string(i) for i in range(3)] == ["Not set", "IPv4 only", "IPv6 only"]
    row.set_selected(1)
    assert row.get_value() == "inet"
    row.set_value("inet6")
    assert row.get_selected() == 2
    row.set_value("")
    assert row.get_selected() == 0 and row.get_value() == ""
    with pytest.raises(ValueError):
        OptionalChoiceRow(
            title="x", choices=("a", "b"), labels=("only one",), dont_set_label="Not set"
        )


def test_legacy_switch_and_address_family_save_through_the_daemon(gui):
    gui.window.show_preferences("ssh-options")
    gui.pump(500)
    prefs = gui.window._preferences_window
    prefs._ensure_page_built("ssh-options")
    if prefs.ssh_overrides_controller is None:
        pytest.skip("no daemon overrides service in this session")
    try:
        assert prefs.legacy_algorithms_row.get_active() is False
        assert prefs.legacy_algorithms_row.get_subtitle()  # it weakens security: says so
        prefs.legacy_algorithms_row.set_active(True)
        prefs.address_family_row.set_value("inet")
        gui.pump(300)
        saved = prefs.ssh_overrides_controller.load()
        assert saved.legacy_algorithms is True and saved.address_family == "inet"
    finally:
        prefs.legacy_algorithms_row.set_active(False)
        prefs.address_family_row.set_value("")
        gui.pump(300)
    saved = prefs.ssh_overrides_controller.load()
    assert saved.legacy_algorithms is False and saved.address_family == ""
