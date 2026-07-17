"""API 1.5 power APIs on PluginContext: run_command (reuses the native SSH/auth
path), the sandboxed files facade, and the http facade. No GTK/host required."""

import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins.api import API_VERSION, CommandResult, PluginContext
from sshpilot.plugins.registry import ProtocolRegistry


def test_api_version_is_at_least_1_11():
    # 1.11 added local captured and interactive command APIs.
    assert API_VERSION >= (1, 11)


class _Conn:
    def __init__(self, nickname):
        self.nickname = nickname


class _Manager:
    def __init__(self, conns):
        self._by_nick = {c.nickname: c for c in conns}

    def find_connection_by_nickname(self, nickname):
        return self._by_nick.get(nickname)


def _ctx(manager=None, plugin_id="test-plugin"):
    return PluginContext(plugin_id=plugin_id, app_config=None,
                         connection_manager=manager or _Manager([]),
                         protocol_registry=ProtocolRegistry())


# --- run_command ----------------------------------------------------------

def test_run_command_unknown_connection_fails_cleanly():
    res = _ctx().run_command("nope", "echo hi")
    assert isinstance(res, CommandResult)
    assert res.exit_code == -1
    assert "nope" in res.stderr


def test_run_command_builds_native_context_and_maps_output(monkeypatch):
    captured = {}

    class _Prepared:
        command = ["ssh", "host", "echo hi"]
        env = {"SSH_ASKPASS": "/x"}
        use_sshpass = False
        password = None

    def _fake_build(ctx):
        captured["remote_command"] = ctx.remote_command
        captured["native_mode"] = ctx.native_mode
        captured["command_type"] = ctx.command_type
        return _Prepared()

    def _fake_run(argv, env=None, **kwargs):
        captured["argv"] = argv
        captured["env_has_askpass"] = env.get("SSH_ASKPASS") == "/x"
        captured["stdin"] = kwargs.get("stdin")
        captured["input"] = kwargs.get("input")
        # run_command captures via temp files (not pipes) so ControlPersist
        # cannot hang on an inherited stderr pipe under verbose SSH.
        stdout = kwargs.get("stdout")
        if stdout is not None:
            stdout.write("hi\n")
            stdout.flush()
        return types.SimpleNamespace(returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr("sshpilot.ssh_connection_builder.build_ssh_connection",
                        _fake_build)
    monkeypatch.setattr(subprocess, "run", _fake_run)

    ctx = _ctx(_Manager([_Conn("web")]))
    res = ctx.run_command("web", "echo hi", timeout=5)

    assert res.ok and res.exit_code == 0 and res.stdout == "hi\n"
    assert captured["remote_command"] == "echo hi"
    assert captured["native_mode"] is True
    assert captured["command_type"] == "ssh"
    assert captured["argv"] == ["ssh", "host", "echo hi"]
    assert captured["env_has_askpass"] is True
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["input"] is None


def test_run_command_feeds_input_when_provided(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        "sshpilot.ssh_connection_builder.build_ssh_connection",
        lambda ctx: types.SimpleNamespace(
            command=["ssh", "host", "sudo -S id"], env={},
            use_sshpass=False, password=None))

    def _fake_run(argv, env=None, **kwargs):
        seen["input"] = kwargs.get("input")
        seen["stdin"] = kwargs.get("stdin")
        stdout = kwargs.get("stdout")
        if stdout is not None:
            stdout.write("")
            stdout.flush()
        return types.SimpleNamespace(returncode=0, stdout=None, stderr=None)

    monkeypatch.setattr(subprocess, "run", _fake_run)

    _ctx(_Manager([_Conn("web")])).run_command(
        "web", "sudo -S id", input="secret\n")
    assert seen.get("input") == "secret\n"
    assert seen.get("stdin") is None


def test_run_command_wraps_sshpass_for_password_auth(monkeypatch):
    seen = {}

    class _Prepared:
        command = ["ssh", "host", "id"]
        env = {}
        use_sshpass = True
        password = "secret"

    monkeypatch.setattr("sshpilot.ssh_connection_builder.build_ssh_connection",
                        lambda ctx: _Prepared())

    def _fake_wrap(argv, password, *, env=None):
        seen["password"] = password
        return (["sshpass", "-f", "/fifo", *argv], lambda: seen.__setitem__("cleaned", True))

    monkeypatch.setattr("sshpilot.ssh_password_exec.wrap_argv_with_sshpass",
                        _fake_wrap)

    def _fake_run(argv, env=None, **kw):
        seen["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    _ctx(_Manager([_Conn("web")])).run_command("web", "id")
    assert seen["password"] == "secret"
    assert seen["argv"][:3] == ["sshpass", "-f", "/fifo"]
    assert seen.get("cleaned") is True  # FIFO temp dir cleaned up


def test_run_command_timeout_is_a_failed_result(monkeypatch):
    monkeypatch.setattr("sshpilot.ssh_connection_builder.build_ssh_connection",
                        lambda ctx: types.SimpleNamespace(
                            command=["ssh"], env={}, use_sshpass=False, password=None))

    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=1)

    monkeypatch.setattr(subprocess, "run", _boom)
    res = _ctx(_Manager([_Conn("web")])).run_command("web", "sleep 99")
    assert res.exit_code == -1 and "timed out" in res.stderr.lower()


# --- local commands --------------------------------------------------------

def test_run_local_command_uses_local_shell(monkeypatch):
    seen = {}

    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["input"] = kwargs["input"]
        return types.SimpleNamespace(returncode=0, stdout="local\n", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    result = _ctx().run_local_command("printf local", input="stdin")

    assert result.ok and result.stdout == "local\n"
    assert seen["argv"][-2:] == ["-lc", "printf local"]
    assert seen["input"] == "stdin"


def test_run_local_command_uses_flatpak_host(monkeypatch):
    seen = {}

    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: True)
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "/usr/bin/flatpak-spawn" if name == "flatpak-spawn" else None,
    )

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _ctx().run_local_command("docker ps").ok
    assert seen["argv"] == [
        "/usr/bin/flatpak-spawn", "--host", "sh", "-lc", "docker ps"]


