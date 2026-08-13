"""Secure runtime storage, capability probe, and manager behavior tests."""

from __future__ import annotations

import os
import stat
import time

import pytest

from sshpilot.core.ssh_diagnostics import SshDiagnosticState
from sshpilot.daemon.ssh_readiness import (
    DEFAULT_READINESS_GRACE_SECONDS,
    SshReadinessManager,
    insert_ssh_diagnostics_options,
    launch_eligible_for_diagnostics,
    resolve_diagnostics_root,
)
from sshpilot.daemon.ssh_diagnostics_monitor import inotify_available


class _Completed:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_subprocess(monkeypatch, *, probe_returncode=0, create_file=True):
    """Replace subprocess.run so the probe succeeds deterministically."""

    def _run(cmd, **kwargs):
        if cmd[1] == "-V":
            return _Completed(stderr=b"OpenSSH_9.6p1\n")
        probe = cmd[cmd.index("-E") + 1]
        if create_file and probe_returncode == 0:
            open(probe, "wb").write(b"")
        return _Completed(returncode=probe_returncode)

    monkeypatch.setattr("sshpilot.daemon.ssh_readiness.subprocess.run", _run)
    return _run


def test_resolve_diagnostics_root_requires_runtime_dir():
    assert resolve_diagnostics_root(None, "daemon-x") is None
    assert resolve_diagnostics_root("", "daemon-x") is None


def test_resolve_diagnostics_root_creates_private_tree(tmp_path):
    root = resolve_diagnostics_root(str(tmp_path), "daemon-x")
    assert root is not None
    for level in (tmp_path, tmp_path / "sshpilot", tmp_path / "sshpilot" / "diagnostics", root):
        info = level.lstat()
        assert stat.S_ISDIR(info.st_mode)
        assert not stat.S_ISLNK(info.st_mode)
        assert stat.S_IMODE(info.st_mode) == 0o700
        assert info.st_uid == os.getuid()


def test_no_tmp_escape_without_runtime_dir(tmp_path):
    os.environ.pop("XDG_RUNTIME_DIR", None)
    assert resolve_diagnostics_root(None, "daemon-x") is None
    # A legacy /tmp tree must never be used as diagnostics storage.
    stale = tmp_path / "sshpilot-stale"
    stale.mkdir(mode=0o700)
    assert resolve_diagnostics_root(None, "daemon-x") is None


