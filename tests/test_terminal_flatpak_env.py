"""Tests for Flatpak environment handling in TerminalWidget."""

from sshpilot.terminal import _sanitize_flatpak_environment


def test_sanitize_flatpak_environment_removes_flatpak_values():
    original_env = {
        "LANG": "en_US.UTF-8",
        "TERM": "xterm-256color",
        "PATH": "/usr/bin:/bin",
        "XDG_DATA_HOME": "/app/data",
        "XDG_DATA_DIRS": "/app/share:/usr/share",
    }

    sanitized = _sanitize_flatpak_environment(original_env)

    assert "XDG_DATA_HOME" not in sanitized
    assert "XDG_DATA_DIRS" not in sanitized
    assert sanitized["LANG"] == original_env["LANG"]
    assert sanitized["TERM"] == original_env["TERM"]
    assert sanitized["PATH"] == original_env["PATH"]

    # Ensure the original dictionary is untouched
    assert original_env["XDG_DATA_HOME"] == "/app/data"
    assert original_env["XDG_DATA_DIRS"] == "/app/share:/usr/share"
