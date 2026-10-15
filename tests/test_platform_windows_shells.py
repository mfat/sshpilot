"""Which shell a Windows local terminal tab runs."""

from __future__ import annotations

import os

from sshpilot.platform.windows.shells import available_shells, shell_command


def _environ(tmp_path, *, cmd=True, powershell=True, wsl=False):
    system32 = tmp_path / "Windows" / "System32"
    (system32 / "WindowsPowerShell" / "v1.0").mkdir(parents=True)
    if cmd:
        (system32 / "cmd.exe").write_text("")
    if powershell:
        (system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe").write_text("")
    if wsl:
        (system32 / "wsl.exe").write_text("")
    return {"SystemRoot": str(tmp_path / "Windows"), "PATH": ""}


def _which(found):
    return lambda name, path=None: found.get(name)


def test_auto_prefers_powershell_7_then_windows_powershell_then_cmd(tmp_path):
    environ = _environ(tmp_path)
    assert shell_command("auto", environ, _which({"pwsh.exe": "C:/pwsh/pwsh.exe"}))[0] == "C:/pwsh/pwsh.exe"
    assert shell_command("auto", environ, _which({})) == [
        os.path.join(environ["SystemRoot"], "System32", "WindowsPowerShell", "v1.0", "powershell.exe"),
        "-NoLogo",
    ]
    no_powershell = _environ(tmp_path / "b", powershell=False)
    assert shell_command(None, no_powershell, _which({}))[0].endswith("cmd.exe")


def test_only_installed_shells_are_offered(tmp_path):
    environ = _environ(tmp_path, wsl=True)
    assert list(available_shells(environ, _which({}))) == ["powershell", "cmd", "wsl"]
    assert shell_command("wsl", environ, _which({}))[1:] == ["--cd", "~"]


def test_a_missing_choice_falls_back(tmp_path):
    environ = _environ(tmp_path)
    assert shell_command("pwsh", environ, _which({}))[0].endswith("powershell.exe")
    assert shell_command("bogus", environ, _which({}))[0].endswith("powershell.exe")
