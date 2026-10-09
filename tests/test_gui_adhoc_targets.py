"""Unsaved targets typed into the search box open in a real terminal tab."""

import shutil
import socket
import threading

import pytest

from tests._gui_harness import requires_gui

requires_gui()

pytestmark = pytest.mark.gui

BANNER = b"SSHPILOT-ADHOC-42\r\n"


class _BannerServer:
    """Accept TCP clients and greet each with a banner telnet prints."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.sock.settimeout(0.25)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            self.accepted += 1
            with conn:
                try:
                    conn.sendall(BANNER)
                    conn.settimeout(3.0)
                    conn.recv(1024)
                except OSError:
                    pass

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=5)
        self.sock.close()


@pytest.fixture
def captured_output(monkeypatch):
    from sshpilot import terminal as terminal_module

    chunks = []
    original = terminal_module.TerminalWidget._on_daemon_output

    def _capture(self, data):
        chunks.append(bytes(data))
        return original(self, data)

    monkeypatch.setattr(terminal_module.TerminalWidget, "_on_daemon_output", _capture)
    return chunks


def _wait_for(gui, predicate, timeout_ms=10000):
    waited = 0
    while not predicate() and waited < timeout_ms:
        gui.pump(200)
        waited += 200
    return predicate()


@pytest.mark.skipif(not shutil.which("telnet"), reason="telnet not installed")
def test_unsaved_telnet_target_reaches_the_server(gui, captured_output):
    from sshpilot.omni_search import search_omni

    server = _BannerServer()
    try:
        win = gui.window
        before = {c.nickname for c in win.connection_manager.get_connections()}
        result = next(
            r for r in search_omni(win, f"telnet 127.0.0.1 {server.port}")
            if r.kind == "adhoc"
        )
        win._omni_search.activate_result(result)

        assert _wait_for(gui, lambda: BANNER.strip() in b"".join(captured_output))
        assert server.accepted >= 1
        # Nothing was saved.
        after = {c.nickname for c in win.connection_manager.get_connections()}
        assert after == before
    finally:
        server.stop()


def test_unsaved_ssh_target_launches_openssh(gui):
    """The daemon opens ``ssh -p 1 probe@127.0.0.1`` instead of refusing an
    unknown connection id; port 1 refuses, which proves OpenSSH ran."""
    win = gui.window
    before = set(win.tab_view.get_pages())
    assert win.open_cli_connect(["ssh", "-p", "1", "probe@127.0.0.1"])

    def _reason():
        for page in win.tab_view.get_pages():
            if page in before:
                continue
            terminal = page.get_child()
            state = getattr(terminal, "connection_state", None)
            if getattr(state, "name", "") == "FAILED":
                return str(getattr(terminal, "connection_state_reason", "") or "")
        return ""

    assert _wait_for(gui, lambda: bool(_reason()))
    reason = _reason()
    assert "does not exist" not in reason
    assert "refused" in reason.lower()