def test_open_local_command_terminal_delegates_to_host():
    calls = []
    host = types.SimpleNamespace(
        events=types.SimpleNamespace(),
        ui=types.SimpleNamespace(),
        open_local_command_terminal=lambda command, **kwargs:
            calls.append((command, kwargs)) or True,
    )
    ctx = PluginContext(
        plugin_id="test-plugin", app_config=None,
        connection_manager=_Manager([]), protocol_registry=ProtocolRegistry(),
        host=host,
    )

    assert ctx.open_local_command_terminal(
        "docker logs -f web", title="Logs",
        pty_prompt="Password:", pty_response="secret",
    )
    assert calls == [(
        "docker logs -f web",
        {"title": "Logs", "pty_prompt": "Password:", "pty_response": "secret"},
    )]


# --- files facade ---------------------------------------------------------

def test_files_roundtrip_in_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    ctx = _ctx(plugin_id="acme")
    ctx.files.write_text("notes/today.txt", "hello")
    assert ctx.files.exists("notes/today.txt")
    assert ctx.files.read_text("notes/today.txt") == "hello"
    # The file really lives under the per-plugin data dir.
    assert ctx.data_dir.endswith(os.path.join("plugin-data", "acme"))
    assert ctx.files.path("notes/today.txt").startswith(ctx.data_dir)


def test_files_rejects_path_escape(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    ctx = _ctx(plugin_id="acme")
    with pytest.raises(ValueError):
        ctx.files.path("../../etc/passwd")
    with pytest.raises(ValueError):
        ctx.files.read_text("../escape.txt")


# --- http facade ----------------------------------------------------------

def test_http_get_parses_response(monkeypatch):
    import urllib.request

    class _Resp:
        status = 200
        headers = {"Content-Type": "application/json"}

        def read(self):
            return b'{"ok": true}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _Resp())
    resp = _ctx().http.get("https://example.com/api")
    assert resp.ok and resp.status == 200
    assert resp.json() == {"ok": True}


def test_http_rejects_non_http_scheme():
    with pytest.raises(ValueError):
        _ctx().http.get("file:///etc/passwd")
