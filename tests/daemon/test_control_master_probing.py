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

import sshpilot.daemon.control_masters as control_masters
from sshpilot.daemon.control_masters import (
    UNRECOGNIZED_RETRY_DELAYS,
    MasterState,
    launch_rides_live_master,
    list_control_paths,
    probe_control_master,
    survey_control_masters,
    terminate_owned_control_masters,
)


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """Record retry pauses instead of sleeping through them."""
    recorded: list[float] = []
    monkeypatch.setattr(control_masters.time, "sleep", recorded.append)
    return recorded


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
    """Every ``ssh -O`` must be isolated from the user's environment before
    ``-O``: no ssh_config (``ControlMaster yes``/``ask`` there made ``-O`` skip
    the socket -- issue #1361), and a failed check must not fall through to a
    real connection to the placeholder host."""
    import os

    assert argv[0] == "ssh"
    command = argv.index("-O")
    assert argv[command + 1] == action
    head = argv[:command]
    assert head[head.index("-F") + 1] == os.devnull
    for option in ("ControlMaster=no", "ProxyCommand=false"):
        assert option in head and head[head.index(option) - 1] == "-o"


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


# -- a socket that accepts but fails the mux handshake ------------------------

_HELLO_FAILED = (
    b"muxclient: master hello exchange failed\r\n"
    b"Connection closed by UNKNOWN port 65535\r\n"
)
_DNS_FAILED = (
    b"muxclient: master hello exchange failed\r\n"
    b"ssh: Could not resolve hostname sshpilot-controlmaster: "
    b"Name or service not known\r\n"
)


def _scripted(outputs):
    """subprocess.run double answering ``-O check`` from a script of results."""
    calls = []

    def _run(argv, **_kwargs):
        calls.append(argv)
        code, stderr = outputs[min(len(calls), len(outputs)) - 1]
        return subprocess.CompletedProcess(argv, code, b"", stderr)

    return _run, calls


@pytest.mark.parametrize("output", [_HELLO_FAILED, _DNS_FAILED, b"garbage"])
def test_a_failed_handshake_or_dns_error_is_never_absent(
    tmp_path, monkeypatch, sleeps, output
):
    """A socket that accepted the connection has something behind it; neither
    the handshake failure nor the placeholder's DNS error proves it gone."""
    run, calls = _scripted([(255, output)])
    monkeypatch.setattr(subprocess, "run", run)
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.UNKNOWN
    assert len(calls) == 1 + len(UNRECOGNIZED_RETRY_DELAYS)
    assert sleeps == list(UNRECOGNIZED_RETRY_DELAYS)


def test_a_failed_handshake_says_so(tmp_path, monkeypatch):
    run, _calls = _scripted([(255, _HELLO_FAILED)])
    monkeypatch.setattr(subprocess, "run", run)
    probe = probe_control_master(tmp_path / "abcdef")
    assert "did not answer as a master" in probe.reason


def test_an_exiting_master_settles_to_absent_on_retry(tmp_path, monkeypatch, sleeps):
    """A real master that is exiting fails the handshake for a moment, then its
    socket is gone. Quit must see the settled answer, not the transient one."""
    run, calls = _scripted([
        (255, _HELLO_FAILED),
        (255, b"Control socket connect(x): No such file or directory\r\n"),
    ])
    monkeypatch.setattr(subprocess, "run", run)
    assert probe_control_master(tmp_path / "abcdef").state is MasterState.ABSENT
    assert len(calls) == 2 and sleeps == [UNRECOGNIZED_RETRY_DELAYS[0]]


def test_a_retry_can_also_find_the_master_live(tmp_path, monkeypatch):
    run, _calls = _scripted([(255, _HELLO_FAILED), (0, b"Master running (pid=77)\r\n")])
    monkeypatch.setattr(subprocess, "run", run)
    probe = probe_control_master(tmp_path / "abcdef")
    assert probe.state is MasterState.LIVE and probe.pid == 77


def test_a_timeout_is_not_retried(tmp_path, monkeypatch, sleeps):
    calls = []

    def _timeout(*args, **_kwargs):
        calls.append(args)
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=2.0)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert probe_control_master(tmp_path / "abcdef").state is MasterState.UNKNOWN
    assert len(calls) == 1 and sleeps == []


def test_a_persistently_failed_handshake_is_kept_and_reported(tmp_path, monkeypatch):
    directory = _control_dir(tmp_path)
    run, _calls = _scripted([(255, _HELLO_FAILED)])
    monkeypatch.setattr(subprocess, "run", run)
    remaining = terminate_owned_control_masters(directory=directory)
    assert (directory / "abcdef").exists(), "a socket something answers on stays"
    assert [p.state for p in remaining] == [MasterState.UNKNOWN]


def test_launch_path_does_not_wait_on_retries(tmp_path, monkeypatch, sleeps):
    path = tmp_path / "abcdef"

    def _run(argv, **_kwargs):
        if "-G" in argv:
            return subprocess.CompletedProcess(argv, 0, f"controlpath {path}\n".encode(), b"")
        return subprocess.CompletedProcess(argv, 255, b"", _HELLO_FAILED)

    monkeypatch.setattr(subprocess, "run", _run)
    assert launch_rides_live_master(["ssh", "host"], {}, runner=_run) is False
    assert sleeps == []
