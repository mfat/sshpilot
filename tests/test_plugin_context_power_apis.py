"""API 1.5 power APIs on PluginContext: run_command (reuses the native SSH/auth
path), the sandboxed files facade, and the http facade. No GTK/host required."""

import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins.api import (
    API_VERSION, CommandResult, PluginContext, StreamHandle,
)
from sshpilot.plugins.registry import ProtocolRegistry


def test_api_version_is_at_least_1_13():
    # 1.13 added line-oriented command streaming APIs.
    assert API_VERSION >= (1, 13)


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
    assert res.stderr == "The daemon is unavailable"


# --- local commands --------------------------------------------------------

def test_run_local_command_uses_local_shell(monkeypatch):
    seen = {}

    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: False)

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
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: False)
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


def test_run_local_command_posix_argv_is_sh_lc(monkeypatch):
    seen = {}
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: False)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/sh" if name == "sh" else None)

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _ctx().run_local_command("docker ps").ok
    assert seen["argv"] == ["/usr/bin/sh", "-lc", "docker ps"]
    # POSIX keeps the locale's decoding.
    assert "encoding" not in seen["kwargs"] and "errors" not in seen["kwargs"]


def test_run_local_command_uses_cmd_on_windows(monkeypatch):
    seen = {}
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: True)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _ctx().run_local_command("docker ps").ok
    assert seen["argv"] == r'"C:\Windows\system32\cmd.exe" /d /s /c "docker ps"'
    assert seen["kwargs"]["text"] is True
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["errors"] == "replace"


def test_run_local_command_windows_falls_back_to_cmd_exe(monkeypatch):
    seen = {}
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: True)
    monkeypatch.delenv("COMSPEC", raising=False)

    def _fake_run(argv, **kwargs):
        seen["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert _ctx().run_local_command("ver").ok
    assert seen["argv"] == '"cmd.exe" /d /s /c "ver"'


@pytest.mark.skipif(sys.platform != "win32", reason="needs a real cmd.exe")
def test_run_local_command_windows_keeps_inner_quotes(monkeypatch):
    # subprocess must not list2cmdline the command: cmd reads \" literally.
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: True)
    from sshpilot.plugins.api import local_command_line
    command = local_command_line(
        [sys.executable, "-c", "import sys; print(sys.argv[1:])", "x y", "{{json .}}"])
    result = _ctx().run_local_command(command)
    assert result.ok, result.stderr
    assert result.stdout.strip() == "['x y', '{{json .}}']"


@pytest.mark.parametrize("failure,expected", [
    ("empty", "Command is empty"),
    ("timeout", "Command timed out"),
    ("host_executor", "flatpak-spawn is unavailable; cannot run host command"),
    ("start", "external OS diagnostic"),
])
def test_local_command_public_failure_contract_is_unchanged(monkeypatch, failure, expected):
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak",
                        lambda: failure == "host_executor")
    monkeypatch.setattr("shutil.which", lambda _name: None)

    def fail(*args, **kwargs):
        if failure == "timeout":
            raise subprocess.TimeoutExpired("docker", 8)
        raise OSError("external OS diagnostic")

    monkeypatch.setattr(subprocess, "run", fail)
    result = _ctx().run_local_command("" if failure == "empty" else "docker ps")
    assert type(result) is CommandResult
    assert (result.exit_code, result.stdout, result.stderr) == (-1, "", expected)


def test_local_command_public_nonzero_result_remains_raw(monkeypatch):
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: False)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs:
                        types.SimpleNamespace(returncode=127, stdout="", stderr="shell output\n"))
    result = _ctx().run_local_command("missing-tool")
    assert (result.exit_code, result.stdout, result.stderr) == (127, "", "shell output\n")


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


# --- streaming commands (API >= 1.13) --------------------------------------

def test_run_command_stream_unknown_connection_calls_on_done():
    done = []
    handle = _ctx().run_command_stream(
        "nope", "echo hi", on_line=lambda _l: None, on_done=done.append)
    assert isinstance(handle, StreamHandle)
    assert not handle.running
    assert done == [-1]


def test_run_local_command_stream_uses_local_shell(monkeypatch):
    seen = {}
    lines = []
    done = []

    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: False)

    class _FakeStdout:
        def __iter__(self):
            yield "local-line\n"

        def close(self):
            pass

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["stdin"] = kwargs.get("stdin")
            self.stdout = _FakeStdout()
            self.stdin = None
            self._code = None

        def poll(self):
            return self._code

        def terminate(self):
            self._code = -15

        def kill(self):
            self._code = -9

        def wait(self, timeout=None):
            self._code = 0
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FakeProc)

    handle = _ctx().run_local_command_stream(
        "printf 'local-line\\n'", on_line=lines.append, on_done=done.append)
    import time
    for _ in range(50):
        if lines and done:
            break
        time.sleep(0.02)
    assert seen["argv"][-2:] == ["-lc", "printf 'local-line\\n'"]
    assert lines == ["local-line"]
    assert done == [0]
    handle.stop()


def _capture_stream_popen(monkeypatch, seen):
    class _FakeStdout:
        def __iter__(self):
            return iter(())

        def close(self):
            pass

    class _FakeProc:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen["kwargs"] = kwargs
            self.stdout = _FakeStdout()
            self.stdin = None

        def poll(self):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", _FakeProc)


def test_run_local_command_stream_posix_argv_is_sh_lc(monkeypatch):
    seen = {}
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: False)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/sh" if name == "sh" else None)
    _capture_stream_popen(monkeypatch, seen)

    _ctx().run_local_command_stream("docker logs -f web", on_line=lambda _l: None).stop()
    assert seen["argv"] == ["/usr/bin/sh", "-lc", "docker logs -f web"]
    assert "encoding" not in seen["kwargs"] and "errors" not in seen["kwargs"]


def test_run_local_command_stream_uses_cmd_on_windows(monkeypatch):
    seen = {}
    monkeypatch.setattr("sshpilot.platform_utils.is_flatpak", lambda: False)
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: True)
    monkeypatch.setenv("COMSPEC", r"C:\Windows\system32\cmd.exe")
    _capture_stream_popen(monkeypatch, seen)

    _ctx().run_local_command_stream("docker logs -f web", on_line=lambda _l: None).stop()
    assert seen["argv"] == (
        r'"C:\Windows\system32\cmd.exe" /d /s /c "docker logs -f web"')
    assert seen["kwargs"]["encoding"] == "utf-8"
    assert seen["kwargs"]["errors"] == "replace"


# --- local command lines (builtin summaries) ----------------------------------------

def test_local_command_line_posix_uses_shlex(monkeypatch):
    from sshpilot.plugins.api import local_command_line
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: False)
    assert local_command_line(["docker", "ps", "--format", "{{.Names}} x"]) == (
        "docker ps --format '{{.Names}} x'")


def test_local_command_line_windows_uses_list2cmdline(monkeypatch):
    from sshpilot.plugins.api import local_command_line
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: True)
    assert local_command_line(["docker", "ps", "--format", "{{.Names}} x"]) == (
        'docker ps --format "{{.Names}} x"')


def test_summary_run_lines_passes_platform_command_line(monkeypatch):
    from sshpilot.plugins.builtin import _summary
    monkeypatch.setattr("sshpilot.plugins.api._windows", lambda: True)
    seen = {}

    class _Ctx:
        def _run_local_command(self, command, timeout):
            seen["command"] = command
            return CommandResult(0, "a\n\nb\n", "")

    assert _summary.run_lines(_Ctx(), ["podman", "ps", "-a"]) == ["a", "b"]
    assert seen["command"] == "podman ps -a"


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
