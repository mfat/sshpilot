"""Real GTK: the gtkghostty backend over the real GtkGhostty.Terminal widget.

Skipped unless the widget's typelib is installed (GI_TYPELIB_PATH and the
library path pointing at a gtkghostty build).
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from tests._gui_harness import requires_gui

Gtk, _Adw, _Gio, GLib = requires_gui()

backend_module = pytest.importorskip(
    "sshpilot.terminal_backend_gtkghostty", reason="gtkghostty is not installed"
)
GtkGhosttyTerminalBackend = backend_module.GtkGhosttyTerminalBackend

pytestmark = pytest.mark.gui


def _pump_until(predicate, seconds=20):
    context = GLib.MainContext.default()
    deadline = GLib.get_monotonic_time() + int(seconds * 1_000_000)
    while not predicate() and GLib.get_monotonic_time() < deadline:
        context.iteration(False)
    return predicate()


class _Config:
    def __init__(self, profile):
        self.profile = profile

    def get_setting(self, key, default=None):
        return {"terminal.theme": "test"}.get(key, default)

    def get_terminal_profile(self, _name):
        return self.profile


def _backend(profile=None):
    config = _Config(profile) if profile is not None else None
    backend = GtkGhosttyTerminalBackend(SimpleNamespace(config=config))
    backend.initialize()
    backend.view.set_grid_size(40, 6)
    return backend


def test_daemon_output_in_and_input_and_replies_out():
    backend = _backend()
    sent = []
    handler = backend.connect_commit(lambda terminal, data, size: sent.append((terminal, data, size)))
    backend.feed(b"hello \x1b[1mworld\x1b[0m\r\n")
    assert backend.get_content().splitlines()[0] == "hello world"
    backend.feed_child_data(b"ls\r")
    backend.feed(b"\x1b[6n")  # the emulator's reply goes to the remote too
    assert [item[1] for item in sent] == [b"ls\r", b"\x1b[2;1R"]
    assert sent[0][0] is backend and sent[0][2] == 3
    backend.disconnect(handler)
    backend.feed_child_data(b"x")
    assert len(sent) == 2
    assert backend.get_size() == (6, 40)
    backend.destroy()


def test_title_size_and_contents_callbacks():
    backend = _backend()
    titles, sizes, changes = [], [], []
    backend.connect_title_changed(lambda terminal: titles.append(terminal.get_window_title()))
    backend.connect_content_changed(lambda _terminal: changes.append(True))
    backend.connect_size_changed(lambda _terminal, columns, rows: sizes.append((columns, rows)))
    backend.feed(b"\x1b]2;remote:~\x07")
    assert titles == ["remote:~"] and changes
    assert _pump_until(lambda: (40, 6) in sizes)  # redelivered after connecting
    backend.view.set_grid_size(30, 5)
    assert sizes[-1] == (30, 5)
    backend.destroy()


def test_theme_reaches_programs_and_the_padding():
    backend = _backend({
        "foreground": "#102030",
        "background": "#f0f0f0",
        "palette": ["#000000"] * 16,
        "font": "Monospace 13",
    })
    backend.apply_theme()
    replies = []
    backend.connect_commit(lambda _t, data, _n: replies.append(data))
    backend.feed(b"\x1b]11;?\x1b\\")
    assert replies[0].startswith(b"\x1b]11;rgb:f0f0/f0f0/f0f0")
    assert backend.view.get_font_desc().get_size() == 13 * 1024
    backend.destroy()


def test_search_selects_hits_without_feeding_copy_on_select():
    backend = _backend()
    backend.feed(b"".join(b"line %d %s\r\n" % (n, b"needle" if n == 5 else b"hay") for n in range(60)))
    copyable = []
    backend.setup_link_handling(None, None, lambda *_a: copyable.append(backend.selection_change_is_copyable()))
    backend.search_set_query("NEEDLE")
    assert backend.search_find_previous()
    assert backend.get_has_selection() and backend.view.get_selected_text() == "needle"
    assert copyable and not any(copyable)  # search hits are not copied
    backend.search_set_query("ne+dle", regex=True)
    assert backend.search_find_next()
    backend.search_set_regex(None)
    assert not backend.search_find_next()
    backend.destroy()


def test_links_are_found_under_the_padded_grid():
    backend = _backend()
    window = Gtk.Window()
    window.set_default_size(500, 200)
    window.set_child(backend.widget)
    window.present()
    backend.feed(b"\x1b]8;;https://example.org/a\x1b\\link\x1b]8;;\x1b\\ then https://plain.example/b\r\n")
    assert _pump_until(lambda: backend.view.get_width() > 0)
    # Scan row 0 in widget coordinates, which include the padding.
    found = {backend.hyperlink_at(x, 10) for x in range(0, 480, 3)}
    assert "https://example.org/a" in found and "https://plain.example/b" in found
    window.destroy()
    backend.destroy()


def _shell():
    if sys.platform == "win32":
        return [os.environ.get("ComSpec") or "cmd.exe", "/d", "/q", "/k"], b"echo typed-%OS%\r", "typed-Windows_NT"
    return ["/bin/sh"], b"echo typed-$((40+2))\n", "typed-42"


def test_local_shell_round_trip():
    backend = _backend()
    started, exited = [], []
    backend.connect_child_exited(lambda _terminal, status: exited.append(status))
    argv, line, expected = _shell()
    backend.spawn_async(
        argv=argv,
        env=dict(os.environ),
        cwd=os.path.expanduser("~"),
        callback=lambda _terminal, pid, error, _data: started.append((pid, error)),
    )
    assert _pump_until(lambda: started)
    pid, error = started[0]
    assert error is None and pid > 0
    assert backend.get_child_pid() == pid and backend.local_shell_running()

    backend.feed_child_data(line)
    assert _pump_until(lambda: expected in backend.get_content()), backend.get_content()

    backend.feed_child_data(b"exit\r" if sys.platform == "win32" else b"exit\n")
    assert _pump_until(lambda: exited)
    assert not backend.local_shell_running()
    backend.destroy()
