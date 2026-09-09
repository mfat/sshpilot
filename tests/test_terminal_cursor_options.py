"""Cursor shape / blink preference (Ptyxis' Cursor group, both backends).

The preference is the *default* presentation: a program that sends DECSCUSR
still wins while it runs. That is why only terminal setup, a fresh page load,
and an explicit preference change apply it -- xterm.js stores a program's
DECSCUSR choice in the very same ``term.options.cursorStyle`` we write, so a
broader "reapply everything" sweep would silently undo it mid-session.
"""

import types

import pytest

from sshpilot.terminal_cursor import (
    CURSOR_BLINK_MODES,
    CURSOR_SHAPES,
    normalize_cursor_blink,
    normalize_cursor_shape,
    xterm_cursor_style,
)


@pytest.mark.parametrize("nick", CURSOR_SHAPES)
def test_every_offered_shape_survives_normalisation(nick):
    assert normalize_cursor_shape(nick) == nick


@pytest.mark.parametrize("nick", CURSOR_BLINK_MODES)
def test_every_offered_blink_mode_survives_normalisation(nick):
    assert normalize_cursor_blink(nick) == nick


@pytest.mark.parametrize("value", [None, "", "bar", "beam", 7, "BLOCK "])
def test_unknown_shapes_fall_back_to_block(value):
    """"bar" is xterm.js' spelling of the I-beam and is not our vocabulary; a
    config carrying it must not leave the widget without a cursor."""
    assert normalize_cursor_shape(value) == "block"


def test_shape_nicks_are_case_insensitive():
    assert normalize_cursor_shape(" IBeam ") == "ibeam"


def test_legacy_true_becomes_follow_system():
    """``cursor_blink: True`` shipped as a default nothing ever read -- both
    call sites hardcoded ``CursorBlinkMode.ON``. It is not a user choice, so it
    must not pin existing installs to "on" and keep ignoring the system's own
    blink setting."""
    assert normalize_cursor_blink(True) == "system"


def test_legacy_false_becomes_off():
    """Only a hand edit could produce it, and it says what it means."""
    assert normalize_cursor_blink(False) == "off"


def test_the_ibeam_is_spelled_bar_for_xterm_js():
    assert xterm_cursor_style("ibeam") == "bar"
    assert xterm_cursor_style("block") == "block"
    assert xterm_cursor_style("underline") == "underline"


# --- VTE backend ----------------------------------------------------------

gi = pytest.importorskip("gi")

from sshpilot import terminal_backends  # noqa: E402
from sshpilot.terminal_backends import (  # noqa: E402
    PyXtermBridgeBackend,
    PyXtermTerminalBackend,
    VTETerminalBackend,
    xterm_cursor_blink_enabled,
)


def _vte_backend():
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    calls = {}
    backend.vte = types.SimpleNamespace(
        set_cursor_shape=lambda value: calls.__setitem__("shape", value),
        set_cursor_blink_mode=lambda value: calls.__setitem__("blink", value),
    )
    return backend, calls


@pytest.mark.parametrize(
    "nick,member",
    [("block", "BLOCK"), ("ibeam", "IBEAM"), ("underline", "UNDERLINE")],
)
def test_vte_shape_mapping(nick, member):
    backend, calls = _vte_backend()

    backend.set_cursor_options(nick, "on")

    assert calls["shape"] == getattr(terminal_backends.Vte.CursorShape, member)


@pytest.mark.parametrize(
    "nick,member",
    [("system", "SYSTEM"), ("on", "ON"), ("off", "OFF")],
)
def test_vte_blink_mapping(nick, member):
    backend, calls = _vte_backend()

    backend.set_cursor_options("block", nick)

    assert calls["blink"] == getattr(terminal_backends.Vte.CursorBlinkMode, member)


def test_vte_defaults_to_block_and_system():
    """initialize() calls this with no arguments, before any config is read.
    "system" honours the desktop's own blink setting, which the previously
    hardcoded ``ON`` overrode with no way to opt out."""
    backend, calls = _vte_backend()

    backend.set_cursor_options()

    assert calls["shape"] == terminal_backends.Vte.CursorShape.BLOCK
    assert calls["blink"] == terminal_backends.Vte.CursorBlinkMode.SYSTEM


