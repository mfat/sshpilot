"""SSH and local terminal tabs on every terminal backend this build offers.

For each backend Preferences lists as available, the test selects it the way
Preferences does, opens an SSH tab and a local terminal tab from the main
window, and proves a live shell answers in each: the marker only appears once a
shell has evaluated ``$((6*7))``. It also checks the tab really got that backend:
the factory falls back to VTE quietly when one fails to load, which would
otherwise pass as a VTE run.

Inside a Flatpak, a local terminal runs on the host, not in the sandbox, and
the local test checks that. ``SSHPILOT_EXPECT_BACKENDS`` (comma separated
names from BACKENDS) runs exactly those backends and fails when one is missing:
the Flatpak job names everything the Flatpak ships. Without it every backend
is tried, and the ones this system lacks skip.

Opt-in: SSHPILOT_GUI_TESTS=1 xvfb-run -a pytest -m gui tests/gui/test_terminal_backends.py
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from tests._gui_harness import requires_gui
from tests.gui._tab_proof import (
    assert_veil_lifted,
    open_tab,
    prove_session_shell,
    type_into,
    wait_for,
    wait_for_prompt,
)

Gtk, Adw, Gio, GLib = requires_gui()

pytestmark = pytest.mark.gui

#: name -> (terminal.backend, terminal.builtin_model), as Preferences stores them.
BACKENDS = {
    "vte": ("vte", None),
    "xterm.js": ("pyxterm", None),
    "gtkghostty": ("gtkghostty", None),
    "pyte": ("pyte", "pyte"),
    "libvterm": ("pyte", "vterm"),
    "libghostty-vt": ("pyte", "ghostty"),
}

LOCAL_COMMAND = (
    "printf 'LOCAL_%s_%s\\n' $((6*7)) "
    "\"$(test -e /.flatpak-info && echo sandbox || echo host)\"\n"
)
#: A local terminal is a shell on this computer: on the host under Flatpak too.
LOCAL_MARKER = "LOCAL_42_host"


def _offered() -> list[tuple[str, object]]:
    """(backend id, model) of every backend Preferences offers as available."""
    from sshpilot.preferences import PreferencesWindow, _detect_pyxterm_backend

    builder = SimpleNamespace(
        _detect_gtkghostty_backend=PreferencesWindow._detect_gtkghostty_backend,
        _detect_pyxterm_backend=_detect_pyxterm_backend,
    )
    return [
        (choice["id"], choice.get("model"))
        for choice in PreferencesWindow._build_backend_choices(builder)
        if choice.get("available")
    ]


def _expected() -> set[str]:
    raw = os.environ.get("SSHPILOT_EXPECT_BACKENDS", "")
    names = {name.strip() for name in raw.split(",") if name.strip()}
    unknown = names - set(BACKENDS)
    assert not unknown, f"SSHPILOT_EXPECT_BACKENDS names unknown backends: {sorted(unknown)}"
    return names


#: What to run: the expected backends when named, otherwise all of them.
PARAMS = sorted(_expected()) or list(BACKENDS)


def _select_backend(h, name: str, monkeypatch) -> tuple[str, object]:
    backend_id, model = BACKENDS[name]
    offered = _offered()
    if (backend_id, model) not in offered:
        message = f"{name} backend not available here (offered: {offered})"
        if name in _expected():
            pytest.fail(message)
        pytest.skip(message)
    # An inherited override would pick the emulator model instead of the config.
    monkeypatch.delenv("SSHPILOT_TERMINAL_MODEL", raising=False)
    config = h.gui.window.config
    config.set_setting("terminal.backend", backend_id)
    if model is not None:
        config.set_setting("terminal.builtin_model", model)
    return backend_id, model


def _assert_backend(h, term, name: str) -> None:
    backend_id, model = BACKENDS[name]
    got = term.get_backend_name()
    assert got == backend_id, f"asked for {name}, the tab fell back to {got}"
    backend = term.backend
    if model is not None:
        assert backend.view.model_name == model, (
            f"asked for {name}, the built-in view runs {backend.view.model_name}"
        )
    if backend_id == "pyxterm":
        # The text is read from the bytes fed to the page; this proves the
        # page itself (WebKit, xterm.js) came up to show them.
        h.pump_until(
            lambda: bool(getattr(backend, "_js_ready", False)),
            timeout=30.0,
            label="xterm.js page ready",
        )


@pytest.mark.parametrize("name", PARAMS)
def test_ssh_tab_runs_a_remote_command(phase14_harness, monkeypatch, name):
    h = phase14_harness
    _select_backend(h, name, monkeypatch)
    h.stop_auth_helper()
    conn = h.add_key_connection(f"Backend{name.title().replace('.', '').replace('-', '')}",
                                encrypted=False)

    term = open_tab(h, conn)
    _assert_backend(h, term, name)
    prove_session_shell(h, term, label=f"SSH ({name})")


@pytest.mark.parametrize("name", PARAMS)
def test_local_tab_runs_a_shell_command(phase14_harness, monkeypatch, name):
    h = phase14_harness
    _select_backend(h, name, monkeypatch)
    win = h.gui.window
    before = set(win.terminal_to_connection)

    assert win.terminal_manager.show_local_terminal()
    h.pump_until(
        lambda: set(win.terminal_to_connection) - before,
        timeout=10.0,
        label="local terminal tab",
    )
    (term,) = set(win.terminal_to_connection) - before
    _assert_backend(h, term, name)

    wait_for_prompt(h, term, timeout=30.0)
    type_into(h, term, LOCAL_COMMAND)
    assert LOCAL_MARKER in wait_for(h, term, LOCAL_MARKER)
    assert_veil_lifted(h, term)
