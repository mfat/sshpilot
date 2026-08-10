"""Tests for the daemon-shared SSH ControlMaster policy."""

import os
import types

import sshpilot.ssh_multiplex as mux


def test_socket_dir_prefers_xdg_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    directory = mux.socket_dir()
    assert directory == str(tmp_path / "sshpilot" / "cm")
    assert os.path.isdir(directory)


def test_socket_dir_falls_back_to_ssh(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert mux.socket_dir() == os.path.expanduser("~/.ssh/sockets")


def test_controlmaster_args_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert mux.controlmaster_args() == [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={tmp_path / 'sshpilot' / 'cm' / '%C'}",
        "-o", "ControlPersist=60",
    ]


def test_controlmaster_args_custom_persist(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert "ControlPersist=120" in mux.controlmaster_args(persist="120")


def test_expire_all_masters_stops_each_socket(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_dir = mux.socket_dir()
    for name in ("aaa", "bbb"):
        open(os.path.join(sock_dir, name), "w").close()

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(mux.subprocess, "run", fake_run)
    mux.expire_all_masters(background=False)

    assert len(calls) == 2
    assert all(argv[0] == "ssh" and "-O" in argv and "stop" in argv for argv in calls)
    assert sorted(os.listdir(sock_dir)) == ["aaa", "bbb"]


def test_expire_all_masters_unlinks_stale_sockets(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    sock_dir = mux.socket_dir()
    stale = os.path.join(sock_dir, "stale")
    open(stale, "w").close()
    monkeypatch.setattr(
        mux.subprocess, "run",
        lambda argv, **kw: types.SimpleNamespace(
            returncode=255, stdout=b"", stderr=b"Control socket connect: refused"),
    )
    mux.expire_all_masters(background=False)
    assert not os.path.exists(stale)


def test_expire_all_masters_missing_dir_is_noop(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "gone"))
    monkeypatch.setattr(mux.os, "listdir", lambda p: (_ for _ in ()).throw(OSError()))
    mux.expire_all_masters(background=False)
