"""Regression tests for issue #1186.

Affected VTE releases abort the process when a PTY-less terminal is asked what
its Backspace key sends (XTGETTCAP ``kb``/``kbs``): ``XTERM_RQTCAP`` passes
``EraseMode::eTTY`` as the fallback without checking for a PTY, and
``map_erase_binding`` then trips ``assert(auto_mode != eTTY)``. vim issues that
query, so opening a file in a daemon-backed tab killed sshPilot. Upstream:
GNOME/vte#2952.

Daemon-backed terminals are exactly the PTY-less case, so the backend pins the
binding for them. Local tabs own a PTY and must keep VTE's default, where AUTO
resolves through the tty to ``^?``.
"""

import types

import pytest

pytest.importorskip("gi")

from sshpilot import terminal_backends
from sshpilot.terminal_backends import VTETerminalBackend


def _gate(monkeypatch, version):
    """Evaluate the version gate as if *version* were the loaded VTE."""
    major, minor, micro = version
    monkeypatch.setattr(terminal_backends.Vte, "get_major_version", lambda: major)
    monkeypatch.setattr(terminal_backends.Vte, "get_minor_version", lambda: minor)
    monkeypatch.setattr(terminal_backends.Vte, "get_micro_version", lambda: micro)
    terminal_backends._vte_needs_backspace_pin.cache_clear()
    try:
        return terminal_backends._vte_needs_backspace_pin()
    finally:
        terminal_backends._vte_needs_backspace_pin.cache_clear()


# Surveyed from src/vteseq.cc at each release tag: the unguarded
# ``EraseMode::eTTY`` argument appears in 0.82.0 and is gone again in 0.82.4
# (0.82 branch backport) and 0.84.1 (0.84 branch).
@pytest.mark.parametrize(
    "version, affected",
    [
        ((0, 76, 0), False),   # predates the buggy call site entirely
        ((0, 80, 0), False),
        ((0, 82, 0), True),    # introduced here
        ((0, 82, 3), True),
        ((0, 82, 4), False),   # backported fix on the 0.82 branch
        ((0, 82, 5), False),
        ((0, 83, 90), True),   # dev snapshots sit *after* 0.82.4 and still crash
        ((0, 83, 91), True),
        ((0, 84, 0), True),    # Ubuntu 26.04 ships this one
        ((0, 84, 1), False),   # fixed on the 0.84 branch
        ((0, 86, 0), False),
    ],
)
def test_version_gate_matches_the_affected_releases(monkeypatch, version, affected):
    assert _gate(monkeypatch, version) is affected


def test_unreadable_version_is_treated_as_affected(monkeypatch):
    """Pinning a fixed VTE is inert; skipping an affected one aborts. When the
    version cannot be read, take the harmless branch."""
    def boom():
        raise RuntimeError("no version symbol")

    monkeypatch.setattr(terminal_backends.Vte, "get_major_version", boom)
    terminal_backends._vte_needs_backspace_pin.cache_clear()
    try:
        assert terminal_backends._vte_needs_backspace_pin() is True
    finally:
        terminal_backends._vte_needs_backspace_pin.cache_clear()


def _backend_with_recorder():
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    bindings = []
    backend.vte = types.SimpleNamespace(
        set_backspace_binding=bindings.append,
    )
    return backend, bindings


def test_pty_less_preparation_pins_the_binding_on_affected_vte(monkeypatch):
    monkeypatch.setattr(terminal_backends, "_vte_needs_backspace_pin", lambda: True)
    backend, bindings = _backend_with_recorder()

    backend.prepare_pty_less_emulation()

    assert bindings == [terminal_backends.Vte.EraseBinding.ASCII_BACKSPACE]


def test_pty_less_preparation_leaves_fixed_vte_alone(monkeypatch):
    """On a fixed VTE the workaround must not fire, so it can retire itself."""
    monkeypatch.setattr(terminal_backends, "_vte_needs_backspace_pin", lambda: False)
    backend, bindings = _backend_with_recorder()

    backend.prepare_pty_less_emulation()

    assert bindings == []


def test_pty_less_preparation_never_raises(monkeypatch):
    """It runs while a daemon session is being wired up; a VTE too old to have
    the setter must not take the session down with it."""
    monkeypatch.setattr(terminal_backends, "_vte_needs_backspace_pin", lambda: True)
    backend = VTETerminalBackend.__new__(VTETerminalBackend)
    backend.vte = types.SimpleNamespace()

    backend.prepare_pty_less_emulation()