def test_vte_configure_applies_the_settings_it_is_given():
    """configure() used to hardcode block + blinking and ignore its mapping."""
    backend, calls = _vte_backend()
    for name in (
        "set_scrollback_lines", "set_scroll_on_keystroke", "set_scroll_on_output",
        "set_mouse_autohide", "set_allow_hyperlink", "set_enable_fallback_scrolling",
        "show",
    ):
        setattr(backend.vte, name, lambda *args: None)

    backend.configure({"cursor_shape": "underline", "cursor_blink": "off"})

    assert calls["shape"] == terminal_backends.Vte.CursorShape.UNDERLINE
    assert calls["blink"] == terminal_backends.Vte.CursorBlinkMode.OFF


def test_vte_never_raises_on_an_old_widget():
    """It runs while a terminal is being wired up."""
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace()

    backend.set_cursor_options("ibeam", "on")


# --- PyXterm backend ------------------------------------------------------


def _pyxterm_backend():
    backend = PyXtermTerminalBackend.__new__(PyXtermTerminalBackend)
    backend.available = True
    backend._cursor_shape = "block"
    backend._cursor_blink = "system"
    scripts = []
    backend._run_javascript = scripts.append
    return backend, scripts


def test_pyxterm_writes_both_options():
    backend, scripts = _pyxterm_backend()

    backend.set_cursor_options("ibeam", "off")

    assert len(scripts) == 1
    assert "window.term.options.cursorStyle = 'bar';" in scripts[0]
    assert "window.term.options.cursorBlink = false;" in scripts[0]


def test_pyxterm_resolves_follow_system(monkeypatch):
    """xterm.js has no "system" blink mode, so GTK's setting is read for it."""
    monkeypatch.setattr(terminal_backends, "system_cursor_blink_enabled", lambda: False)
    backend, scripts = _pyxterm_backend()

    backend.set_cursor_options("block", "system")

    assert "window.term.options.cursorBlink = false;" in scripts[0]


def test_pyxterm_stores_the_nicks_for_the_next_page_load():
    """The page is re-seeded on load the way the theme and font are."""
    backend, _scripts = _pyxterm_backend()

    backend.set_cursor_options("underline", "on")

    assert (backend._cursor_shape, backend._cursor_blink) == ("underline", "on")


def test_pyxterm_stores_the_nicks_even_when_unavailable():
    """No WebView yet is the normal state at construction time."""
    backend, scripts = _pyxterm_backend()
    backend.available = False

    backend.set_cursor_options("ibeam", "off")

    assert (backend._cursor_shape, backend._cursor_blink) == ("ibeam", "off")
    assert scripts == []


def test_system_blink_defaults_to_on_without_gtk_settings(monkeypatch):
    """Gtk.Settings.get_default() returns None outside a display."""
    monkeypatch.setattr(
        terminal_backends.Gtk.Settings, "get_default", staticmethod(lambda: None)
    )

    assert xterm_cursor_blink_enabled("system") is True


def test_the_preference_is_seeded_before_the_preready_flush():
    """xterm.js parks a program's DECSCUSR in the same ``cursorStyle`` option
    we write, and output buffered before the page was ready can carry one (a
    prompt that picks its own cursor). Writing the preference after that flush
    would race the parser for it, so the seed must go out first."""
    backend = PyXtermBridgeBackend.__new__(PyXtermBridgeBackend)
    backend.available = True
    backend._cursor_shape = "underline"
    backend._cursor_blink = "off"
    backend._preready_output = ["\x1b[5 q$ "]
    backend._preready_bytes = 8
    backend._stored_font = None
    backend._bridge = None
    backend._pending_spawn = None
    backend._shortcut_passthrough = False
    backend._fc_written = 0
    backend._fc_pending = 0
    backend.apply_theme = lambda *a, **k: None
    scripts = []
    backend._run_javascript = scripts.append

    backend._on_pty_message(None, _ReadyMessage())

    cursor = next(i for i, js in enumerate(scripts) if "cursorStyle" in js)
    write = next(i for i, js in enumerate(scripts) if "termWriteB64" in js)
    assert cursor < write, "cursor preference must be seeded before buffered output"


class _ReadyMessage:
    """Stands in for the WebKit JSCValue the bridge unwraps."""

    def to_json(self, _indent):
        import json

        return json.dumps({"type": "ready", "rows": 24, "cols": 80})
