"""The connection editor, end to end, agrees with OpenSSH.

The other editor tests feed the dialog a hand-built connection and a canned
effective-config comparison. Here nothing is faked: the session app's own
daemon reads a real configuration with ``Host *``, ``Match`` and an
``Include``, the main window opens the editor the way a click does, Save goes
through the window's real save path into the store, and the editor is opened
again on what was written. At each step the file and the form are compared
with ``ssh -G``.
"""
from __future__ import annotations

import shutil
import subprocess
import time

import pytest

pytestmark = [
    pytest.mark.gui,
    pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH"),
]

INCLUDED = "Host inc\n    HostName 192.0.2.20\n"

# Include first: `inc`'s own block is read before `Host *`, so it keeps what
# it sets and inherits the rest. `own` comes after `Host *`, so OpenSSH takes
# User from `Host *` (line 4) and the editor has to say so.
ROOT = """Include {fragments}/*.conf

Host *
    User globaluser
    Port 2222

Host own
    HostName 192.0.2.11
    User ownuser

Match originalhost inc
    ProxyJump jumpbox
"""


def _pump(until, seconds=10.0):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    deadline = time.time() + seconds
    while time.time() < deadline:
        if until():
            return True
        while context.pending():
            context.iteration(False)
        time.sleep(0.02)
    return bool(until())


def _ssh_g(root, host):
    out = subprocess.run(
        ["ssh", "-G", "-F", str(root), host],
        capture_output=True, text=True, check=True,
    ).stdout
    found = {}
    for line in out.splitlines():
        key, _sep, value = line.partition(" ")
        found.setdefault(key, value)
    return found


def _find(window, nickname):
    manager = window.connection_manager
    try:
        manager.refresh()
    except Exception:
        pass
    return manager.find_connection_by_nickname(nickname)


@pytest.fixture
def config(gui):
    """The real configuration, loaded by the session daemon; restored after."""
    from pathlib import Path

    from sshpilot.api.models.daemon import OperationMode

    # Whichever file the daemon reads in its current mode (the session app
    # runs in Isolated Mode).
    mode = gui.window.client.get_operation_mode()
    files = (
        mode.isolated_files
        if mode.active_mode == OperationMode.ISOLATED
        else mode.default_files
    )
    root = Path(files.root_config_path)
    root.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    original = root.read_bytes() if root.exists() else None
    fragments = root.parent / "roundtrip.d"
    fragments.mkdir(exist_ok=True)
    included = fragments / "inc.conf"
    included.write_text(INCLUDED)
    root.write_text(ROOT.format(fragments=fragments))
    assert _pump(lambda: _find(gui.window, "inc") and _find(gui.window, "own")), (
        "the daemon never loaded the configuration"
    )
    yield root, included
    for dialog in _editors():
        dialog.close()
    if original is None:
        root.unlink()
    else:
        root.write_bytes(original)
    shutil.rmtree(fragments)
    _pump(lambda: _find(gui.window, "inc") is None)


def _editors():
    from gi.repository import Gtk
    from sshpilot.connection_dialog import ConnectionDialog

    return [
        w for w in Gtk.Window.list_toplevels()
        if isinstance(w, ConnectionDialog) and w.get_visible()
    ]


def _open_editor(gui, nickname):
    """Open the editor the way the connection list does, and wait for the
    daemon's snapshot and its `ssh -G` resolution to reach the form."""
    before = set(map(id, _editors()))
    gui.window.show_connection_dialog(_find(gui.window, nickname))
    assert _pump(lambda: [d for d in _editors() if id(d) not in before])
    dialog = next(d for d in _editors() if id(d) not in before)
    assert _pump(lambda: getattr(dialog, "_daemon_editor_loaded", False)), (
        "the editor never received the daemon snapshot"
    )
    return dialog


def _save(dialog):
    dialog.on_save_clicked()
    assert _pump(lambda: not dialog.get_visible()), "Save did not complete"


def test_untouched_save_writes_nothing(gui, config):
    """Inherited values fill the form but are not this host's to write."""
    root, included = config
    root_before, included_before = root.read_bytes(), included.read_bytes()

    dialog = _open_editor(gui, "inc")
    assert _pump(lambda: dialog.proxy_jump_row.get_text() == "jumpbox")
    assert dialog.username_row.get_text() == "globaluser"
    assert dialog.port_row.get_text() == "2222"
    _save(dialog)

    assert root.read_bytes() == root_before
    assert included.read_bytes() == included_before


def test_an_edit_lands_in_the_included_block_and_reopens_as_openssh_resolves(
    gui, config
):
    root, included = config
    root_before = root.read_bytes()

    dialog = _open_editor(gui, "inc")
    assert _pump(lambda: dialog.proxy_jump_row.get_text() == "jumpbox")
    dialog.hostname_row.set_text("192.0.2.21")
    _save(dialog)

    # Only the authored line changed, in the file that declares the host;
    # nothing inherited was pinned into the block.
    assert _pump(lambda: "192.0.2.21" in included.read_text())
    assert included.read_text() == "Host inc\n    HostName 192.0.2.21\n"
    assert root.read_bytes() == root_before

    resolved = _ssh_g(root, "inc")
    assert resolved["hostname"] == "192.0.2.21"
    assert resolved["user"] == "globaluser"
    assert resolved["port"] == "2222"
    assert resolved["proxyjump"] == "jumpbox"

    # Reopened, the form states what `ssh inc` will use, and marks which of
    # it comes from elsewhere.
    reopened = _open_editor(gui, "inc")
    assert _pump(lambda: reopened.proxy_jump_row.get_text() == "jumpbox")
    assert reopened.hostname_row.get_text() == resolved["hostname"]
    assert reopened.username_row.get_text() == resolved["user"]
    assert reopened.port_row.get_text() == resolved["port"]
    assert not reopened.hostname_row.has_css_class("dim-label")
    for row in (reopened.username_row, reopened.port_row, reopened.proxy_jump_row):
        assert row.has_css_class("dim-label")


def test_an_earlier_host_star_is_reported_from_the_real_resolution(gui, config):
    root, _included = config
    resolved = _ssh_g(root, "own")
    assert resolved["user"] == "globaluser"  # what OpenSSH will actually use

    dialog = _open_editor(gui, "own")
    assert _pump(lambda: bool(getattr(dialog, "_earlier_directives", None)))

    # The host's own value stays in the form, flagged as losing to Host *.
    assert dialog.username_row.get_text() == "ownuser"
    icon = dialog.username_row._sshpilot_override_icon
    assert icon.get_visible()
    tooltip = dialog.username_row.get_tooltip_text()
    assert resolved["user"] in tooltip
    assert "Host *" in tooltip and "line 4" in tooltip
    assert dialog._override_banner.get_revealed()
    # Port is not authored by `own`, so it is inherited, not overridden.
    assert dialog.port_row.get_text() == resolved["port"]
    port_icon = getattr(dialog.port_row, "_sshpilot_override_icon", None)
    assert port_icon is None or not port_icon.get_visible()
    assert not dialog.port_row.has_css_class("error")
