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
