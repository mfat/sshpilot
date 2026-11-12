"""The real connection dialog marks what Settings ▸ SSH Options override."""
from __future__ import annotations

import time
import types

import pytest

from sshpilot.api.models.settings import GlobalSshOverrides

pytestmark = pytest.mark.gui


class _Manager:
    isolated_mode = False
    connections = ()

    def load_ssh_keys(self): return []
    def get_key_passphrase(self, p): return ""
    def find_connection_by_nickname(self, n): return None
    def get_password(self, h, u): return None
    def format_ssh_config_entry(self, data): return ""
    def get_metadata(self, n): return {}


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


def _pump(until, seconds=3.0):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline and not until():
        while context.pending():
            context.iteration(False)
        time.sleep(0.01)


def _dialog(*forced, ready=True):
    from gi.repository import Gtk
    from sshpilot.connection_dialog import ConnectionDialog
    from sshpilot.plugins.loader import ensure_builtin_protocols

    ensure_builtin_protocols()
    snapshot = GlobalSshOverrides(
        revision="r1", connect_timeout=0, strict_host_key_checking="",
        compression="", verbosity=0, debug_enabled=False, forced_options=forced,
    )
    parent = Gtk.Window()
    parent.connection_manager = _Manager()
    parent._daemon_ready = lambda: ready
    parent.client = types.SimpleNamespace(get_global_ssh_overrides=lambda: snapshot)
    return ConnectionDialog(
        parent, connection=None, connection_manager=parent.connection_manager
    )


def test_dialog_marks_forced_fields_and_reveals_the_banner(gui):
    dialog = _dialog("User=deploy", "ForwardX11=yes", "ServerAliveInterval=15")
    dialog.username_row.set_text("alice")
    dialog.advanced_tab.set_config_entries([("ServerAliveInterval", "90")])
    _pump(lambda: bool(getattr(dialog, "_forced_options", None)))

    assert dialog._forced_options["user"] == "deploy"
    assert dialog.username_row._sshpilot_override_icon.get_visible()
    assert dialog.x11_row._sshpilot_override_icon.get_visible()
    assert getattr(dialog.port_row, "_sshpilot_override_icon", None) is None
    (row,) = dialog.advanced_tab.option_list.rows()
    assert "15" in row.note_label.get_label()
    assert dialog._override_banner.get_revealed()
    # Solid error colours from style.css, so it is not missed.
    assert dialog._override_banner.has_css_class("error")
    # Informing only: the value stays, and stays editable.
    assert dialog.username_row.get_text() == "alice"
    assert dialog.username_row.get_sensitive()


def test_keepalive_alone_does_not_raise_the_banner(gui):
    """The default: keepalive is forced, but this connection sets none."""
    dialog = _dialog("ServerAliveInterval=15", "ServerAliveCountMax=3")
    _pump(lambda: bool(getattr(dialog, "_forced_options", None)))

    assert dialog._forced_options
    assert getattr(dialog, "_override_banner", None) is None

    dialog.advanced_tab.set_config_entries([("ServerAliveInterval", "90")])
    dialog.advanced_tab.on_entry_changed(None)
    assert dialog._override_banner.get_revealed()


def test_banner_is_hidden_for_a_non_ssh_protocol(gui):
    dialog = _dialog("User=deploy")
    dialog.username_row.set_text("alice")
    _pump(lambda: bool(getattr(dialog, "_forced_options", None)))
    assert dialog._override_banner.get_revealed()

    for index, backend in enumerate(dialog._protocol_backends):
        if backend.protocol_id != "ssh":
            dialog.protocol_row.set_selected(index)
            break
    assert not dialog._override_banner.get_revealed()


def test_no_daemon_leaves_the_dialog_unmarked(gui):
    dialog = _dialog("User=deploy", ready=False)
    _pump(lambda: False, seconds=0.3)
    assert not getattr(dialog, "_forced_options", None)
    assert getattr(dialog, "_override_banner", None) is None


def test_an_empty_username_takes_the_forced_one_without_a_banner(gui):
    """Settings decide the account, but nothing this connection set is lost."""
    dialog = _dialog("User=deploy")
    _pump(lambda: bool(getattr(dialog, "_forced_options", None)))

    icon = dialog.username_row._sshpilot_override_icon
    assert icon.get_visible()
    assert _icon_name(icon) == "dialog-information-symbolic"
    assert getattr(dialog, "_override_banner", None) is None

    dialog.username_row.set_text("alice")
    assert _icon_name(icon) == "dialog-warning-symbolic"
    assert dialog._override_banner.get_revealed()


def test_jump_host_loses_to_a_forced_proxy_command_in_the_real_dialog(gui):
    dialog = _dialog("ProxyCommand=nc proxy.example.com 22")
    dialog.proxy_jump_row.set_text("bastion")
    _pump(lambda: bool(getattr(dialog, "_forced_options", None)))

    assert _icon_name(dialog.proxy_jump_row._sshpilot_override_icon) == (
        "dialog-warning-symbolic"
    )
    assert dialog._override_banner.get_revealed()


