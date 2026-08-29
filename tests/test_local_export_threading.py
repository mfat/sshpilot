"""Regression coverage for issue #1200: local ``.spbk`` export deadlock.

``_run_local_export`` (extracted from ``_choose_export_path``, mirroring the
already-threaded ``_export_to_ssh_server``/``_run_daemon_import`` pattern) must
run the blocking ``export_backup()`` daemon RPC off the GTK main thread. The
daemon's encryption-passphrase interaction can only be claimed/answered once
that same thread's main loop is free to process it, so calling the RPC inline
(the old code) deadlocks an encrypted export until the interaction expires.
"""

import threading
import time
from types import SimpleNamespace

from sshpilot.api.models.secrets import (
    SecretTransferMessage,
    SecretTransferMessageCode,
)
from sshpilot.window_dialogs import WindowConfigDialogsMixin


class _ExportWin(WindowConfigDialogsMixin):
    def __init__(self):
        self.dialogs = []

    def _simple_dialog(self, heading, body):
        self.dialogs.append((heading, body))


class _MainLoopSimulator:
    """Stands in for the GTK main loop: ``idle_add`` only queues callbacks —
    they run only when ``drain()`` is called, exactly like the real GLib main
    loop only runs callbacks on the thread that is pumping it."""

    def __init__(self):
        self._queue = []
        self._lock = threading.Lock()

    def idle_add(self, callback):
        with self._lock:
            self._queue.append(callback)
        return 0

    def drain(self):
        while True:
            with self._lock:
                if not self._queue:
                    return
                callback = self._queue.pop(0)
            callback()


def test_local_export_does_not_deadlock_encryption_interaction(monkeypatch):
    """Test 1 from the issue: export_backup() cannot complete until a
    frontend interaction scheduled on the (simulated) GTK main-loop side is
    processed. The export must run off the calling thread so that thread
    stays free to pump the loop and answer the interaction."""
    win = _ExportWin()
    loop = _MainLoopSimulator()
    monkeypatch.setattr("sshpilot.window_dialogs.GLib.idle_add", loop.idle_add)
    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.progress_dialog",
        lambda *a, **kw: (lambda _t: None, lambda: None))

    interaction_answered = threading.Event()
    export_thread_ident = {}

    class Controller:
        def export_backup(self, **kwargs):
            export_thread_ident['id'] = threading.get_ident()
            # Mirrors the daemon: the encryption-passphrase interaction is
            # delivered to the frontend via the same idle_add the real
            # password dialog would use, and can only be answered once
            # something pumps the (simulated) main loop.
            loop.idle_add(interaction_answered.set)
            if not interaction_answered.wait(timeout=2.0):
                raise AssertionError(
                    "encryption interaction was never processed — the "
                    "caller's thread is blocked inside export_backup "
                    "(issue #1200 deadlock)")
            return SimpleNamespace(
                status=SimpleNamespace(value="success"),
                counts={"credentials": 0, "private_keys": 0}, warnings=())

    win.secrets_controller = Controller()
    calling_thread_ident = threading.get_ident()

    win._run_local_export(
        export_path="/tmp/x.spbk", connections=[], options={}, encrypt=True)

    # The call above must return immediately — it only shows a progress
    # dialog and starts a worker thread — leaving this ("GTK main") thread
    # free to pump the loop, exactly as GTK is between event-loop iterations.
    # Keep pumping until the interaction is answered: checking 'id' alone
    # would race, exiting right after it is set but before the interaction
    # callback that follows it is queued.
    deadline = time.monotonic() + 2.0
    while not interaction_answered.is_set() and time.monotonic() < deadline:
        loop.drain()
        time.sleep(0.01)

    assert export_thread_ident.get('id') is not None, "export_backup was never called"
    assert export_thread_ident['id'] != calling_thread_ident, (
        "export_backup ran on the calling (GTK) thread — the fix must run "
        "it on a worker thread")
    assert interaction_answered.is_set(), "interaction was never answered"

    # Drain the worker's completion callback too (also delivered via idle_add).
    deadline = time.monotonic() + 2.0
    while not win.dialogs and time.monotonic() < deadline:
        loop.drain()
        time.sleep(0.01)

    assert win.dialogs, "export completion was never reported to the UI"
    assert win.dialogs[0] == ("Export Successful", (
        "Backup saved to:\n/tmp/x.spbk\n\n0 credential(s) and 0 private "
        "key(s) included; encryption: on."))


def test_local_export_surfaces_worker_exception(monkeypatch):
    """Exceptions raised inside the worker thread must be reported to the
    user, not swallowed or left to crash a background thread silently."""
    win = _ExportWin()

    class Thread:
        def __init__(self, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr("sshpilot.window_dialogs.threading.Thread", Thread)
    monkeypatch.setattr(
        "sshpilot.window_dialogs.GLib.idle_add", lambda cb: (cb(), False)[1])
    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.progress_dialog",
        lambda *a, **kw: (lambda _t: None, lambda: None))

    class Controller:
        def export_backup(self, **kwargs):
            raise RuntimeError("daemon unreachable")

    win.secrets_controller = Controller()
    win._run_local_export(
        export_path="/tmp/x.spbk", connections=[], options={}, encrypt=False)

    assert len(win.dialogs) == 1
    assert win.dialogs[0][0] == "Export Failed"
    assert "daemon unreachable" in win.dialogs[0][1]


def test_local_export_unencrypted_success(monkeypatch):
    """Unencrypted export must be unaffected by the threading fix."""
    win = _ExportWin()

    class Thread:
        def __init__(self, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            self.target()

    calls = []

    class Controller:
        def export_backup(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                status=SimpleNamespace(value="success"),
                counts={"credentials": 3, "private_keys": 1}, warnings=())

    monkeypatch.setattr("sshpilot.window_dialogs.threading.Thread", Thread)
    monkeypatch.setattr(
        "sshpilot.window_dialogs.GLib.idle_add", lambda cb: (cb(), False)[1])
    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.progress_dialog",
        lambda *a, **kw: (lambda _t: None, lambda: None))

    win.secrets_controller = Controller()
    win._run_local_export(
        export_path="/tmp/plain.spbk", connections=[],
        options={"app_settings": True}, encrypt=False)

    assert len(calls) == 1
    assert calls[0]["options"]["encrypted"] is False
    assert len(win.dialogs) == 1
    assert win.dialogs[0][0] == "Export Successful"
    assert "encryption: off" in win.dialogs[0][1]


def test_local_export_reports_encryption_timeout_message(monkeypatch):
    """The daemon's distinct timeout message must reach the user unchanged —
    not rewritten to a generic 'cancelled' message on the frontend."""
    win = _ExportWin()

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    class Controller:
        def export_backup(self, **kwargs):
            return SimpleNamespace(
                status=SimpleNamespace(value="interaction_required"),
                message=SecretTransferMessage(
                    code=SecretTransferMessageCode.ENCRYPTION_REQUEST_TIMED_OUT,
                ),
                counts={}, warnings=())

    monkeypatch.setattr("sshpilot.window_dialogs.threading.Thread", Thread)
    monkeypatch.setattr(
        "sshpilot.window_dialogs.GLib.idle_add", lambda cb: (cb(), False)[1])
    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.progress_dialog",
        lambda *a, **kw: (lambda _t: None, lambda: None))

    win.secrets_controller = Controller()
    win._run_local_export(
        export_path="/tmp/x.spbk", connections=[], options={}, encrypt=True)

    assert win.dialogs == [
        ("Export Failed", "Encryption password request timed out"),
    ]
