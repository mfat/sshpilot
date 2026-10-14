"""Plain-text links and console-host titles in the built-in terminal."""

from __future__ import annotations

import pytest

from sshpilot.terminal_model_common import is_console_host_title, url_at


def test_url_under_the_column():
    text = "see https://example.org/a?b=1, or ftp://host/x."
    start = text.index("https")
    assert url_at(text, start) == "https://example.org/a?b=1"
    assert url_at(text, start + 10) == "https://example.org/a?b=1"
    assert url_at(text, text.index("ftp") + 3) == "ftp://host/x"
    assert url_at(text, 0) is None
    assert url_at(text, text.index(",")) is None


@pytest.mark.parametrize(
    "title",
    [
        r"C:\Windows\System32\OpenSSH\ssh.exe",
        r"C:\WINDOWS\system32\cmd.exe - exit",
        r"Administrator: C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
        r"c:\tools\run.bat",
    ],
)
def test_console_host_titles_are_recognised(title):
    assert is_console_host_title(title)


@pytest.mark.parametrize(
    "title",
    ["root@router: ~", "vim notes.txt", "", "C:\\Users\\me", "build.exe is done", "user@host: C:\\x.exe"],
)
def test_shell_titles_are_kept(title):
    assert not is_console_host_title(title)