def test_dialog_marks_a_field_an_earlier_block_beats(gui):
    """`Host *` before the host's own block decides User in OpenSSH, and the
    app launches plain `ssh <alias>`. The editor says so on the field."""
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
        full=("user deploy", "hostname web.example.com", "port 22"),
        earlier=(
            EarlierDirective(
                key="user", value="deploy", block="Host *",
                file="/home/me/.ssh/config", line=2,
            ),
        ),
    )
    snapshot = GlobalSshOverrides(
        revision="r1", connect_timeout=0, strict_host_key_checking="",
        compression="", verbosity=0, debug_enabled=False, forced_options=(),
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
        "username": "admin",
        "__authored_directives": ("hostname", "user"),
    })
    dialog = ConnectionDialog(
        parent, connection=connection, connection_manager=parent.connection_manager
    )
    _pump(lambda: bool(getattr(dialog, "_earlier_directives", None)))

    icon = dialog.username_row._sshpilot_override_icon
    assert icon.get_visible()
    assert _icon_name(icon) == "dialog-warning-symbolic"
    tooltip = dialog.username_row.get_tooltip_text()
    assert "deploy" in tooltip and "Host *" in tooltip and "line 2" in tooltip
    assert dialog._override_banner.get_revealed()
    assert "earlier block" in dialog._override_banner.get_title()
    # Informing only: the value stays, and stays editable.
    assert dialog.username_row.get_text() == "admin"
    assert dialog.username_row.get_sensitive()

    # Agreeing with the earlier block loses nothing.
    dialog.username_row.set_text("deploy")
    assert not icon.get_visible()
    assert not dialog._override_banner.get_revealed()


def test_new_connection_is_checked_against_the_alias_being_typed(gui):
    """A new block goes to the end of the file, so `Host *` anywhere in it
    decides first. The editor asks for the typed alias and marks the field."""
    from gi.repository import Gtk
    from sshpilot.api.models.connections import EarlierDirective
    from sshpilot.connection_dialog import ConnectionDialog
    from sshpilot.plugins.loader import ensure_builtin_protocols

    ensure_builtin_protocols()
    asked = []

    def earlier(host):
        asked.append(host)
        if not host.endswith(".lab"):
            return ()
        return (
            EarlierDirective(
                key="user", value="labuser", block="Host *.lab",
                file="/home/me/.ssh/config", line=2,
            ),
        )

    snapshot = GlobalSshOverrides(
        revision="r1", connect_timeout=0, strict_host_key_checking="",
        compression="", verbosity=0, debug_enabled=False, forced_options=(),
    )
    parent = Gtk.Window()
    parent.connection_manager = _Manager()
    parent._daemon_ready = lambda: True
    parent.client = types.SimpleNamespace(
        get_global_ssh_overrides=lambda: snapshot,
        get_earlier_directives=earlier,
    )
    dialog = ConnectionDialog(
        parent, connection=None, connection_manager=parent.connection_manager
    )
    dialog.username_row.set_text("admin")
    dialog.nickname_row.set_text("web.lab")
    _pump(lambda: bool(getattr(dialog, "_earlier_directives", None)))

    assert asked[-1] == "web.lab"
    tooltip = dialog.username_row.get_tooltip_text()
    assert "labuser" in tooltip and "Host *.lab" in tooltip
    assert dialog._override_banner.get_revealed()

    # Another alias, another answer: the mark goes away.
    dialog.nickname_row.set_text("web.prod")
    _pump(lambda: asked[-1] == "web.prod" and not dialog._earlier_directives)
    assert not dialog.username_row._sshpilot_override_icon.get_visible()
    assert not dialog._override_banner.get_revealed()


def test_switches_auth_method_and_local_command_are_marked_in_the_real_dialog(gui):
    """Not only text fields: every element that writes an option is judged."""
    from gi.repository import Gtk
    from sshpilot.api.models.connections import (
        EarlierDirective,
        EffectiveConfigComparison,
    )
    from sshpilot.connection_dialog import ConnectionDialog
    from sshpilot.connection_dialog_overrides import GLOBAL, OVERRIDDEN
    from sshpilot.connection_model import Connection
    from sshpilot.plugins.loader import ensure_builtin_protocols

    ensure_builtin_protocols()

    def line(key, value, number):
        return EarlierDirective(
            key=key, value=value, block="Host *",
            file="/home/me/.ssh/config", line=number,
        )

    comparison = EffectiveConfigComparison(
        connection_id="web", host="web", available=True,
        full=("hostname web.example.com",),
        earlier=(
            line("forwardx11", "yes", 2),
            line("preferredauthentications", "publickey", 3),
            line("permitlocalcommand", "no", 4),
            line("forwardagent", "yes", 5),
        ),
    )
    snapshot = GlobalSshOverrides(
        revision="r1", connect_timeout=0, strict_host_key_checking="",
        compression="", verbosity=0, debug_enabled=False, forced_options=(),
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
    _pump(lambda: bool(getattr(dialog, "_earlier_directives", None)))

    # Off, nothing authored, yet X11 is forwarded: an information mark.
    assert dialog.x11_row._sshpilot_override_state == GLOBAL
    assert "ForwardX11" in dialog.x11_row.get_tooltip_text()
    # A selector on Default while the earlier block decides.
    assert dialog.forward_agent_row._sshpilot_override_state == GLOBAL
    # Key login is still tried by "publickey": nothing to say.
    assert getattr(dialog.auth_method_row, "_sshpilot_override_state", None) is None
    assert getattr(dialog, "_override_banner", None) is None or (
        not dialog._override_banner.get_revealed()
    )

    # Password login is not in the earlier list: the choice here is not used.
    dialog.auth_toggle.set_active(1)
    assert dialog.auth_method_row._sshpilot_override_state == OVERRIDDEN
    assert dialog.auth_method_row._sshpilot_override_icon.get_visible()
    assert "publickey" in dialog.auth_method_row.get_tooltip_text()
    assert dialog._override_banner.get_revealed()
    dialog.auth_toggle.set_active(0)
    assert not dialog._override_banner.get_revealed()

    # A local command cannot run while an earlier block forbids them.
    dialog.local_command_row.set_text("notify-send connected")
    assert dialog.local_command_row._sshpilot_override_state == OVERRIDDEN
    assert "PermitLocalCommand" in dialog.local_command_row.get_tooltip_text()
