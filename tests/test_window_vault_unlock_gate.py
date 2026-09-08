"""Vault-unlock gate for config export/import (WindowConfigDialogsMixin).

Export/import is daemon-owned: the gate reads the lock state from the
``secrets_controller`` (never ``secret_storage``) and drives unlock through
``prompt_unlock``. Applying a manifest runs the daemon RPC in a worker thread
and reports the ``SecretTransferResult`` through ``_show_daemon_import_result``.
"""

from types import SimpleNamespace

from sshpilot.window_dialogs import WindowConfigDialogsMixin


class _Win(WindowConfigDialogsMixin):
    def __init__(self):
        self.dialogs = []
        self.import_results = []
        self.config = object()
        self.connection_manager = object()

    def _simple_dialog(self, heading, body):
        self.dialogs.append((heading, body))

    def _show_daemon_import_result(self, result):
        self.import_results.append(result)


class _Controller:
    def __init__(self, needs_unlock=False):
        self.state = SimpleNamespace(needs_unlock=needs_unlock)

    def load_state(self):
        return self.state


def _ok_result(**counts):
    return SimpleNamespace(
        status=SimpleNamespace(value="success"),
        counts=counts,
        warnings=(),
        message="",
    )


def test_vault_unlock_gate_skips_when_not_needed():
    win = _Win()
    calls = []
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1), needed=False, cancelled_heading="X")
    assert calls == [1]
    assert win.dialogs == []


def test_vault_unlock_gate_aborts_without_controller():
    win = _Win()
    calls = []
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1), needed=True, cancelled_heading="X")
    assert calls == []
    assert len(win.dialogs) == 1
    assert win.dialogs[0][0] == "X"


def test_vault_unlock_gate_proceeds_when_already_unlocked():
    win = _Win()
    win.secrets_controller = _Controller(needs_unlock=False)
    calls = []
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1), needed=True, cancelled_heading="X")
    assert calls == [1]
    assert win.dialogs == []


def test_vault_unlock_gate_aborts_when_unlock_cancelled(monkeypatch):
    win = _Win()
    win.secrets_controller = _Controller(needs_unlock=True)
    calls = []

    def fake_prompt_unlock(_parent, on_done=None, **_kw):
        on_done(False)
        return True

    monkeypatch.setattr(
        "sshpilot.secret_unlock_dialog.prompt_unlock", fake_prompt_unlock)
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1),
        needed=True,
        cancelled_heading="Export Cancelled",
    )
    assert calls == []
    assert len(win.dialogs) == 1
    assert win.dialogs[0][0] == "Export Cancelled"


def test_vault_unlock_gate_on_cancelled_skips_dead_end_dialog(monkeypatch):
    """Export flows restore the options dialog instead of a cancel alert."""
    win = _Win()
    win.secrets_controller = _Controller(needs_unlock=True)
    cancelled = []

    def fake_prompt_unlock(_parent, on_done=None, **_kw):
        on_done(False)
        return True

    monkeypatch.setattr(
        "sshpilot.secret_unlock_dialog.prompt_unlock", fake_prompt_unlock)
    win._run_after_vault_unlock_for_secrets(
        lambda: None,
        needed=True,
        cancelled_heading="Export Cancelled",
        on_cancelled=lambda: cancelled.append(1),
    )
    assert cancelled == [1]
    assert win.dialogs == []


def test_vault_unlock_gate_proceeds_after_successful_unlock(monkeypatch):
    win = _Win()
    win.secrets_controller = _Controller(needs_unlock=True)
    calls = []

    def fake_prompt_unlock(_parent, on_done=None, **_kw):
        on_done(True)
        return True

    monkeypatch.setattr(
        "sshpilot.secret_unlock_dialog.prompt_unlock", fake_prompt_unlock)
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1), needed=True, cancelled_heading="X")
    assert calls == [1]
    assert win.dialogs == []


