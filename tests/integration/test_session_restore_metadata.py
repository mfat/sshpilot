"""Session restore metadata persistence (not GTK tab restore).

Proves DaemonSessionRestoreManager save/query/dedup against active daemon
sessions. Does not create restored TerminalWidget tabs.
"""
from __future__ import annotations

import threading
import time


from sshpilot.api import DaemonClient
from sshpilot.api.models.sessions import (
    OpenSessionRequest,
    CloseSessionRequest,
)


class _BlockingSessionRunner:
    """Keep a session STARTING/RUNNING until terminate/kill."""

    def __init__(self) -> None:
        self.started = threading.Event()

    def start(self, spec, on_exit, on_output=None, on_eof=None):
        self.started.set()
        return _BlockingHandle(on_exit)


class _BlockingSessionRunnerTerminal(_BlockingSessionRunner):
    """Variant that advertises terminal capability for input/output tests."""

    terminal_capable = True


class _BlockingHandle:
    def __init__(self, on_exit) -> None:
        self._on_exit = on_exit
        self._exit_info = None
        self._exit_event = threading.Event()

    def terminate(self):
        from sshpilot.api.models.sessions import SessionExitInfo

        self._exit_info = SessionExitInfo(exit_code=0, reason="terminated")
        self._exit_event.set()
        self._on_exit(self._exit_info)

    def kill(self):
        from sshpilot.api.models.sessions import SessionExitInfo

        self._exit_info = SessionExitInfo(exit_code=-1, signal="KILL", reason="killed")
        self._exit_event.set()
        self._on_exit(self._exit_info)

    def wait(self, timeout=None):
        self._exit_event.wait(timeout=timeout)
        return self._exit_info

    def resize(self, *_args, **_kwargs):
        return None

    def write(self, data):
        return None

    def close_input(self):
        return None


def _wait_for_sessions_active(client, timeout=5.0):
    """Wait until sessions_active > 0."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.get_daemon_status().resources.sessions_active:
            return True
        time.sleep(0.02)
    return False


def _wait_sessions_drained(client, timeout=5.0):
    """Wait until sessions_active == 0."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not client.get_daemon_status().resources.sessions_active:
            return True
        time.sleep(0.05)
    return False


class TestSessionRestore:
    """Prove session restore metadata works."""

    def test_save_and_query_metadata(self, daemon_factory):
        """Session metadata can be saved and queried against active sessions."""
        from sshpilot.daemon_session_restore import DaemonSessionRestoreManager

        runner = _BlockingSessionRunner()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        summary = client.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )
        assert _wait_for_sessions_active(client, timeout=5.0)

        class FakeConfig:
            def __init__(self):
                self._store = {}

            def get_setting(self, key, default=None):
                return self._store.get(key, default)

            def set_setting(self, key, value):
                self._store[key] = value

        config = FakeConfig()
        manager = DaemonSessionRestoreManager(config)

        class FakeTabState:
            session_id = str(summary.id)
            daemon_instance_id = str(client.server_instance_id)
            connection_id = str(connection.id)
            expected_sequence = 0
            view_id = "test-view"

        manager.save_session_metadata(FakeTabState(), "Test Terminal")

        saved = config.get_setting("terminal.daemon_session_restore_state", [])
        assert len(saved) == 1
        assert saved[0]["session_id"] == str(summary.id)

        restorable = manager.get_restorable_sessions(client)
        assert len(restorable) == 1
        assert restorable[0].session_id == str(summary.id)

        manager.remove_session_metadata(str(summary.id))
        saved = config.get_setting("terminal.daemon_session_restore_state", [])
        assert len(saved) == 0

        client.close_session(CloseSessionRequest(session_id=summary.id))
        _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)

    def test_metadata_deduplication(self, daemon_factory):
        """Saving metadata for the same session replaces the old entry."""
        from sshpilot.daemon_session_restore import DaemonSessionRestoreManager

        runner = _BlockingSessionRunner()
        server, _manager = daemon_factory(
            idle_shutdown_seconds=5.0,
            session_runner=runner,
        )
        client = DaemonClient(socket_path=server.socket_path)
        connection = client.list_connections()[0]

        summary = client.open_session(
            OpenSessionRequest(connection_id=connection.id)
        )
        assert _wait_for_sessions_active(client, timeout=5.0)

        class FakeConfig:
            def __init__(self):
                self._store = {}

            def get_setting(self, key, default=None):
                return self._store.get(key, default)

            def set_setting(self, key, value):
                self._store[key] = value

        config = FakeConfig()
        manager = DaemonSessionRestoreManager(config)

        class FakeTabState:
            session_id = str(summary.id)
            daemon_instance_id = str(client.server_instance_id)
            connection_id = str(connection.id)
            expected_sequence = 0
            view_id = "test-view"

        manager.save_session_metadata(FakeTabState(), "Tab 1")
        manager.save_session_metadata(FakeTabState(), "Tab 2")

        saved = config.get_setting("terminal.daemon_session_restore_state", [])
        assert len(saved) == 1
        assert saved[0]["tab_title"] == "Tab 2"

        client.close_session(CloseSessionRequest(session_id=summary.id))
        _wait_sessions_drained(client, timeout=5.0)

        client.close()
        server.shutdown()
        assert server.wait_stopped(timeout=5.0)


# ---------------------------------------------------------------------------
# Protocol compatibility
# ---------------------------------------------------------------------------
