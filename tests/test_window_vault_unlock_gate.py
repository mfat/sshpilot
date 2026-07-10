"""Vault-unlock gate for config export/import (WindowConfigDialogsMixin)."""

from sshpilot.window_dialogs import WindowConfigDialogsMixin


class _Win(WindowConfigDialogsMixin):
    def __init__(self):
        self.dialogs = []
        self.import_results = []
        self.config = object()
        self.connection_manager = object()

    def _simple_dialog(self, heading, body):
        self.dialogs.append((heading, body))

    def _show_import_success(self, *args, **kwargs):
        self.import_results.append((args, kwargs))


def test_vault_unlock_gate_skips_when_not_needed():
    win = _Win()
    calls = []
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1), needed=False, cancelled_heading="X")
    assert calls == [1]
    assert win.dialogs == []


def test_vault_unlock_gate_proceeds_when_already_unlocked(monkeypatch):
    win = _Win()
    calls = []

    class Mgr:
        def selected_needs_unlock(self):
            return False

    monkeypatch.setattr(
        "sshpilot.secret_storage.get_secret_manager", lambda: Mgr())
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1), needed=True, cancelled_heading="X")
    assert calls == [1]
    assert win.dialogs == []


def test_vault_unlock_gate_aborts_when_unlock_cancelled(monkeypatch):
    win = _Win()
    calls = []

    class Mgr:
        def selected_needs_unlock(self):
            return True

    def fake_prompt_unlock(_parent, on_done=None, **_kw):
        on_done(False)
        return True

    monkeypatch.setattr(
        "sshpilot.secret_storage.get_secret_manager", lambda: Mgr())
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


def test_vault_unlock_gate_proceeds_after_successful_unlock(monkeypatch):
    win = _Win()
    calls = []

    class Mgr:
        def selected_needs_unlock(self):
            return True

    def fake_prompt_unlock(_parent, on_done=None, **_kw):
        on_done(True)
        return True

    monkeypatch.setattr(
        "sshpilot.secret_storage.get_secret_manager", lambda: Mgr())
    monkeypatch.setattr(
        "sshpilot.secret_unlock_dialog.prompt_unlock", fake_prompt_unlock)
    win._run_after_vault_unlock_for_secrets(
        lambda: calls.append(1), needed=True, cancelled_heading="X")
    assert calls == [1]
    assert win.dialogs == []


def test_manifest_import_shows_progress_and_applies_in_worker(monkeypatch):
    win = _Win()
    events = []

    class BackupManager:
        last_import_skipped_keys = 1
        last_import_secrets_persisted = True
        last_import_skipped_credentials = 2
        last_merge_collisions = ["duplicate"]
        last_merge_dropped_globals = 3

        def __init__(self, config, connection_manager):
            assert config is win.config
            assert connection_manager is win.connection_manager

        def apply_imported_manifest(self, manifest, **kwargs):
            events.append("apply")
            assert manifest == {"credentials": [], "private_keys": []}
            assert kwargs == {
                "mode": "merge",
                "create_backup": True,
                "restore_options": {"secrets": False},
            }
            return True, None, 4, 5

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

    monkeypatch.setattr("sshpilot.backup_manager.BackupManager", BackupManager)
    monkeypatch.setattr(
        "sshpilot.bitwarden_backup_setup.progress_dialog", progress_dialog)
    monkeypatch.setattr("sshpilot.window_dialogs.threading.Thread", Thread)
    monkeypatch.setattr("sshpilot.window_dialogs.GLib.idle_add", idle_add)

    win._perform_spbk_import(
        {"credentials": [], "private_keys": []},
        mode="merge",
        restore_options={"secrets": False},
    )

    assert events == [
        ("progress", "Import Configuration",
         "Applying backup — this may take a while…"),
        "thread",
        "apply",
        "idle",
        "close",
    ]
    assert win.dialogs == []
    assert win.import_results == [
        (
            (4, 0, 5, 0, 1, True, 2),
            {
                "merge_collisions": ["duplicate"],
                "dropped_globals": 3,
            },
        )
    ]
