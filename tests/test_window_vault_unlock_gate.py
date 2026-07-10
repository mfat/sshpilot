"""Vault-unlock gate for config export/import (WindowConfigDialogsMixin)."""

from sshpilot.window_dialogs import WindowConfigDialogsMixin


class _Win(WindowConfigDialogsMixin):
    def __init__(self):
        self.dialogs = []

    def _simple_dialog(self, heading, body):
        self.dialogs.append((heading, body))


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
