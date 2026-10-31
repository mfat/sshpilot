"""ControlMaster probing must distinguish 'no master' from 'cannot tell'.

Collapsing the two is how a live master loses its socket: sshPilot unlinks
the ControlPath, the master keeps running with an authenticated connection
to the remote host, and nothing can reach it again to shut it down.

These tests drive the real command boundary — the ``ssh`` invocation itself —
rather than the layer above it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sshpilot.daemon.control_masters import (
    MasterState,
    list_control_paths,
    probe_control_master,
    survey_control_masters,
    terminate_owned_control_masters,
)


def _control_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cm"
    directory.mkdir(mode=0o700)
    (directory / "abcdef").write_bytes(b"")
    return directory


def test_a_timeout_is_unknown_not_absent(tmp_path, monkeypatch):
    """Required: a timed-out check must not become false absence."""
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _timeout)
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.UNKNOWN
    assert "timed out" in probe.reason


def test_missing_ssh_is_unknown_not_absent(tmp_path, monkeypatch):
    """Required: inability to execute ssh must not become false absence."""
    def _missing(*_args, **_kwargs):
        raise FileNotFoundError("ssh")

    monkeypatch.setattr(subprocess, "run", _missing)
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.UNKNOWN
    assert "unavailable" in probe.reason


def test_malformed_failure_output_is_unknown_not_absent(tmp_path, monkeypatch):
    """Required: unrecognised output cannot be read as 'nothing there'."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=255, stdout=b"", stderr=b"something we do not parse"
        ),
    )
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.UNKNOWN


def test_a_refused_connection_is_positively_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[],
            returncode=255,
            stdout=b"",
            stderr=b"ssh: connect to ControlPath: No such file or directory",
        ),
    )
    assert probe_control_master(tmp_path / "abcdef").state is MasterState.ABSENT


def test_a_running_master_is_live_with_its_pid(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b"Master running (pid=31337)\n"
        ),
    )
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.LIVE
    assert probe.pid == 31337


def test_a_live_master_without_a_pid_is_still_live(tmp_path, monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=0, stdout=b"", stderr=b"Master running\n"
        ),
    )
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.LIVE, "unnameable is not the same as absent"


def test_timeout_never_unlinks_the_control_path(tmp_path, monkeypatch):
    """Required: the socket of a master we could not check must survive."""
    directory = _control_dir(tmp_path)
    path = directory / "abcdef"

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _timeout)
    remaining = terminate_owned_control_masters(directory=directory)

    assert path.exists(), "an unchecked ControlPath must never be unlinked"
    assert [probe.state for probe in remaining] == [MasterState.UNKNOWN]


def test_missing_ssh_never_unlinks_the_control_path(tmp_path, monkeypatch):
    directory = _control_dir(tmp_path)
    monkeypatch.setattr(
        subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(FileNotFoundError())
    )
    remaining = terminate_owned_control_masters(directory=directory)
    assert (directory / "abcdef").exists()
    assert remaining and remaining[0].state is MasterState.UNKNOWN


def test_only_a_positively_absent_path_is_removed(tmp_path, monkeypatch):
    directory = _control_dir(tmp_path)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            args=[], returncode=255, stdout=b"", stderr=b"Connection refused"
        ),
    )
    remaining = terminate_owned_control_masters(directory=directory)
    assert not (directory / "abcdef").exists(), "a dead ControlPath is cleaned up"
    assert remaining == ()


def test_an_unreadable_directory_raises_rather_than_reporting_none(tmp_path):
    """Required: failure to enumerate must not mean 'zero ControlMasters'."""
    import os

    directory = _control_dir(tmp_path)
    directory.chmod(0o000)
    if os.getuid() == 0:
        pytest.skip("root can read a 0000 directory")
    try:
        with pytest.raises(OSError):
            survey_control_masters(directory=directory)
    finally:
        directory.chmod(0o700)


def test_a_missing_directory_is_genuinely_empty(tmp_path):
    """sshPilot creates the directory before the first master, so no dir = none."""
    assert list_control_paths(tmp_path / "never-created") == ()


def test_an_unresolvable_directory_is_not_empty(monkeypatch):
    import sshpilot.daemon.control_masters as control_masters

    monkeypatch.setattr(control_masters, "control_masters_possible", lambda: True)
    monkeypatch.setattr(control_masters, "control_master_directory", lambda: None)
    with pytest.raises(LookupError):
        survey_control_masters()


def _assert_overrides_user_controlmaster(argv, action):
    """``-o ControlMaster=no`` must precede ``-O`` so a ``ControlMaster yes``
    or ``ask`` in the user's ssh_config cannot divert the command to the
    placeholder host (issue #1361)."""
    assert argv[0] == "ssh"
    assert "ControlMaster=no" in argv
    assert argv[argv.index("ControlMaster=no") - 1] == "-o"
    assert argv.index("ControlMaster=no") < argv.index("-O")
    assert argv[argv.index("-O") + 1] == action


def test_probe_overrides_user_controlmaster_setting(tmp_path, monkeypatch):
    seen = []

    def _run(argv, **_kwargs):
        seen.append(argv)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b"Master running (pid=4242)\r\n"
        )

    monkeypatch.setattr(subprocess, "run", _run)
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.LIVE and probe.pid == 4242
    _assert_overrides_user_controlmaster(seen[0], "check")


def test_terminate_exit_overrides_user_controlmaster_setting(tmp_path, monkeypatch):
    directory = _control_dir(tmp_path)
    seen = []
    exited = []

    def _run(argv, **_kwargs):
        seen.append(argv)
        action = argv[argv.index("-O") + 1]
        if action == "exit":
            exited.append(True)
            return subprocess.CompletedProcess(argv, 0, b"", b"Exit request sent.\r\n")
        if exited:
            return subprocess.CompletedProcess(
                argv, 255, b"", b"Control socket connect(x): No such file or directory\n"
            )
        return subprocess.CompletedProcess(argv, 0, b"", b"Master running (pid=4242)\r\n")

    monkeypatch.setattr(subprocess, "run", _run)
    assert terminate_owned_control_masters(directory=directory) == ()
    actions = [argv[argv.index("-O") + 1] for argv in seen]
    assert actions == ["check", "exit", "check"]
    for argv, action in zip(seen, actions):
        _assert_overrides_user_controlmaster(argv, action)
