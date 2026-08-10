"""Daemon session restore replay semantics for freshly created tabs."""

import time
from unittest.mock import Mock

from sshpilot.daemon_session_restore import (
    DaemonSessionRestoreManager,
    SessionRestoreMetadata,
)


class _FakeConfig:
    def __init__(self):
        self._store = {}

    def get_setting(self, key, default=None):
        return self._store.get(key, default)

    def set_setting(self, key, value):
        self._store[key] = value


def _fake_window(connection):
    window = Mock()
    window.connection_manager.get_connections.return_value = [connection]
    window.terminal_manager._resolve_group_color_and_name.return_value = (
        None,
        None,
    )
    window.connection_to_terminals = {}
    window.terminal_to_connection = {}
    window.active_terminals = {}
    return window


def _metadata(last_sequence):
    return SessionRestoreMetadata(
        session_id="sess-1",
        daemon_instance_id="inst",
        connection_id="conn-1",
        last_sequence=last_sequence,
        tab_title="host",
        view_id="gtk-old",
        timestamp=time.time(),
    )


def test_restore_session_replays_retained_scrollback_into_blank_widget(
    monkeypatch,
):
    """A restored tab is a brand-new blank widget: it must request the
    daemon's retained scrollback (from_sequence=0) instead of skipping ahead
    to the sequence the previous — now destroyed — widget had already seen.

    With from_sequence=last_sequence an idle session delivers an empty replay
    slice and the restored terminal stays blank until the next live output
    (e.g. until the user presses Enter).
    """
    captured = {}

    class _FakeTerminal:
        def __init__(self, *args, **kwargs):
            pass

        def connect(self, *args, **kwargs):
            return 0

        def apply_theme(self):
            pass

        def attach_daemon_session(self, client, bridge, session_id, **kwargs):
            captured["session_id"] = session_id
            captured.update(kwargs)
            return True

    monkeypatch.setattr("sshpilot.terminal.TerminalWidget", _FakeTerminal)

    window = _fake_window(Mock(nickname="conn-1"))
    manager = DaemonSessionRestoreManager(_FakeConfig())

    assert manager.restore_session(window, _metadata(500), Mock(), Mock())

    assert captured["session_id"] == "sess-1"
    assert captured["from_sequence"] == 0
