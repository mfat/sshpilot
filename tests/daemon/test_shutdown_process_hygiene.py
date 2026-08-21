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



def _record_sweeps(monkeypatch) -> list:
    import sshpilot.daemon.server as server_module

    calls = []
    monkeypatch.setattr(
        server_module.__dict__.setdefault("_test_hooks", type("H", (), {})()),
        "unused",
        None,
        raising=False,
    )
    import sshpilot.daemon.control_masters as control_masters

    monkeypatch.setattr(
        control_masters,
        "terminate_owned_control_masters",
        lambda **kwargs: calls.append(kwargs) or (),
    )
    return calls


def test_default_socket_daemon_retires_its_control_masters(
    daemon_factory, monkeypatch
):

    server, _manager = daemon_factory()
    calls = _record_sweeps(monkeypatch)
    import sshpilot.daemon.control_masters as control_masters

    monkeypatch.setattr(
        control_masters,
        "owns_default_control_master_namespace",
        lambda _path: True,
    )

    server._expire_control_masters()

    assert calls, "the owning daemon must retire the masters it left behind"


def test_daemon_on_an_explicit_socket_leaves_shared_masters_alone(
    daemon_factory, monkeypatch
):
    """A second instance shares the ControlMaster directory but owns none of it.

    Test fixtures and development instances run on an explicit ``--socket``.
    Sweeping from there would tear down the live sessions of the user's real
    daemon, which shares the per-user master directory.
    """

    server, _manager = daemon_factory()
    calls = _record_sweeps(monkeypatch)

    # daemon_factory always uses an explicit socket, which is precisely the
    # instance that must not sweep the shared per-user directory.
    server._expire_control_masters()

    assert calls == []


def test_control_master_sweep_survives_a_broken_socket_resolution(
    daemon_factory, monkeypatch
):
    """Shutdown hygiene is best-effort; it must never raise out of cleanup."""

    server, _manager = daemon_factory()
    calls = _record_sweeps(monkeypatch)
    import sshpilot.daemon.control_masters as control_masters

    def _boom(*_args, **_kwargs):
        raise RuntimeError("no runtime directory")

    monkeypatch.setattr(
        control_masters, "owns_default_control_master_namespace", lambda _p: True
    )
    monkeypatch.setattr(control_masters, "terminate_owned_control_masters", _boom)

    server._expire_control_masters()
    assert calls == []


# ---------------------------------------------------------------------------
# The GUI's forced-teardown fallback must obey the same ownership rule as the
# daemon's own shutdown — that is the whole reason the rule is one function.
# ---------------------------------------------------------------------------


def test_forced_teardown_sweeps_masters_only_for_the_default_socket(monkeypatch):
    """Required: an explicit --socket teardown cannot sweep default masters."""
    import sshpilot.daemon.runtime_verification as verification
    from sshpilot.daemon.lifecycle import resolve_socket_path

    swept = []
    monkeypatch.setattr(
        verification,
        "terminate_owned_control_masters",
        lambda **kwargs: swept.append(kwargs) or (),
    )
    monkeypatch.setattr(
        verification, "reap_orphaned_children", lambda **_kwargs: ()
    )
    monkeypatch.setattr(
        verification,
        "verify_sshpilot_runtime_terminated",
        lambda **_kwargs: verification.ShutdownVerificationResult(),
    )

    verification.terminate_owned_runtime(socket_path=Path("/tmp/explicit.sock"))
    assert swept == [], "an explicit-socket teardown owns no shared masters"

    verification.terminate_owned_runtime(socket_path=resolve_socket_path())
    assert swept, "the default-socket teardown must retire owned masters"


def test_quit_teardown_reports_a_master_that_would_not_exit(monkeypatch, tmp_path):
    """Required: a ControlMaster that survives blocks a 'successful' Quit."""
    import sshpilot.daemon.runtime_verification as verification
    from sshpilot.daemon.control_masters import ControlMasterProbe, MasterState

    monkeypatch.setattr(
        verification, "reap_orphaned_children", lambda **_kwargs: ()
    )
    stubborn = ControlMasterProbe(
        path=tmp_path / "cm" / "abc", state=MasterState.LIVE, pid=777
    )
    monkeypatch.setattr(
        verification,
        "terminate_owned_control_masters",
        lambda **_kwargs: (stubborn,),
    )
    monkeypatch.setattr(
        verification, "survey_control_masters", lambda **_kwargs: (stubborn,)
    )
    monkeypatch.setattr(
        verification, "probe_socket_owner", lambda *_a, **_k: (False, None)
    )

    result = verification.terminate_owned_runtime(
        socket_path=tmp_path / "sshpilotd.sock", sweep_control_masters=True
    )
    assert result.terminated is False
    assert "pid=777" in result.describe()


def test_control_master_ownership_rule_has_one_definition():
    """Server and GUI must resolve ownership through the same function."""
    import inspect

    import sshpilot.daemon.server as server_module
    import sshpilot.daemon.runtime_verification as verification

    server_source = inspect.getsource(server_module.DaemonServer._expire_control_masters)
    assert "owns_default_control_master_namespace" in server_source
    verification_source = inspect.getsource(verification.terminate_owned_runtime)
    assert "owns_default_control_master_namespace" in verification_source
