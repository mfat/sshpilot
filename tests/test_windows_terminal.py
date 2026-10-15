"""Tests for Windows external-terminal discovery and launch (platform_utils)."""

import base64

import pytest

platform_utils = pytest.importorskip("sshpilot.platform_utils")

SSH = r"C:\Program Files\OpenSSH\ssh.exe"
ARGV = (SSH, "-F", r"C:\Users\me\.ssh\config", "it's-a-host")


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(platform_utils, "is_windows", lambda: True)
    monkeypatch.setattr(platform_utils, "is_macos", lambda: False)


def _installed(monkeypatch, *names):
    paths = {name: rf"C:\bin\{name}" for name in names}
    monkeypatch.setattr(platform_utils.shutil, "which", lambda name: paths.get(name))


def _launch(monkeypatch, terminal, environment=None):
    seen = []
    monkeypatch.setattr(
        platform_utils.subprocess, "Popen",
        lambda cmd, **kw: seen.append((cmd, kw)) or object(),
    )
    ok = platform_utils.open_system_terminal(
        terminal, "ignored", environment=environment, argv=ARGV
    )
    return ok, seen


def _decoded_script(cmd):
    return base64.b64decode(cmd[cmd.index("-EncodedCommand") + 1]).decode("utf-16-le")


def test_default_prefers_windows_terminal(monkeypatch, windows):
    _installed(monkeypatch, "wt.exe", "powershell.exe", "cmd.exe")
    assert platform_utils.get_default_terminal_command() == [r"C:\bin\wt.exe"]


def test_falls_back_to_powershell_then_cmd(monkeypatch, windows):
    _installed(monkeypatch, "powershell.exe", "cmd.exe")
    assert platform_utils.find_any_terminal() == [r"C:\bin\powershell.exe"]
    _installed(monkeypatch, "cmd.exe")
    assert platform_utils.find_any_terminal() == [r"C:\bin\cmd.exe"]


def test_preferred_terminal_uses_shutil_which(monkeypatch, windows):
    _installed(monkeypatch, "pwsh.exe")

    class Config:
        def get_setting(self, key, default=None):
            return {"external-terminal": "pwsh.exe"}.get(key, default)

    assert platform_utils.get_user_preferred_terminal(Config()) == [r"C:\bin\pwsh.exe"]


def test_powershell_runs_argv_literally_and_stays_open(monkeypatch, windows):
    ok, seen = _launch(
        monkeypatch, [r"C:\bin\powershell.exe"], {"SSH_AUTH_SOCK": r"\\.\pipe\agent"}
    )
    assert ok is True
    cmd, kw = seen[0]
    assert "-NoExit" in cmd
    assert kw["creationflags"] == getattr(platform_utils.subprocess, "CREATE_NEW_CONSOLE", 0)
    script = _decoded_script(cmd)
    assert "$env:SSH_AUTH_SOCK = '\\\\.\\pipe\\agent'" in script
    assert (
        "& 'C:\\Program Files\\OpenSSH\\ssh.exe' '-F' "
        "'C:\\Users\\me\\.ssh\\config' 'it''s-a-host'"
    ) in script


def test_windows_terminal_hosts_powershell(monkeypatch, windows):
    _installed(monkeypatch, "pwsh.exe")
    ok, seen = _launch(monkeypatch, [r"C:\bin\wt.exe"])
    assert ok is True
    cmd, _ = seen[0]
    assert cmd[:4] == [r"C:\bin\wt.exe", "new-tab", "--", r"C:\bin\pwsh.exe"]
    assert "it''s-a-host" in _decoded_script(cmd)


def test_cmd_refuses_metacharacters(monkeypatch, windows):
    seen = []
    monkeypatch.setattr(
        platform_utils.subprocess, "Popen", lambda cmd, **kw: seen.append(cmd)
    )
    assert platform_utils.open_system_terminal(
        ["cmd.exe"], "ignored", argv=(SSH, "host&calc")
    ) is False
    assert seen == []


def test_cmd_keeps_window_open(monkeypatch, windows):
    ok, seen = _launch(monkeypatch, ["cmd.exe"])
    assert ok is True
    cmd, _ = seen[0]
    assert cmd[:2] == ["cmd.exe", "/k"]
    assert cmd[2].startswith('"C:\\Program Files\\OpenSSH\\ssh.exe" -F')
