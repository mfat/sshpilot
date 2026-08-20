"""Regression tests for local-shell environment sanitization.

sshPilot inherits the environment of the terminal it was launched from. On
macOS that leaks Apple Terminal's ``TERM_PROGRAM`` / ``TERM_PROGRAM_VERSION`` /
``TERM_SESSION_ID`` into the embedded local shell, which then believes it is
running inside Apple Terminal and macOS loads its shell-session integration —
printing a ``Restored session: ...`` banner sshPilot never asked for. The
sanitizer re-identifies the embedded shell as sshPilot and nothing else.
"""

import sys
import types

from sshpilot.terminal import sanitize_local_shell_env


def _ensure_cairo_stub():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()


def _apple_terminal_env():
    return {
        "TERM_PROGRAM": "Apple_Terminal",
        "TERM_PROGRAM_VERSION": "455",
        "TERM_SESSION_ID": "ABC",
        "TERM": "xterm-256color",
        "HOME": "/Users/test",
        "SSH_AUTH_SOCK": "/tmp/agent.sock",
    }


def test_sanitize_local_shell_env_strips_apple_terminal_identity(monkeypatch):
    import sshpilot.terminal as terminal

    monkeypatch.setattr(terminal, "is_macos", lambda: True)
    sanitized = sanitize_local_shell_env(_apple_terminal_env())

    assert sanitized["TERM_PROGRAM"] == "sshPilot"
    assert "TERM_PROGRAM_VERSION" not in sanitized
    assert "TERM_SESSION_ID" not in sanitized
    # Everything else must pass through untouched.
    assert sanitized["TERM"] == "xterm-256color"
    assert sanitized["HOME"] == "/Users/test"
    assert sanitized["SSH_AUTH_SOCK"] == "/tmp/agent.sock"


def test_sanitize_local_shell_env_does_not_mutate_source(monkeypatch):
    import sshpilot.terminal as terminal

    monkeypatch.setattr(terminal, "is_macos", lambda: True)
    source = _apple_terminal_env()
    before = dict(source)
    sanitize_local_shell_env(source)
    assert source == before, "the helper must not mutate the caller's dict"


def test_sanitize_local_shell_env_non_macos_unchanged(monkeypatch):
    import sshpilot.terminal as terminal

    monkeypatch.setattr(terminal, "is_macos", lambda: False)
    source = _apple_terminal_env()
    sanitized = sanitize_local_shell_env(source)
    assert sanitized == source
    assert sanitized is not source, "the helper still returns a copy"


def test_sanitize_local_shell_env_sets_program_even_when_absent(monkeypatch):
    import sshpilot.terminal as terminal

    monkeypatch.setattr(terminal, "is_macos", lambda: True)
    sanitized = sanitize_local_shell_env({"HOME": "/Users/test", "PATH": "/usr/bin"})
    assert sanitized["TERM_PROGRAM"] == "sshPilot"
    assert "TERM_PROGRAM_VERSION" not in sanitized
    assert "TERM_SESSION_ID" not in sanitized
    assert sanitized["HOME"] == "/Users/test"


def test_direct_spawn_path_runs_env_through_sanitizer(monkeypatch):
    """The direct-spawn local shell must not leak the launching terminal."""
    _ensure_cairo_stub()
    import sshpilot.identity as identity_mod
    import sshpilot.terminal as terminal
    from sshpilot.terminal import TerminalWidget

    monkeypatch.setattr(terminal, "is_flatpak", lambda: False)
    monkeypatch.setattr(terminal, "is_macos", lambda: True)
    monkeypatch.setattr(
        terminal.pwd,
        "getpwuid",
        lambda _uid: types.SimpleNamespace(
            pw_name="testuser", pw_dir="/Users/test", pw_shell="/bin/zsh"
        ),
    )
    manager = types.SimpleNamespace(apply_selected_to_env=lambda env: dict(env))
    monkeypatch.setattr(identity_mod, "get_identity_manager", lambda: manager)
    monkeypatch.setattr(terminal.GLib, "timeout_add_seconds", lambda *a, **k: 1)

    spawned = {}
    widget = TerminalWidget.__new__(TerminalWidget)
    widget.backend = types.SimpleNamespace(
        spawn_async=lambda **kwargs: spawned.update(kwargs)
    )
    widget._fallback_timer_id = None

    widget._setup_local_shell_direct()

    env = spawned.get("env") or {}
    assert "TERM_PROGRAM_VERSION" not in env
    assert "TERM_SESSION_ID" not in env
    assert env.get("TERM_PROGRAM") == "sshPilot"
    assert "HOME" in env


def test_agent_spawn_path_runs_env_through_sanitizer(monkeypatch):
    """The agent-based local shell must not leak the launching terminal."""
    _ensure_cairo_stub()
    import sshpilot.identity as identity_mod
    import sshpilot.terminal as terminal
    from sshpilot.terminal import TerminalWidget

    monkeypatch.setattr(terminal, "is_macos", lambda: True)
    manager = types.SimpleNamespace(apply_selected_to_env=lambda env: dict(env))
    monkeypatch.setattr(identity_mod, "get_identity_manager", lambda: manager)
    monkeypatch.setattr(terminal.GLib, "timeout_add_seconds", lambda *a, **k: 1)

    spawned = {}
    widget = TerminalWidget.__new__(TerminalWidget)
    widget.backend = types.SimpleNamespace(
        spawn_async=lambda **kwargs: spawned.update(kwargs)
    )
    widget._fallback_timer_id = None
    widget.process_pid = None

    client = types.SimpleNamespace(build_agent_command=lambda **kwargs: ["/bin/sh"])
    assert widget._spawn_agent_shell(client, 80, 24) is True

    env = spawned.get("env") or {}
    assert "TERM_PROGRAM_VERSION" not in env
    assert "TERM_SESSION_ID" not in env
    assert env.get("TERM_PROGRAM") == "sshPilot"