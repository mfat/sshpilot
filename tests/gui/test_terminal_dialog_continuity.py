"""Real-VTE visual regression for a dialog stream crossing GTK backlog loss."""

from __future__ import annotations

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

import gi

gi.require_version("Vte", "3.91")
from gi.repository import Vte

from sshpilot.api.models.common import SessionId
from sshpilot.api.models.terminal import TerminalOutput
from sshpilot.api.terminal_events import TerminalSubscription
from sshpilot.gtk_client_bridge import GtkTerminalBinding

pytestmark = pytest.mark.gui


class _Client:
    def subscribe_terminal(self, _session_id, on_output, **_callbacks):
        self.on_output = on_output
        return TerminalSubscription(lambda: None)


@pytest.mark.xfail(
    strict=True,
    reason="current GTK backlog overflow drops the alternate-screen/dialog setup",
)
def test_real_vte_retains_post_flood_dialog_title_and_buttons():
    terminal = Vte.Terminal()
    window = Gtk.Window()
    window.set_default_size(800, 500)
    window.set_child(terminal)
    window.present()
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)

    client = _Client()
    dispatches = []
    binding = GtkTerminalBinding(
        client,
        SessionId("session-dialog-vte"),
        dispatcher=dispatches.append,
        on_output=lambda output: terminal.feed(output.data),
        on_continuity_lost=lambda *_loss: terminal.feed(
            b"\r\n[Earlier terminal output is no longer available]\r\n"
        ),
        on_eof=None,
        on_error=None,
        max_pending_bytes=64,
        on_close=lambda _binding: None,
    )
    chunks = (
        b"output-line-00000001\r\n" * 2,
        b"output-line-00000003\r\n",
        b"\x1b[?1049h\x1b[2J\x1b[8;16HPOST-FLOOD DIALOG",
        b"\x1b[15;29H<Yes>   <No>",
    )
    try:
        sequence = 0
        for chunk in chunks:
            client.on_output(
                TerminalOutput(
                    session_id=SessionId("session-dialog-vte"),
                    sequence=sequence,
                    data=chunk,
                )
            )
            sequence += len(chunk)
        dispatches.pop()()
        while context.pending():
            context.iteration(False)
        content = terminal.get_text_format(Vte.Format.TEXT)
        text = content[0] if isinstance(content, tuple) else content
    finally:
        binding.close()
        window.destroy()

    assert "POST-FLOOD DIALOG" in text
    assert "<Yes>" in text
    assert "<No>" in text