def test_daemon_import_runs_apply_in_worker(monkeypatch):
    win = _Win()
    events = []

    def run():
        events.append("apply")
        return _ok_result(restored=4, skipped=2, keys_written=5, keys_skipped=1)

    class Thread:
        def __init__(self, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            events.append("thread")
            self.target()

    def progress_dialog(parent, heading, message):
        assert parent is win
        events.append(("progress", heading, message))
        return lambda _text: None, lambda: events.append("close")

    def idle_add(callback):
        events.append("idle")
        return callback()

    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.progress_dialog", progress_dialog)
    monkeypatch.setattr("sshpilot.window_dialogs.threading.Thread", Thread)
    monkeypatch.setattr("sshpilot.window_dialogs.GLib.idle_add", idle_add)

    win._run_daemon_import(run, needed=False, cancelled_heading="X")

    assert events == [
        ("progress", "Import Configuration",
         "Applying backup — this may take a while…"),
        "thread",
        "apply",
        "idle",
        "close",
    ]
    assert win.dialogs == []
    assert len(win.import_results) == 1
    assert win.import_results[0].counts["restored"] == 4


def test_daemon_import_gates_on_vault_unlock(monkeypatch):
    win = _Win()
    win.secrets_controller = _Controller(needs_unlock=True)
    called = []

    def fake_prompt_unlock(_parent, on_done=None, **_kw):
        on_done(False)
        return True

    monkeypatch.setattr(
        "sshpilot.secret_unlock_dialog.prompt_unlock", fake_prompt_unlock)
    win._run_daemon_import(
        lambda: (called.append(1) or _ok_result()),
        needed=True,
        cancelled_heading="Import Cancelled",
    )
    assert called == []
    assert len(win.dialogs) == 1
    assert win.dialogs[0][0] == "Import Cancelled"


def test_bitwarden_export_shows_progress_before_vault_unlock(monkeypatch):
    """Progress sheet must appear before the vault gate — Bitwarden is slow and
    the options window is already closed, so a silent gap looks like a no-op.
    """
    win = _Win()
    events = []
    statuses = []

    class Controller:
        def export_backup(self, **_kwargs):
            events.append("export")
            result = _ok_result()
            result.path = "SSH Pilot backup"
            return result

        def load_state(self):
            events.append("load_state")
            return SimpleNamespace(needs_unlock=False)

    win.secrets_controller = Controller()

    def progress(**kwargs):
        events.append(("progress", kwargs.get("status")))
        return statuses.append, lambda: events.append("close")

    win._backup_progress_dialog = progress
    win._show_export_result = lambda **kw: events.append("result")

    def ensure(_window, on_ready):
        events.append("ensure")
        on_ready(True)

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            events.append("thread")
            self.target()

    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.ensure_bitwarden_ready", ensure)
    monkeypatch.setattr("sshpilot.window_dialogs.threading.Thread", Thread)
    monkeypatch.setattr(
        "sshpilot.window_dialogs.GLib.idle_add", lambda cb: cb())

    win._export_to_bitwarden(
        [], {"secrets": True, "private_keys": False, "app_settings": True,
             "ssh_config": True, "known_hosts": True})

    # Progress opens before the secrets-backend unlock check / export call.
    assert events[0] == "ensure"
    assert events[1][0] == "progress"
    assert "Preparing" in events[1][1]
    assert events.index(("progress", events[1][1])) < events.index("load_state")
    assert "export" in events
    assert "close" in events
    assert "result" in events
    assert events.index("close") < events.index("result")


def test_make_spbk_import_apply_routes_through_controller(monkeypatch):
    win = _Win()
    calls = []

    class Controller:
        def import_backup(self, *, source, options):
            calls.append((source, options))
            return _ok_result(restored=1)

    win.secrets_controller = Controller()
    apply = win._make_spbk_import_apply(
        "/tmp/backup.spbk", {"secrets": False, "ssh_config": True})

    class Thread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.progress_dialog",
        lambda parent, heading, message: (lambda _t: None, lambda: None))
    monkeypatch.setattr("sshpilot.window_dialogs.threading.Thread", Thread)
    monkeypatch.setattr("sshpilot.window_dialogs.GLib.idle_add",
                        lambda cb: (cb(), False)[1])

    apply("merge", {"ssh_config": True})

    assert calls == [
        ("/tmp/backup.spbk",
         {"mode": "merge", "ssh_config": True}),
    ]
    assert len(win.import_results) == 1
