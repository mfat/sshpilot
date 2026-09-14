"""Regression: opening the file manager for a connection must unlock a
locked session-backed vault first, the same way opening a terminal does.

Without this gate, a locked KDBX vault (or any locked session-backed
backend) has no stored credential for the daemon's SFTP broker to hand
off, so the daemon falls back to a raw host-password/askpass prompt
instead of ever showing the vault master-password prompt.
"""

from types import SimpleNamespace
from unittest import mock

from sshpilot import window_file_manager as wfm


class _FakeWindow(wfm.WindowFileManagerMixin):
    def __init__(self, *, unlock_return=False, has_terminal_manager=True):
        if has_terminal_manager:
            self.terminal_manager = mock.Mock()
            self.terminal_manager._maybe_unlock_secrets_then = mock.Mock(
                return_value=unlock_return
            )
        self.config = mock.Mock()
        self.config.get_setting = lambda key, default=None: default


def _connection():
    return SimpleNamespace(
        nickname="host", username="user", port=22, hostname="host.example",
    )


def _patch_placeholder(monkeypatch, window):
    calls = []
    monkeypatch.setattr(
        window, "_create_file_manager_placeholder_tab",
        lambda *a, **k: (calls.append(1), {})[1],
    )
    monkeypatch.setattr(wfm, "has_internal_file_manager", lambda: True)
    return calls


def test_gate_blocks_placeholder_creation_while_unlock_is_in_flight(monkeypatch):
    window = _FakeWindow(unlock_return=True)
    calls = _patch_placeholder(monkeypatch, window)

    window._open_manage_files_now_for_connection(_connection())

    assert calls == []
    window.terminal_manager._maybe_unlock_secrets_then.assert_called_once()


def test_gate_proceeds_immediately_when_no_unlock_needed(monkeypatch):
    window = _FakeWindow(unlock_return=False)
    calls = _patch_placeholder(monkeypatch, window)

    window._open_manage_files_now_for_connection(_connection())

    assert calls == [1]


def test_retry_callback_reinvokes_without_regating(monkeypatch):
    window = _FakeWindow(unlock_return=True)
    calls = _patch_placeholder(monkeypatch, window)

    captured_retry = []
    window.terminal_manager._maybe_unlock_secrets_then = mock.Mock(
        side_effect=lambda retry: (captured_retry.append(retry), True)[1]
    )

    window._open_manage_files_now_for_connection(_connection())
    assert calls == []
    assert len(captured_retry) == 1

    # The vault is unlocked now; the retry callback must proceed without
    # calling the gate a second time.
    captured_retry[0]()

    assert calls == [1]
    window.terminal_manager._maybe_unlock_secrets_then.assert_called_once()


def test_gate_skipped_without_a_terminal_manager(monkeypatch):
    window = _FakeWindow(has_terminal_manager=False)
    calls = _patch_placeholder(monkeypatch, window)

    window._open_manage_files_now_for_connection(_connection())

    assert calls == [1]
