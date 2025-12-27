import shlex
import socket

import pytest
from gi.repository import Gtk

from sshpilot.terminal_backends import PyXtermTerminalBackend


def _build_stub_backend():
    """Create a PyXtermTerminalBackend instance without running __init__."""

    backend = object.__new__(PyXtermTerminalBackend)
    backend.owner = None
    backend.available = True
    backend._pyxterm_cli_module = "sshpilot.vendor.pyxtermjs"
    backend._backup_pyxtermjs_template = lambda: None
    backend._replace_pyxtermjs_template = lambda: None
    backend._writable_template_path = None
    backend._temp_script_path = None
    backend._webview = None
    backend.widget = Gtk.Box()
    return backend


def test_pyxterm_preserves_flatpak_bash_arguments(monkeypatch):
    """
    Ensure pyxterm command arguments keep their quoting intact.

    The Flatpak local-shell flow uses a command like:
        flatpak-spawn --host bash -c "<script>"

    Previously, joining arguments with spaces caused the script string to be
    re-split by pyxtermjs, launching a Python prompt instead of the intended
    shell. The backend should quote each argument so shlex.split in pyxtermjs
    rebuilds the original argv.
    """

    backend = _build_stub_backend()
    command = [
        "flatpak-spawn",
        "--host",
        "bash",
        "-c",
        'echo "$SHELL"',
    ]

    captured = {}

    class DummyProcess:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            self.pid = 1234

        def poll(self):
            return None

    monkeypatch.setattr("subprocess.Popen", lambda cmd, **kwargs: DummyProcess(cmd, **kwargs))

    class DummyConnection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr("socket.create_connection", lambda *a, **k: DummyConnection())

    backend.spawn_async(command)

    pyxterm_cmd = captured.get("cmd")
    assert pyxterm_cmd is not None, "PyXterm command should be captured"

    assert "--command" in pyxterm_cmd
    assert pyxterm_cmd[pyxterm_cmd.index("--command") + 1] == "flatpak-spawn"

    cmd_args_entry = next(part for part in pyxterm_cmd if part.startswith("--cmd-args="))
    parsed_args = shlex.split(cmd_args_entry.split("=", 1)[1])
    assert parsed_args == command[1:]
