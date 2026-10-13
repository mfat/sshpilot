"""Dropping a connection on a terminal focuses the new split pane.

Focus is still on the drag source (the sidebar) when the drop lands, and an
SSH pane's spawn does not grab it, so without an explicit hand-off no pane
ends up focused.

Opt-in only: SSHPILOT_GUI_TESTS=1 pytest -m gui
"""

import socket

import pytest

from tests._gui_harness import requires_gui  # the `gui` fixture comes from conftest

requires_gui()

pytestmark = pytest.mark.gui


def _is_inside(widget, ancestor):
    while widget is not None:
        if widget is ancestor:
            return True
        widget = widget.get_parent()
    return False


def test_connection_drop_on_terminal_focuses_dropped_pane(gui, monkeypatch):
    from sshpilot.dnd_payload import encode_dnd_payload

    # Accepts but never answers, so ssh stays alive waiting for a banner.
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    try:
        win = gui.window
        port = server.getsockname()[1]
        assert win.open_cli_connect(["ssh", "-p", str(port), "probe@127.0.0.1"])
        gui.pump(1000)
        terminal = win.tab_view.get_selected_page().get_child()
        monkeypatch.setattr(
            win.connection_manager, "find_connection_by_nickname",
            lambda _nick: terminal.connection,
        )
        win.connection_list.grab_focus()
        gui.pump(100)

        payload = encode_dnd_payload({"type": "connection", "connection_nickname": "x"})
        assert terminal._convert_to_split_dt.emit("drop", payload, 10.0, 10.0)
        gui.pump(1000)

        split = win.tab_view.get_selected_page().get_child()
        assert _is_inside(win.get_focus(), split._panes[1])
    finally:
        server.close()
