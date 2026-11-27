"""The real connection dialog marks its fields from OpenSSH's answer for the edit.

OpenSSH itself answers (``tests.editor_answer_rig``): the daemon's service on
a temporary configuration, with Settings ▸ SSH Options as given.
"""
from __future__ import annotations

import time

import pytest


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


def _parent(text="", forced=(), ready=True):
    from gi.repository import Gtk
    from sshpilot.plugins.loader import ensure_builtin_protocols
    from tests.editor_answer_rig import RealConfigClient

    ensure_builtin_protocols()
    parent = Gtk.Window()
    parent.connection_manager = _Manager()
    parent._daemon_ready = lambda: ready
    # OpenSSH itself answers, through the daemon's service, on *text*.
    parent.client = RealConfigClient(text, forced=forced)
    return parent


def _dialog(*forced, ready=True, text="", nickname="web"):
    """A new connection named *nickname* (its block goes to the end of
    *text*), with *forced* as Settings ▸ SSH Options."""
    from sshpilot.connection_dialog import ConnectionDialog

    parent = _parent(text, forced, ready)
    dialog = ConnectionDialog(
        parent, connection=None, connection_manager=parent.connection_manager
    )
    if nickname:
        dialog.nickname_row.set_text(nickname)
    return dialog


def _saved(text, **fields):
    """The dialog on the saved host ``web`` of *text*."""
    from sshpilot.connection_dialog import ConnectionDialog
    from sshpilot.connection_model import Connection

    parent = _parent(text)
    connection = Connection({
        "nickname": "web", "host": "web", "hostname": "web.example.com",
        "__authored_directives": ("hostname",) + tuple(fields.pop("authored", ())),
        **fields,
    })
    return ConnectionDialog(
        parent, connection=connection, connection_manager=parent.connection_manager
    )


def _state(row):
    return getattr(row, "_sshpilot_override_state", None)


def _tag(row):
    label = getattr(row, "_sshpilot_override_pill", None)
    return label.get_label() if label is not None and label.get_visible() else ""


def test_dialog_marks_forced_fields_and_reveals_the_banner(gui):
    dialog = _dialog("User=deploy", "ForwardX11=yes", "ServerAliveInterval=15")
    dialog.username_row.set_text("alice")
    dialog.advanced_tab.set_option("ServerAliveInterval", "90")
    _pump(lambda: _state(dialog.username_row) == "overridden")

    assert _tag(dialog.username_row) == "Overridden"
    assert "deploy" in dialog.username_row.get_tooltip_text()
    assert dialog.username_row.has_css_class("warning")
    # Off, and Settings forward X11: the row names who decides.
    assert _state(dialog.x11_row) == "global"
    assert _tag(dialog.x11_row) == "Settings ▸ SSH Options"
    assert _state(dialog.port_row) != "overridden"
    (row,) = dialog.advanced_tab.option_list.rows()
    assert "15" in row.note_label.get_label()
    assert dialog._override_banner.get_revealed()
    # A plain banner (nothing is wrong), with a way to the deciding lines.
    assert not dialog._override_banner.has_css_class("error")
    assert dialog._override_banner.get_button_label() == "Advanced Configuration"
    # Informing only: the value stays, and stays editable.
    assert dialog.username_row.get_text() == "alice"
    assert dialog.username_row.get_sensitive()


def test_keepalive_alone_does_not_raise_the_banner(gui):
    """The default: keepalive is forced, but this connection sets none."""
    dialog = _dialog("ServerAliveInterval=15", "ServerAliveCountMax=3")
    _pump(lambda: getattr(dialog, "_editor_answer", None) is not None)

    assert dialog._editor_answer.forced
    assert getattr(dialog, "_override_banner", None) is None

    dialog.advanced_tab.set_option("ServerAliveInterval", "90")
    _pump(lambda: getattr(dialog, "_override_banner", None) is not None
          and dialog._override_banner.get_revealed())
    assert dialog._override_banner.get_revealed()


