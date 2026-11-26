"""The authentication section inside the real connection dialog.

* The "OpenSSH will try" line is what shows while the method list is folded:
  when an earlier block overrules a method set here, the line is there, red,
  and says which row is not used and why (the note of the row inside).
* A linked login profile locks what it sets -- the order, single methods --
  and nothing else; unlinking gives everything back.
"""
from __future__ import annotations

import types

import pytest

from sshpilot.api.models.login_profiles import (
    LoginProfileSettings,
    LoginProfileSnapshot,
    LoginProfileSummary,
)
from sshpilot.api.models.settings import GlobalSshOverrides
from tests.test_gui_auth_section_widgets import gtk  # noqa: F401 - fixture
from tests.test_gui_connection_dialog_overrides import _Manager, _pump

pytestmark = pytest.mark.gui

SSH_G = {
    "gssapiauthentication": "no",
    "hostbasedauthentication": "no",
    "pubkeyauthentication": "true",
    "kbdinteractiveauthentication": "yes",
    "passwordauthentication": "yes",
}

ORDER = "lp-aaaaaaaaaaaaaaaa"
SWITCH = "lp-bbbbbbbbbbbbbbbb"
PLAIN = "lp-cccccccccccccccc"

PROFILES = LoginProfileSnapshot(
    available=True,
    profiles=(
        LoginProfileSummary(id=ORDER, revision=1, settings=LoginProfileSettings(
            name="Password first",
            extra_ssh_config="PreferredAuthentications password,publickey",
        )),
        LoginProfileSummary(id=SWITCH, revision=1, settings=LoginProfileSettings(
            name="No Kerberos", extra_ssh_config="GSSAPIAuthentication no",
        )),
        LoginProfileSummary(id=PLAIN, revision=1, settings=LoginProfileSettings(
            name="Deploy", username="deploy",
        )),
    ),
)


def _dialog(*earlier, forced=()):
    """The real dialog on a saved host, *earlier* directives of a ``Host *``
    above it, and ``ssh -G``'s answer for a host with no order of its own."""
    from gi.repository import Gtk

    from sshpilot.api.models.connections import (
        EarlierDirective,
        EffectiveConfigComparison,
    )
    from sshpilot.connection_dialog import ConnectionDialog
    from sshpilot.connection_model import Connection
    from sshpilot.plugins.loader import ensure_builtin_protocols

    ensure_builtin_protocols()
    comparison = EffectiveConfigComparison(
        connection_id="web", host="web", available=True,
        full=("hostname web.example.com",),
        earlier=tuple(
            EarlierDirective(
                key=key, value=value, block="Host *",
                file="/home/me/.ssh/config", line=number,
            )
            for number, (key, value) in enumerate(earlier, start=2)
        ),
    )
    snapshot = GlobalSshOverrides(
        revision="r1", connect_timeout=0, strict_host_key_checking="",
        compression="", verbosity=0, debug_enabled=False, forced_options=forced,
    )
    parent = Gtk.Window()
    parent.connection_manager = _Manager()
    parent._daemon_ready = lambda: True
    parent.client = types.SimpleNamespace(
        get_global_ssh_overrides=lambda: snapshot,
        get_effective_config=lambda _cid: comparison,
    )
    connection = Connection({
        "nickname": "web", "host": "web", "hostname": "web.example.com",
        "__authored_directives": ("hostname",),
    })
    dialog = ConnectionDialog(
        parent, connection=connection, connection_manager=parent.connection_manager
    )
    # The dialog fills itself in once it is up (the section gets the host's
    # own lines then); acting before that would be undone by the load.
    blank = dialog.auth_policy.policy
    _pump(lambda: dialog.auth_policy.policy is not blank)
    assert dialog.auth_policy.policy is not blank, "the connection never loaded"
    if earlier:
        _pump(lambda: bool(getattr(dialog, "_earlier_directives", None)))
    resolved = dict(SSH_G)
    resolved.update(dict(earlier))
    dialog.auth_policy.policy.set_inherited(resolved)
    dialog.auth_policy.sync()
    return dialog


def _link(dialog, profile_id):
    """Pick a profile in the dialog's Profile row (``None``: no profile)."""
    dialog._on_login_profile_snapshot(PROFILES)
    choices = dialog._login_profile_choices
    for index, choice in enumerate(choices):
        if choice.profile_id == profile_id and (profile_id or choice.mode is None):
            dialog.login_profile_row.set_selected(index)
            _pump(lambda: False, seconds=0.1)
            return
    raise AssertionError(f"no choice for {profile_id!r}")


# -- the line under the folded list ------------------------------------------------

def test_nothing_set_here_shows_no_line(gtk):
    dialog = _dialog(("passwordauthentication", "no"))
    section = dialog.auth_policy
    assert not dialog.auth_order_row.get_expanded()
    # The earlier block's "no" is simply what the host inherits.
    assert dialog.auth_order_row.get_subtitle() == "publickey, keyboard-interactive"
    assert not section.preview.get_visible()
    assert not section.effective_row.has_css_class("error")


