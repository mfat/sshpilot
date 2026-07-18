"""Tests for SSH connection multiplexing (ControlMaster).

Offline: the pool and option helpers are pure logic; the PluginContext
integration is driven with a fake connection_manager + monkeypatched
build_ssh_connection/subprocess, so no real ssh runs.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import sshpilot.ssh_multiplex as mux


@pytest.fixture(autouse=True)
def _reset_pool():
    # Each test starts with an empty pool regardless of order.
    mux._pool = mux._MultiplexPool()
    yield


# --- socket policy ----------------------------------------------------------

def test_socket_dir_prefers_xdg_runtime(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    d = mux.socket_dir()
    assert d == str(tmp_path / "sshpilot" / "cm")
    assert os.path.isdir(d)  # created


def test_socket_dir_falls_back_to_ssh(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert mux.socket_dir() == os.path.expanduser("~/.ssh/sockets")


def test_controlmaster_args_tokens(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    args = mux.controlmaster_args()
    cp = os.path.join(str(tmp_path), "sshpilot", "cm", "%C")
    assert args == [
        "-o", "ControlMaster=auto",
        "-o", f"ControlPath={cp}",
        "-o", "ControlPersist=60",
    ]


def test_controlmaster_args_custom_persist(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    assert "ControlPersist=120" in mux.controlmaster_args(persist="120")


# --- expire_all_masters -----------------------------------------------------

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
    for argv in calls:
        assert argv[0] == "ssh"
        assert "-O" in argv and "stop" in argv
        assert any(str(a).startswith("ControlPath=") for a in argv)
    # Responsive masters unlink their own socket; ours stay (fake run).
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
    mux.expire_all_masters(background=False)  # must not raise


# --- refcounted pool --------------------------------------------------------

def test_pool_refcount_acquire_release():
    assert mux.is_active("web") is False
    mux.acquire("web")
    mux.acquire("web")           # two surfaces want host 'web'
    assert mux.is_active("web") is True
    assert mux.release("web") is False  # still one reference left
    assert mux.is_active("web") is True
    assert mux.release("web") is True   # last reference → now zero
    assert mux.is_active("web") is False


def test_pool_release_unknown_is_false():
    assert mux.release("never-acquired") is False


def test_pool_ignores_empty_nickname():
    mux.acquire("")
    assert mux.is_active("") is False
    assert mux.release("") is False


def test_pool_isolates_hosts():
    mux.acquire("a")
    assert mux.is_active("a") is True
    assert mux.is_active("b") is False


# --- PluginContext integration ---------------------------------------------

def _ctx_and_spy(monkeypatch, active_nick=None):
    """A PluginContext wired with a fake connection_manager; build_ssh_connection
    is monkeypatched to capture the ConnectionContext it receives."""
    from sshpilot.plugins import api

    captured = {}

    class _Prepared:
        command = ["ssh", "host"]
        env = {}
        use_sshpass = False
        password = None
        use_askpass = False

    def fake_build(ctx):
        captured["extra_args"] = ctx.extra_args
        captured["remote_command"] = ctx.remote_command
        return _Prepared()

    monkeypatch.setattr(api, "build_ssh_connection", fake_build, raising=False)
    # Also patch the symbol imported inside run_command's function body.
    import sshpilot.ssh_connection_builder as scb
    monkeypatch.setattr(scb, "build_ssh_connection", fake_build, raising=False)

    ran = {}

    def fake_run(argv, **kwargs):
        ran["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="ok", stderr="")

    import subprocess
    monkeypatch.setattr(subprocess, "run", fake_run)

    conn = types.SimpleNamespace(nickname="web", host="web")
    cm = types.SimpleNamespace(find_connection_by_nickname=lambda n: conn)
    ctx = api.PluginContext(
        plugin_id="t",
        app_config=types.SimpleNamespace(
            get_setting=lambda k, d=None: d, set_setting=lambda k, v: None),
        connection_manager=cm, protocol_registry=None)
    return ctx, captured


def test_run_command_no_multiplex_when_inactive(monkeypatch):
    ctx, captured = _ctx_and_spy(monkeypatch)
    ctx.run_command("web", "echo hi")
    assert captured["extra_args"] is None  # nothing injected


def test_run_command_injects_controlmaster_when_active(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    ctx, captured = _ctx_and_spy(monkeypatch)
    ctx.acquire_multiplex("web")
    ctx.run_command("web", "echo hi")
    args = captured["extra_args"]
    assert args is not None
    assert "ControlMaster=auto" in args
    assert any(a.startswith("ControlPath=") for a in args)


def test_release_multiplex_runs_exit_on_last_ref(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    ctx, captured = _ctx_and_spy(monkeypatch)
    ctx.acquire_multiplex("web")
    ctx.acquire_multiplex("web")
    ctx.release_multiplex("web")               # still one ref → no teardown
    assert "extra_args" not in captured or captured.get("extra_args") is None \
        or "-O" not in (captured.get("extra_args") or [])
    ctx.release_multiplex("web")               # last ref → ssh -O exit built
    assert captured["extra_args"][:2] == ["-O", "exit"]
    assert not mux.is_active("web")
