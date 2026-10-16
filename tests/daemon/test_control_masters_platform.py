"""Where sshPilot never multiplexes, nothing in the socket directory is a master.

On Windows ``ControlPath=none`` is forced, yet the per-user socket directory
can still hold stray files (left by an old build, a test, anything). Probing
them reports UNKNOWN, and UNKNOWN refuses quit, so a stray file would block
quitting for good. On platforms that do multiplex the same files must still
be probed: an unchecked path there could be a live, authenticated connection.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import sshpilot.ssh_multiplex as ssh_multiplex
from sshpilot.daemon import control_masters
from sshpilot.daemon.control_masters import (
    MasterState,
    survey_control_masters,
    terminate_owned_control_masters,
)
from sshpilot.daemon.runtime_verification import (
    SURVIVOR_UNVERIFIED,
    terminate_owned_runtime,
    verify_sshpilot_runtime_terminated,
)


@pytest.fixture
def stray_socket_dir(tmp_path, monkeypatch):
    """The default socket directory, holding files no ssh will answer for."""
    directory = tmp_path / "cm"
    directory.mkdir()
    for name in ("aaa", "bbb", "stale"):
        (directory / name).write_bytes(b"")
    monkeypatch.setattr(ssh_multiplex, "socket_dir", lambda: str(directory))
    return directory


@pytest.fixture
def unparseable_ssh(monkeypatch):
    """Every ``ssh -O`` answers with output that means "cannot tell"."""
    calls = []

    def _run(args, *_a, **_k):
        calls.append(args)
        return subprocess.CompletedProcess(
            args=args, returncode=255, stdout=b"", stderr=b"unrecognised"
        )

    monkeypatch.setattr(subprocess, "run", _run)
    return calls


def _no_multiplexing(monkeypatch):
    monkeypatch.setattr(control_masters, "control_masters_possible", lambda: False)


def _multiplexing(monkeypatch):
    monkeypatch.setattr(control_masters, "control_masters_possible", lambda: True)


def test_the_seam_follows_multiplexing_support(monkeypatch):
    monkeypatch.setattr(ssh_multiplex, "MULTIPLEXING_SUPPORTED", False)
    assert control_masters.control_masters_possible() is False
    monkeypatch.setattr(ssh_multiplex, "MULTIPLEXING_SUPPORTED", True)
    assert control_masters.control_masters_possible() is True


# --- no multiplexing (Windows) ----------------------------------------------


def test_survey_ignores_stray_files_without_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    _no_multiplexing(monkeypatch)
    assert survey_control_masters() == ()
    assert unparseable_ssh == []


def test_survey_without_multiplexing_does_not_resolve_the_directory(monkeypatch):
    _no_multiplexing(monkeypatch)
    monkeypatch.setattr(control_masters, "control_master_directory", lambda: None)
    assert survey_control_masters() == ()


def test_sweep_leaves_stray_files_alone_without_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    _no_multiplexing(monkeypatch)
    assert terminate_owned_control_masters() == ()
    assert unparseable_ssh == []
    assert sorted(p.name for p in stray_socket_dir.iterdir()) == ["aaa", "bbb", "stale"]


def test_quit_verification_passes_with_stray_files_without_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    _no_multiplexing(monkeypatch)
    result = verify_sshpilot_runtime_terminated(
        socket_path=None,
        registry_path=stray_socket_dir.parent / "registry.json",
        check_control_masters=True,
    )
    assert result.survivors == ()


def test_teardown_fallback_passes_with_stray_files_without_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    _no_multiplexing(monkeypatch)
    result = terminate_owned_runtime(
        socket_path=None,
        registry_path=stray_socket_dir.parent / "registry.json",
        sweep_control_masters=True,
    )
    assert result.survivors == ()


def test_an_explicit_directory_is_still_probed_without_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    """The seam covers only the default namespace, not a caller's own path."""
    _no_multiplexing(monkeypatch)
    probes = survey_control_masters(directory=stray_socket_dir)
    assert [p.state for p in probes] == [MasterState.UNKNOWN] * 3


# --- multiplexing (Linux/macOS) ---------------------------------------------


def test_survey_probes_every_file_with_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    _multiplexing(monkeypatch)
    probes = survey_control_masters()
    assert [p.path.name for p in probes] == ["aaa", "bbb", "stale"]
    assert all(p.state is MasterState.UNKNOWN for p in probes)
    assert len(unparseable_ssh) == 3


def test_survey_with_multiplexing_still_refuses_an_unresolved_directory(monkeypatch):
    _multiplexing(monkeypatch)
    monkeypatch.setattr(control_masters, "control_master_directory", lambda: None)
    with pytest.raises(LookupError):
        survey_control_masters()


def test_quit_verification_refuses_unchecked_paths_with_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    _multiplexing(monkeypatch)
    result = verify_sshpilot_runtime_terminated(
        socket_path=None,
        registry_path=stray_socket_dir.parent / "registry.json",
        check_control_masters=True,
    )
    assert len(result.survivors) == 3
    assert all(s.kind == SURVIVOR_UNVERIFIED for s in result.survivors)


def test_sweep_never_unlinks_unchecked_paths_with_multiplexing(
    stray_socket_dir, unparseable_ssh, monkeypatch
):
    _multiplexing(monkeypatch)
    surviving = terminate_owned_control_masters()
    assert len(surviving) == 3
    assert sorted(p.name for p in stray_socket_dir.iterdir()) == ["aaa", "bbb", "stale"]