def test_banner_is_hidden_for_a_non_ssh_protocol(gui):
    dialog = _dialog("User=deploy")
    dialog.username_row.set_text("alice")
    _pump(lambda: _state(dialog.username_row) == "overridden")
    assert dialog._override_banner.get_revealed()

    for index, backend in enumerate(dialog._protocol_backends):
        if backend.protocol_id != "ssh":
            dialog.protocol_row.set_selected(index)
            break
    assert not dialog._override_banner.get_revealed()


def test_no_daemon_leaves_the_dialog_unmarked(gui):
    dialog = _dialog("User=deploy", ready=False)
    dialog.username_row.set_text("alice")
    _pump(lambda: False, seconds=0.5)
    assert getattr(dialog, "_editor_answer", None) is None
    assert getattr(dialog, "_override_banner", None) is None


def test_an_empty_username_takes_the_forced_one_without_a_banner(gui):
    """Settings decide the account, but nothing this connection set is lost."""
    dialog = _dialog("User=deploy")
    _pump(lambda: _state(dialog.username_row) == "global")

    assert _tag(dialog.username_row) == "Settings ▸ SSH Options"
    assert getattr(dialog, "_override_banner", None) is None

    dialog.username_row.set_text("alice")
    _pump(lambda: _state(dialog.username_row) == "overridden")
    assert _tag(dialog.username_row) == "Overridden"
    assert dialog._override_banner.get_revealed()


def test_jump_host_loses_to_a_forced_proxy_command_in_the_real_dialog(gui):
    dialog = _dialog("ProxyCommand=nc proxy.example.com 22")
    dialog.proxy_jump_row.set_text("bastion")
    _pump(lambda: _state(dialog.proxy_jump_row) == "overridden")
    assert _state(dialog.proxy_jump_row) == "overridden"
    assert dialog._override_banner.get_revealed()


def test_dialog_marks_a_field_an_earlier_block_beats(gui):
    """`Host *` before the host's own block decides User in OpenSSH, and the
    app launches plain `ssh <alias>`. The editor marks the field as Advanced
    Configuration marks the line."""
    dialog = _saved(
        "Host *\n    User deploy\n\nHost web\n    HostName web.example.com\n"
        "    User admin\n",
        username="admin", authored=("user",),
    )
    _pump(lambda: _state(dialog.username_row) == "overridden")

    assert _tag(dialog.username_row) == "No effect"
    # The note follows the field's own check ("Valid username").
    assert dialog.username_row.get_tooltip_text().endswith(
        "\nOpenSSH uses deploy from line 2 (Host *)"
    )
    assert dialog._override_banner.get_revealed()
    # Advanced Configuration's own sentence for the same line.
    assert dialog._override_banner.get_title() == (
        "1 directive will not take effect due to configuration precedence."
    )
    # Informing only: the value stays, and stays editable.
    assert dialog.username_row.get_text() == "admin"
    assert dialog.username_row.get_sensitive()
    # Struck and tinted, not red: red is for a line OpenSSH rejects. Save
    # is not blocked.
    assert dialog.username_row.has_css_class("warning")
    assert not dialog.username_row.has_css_class("error")
    assert dialog.username_row.get_delegate()._sshpilot_struck
    assert dialog.save_button.get_sensitive()

    # Cleared, the field sets nothing and names the block that decides.
    dialog.username_row.set_text("")
    _pump(lambda: _state(dialog.username_row) == "global")
    # Emptied: the tag says what the row gets, as a placeholder cannot.
    assert _tag(dialog.username_row) == "Not set (deploy, from Host *)"
    assert not dialog._override_banner.get_revealed()


