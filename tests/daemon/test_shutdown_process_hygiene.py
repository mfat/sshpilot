"""Quitting must not leave processes behind — and must not reach too far.

The daemon retires the ControlMasters its sessions spawned, because OpenSSH
backgrounds a master into its own session: it is not one of our children, and
without this it would outlive the whole application for its ``ControlPersist``
window, holding an authenticated connection to the remote host open.

The masters live in a single per-user directory, so the sweep is deliberately
narrow: only the daemon that owns the default socket may run it.
"""

from __future__ import annotations

from pathlib import Path

import pytest


def _record_sweeps(monkeypatch) -> list:
    import sshpilot.ssh_multiplex as ssh_multiplex

    calls = []
    monkeypatch.setattr(
        ssh_multiplex,
        "expire_all_masters",
        lambda **kwargs: calls.append(kwargs),
    )
    return calls


def test_default_socket_daemon_retires_its_control_masters(
    daemon_factory, monkeypatch
):
    import sshpilot.daemon.server as server_module

    server, _manager = daemon_factory()
    calls = _record_sweeps(monkeypatch)
    monkeypatch.setattr(
        server_module, "resolve_socket_path", lambda *_a, **_k: server.socket_path
    )

    server._expire_control_masters()

    assert calls, "the owning daemon must retire the masters it left behind"
    assert calls[0]["mode"] == "exit", "shutdown kills masters, it does not drain"
    assert calls[0]["background"] is False, "the sweep must finish before exit"


def test_daemon_on_an_explicit_socket_leaves_shared_masters_alone(
    daemon_factory, monkeypatch
):
    """A second instance shares the ControlMaster directory but owns none of it.

    Test fixtures and development instances run on an explicit ``--socket``.
    Sweeping from there would tear down the live sessions of the user's real
    daemon, which shares the per-user master directory.
    """
    import sshpilot.daemon.server as server_module

    server, _manager = daemon_factory()
    calls = _record_sweeps(monkeypatch)
    monkeypatch.setattr(
        server_module,
        "resolve_socket_path",
        lambda *_a, **_k: Path("/somewhere/else/sshpilotd.sock"),
    )

    server._expire_control_masters()

    assert calls == []


def test_control_master_sweep_survives_a_broken_socket_resolution(
    daemon_factory, monkeypatch
):
    """Shutdown hygiene is best-effort; it must never raise out of cleanup."""
    import sshpilot.daemon.server as server_module

    server, _manager = daemon_factory()
    calls = _record_sweeps(monkeypatch)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no runtime directory")

    monkeypatch.setattr(server_module, "resolve_socket_path", _boom)

    server._expire_control_masters()
    assert calls == []
