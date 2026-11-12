"""The connection editor says which of its fields Settings ▸ SSH Options override.

Those options are sent ahead of a connection's own, and OpenSSH keeps the first
value it obtains, so a field can hold a value the app never uses. The editor
marks it and explains; it never disables the field or drops the value.
"""

import types

import pytest

from sshpilot.api.models.settings import GlobalSshOverrides
from sshpilot.connection_dialog_overrides import (
    OVERRIDABLE_ROWS,
    ConnectionDialogOverridesMixin as Overrides,
    advanced_option_note,
    forced_map,
    inherited_note_text,
    row_override_note,
)
from sshpilot.connection_dialog_validation import (
    ConnectionDialogValidationMixin as Validation,
)
from sshpilot.ssh_config_formatter import MANAGED_HOST_OPTIONS


def _real_gtk_available():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw
        Adw.init()
        return type(Adw.PreferencesGroup()).__name__ == "PreferencesGroup"
    except Exception:
        return False


needs_adw = pytest.mark.skipif(
    not _real_gtk_available(),
    reason="needs real libadwaita (gi is stubbed under the suite)",
)


def _snapshot(*forced):
    return GlobalSshOverrides(
        revision="r1",
        connect_timeout=0,
        strict_host_key_checking="",
        compression="",
        verbosity=0,
        debug_enabled=False,
        forced_options=forced,
    )


def test_forced_map_keeps_the_first_value_per_keyword():
    forced = forced_map(_snapshot("User=root", "ServerAliveInterval=15", "user=later"))
    assert forced == {"user": "root", "serveraliveinterval": "15"}
    assert forced_map(None) == {}


def test_a_forced_field_is_explained_with_the_value_in_force():
    note = row_override_note("User", {"user": "root"})
    assert "User" in note and "root" in note
    assert row_override_note("Port", {"user": "root"}) is None


def test_advanced_option_that_loses_to_settings_gets_a_note():
    forced = {"serveraliveinterval": "15", "sendenv": "LANG"}
    assert "15" in advanced_option_note("ServerAliveInterval", "90", forced)
    # Settings force the very value this host asks for: nothing is lost.
    assert advanced_option_note("ServerAliveInterval", "15", forced) is None
    assert advanced_option_note("Ciphers", "aes256-ctr", forced) is None


def test_an_option_openssh_collects_is_reported_as_added_not_lost():
    note = advanced_option_note("SendEnv", "TERM", {"sendenv": "LANG"})
    assert "LANG" in note and "Both" in note


def test_every_marked_row_is_an_option_the_dialog_owns():
    for _row, keyword in OVERRIDABLE_ROWS:
        assert keyword.lower() in MANAGED_HOST_OPTIONS | {"exitonforwardfailure"}


def _dialog(**attrs):
    dialog = types.SimpleNamespace(_inherited_row_handlers={}, **attrs)
    for name in ("_set_row_override_note", "_set_row_inherited_note", "_refresh_row_tooltip"):
        setattr(dialog, name, getattr(Validation, name).__get__(dialog))
    for name in (
        "_set_forced_options", "_apply_override_notes", "_mark_row_override",
        "_overridden_field_count", "_advanced_option_note",
    ):
        setattr(dialog, name, getattr(Overrides, name).__get__(dialog))
    dialog.banner_syncs = []
    dialog._sync_override_banner = lambda: dialog.banner_syncs.append(
        dialog._overridden_field_count()
    )
    return dialog


@needs_adw
def test_forced_row_is_marked_stays_editable_and_keeps_its_text():
    from gi.repository import Adw

    row = Adw.EntryRow(title="Username")
    row.set_text("alice")
    switch = Adw.SwitchRow(title="X11")
    dialog = _dialog(username_row=row, x11_row=switch)

    dialog._set_forced_options({"user": "root"})

    assert row._sshpilot_override_icon.get_visible()
    assert "root" in row.get_tooltip_text()
    assert row.get_text() == "alice" and row.get_sensitive()
    assert getattr(switch, "_sshpilot_override_icon", None) is None
    assert dialog.banner_syncs == [1]


@needs_adw
def test_marker_goes_away_with_the_override():
    from gi.repository import Adw

    row = Adw.SwitchRow(title="X11")
    dialog = _dialog(x11_row=row)
    dialog._set_forced_options({"forwardx11": "yes"})
    assert row._sshpilot_override_icon.get_visible()

    dialog._set_forced_options({})

    assert not row._sshpilot_override_icon.get_visible()
    assert not row.get_tooltip_text()
    assert dialog.banner_syncs[-1] == 0


@needs_adw
def test_override_note_replaces_the_inherited_advice_and_gives_it_back():
    """"Edit to set it for this host" is false while the option is forced."""
    from gi.repository import Adw

    row = Adw.EntryRow(title="Port")
    dialog = _dialog(port_row=row)
    dialog._inherited_row_handlers["port_row"] = 1
    dialog._set_row_inherited_note(row, inherited_note_text())

    dialog._set_forced_options({"port": "2222"})
    assert inherited_note_text() not in row.get_tooltip_text()
    assert "2222" in row.get_tooltip_text()

    dialog._set_forced_options({})
    assert row.get_tooltip_text() == inherited_note_text()


def test_banner_counts_advanced_options_that_lose_but_not_added_ones():
    tab = types.SimpleNamespace(
        option_list=None,
        get_config_entries=lambda: [
            ("ServerAliveInterval", "90"),
            ("SendEnv", "TERM"),
            ("Ciphers", "aes256-ctr"),
        ],
    )
    dialog = _dialog(advanced_tab=tab)
    dialog._forced_options = {"serveraliveinterval": "15", "sendenv": "LANG"}
    assert dialog._overridden_field_count() == 1