def test_issue_1406_extra_options_lists_what_the_host_inherits(gui):
    """A `Host *` after the host's block: the Extra Options page lists the
    option it sets, where it comes from, and can set it for this host."""
    dialog = _saved(
        "Host web\n    HostName web.example.com\n\nHost *\n    Compression yes\n"
    )
    tab = dialog.advanced_tab
    _pump(lambda: tab.inherited_group.get_visible())

    assert tab.inherited_group.get_visible()
    (row,) = tab._inherited_rows
    assert (row.get_title(), row.get_subtitle()) == ("Compression", "yes")
    assert tab._inherited_shown == (("Compression", "yes", "line 5 (Host *)"),)

    tab.set_option("Compression", "no")
    _pump(lambda: not tab.inherited_group.get_visible())
    assert not tab.inherited_group.get_visible()
    assert tab.get_extra_ssh_config() == "Compression no"


def test_the_config_button_opens_advanced_configuration_over_the_dialog(gui):
    """The dialog is modal, so what it opens must be opened over it to be
    usable. Its one way into the configuration is Advanced Configuration,
    at this host's block; OpenSSH is asked again after a save there."""
    from sshpilot.connection_dialog import ConnectionDialog
    from sshpilot.connection_model import Connection

    opened = []
    parent = _parent()
    parent.open_advanced_config = lambda **target: opened.append(target)

    new = ConnectionDialog(parent, connection_manager=parent.connection_manager)
    assert new.advanced_config_button.get_visible()
    assert not hasattr(new, "edit_ssh_config_button")       # the two older buttons are gone
    assert not hasattr(new, "effective_config_button")
    new.advanced_config_button.emit("activated")
    assert opened[-1]["over"] is new and opened[-1]["host"] == ""   # no block yet
    new.close()

    connection = Connection({
        "nickname": "web", "host": "web", "hostname": "web.example.com",
        "username": "admin", "source": "/home/u/.ssh/conf.d/web.conf",
        "__authored_directives": ("hostname", "user"),
    })
    dialog = ConnectionDialog(
        parent, connection=connection, connection_manager=parent.connection_manager
    )
    dialog.advanced_config_button.emit("activated")
    target = opened[-1]
    assert target["over"] is dialog and target["host"] == "web"
    assert target["file"] == "/home/u/.ssh/conf.d/web.conf"
    asked = []
    dialog._schedule_effective_refresh = lambda delay=None: asked.append(delay)
    target["on_saved"]()  # Advanced Configuration saved
    assert asked == [0]
    dialog.close()


def test_new_connection_is_checked_against_the_alias_being_typed(gui):
    """A new block goes to the end of the file, so `Host *` anywhere in it
    decides first. The editor asks about the typed alias and marks the field."""
    dialog = _dialog(text="Host *.lab\n    User labuser\n", nickname="")
    dialog.username_row.set_text("admin")
    dialog.nickname_row.set_text("web.lab")
    _pump(lambda: _state(dialog.username_row) == "overridden")

    asked = dialog.parent_window.client.questions
    assert asked[-1].create.nickname == "web.lab"
    tooltip = dialog.username_row.get_tooltip_text()
    assert "labuser" in tooltip and "Host *.lab" in tooltip
    assert dialog._override_banner.get_revealed()

    # Another alias, another answer: the mark goes away.
    dialog.nickname_row.set_text("web.prod")
    _pump(lambda: _state(dialog.username_row) is None)
    assert _tag(dialog.username_row) == ""
    assert not dialog._override_banner.get_revealed()


