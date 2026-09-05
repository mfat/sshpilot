"""Regression tests for the PTY-less Backspace binding (#1186, #1240 follow-up).

Daemon-backed terminals have no PTY, so VTE's ``AUTO`` erase binding has no
tty to ask for ``VERASE``. Two things follow, and one pin fixes both.

``AUTO`` falls back to ``^H`` for a key press while every remote pty still
sets ``VERASE`` to ``^?``. Readline binds both so shell editing looks fine,
but a canonical-mode prompt keeps the ``^H`` as a literal character: fixing a
typo in a remote ``sudo`` password silently submits the wrong bytes.

Separately, on VTE 0.82.0-0.84.0 a program asking the terminal what Backspace
sends (XTGETTCAP ``kb``/``kbs``) makes ``XTERM_RQTCAP`` pass ``EraseMode::eTTY``
as the fallback without checking for a PTY, and ``map_erase_binding`` trips
``assert(auto_mode != eTTY)``. vim issues that query, so opening a file in a
daemon-backed tab killed sshPilot (GNOME/vte#2952).

Naming ``ASCII_DELETE`` sends what a PTY-owning terminal sends and keeps the
PTY-consulting branch unreachable. Local tabs own a PTY and must keep VTE's
default, where ``AUTO`` resolves through the tty.
"""

import types

import pytest

pytest.importorskip("gi")

from sshpilot import terminal_backends
from sshpilot.terminal_backends import VTETerminalBackend


def _backend_with_recorder():
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    bindings = []
    backend.vte = types.SimpleNamespace(
        set_backspace_binding=bindings.append,
    )
    return backend, bindings


def test_pty_less_preparation_pins_ascii_delete():
    """``^?`` is what a PTY-owning VTE sends and what remote ptys erase on."""
    backend, bindings = _backend_with_recorder()

    backend.prepare_pty_less_emulation()

    assert bindings == [terminal_backends.Vte.EraseBinding.ASCII_DELETE]


def test_the_pin_is_not_gated_on_the_vte_version(monkeypatch):
    """Releases carrying the #1186 fix still resolve ``AUTO`` to ``^H`` for a
    key press, so they need the pin just as much as the aborting ones."""
    for version in [(0, 76, 0), (0, 82, 0), (0, 84, 0), (0, 84, 1), (0, 86, 0)]:
        major, minor, micro = version
        monkeypatch.setattr(terminal_backends.Vte, "get_major_version", lambda: major)
        monkeypatch.setattr(terminal_backends.Vte, "get_minor_version", lambda: minor)
        monkeypatch.setattr(terminal_backends.Vte, "get_micro_version", lambda: micro)
        backend, bindings = _backend_with_recorder()

        backend.prepare_pty_less_emulation()

        assert bindings == [terminal_backends.Vte.EraseBinding.ASCII_DELETE], version


def test_pty_less_preparation_never_raises():
    """It runs while a daemon session is being wired up; a VTE too old to have
    the setter must not take the session down with it."""
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace()

    backend.prepare_pty_less_emulation()