def test_an_overruled_method_is_announced_by_the_line_while_the_list_is_folded(gtk):
    from sshpilot.connection_dialog_overrides import OVERRIDDEN

    dialog = _dialog(("passwordauthentication", "no"))
    section = dialog.auth_policy
    check = dialog.auth_password_row._check
    assert not check.get_active()
    # Checking it writes "PasswordAuthentication yes" here, which the
    # earlier block beats: checked, but not tried.
    check.set_active(True)
    assert "PasswordAuthentication yes" in section.policy.to_lines()
    assert dialog.auth_password_row._sshpilot_override_state == OVERRIDDEN
    assert dialog._override_banner.get_revealed()

    # The row with the mark is inside the folded list. What the banner's
    # "marked below" points at is the line under it.
    assert not dialog.auth_order_row.get_expanded()
    assert section.preview.get_visible()
    assert section.effective_row.has_css_class("error")
    assert "password" in dialog.auth_order_row.get_subtitle()
    assert "password" not in section.effective_row.get_subtitle()
    tip = section.effective_row.get_tooltip_markup()
    # It names the row, then gives that row's own note.
    assert tip.startswith(f"<b>{dialog.auth_password_row.get_title()}</b>\n")
    assert "PasswordAuthentication" in tip and "Host *" in tip
    assert "line 2" in tip

    # Unchecking removes the line set here: nothing is overruled any more.
    check.set_active(False)
    assert not section.preview.get_visible()
    assert not section.effective_row.has_css_class("error")
    assert section.effective_row.get_tooltip_markup() is None
    assert not dialog._override_banner.get_revealed()


def test_an_order_an_earlier_block_beats_is_named_in_the_line(gtk):
    dialog = _dialog(("preferredauthentications", "publickey"))
    section = dialog.auth_policy
    dialog.auth_password_row._check.set_active(True)
    assert section.policy.order == ["publickey", "password"]
    assert section.preview.get_visible()
    assert section.effective_row.has_css_class("error")
    assert section.effective_row.get_subtitle() == "publickey"
    tip = section.effective_row.get_tooltip_markup()
    assert tip.startswith(f"<b>{dialog.auth_order_row.get_title()}</b>\n")
    assert "PreferredAuthentications" in tip


def test_settings_forcing_the_order_is_named_in_the_line_too(gtk):
    dialog = _dialog(forced=("PreferredAuthentications=publickey",))
    section = dialog.auth_policy
    _pump(lambda: bool(getattr(dialog, "_forced_options", None)))
    dialog.auth_password_row._actions.activate_action("top", None)
    assert section.policy.order[0] == "password"
    assert section.preview.get_visible()
    assert section.effective_row.has_css_class("error")
    assert section.effective_row.get_subtitle() == "publickey"
    tip = section.effective_row.get_tooltip_markup()
    assert tip.startswith(f"<b>{dialog.auth_order_row.get_title()}</b>\n")
    assert "Settings" in tip


# -- a linked login profile ----------------------------------------------------------

def test_a_profile_that_sets_the_order_locks_the_whole_list(gtk):
    dialog = _dialog()
    section = dialog.auth_policy
    assert section._editable()
    # The host has an order of its own.
    section.rows["password"]._actions.activate_action("top", None)
    own = section.policy.to_lines()
    assert own and section.reset_button.get_sensitive()

    _link(dialog, ORDER)
    note = section._lock_note
    assert "Password first" in note
    assert section._order_locked and not section._editable()
    for method, row in section.rows.items():
        assert not row._check.get_sensitive(), method
        assert row._check.get_tooltip_text() == note, method
        for name in ("up", "down", "top"):
            assert not row._actions.lookup_action(name).get_enabled(), (method, name)
    assert not section.reset_button.get_sensitive()
    assert section.reset_button.get_tooltip_text() == note
    # Nothing can be picked up either, and a refused click changes nothing.
    assert section._on_drag_prepare(None, 4, 4, section.rows["password"]) is None
    before = section.policy.to_lines()
    section.rows["password"]._check.activate()
    assert section.policy.to_lines() == before

    _link(dialog, None)
    # Unlinked: everything is the connection's again, its own order intact.
    assert section.policy.to_lines() == own
    assert not section._order_locked and section._editable()
    assert section.rows["password"]._check.get_sensitive()
    assert section.rows["password"]._check.get_tooltip_text() != note
    assert section.reset_button.get_sensitive()


def test_a_profile_that_sets_one_method_locks_only_that_method(gtk):
    dialog = _dialog()
    section = dialog.auth_policy
    _link(dialog, SWITCH)
    assert not section._order_locked and section._editable()
    assert section._locked_methods == {"gssapi-with-mic"}
    # Kerberos is off for this host; turning it on is the profile's to say.
    kerberos = section.rows["gssapi-with-mic"]._check
    assert not kerberos.get_active() and not kerberos.get_sensitive()
    assert "No Kerberos" in kerberos.get_tooltip_text()
    # Every other method is the connection's own.
    password = section.rows["password"]._check
    assert password.get_sensitive()
    password.set_active(False)
    assert "password" not in section.policy.checked()
    assert section.rows["publickey"]._actions.lookup_action("down").get_enabled()

    _link(dialog, None)
    assert section._locked_methods == set()
    assert kerberos.get_sensitive()


def test_a_profile_without_authentication_lines_locks_nothing_in_the_section(gtk):
    dialog = _dialog()
    section = dialog.auth_policy
    _link(dialog, PLAIN)
    assert dialog.username_row.get_text() == "deploy"
    assert not section._order_locked and section._locked_methods == set()
    for method in ("publickey", "password", "keyboard-interactive"):
        assert section.rows[method]._check.get_sensitive(), method


def test_switching_profiles_moves_the_lock(gtk):
    dialog = _dialog()
    section = dialog.auth_policy
    _link(dialog, ORDER)
    assert section._order_locked
    _link(dialog, SWITCH)
    assert not section._order_locked
    assert section._locked_methods == {"gssapi-with-mic"}
    assert "No Kerberos" in section._lock_note