def test_switches_auth_method_and_local_command_are_marked_in_the_real_dialog(gui):
    """Not only text fields: every element that writes an option is judged."""
    from sshpilot.connection_dialog_overrides import GLOBAL, OVERRIDDEN

    dialog = _saved(
        "Host *\n    ForwardX11 yes\n    PreferredAuthentications publickey\n"
        "    PermitLocalCommand no\n    ForwardAgent yes\n\n"
        "Host web\n    HostName web.example.com\n"
    )
    _pump(lambda: _state(dialog.x11_row) == GLOBAL)

    # Off, nothing authored, yet X11 is forwarded: the row names the block.
    assert _tag(dialog.x11_row) == "from Host *"
    assert "yes" in dialog.x11_row.get_tooltip_text()
    # A selector on Default while the earlier block decides.
    assert _state(dialog.forward_agent_row) == GLOBAL
    # The host sets no order; the row shows the one in force: nothing to say.
    assert _state(dialog.auth_order_row) is None
    assert getattr(dialog, "_override_banner", None) is None or (
        not dialog._override_banner.get_revealed()
    )

    # Password is not in the inherited order: checking it gives the host an
    # order of its own (publickey,password), which the earlier one beats.
    assert dialog.auth_password_row._check.get_sensitive()
    dialog.auth_password_row._check.set_active(True)
    assert dialog.auth_policy.policy.order == ["publickey", "password"]
    _pump(lambda: _state(dialog.auth_order_row) == OVERRIDDEN)
    assert _tag(dialog.auth_order_row) == "No effect"
    assert "publickey" in dialog.auth_order_row.get_tooltip_text()
    assert dialog._override_banner.get_revealed()
    # The header shows the order set here; the line below the list states
    # what is really tried.
    assert dialog.auth_order_row.get_subtitle() == "publickey, password"
    assert dialog.auth_policy.effective_row.get_subtitle() == "publickey"
    dialog.auth_policy.reset_button.emit("clicked")
    _pump(lambda: not dialog._override_banner.get_revealed())
    assert not dialog._override_banner.get_revealed()

    # A local command cannot run while an earlier block forbids them.
    dialog.local_command_row.set_text("notify-send connected")
    _pump(lambda: _state(dialog.local_command_row) == OVERRIDDEN)
    assert "PermitLocalCommand" in dialog.local_command_row.get_tooltip_text()


def test_advanced_configuration_opened_from_a_modal_dialog_is_an_ordinary_window(gui):
    """Opened from the connection editor it was a modal child of that
    dialog, which a desktop does not let be maximized. It is the app's one
    Advanced Configuration window, of the main window, in a window group of
    its own so that the modal dialog does not stop input to it."""
    from gi.repository import Gtk

    main = gui.window
    editor = Gtk.Window(transient_for=main, modal=True)     # stands for the editor
    editor.present()
    try:
        window = main.open_advanced_config(host="nowhere", over=editor)
        assert window is not None, "Advanced Configuration did not open"
        assert not window.get_modal() and window.get_resizable()
        assert window.get_transient_for() is main
        assert window.get_group() is not main.get_group()
        assert window.get_group() is not editor.get_group()
        assert main.open_advanced_config(over=editor) is window     # still only one
        assert main._open_advanced_config() is window               # the menu's too
        window.close()
        _pump(lambda: main._advanced_config_window is None)
        assert main._advanced_config_window is None                 # closed: forgotten
        fresh = main._open_advanced_config()                        # so the next one is new
        assert fresh is not None and fresh is not window
        fresh.close()
    finally:
        editor.destroy()


def test_a_key_added_while_an_inherited_value_shows_is_saved(gui, tmp_path):
    """Regression: an answer refilling an inherited row re-baselined the
    whole form, so the added key was not in the save."""
    key = tmp_path / "kwp4"
    key.write_text("x")
    dialog = _saved("Host web\n    HostName web.example.com\n\nHost *\n    User shared\n")
    _pump(lambda: "username_row" in (getattr(dialog, "_inherited_row_handlers", None) or {}))
    asked = len(dialog.parent_window.client.questions)
    dialog.key_editor.add_path(str(key))
    _pump(lambda: len(dialog.parent_window.client.questions) > asked)
    _pump(lambda: False, seconds=0.5)
    data = dialog._collect_connection_data()
    assert "identity_files" in data["__changed_fields"]
    assert "username" not in data["__changed_fields"]
