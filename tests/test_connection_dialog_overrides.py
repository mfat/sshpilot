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
    GLOBAL,
    OVERRIDDEN,
    override_state,
    suppressing_option,
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


def _icon_name(image):
    """The icon an image shows. The app's resolver (``icon_utils``) stores a
    GIcon rather than a plain name once it has patched ``Gtk.Image``."""
    name = image.get_icon_name()
    if name:
        return name
    gicon = image.get_gicon()
    if hasattr(gicon, "get_names"):
        return gicon.get_names()[0]
    return gicon.get_file().get_basename().rsplit(".", 1)[0]


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


def test_a_field_set_here_to_something_else_is_overridden():
    state, note = override_state("User", "alice", {"user": "root"})
    assert state == OVERRIDDEN
    assert "User" in note and "root" in note and "does not use" in note


def test_an_empty_field_uses_the_global_value_and_is_not_a_conflict():
    state, note = override_state("User", None, {"user": "root"})
    assert state == GLOBAL
    assert "root" in note and "uses that value" in note


def test_a_field_holding_the_forced_value_loses_nothing():
    assert override_state("User", "root", {"user": "root"})[0] == GLOBAL
    assert override_state("ProxyJump", "a, b", {"proxyjump": "a,b"})[0] == GLOBAL
    assert override_state("ForwardX11", "yes", {"forwardx11": "YES"})[0] == GLOBAL


def test_nothing_is_said_about_an_option_settings_leave_alone():
    assert override_state("Port", "2200", {"user": "root"}) == (None, None)


def test_a_forced_proxy_command_silences_the_jump_hosts():
    """The review's case: Settings force ProxyCommand, the host has a jump.
    OpenSSH applies whichever it obtains first, and Settings come first."""
    forced = {"proxycommand": "nc proxy.example.com 22"}
    state, note = override_state("ProxyJump", "bastion", forced)
    assert state == OVERRIDDEN
    assert "ProxyCommand" in note and "nc proxy.example.com 22" in note
    # An empty jump-hosts row loses nothing, so it is not marked.
    assert override_state("ProxyJump", None, forced) == (None, None)
    # `ProxyCommand none` still occupies the slot (ssh -G shows no proxy).
    assert suppressing_option("ProxyJump", {"proxycommand": "none"}) == (
        "ProxyCommand", "none",
    )


def test_a_forced_jump_silences_the_proxy_command_unless_it_is_none():
    assert override_state("ProxyCommand", "nc x 22", {"proxyjump": "gw"})[0] == OVERRIDDEN
    # `ssh -o ProxyJump=none -o ProxyCommand=…` keeps the ProxyCommand.
    assert suppressing_option("ProxyCommand", {"proxyjump": "none"}) is None


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
        "_override_row_value", "_watch_rows_for_overrides",
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


@needs_adw
def test_an_empty_field_gets_an_information_mark_and_no_banner():
    from gi.repository import Adw

    row = Adw.EntryRow(title="Username")
    dialog = _dialog(username_row=row)

    dialog._set_forced_options({"user": "admin"})

    icon = row._sshpilot_override_icon
    assert icon.get_visible() and _icon_name(icon) == "dialog-information-symbolic"
    assert "uses that value" in row.get_tooltip_text()
    assert dialog.banner_syncs == [0]

    row.set_text("alice")  # now set here, and lost to Settings
    assert _icon_name(icon) == "dialog-warning-symbolic"
    assert dialog.banner_syncs[-1] == 1


@needs_adw
def test_jump_hosts_row_is_marked_for_a_forced_proxy_command():
    from gi.repository import Adw

    jump, command = Adw.EntryRow(title="Jump hosts"), Adw.EntryRow(title="Proxy command")
    jump.set_text("bastion")
    dialog = _dialog(proxy_jump_row=jump, proxy_command_row=command)

    dialog._set_forced_options({"proxycommand": "nc proxy.example.com 22"})

    assert jump._sshpilot_override_state == OVERRIDDEN
    assert "ProxyCommand" in jump.get_tooltip_text()
    assert command._sshpilot_override_state == GLOBAL
    assert dialog.banner_syncs[-1] == 1


@needs_adw
def test_switch_off_takes_the_global_value_switch_on_against_it_is_overridden():
    from gi.repository import Adw

    row = Adw.SwitchRow(title="X11")
    dialog = _dialog(x11_row=row)
    dialog._set_forced_options({"forwardx11": "no"})
    assert row._sshpilot_override_state == GLOBAL

    row.set_active(True)  # watched: re-marked without another fetch
    assert row._sshpilot_override_state == OVERRIDDEN


@pytest.mark.parametrize(
    "fragment, winner, jump_row, command_row",
    [
        # What `ssh -G` reports for each order, checked 2026-11-12.
        (("ProxyCommand=nc proxy 22", "ProxyJump=bastion"),
         ("ProxyCommand", "nc proxy 22"), OVERRIDDEN, GLOBAL),
        (("ProxyJump=bastion", "ProxyCommand=nc proxy 22"),
         ("ProxyJump", "bastion"), GLOBAL, OVERRIDDEN),
        (("ProxyJump=none", "ProxyCommand=nc proxy 22"),
         ("ProxyCommand", "nc proxy 22"), OVERRIDDEN, GLOBAL),
    ],
)
def test_both_proxies_forced_the_first_one_decides_both_fields(
    fragment, winner, jump_row, command_row
):
    """Each field is judged by the forced proxy OpenSSH actually uses, never
    by the losing one that happens to share its keyword."""
    forced = forced_map(_snapshot(*fragment))
    assert suppressing_option("ProxyJump", forced) == winner
    assert suppressing_option("ProxyCommand", forced) == winner
    # Each row holds exactly what Settings forced under its own keyword.
    own_jump = forced["proxyjump"]
    own_command = forced["proxycommand"]
    assert override_state("ProxyJump", own_jump, forced)[0] == jump_row
    assert override_state("ProxyCommand", own_command, forced)[0] == command_row
    # An empty field takes the winner, and the note names it.
    note = override_state(winner[0], None, forced)[1]
    assert winner[1] in note


def test_proxy_jump_none_alone_leaves_the_proxy_command_alone():
    forced = forced_map(_snapshot("ProxyJump=none"))
    assert suppressing_option("ProxyJump", forced) == ("ProxyJump", "none")
    assert suppressing_option("ProxyCommand", forced) is None
