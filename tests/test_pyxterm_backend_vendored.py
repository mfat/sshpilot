import socket
import subprocess
import sys
import time
import types
from pathlib import Path


def test_pyxterm_backend_launches_vendored_cli(monkeypatch):
    from sshpilot.terminal_backends import PyXtermTerminalBackend

    backend = PyXtermTerminalBackend.__new__(PyXtermTerminalBackend)
    backend.available = True
    backend.owner = types.SimpleNamespace(emit=lambda *a, **k: None)

    class WebViewStub:
        def __init__(self):
            self.loaded = None
            self.connections = []

        def load_uri(self, uri):
            self.loaded = uri

        def connect(self, *args):
            self.connections.append(args)
            return object()

    backend.widget = types.SimpleNamespace(grab_focus=lambda: None)
    backend._webview = WebViewStub()
    vendor_init = Path(__file__).resolve().parents[1] / "sshpilot" / "vendor" / "pyxtermjs" / "__init__.py"
    vendored_module = types.SimpleNamespace(__file__=str(vendor_init))
    backend._vendored_pyxterm = vendored_module
    backend._pyxterm = vendored_module
    backend._pyxterm_cli_module = "sshpilot.vendor.pyxtermjs"
    backend._template_backed_up = False
    backend._temp_script_path = None
    backend._server_process = None
    backend._child_pid = None
    backend._backup_pyxtermjs_template = lambda: None
    backend._replace_pyxtermjs_template = lambda: None

    captured: dict[str, object] = {}
    dummy_port = 24680

    class DummySocket:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def bind(self, address):
            return None

        def listen(self, backlog):
            return None

        def getsockname(self):
            return ("127.0.0.1", dummy_port)

    class DummyConnection:
        def __init__(self, *args, **kwargs):
            captured["probe"] = (args, kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    class DummyPopen:
        def __init__(self, cmd, **kwargs):
            captured["cmd"] = cmd
            captured["popen_kwargs"] = kwargs
            self.pid = 4242
            self._returncode = None
            self.returncode = None

        def poll(self):
            return self._returncode

        def terminate(self):
            self._returncode = 0

        def wait(self, timeout=None):
            return self._returncode

        def kill(self):
            self._returncode = -9

    monkeypatch.setattr(socket, "socket", lambda *a, **k: DummySocket())
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: DummyConnection(*a, **k))
    monkeypatch.setattr(time, "sleep", lambda *a, **k: None)
    monkeypatch.setattr(subprocess, "Popen", DummyPopen)

    backend.spawn_async(["bash"], env=None, cwd=None)

    assert captured["cmd"][:3] == [sys.executable, "-m", "sshpilot.vendor.pyxtermjs"]
    assert captured["cmd"][captured["cmd"].index("--port") + 1] == str(dummy_port)
    assert backend._webview.loaded == f"http://127.0.0.1:{dummy_port}"