def test_resolve_diagnostics_root_rejects_symlinked_runtime(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    symlink = tmp_path / "runtime"
    symlink.symlink_to(real)
    assert resolve_diagnostics_root(str(symlink), "daemon-x") is None


def test_loose_umask_directory_is_tightened(tmp_path):
    os.makedirs(tmp_path / "sshpilot" / "diagnostics", mode=0o777, exist_ok=True)
    root = resolve_diagnostics_root(str(tmp_path), "daemon-x")
    assert root is not None
    diag = tmp_path / "sshpilot" / "diagnostics"
    assert stat.S_IMODE(diag.lstat().st_mode) == 0o700


def test_launch_eligibility_gates_binary_and_user_options():
    assert launch_eligible_for_diagnostics(["/usr/bin/ssh", "host"])
    assert not launch_eligible_for_diagnostics(["/usr/bin/scp", "host"])
    assert not launch_eligible_for_diagnostics(["/usr/bin/ssh-copy-id", "host"])
    assert not launch_eligible_for_diagnostics(["/usr/bin/ssh", "-E", "custom", "host"])
    assert not launch_eligible_for_diagnostics(["/usr/bin/ssh", "-Ecustom", "host"])
    assert not launch_eligible_for_diagnostics(["/usr/bin/ssh", "-v", "host"])
    assert not launch_eligible_for_diagnostics(["/usr/bin/ssh", "-vv", "host"])
    assert not launch_eligible_for_diagnostics([])


def test_remote_command_verbosity_does_not_disqualify():
    """A ``-v`` or ``-E`` after the destination belongs to the remote command
    and is not user OpenSSH verbosity; it must not block instrumentation."""
    assert launch_eligible_for_diagnostics(
        ["/usr/bin/ssh", "host", "tail", "-v", "/var/log/syslog"]
    )
    assert launch_eligible_for_diagnostics(
        ["/usr/bin/ssh", "host", "scp", "-E", "file", "user@other:/tmp"]
    )
    assert launch_eligible_for_diagnostics(
        ["/usr/bin/ssh", "-o", "BatchMode=yes", "host", "-v"]
    )


def test_option_values_are_not_mistaken_for_the_destination():
    """Options that consume a separate argument are skipped so their values are
    never treated as the destination, and a user ``-v`` after them is still
    seen."""
    assert not launch_eligible_for_diagnostics(
        ["/usr/bin/ssh", "-o", "BatchMode=yes", "-v", "host"]
    )
    assert not launch_eligible_for_diagnostics(
        ["/usr/bin/ssh", "-F", "/cfg", "-vv", "host"]
    )
    assert launch_eligible_for_diagnostics(
        ["/usr/bin/ssh", "-o", "BatchMode=yes", "host"]
    )


def test_diagnostics_options_inserted_before_destination():
    argv = ("/usr/bin/ssh", "-F", "/cfg", "-p", "2222", "alice@host")
    out = insert_ssh_diagnostics_options(argv, "/diag/sess.log")
    assert out[0] == "/usr/bin/ssh"
    assert out[1] == "-F"
    assert out[3:6] == ("-v", "-E", "/diag/sess.log")
    assert out[-1] == "alice@host"


def test_diagnostics_options_without_config_file():
    argv = ("/usr/bin/ssh", "-p", "22", "host")
    out = insert_ssh_diagnostics_options(argv, "/diag/sess.log")
    assert out[1:4] == ("-v", "-E", "/diag/sess.log")
    assert out[-1] == "host"


def _manager(tmp_path, monkeypatch, probe_returncode=0, **kwargs):
    _fake_subprocess(monkeypatch, probe_returncode=probe_returncode)
    return SshReadinessManager(
        instance_id="daemon-test",
        runtime_dir=str(tmp_path),
        **kwargs,
    )


def _await(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_prepare_launch_engages_and_creates_private_file(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.available
    path = manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {})
    assert path is not None
    info = os.lstat(path)
    assert stat.S_ISREG(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o600
    assert manager.is_engaged("session-1")
    manager.finish("session-1")
    assert not manager.is_engaged("session-1")
    assert _await(lambda: not os.path.exists(path))
    manager.close()


def test_prepare_launch_disabled_when_probe_fails(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, probe_returncode=1)
    assert manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is None
    assert not manager.is_engaged("session-1")
    manager.close()


def test_prepare_launch_disabled_for_ineligible_argv(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    assert manager.prepare_launch("session-1", ("/usr/bin/scp", "host"), {}) is None
    assert not manager.is_engaged("session-1")
    manager.close()


def test_prepare_launch_disabled_without_runtime_dir(tmp_path, monkeypatch):
    manager = SshReadinessManager(instance_id="daemon-test", runtime_dir="")
    assert not manager.available
    assert manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is None
    manager.close()


def test_capability_probe_is_cached_per_executable(tmp_path, monkeypatch):
    manager = SshReadinessManager(instance_id="daemon-test", runtime_dir=str(tmp_path))
    probe_calls = []

    def _run(cmd, **kwargs):
        if cmd[1] == "-V":
            return _Completed(stderr=b"OpenSSH_9.6p1\n")
        probe_calls.append(cmd)
        open(cmd[cmd.index("-E") + 1], "wb").write(b"")
        return _Completed()

    monkeypatch.setattr("sshpilot.daemon.ssh_readiness.subprocess.run", _run)
    first = manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {})
    second = manager.prepare_launch("session-2", ("/usr/bin/ssh", "host2"), {})
    assert first and second
    assert len(probe_calls) == 1  # probe executed once for the executable
    manager.close()


def test_flatpak_like_environment_never_escapes_to_tmp(monkeypatch, tmp_path):
    monkeypatch.setenv("FLATPAK_ID", "io.github.sshpilot")
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))
    manager = SshReadinessManager(instance_id="daemon-x")
    if manager.available:
        path = manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {})
        if path is not None:
            assert str(path).startswith(str(tmp_path / "runtime"))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    unset = SshReadinessManager(instance_id="daemon-x")
    assert not unset.available
    assert unset.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is None
    unset.close()


@pytest.mark.skipif(
    not inotify_available(),
    reason="inotify is unavailable on this platform",
)
def test_diagnostics_deliver_authenticated_result(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch)
    results = []
    manager.subscribe("session-1", lambda _sid, result: results.append(result))
    path = manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {})
    assert path is not None
    with open(path, "ab") as handle:
        handle.write(b'debug1: Authenticated to host ([::1]:22) using "publickey".\n')
    deadline = time.monotonic() + 2.0
    while not results and time.monotonic() < deadline:
        time.sleep(0.01)
    assert results
    assert results[0].state is SshDiagnosticState.AUTHENTICATED
    manager.finish("session-1")
    manager.close()


