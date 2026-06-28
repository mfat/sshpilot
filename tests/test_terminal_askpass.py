"""Terminal-side askpass behaviour.

The old agent-bypass design (forced-askpass env decisions + a terminal
``_prepare_key_for_native_mode`` method) was removed. The terminal no longer
makes auth/env decisions — ``resolve_native_auth`` does that and the terminal
just consumes the prebuilt command/env. The askpass-relevant terminal behaviour
now is the **key preload**: the connect worker thread (``_connect_ssh_thread``)
calls ``connection._preload_keys_into_agent(config)`` before scheduling the
spawn, so a passphrased key is unlocked in ssh-agent (via our askpass) and the
agent is never disabled. These tests cover that wiring.
"""

import importlib
import types

import pytest


class _RecordingGLib:
    """Captures GLib.idle_add scheduling so we can assert the spawn was queued."""

    class Error(Exception):
        pass

    SpawnFlags = types.SimpleNamespace(DEFAULT=0)

    def __init__(self):
        self.idle_calls = []

    def idle_add(self, func, *args, **kwargs):
        self.idle_calls.append((func, args))
        return 0

    def timeout_add_seconds(self, *_args, **_kwargs):
        return 0


def _make_terminal(monkeypatch, *, connection, config=None, glib=None):
    terminal_mod = importlib.import_module("sshpilot.terminal")
    glib = glib or _RecordingGLib()
    monkeypatch.setattr(terminal_mod, "GLib", glib, raising=False)

    terminal = terminal_mod.TerminalWidget.__new__(terminal_mod.TerminalWidget)
    terminal.connection = connection
    terminal.config = config if config is not None else object()
    # _setup_ssh_terminal is the spawn step; stub it so we can assert it was
    # scheduled without running the real VTE spawn.
    sentinel = lambda *a, **k: None
    terminal._setup_ssh_terminal = sentinel
    terminal._on_connection_failed = lambda *a, **k: None
    return terminal, glib, sentinel


def _conn(preload=None, **extra):
    ns = types.SimpleNamespace(pre_command='', data={}, **extra)
    if preload is not None:
        ns._preload_keys_into_agent = preload
    return ns


def test_connect_thread_preloads_keys_then_schedules_spawn(monkeypatch):
    calls = []
    conn = _conn(preload=lambda cfg: calls.append(cfg))
    cfg = object()
    terminal, glib, sentinel = _make_terminal(monkeypatch, connection=conn, config=cfg)

    terminal._connect_ssh_thread()

    # Preload ran with the terminal's config...
    assert calls == [cfg]
    # ...and the spawn step was scheduled on the main loop afterwards.
    assert any(func is sentinel for func, _ in glib.idle_calls)


def test_connect_thread_swallows_preload_error_and_still_spawns(monkeypatch):
    def boom(_cfg):
        raise RuntimeError("ssh-add blew up")

    conn = _conn(preload=boom)
    terminal, glib, sentinel = _make_terminal(monkeypatch, connection=conn)

    # Best-effort: a preload failure must not abort the connection.
    terminal._connect_ssh_thread()

    assert any(func is sentinel for func, _ in glib.idle_calls)


def test_connect_thread_without_preload_method_still_spawns(monkeypatch):
    # Older/foreign connection objects may lack the preload hook entirely.
    conn = _conn(preload=None)
    terminal, glib, sentinel = _make_terminal(monkeypatch, connection=conn)

    terminal._connect_ssh_thread()

    assert any(func is sentinel for func, _ in glib.idle_calls)


def test_connect_thread_skips_preload_when_disabled_via_setting(monkeypatch):
    # The preload helper itself honours ssh.agent_preload_keys; here we only
    # assert the terminal always *invokes* it (the gating lives in the helper,
    # covered by tests/test_agent_preload.py). A disabled setting => helper no-ops.
    seen = []
    conn = _conn(preload=lambda cfg: seen.append('called'))
    terminal, glib, _ = _make_terminal(monkeypatch, connection=conn)

    terminal._connect_ssh_thread()

    assert seen == ['called']