@pytest.mark.skipif(
    not inotify_available(),
    reason="inotify is unavailable on this platform",
)
def test_grace_expiry_fires_once(tmp_path, monkeypatch):
    manager = _manager(tmp_path, monkeypatch, grace_seconds=0.05)
    expired = []
    manager.subscribe(
        "session-1",
        lambda _sid, _result: None,
        lambda sid: expired.append(sid),
    )
    assert manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is not None
    deadline = time.monotonic() + 2.0
    while not expired and time.monotonic() < deadline:
        time.sleep(0.01)
    assert expired == ["session-1"]
    time.sleep(0.2)
    assert len(expired) == 1
    manager.finish("session-1")
    manager.close()


@pytest.mark.skipif(
    not inotify_available(),
    reason="inotify is unavailable on this platform",
)
def test_registration_failure_disables_diagnostics_and_cleans_file(tmp_path, monkeypatch):
    """When the inotify watch cannot be installed the launch degrades to the
    PTY evidence path: no engagement, no grace timer, and the owned file is
    removed."""
    from sshpilot.daemon import ssh_diagnostics_monitor as monitor_mod

    class _BrokenLibc:
        def inotify_add_watch(self, *args):
            return -1

    manager = _manager(tmp_path, monkeypatch)
    monkeypatch.setattr(monitor_mod, "_INOTIFY_LIBC", _BrokenLibc())
    assert manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is None
    assert not manager.is_engaged("session-1")
    assert manager._root is not None
    assert not any(p.suffix == ".log" for p in manager._root.iterdir())
    manager.close()


@pytest.mark.skipif(
    not inotify_available(),
    reason="inotify is unavailable on this platform",
)
def test_submission_failure_disables_diagnostics_and_cleans_file(tmp_path, monkeypatch):
    """A monitor that cannot accept the command (closed / queue full) must
    degrade the launch the same way — never a session startup error."""

    def _boom(*args, **kwargs):
        raise RuntimeError("diagnostics monitor is closed")

    manager = _manager(tmp_path, monkeypatch)
    assert manager._monitor is not None
    monkeypatch.setattr(manager._monitor, "_submit", _boom)
    assert manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is None
    assert not manager.is_engaged("session-1")
    assert manager._root is not None
    assert not any(p.suffix == ".log" for p in manager._root.iterdir())
    manager.close()


@pytest.mark.skipif(
    not inotify_available(),
    reason="inotify is unavailable on this platform",
)
def test_registration_failure_never_arms_grace_timer(tmp_path, monkeypatch):
    from sshpilot.daemon import ssh_diagnostics_monitor as monitor_mod

    class _BrokenLibc:
        def inotify_add_watch(self, *args):
            return -1

    manager = _manager(tmp_path, monkeypatch, grace_seconds=0.05)
    expired = []
    manager.subscribe(
        "session-1",
        lambda _sid, _result: None,
        lambda sid: expired.append(sid),
    )
    monkeypatch.setattr(monitor_mod, "_INOTIFY_LIBC", _BrokenLibc())
    assert manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is None
    time.sleep(0.2)
    assert expired == []
    manager.close()


@pytest.mark.skipif(
    not inotify_available(),
    reason="inotify is unavailable on this platform",
)
def test_grace_fires_only_after_registration_is_acknowledged(tmp_path, monkeypatch):
    """The grace timer must be armed only once the watch is confirmed, so a
    slow or failed registration cannot start the clock early."""
    manager = _manager(tmp_path, monkeypatch, grace_seconds=0.3)
    expired = []
    manager.subscribe(
        "session-1",
        lambda _sid, _result: None,
        lambda sid: expired.append(sid),
    )
    assert manager.prepare_launch("session-1", ("/usr/bin/ssh", "host"), {}) is not None
    assert manager.is_engaged("session-1")
    time.sleep(0.5)
    assert expired == ["session-1"]
    manager.finish("session-1")
    manager.close()


def test_daemon_readiness_grace_default_matches_manager_canonical():
    from sshpilot.daemon.server import DEFAULT_SSH_READINESS_GRACE_SECONDS

    assert DEFAULT_SSH_READINESS_GRACE_SECONDS == DEFAULT_READINESS_GRACE_SECONDS
