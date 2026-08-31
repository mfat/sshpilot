from datetime import datetime, timedelta, timezone

from sshpilot.api.errors import ErrorCode
from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.connections import ConnectionSummary
from sshpilot.api.models.operations import (
    SftpFailure,
    SftpFailureCode,
    SftpServiceState,
    SftpServiceSummary,
)
from sshpilot.api.models.sessions import (
    PluginSessionFailure,
    PluginSessionFailureCode,
    SessionExitInfo,
    SessionFailure,
    SessionState,
    SessionSummary,
)
from sshpilot.connection_manager import ConnectionState
from sshpilot.gtk.connection_runtime_status import (
    ConnectionRuntimeStatus,
    ConnectionRuntimeStatusStore,
)


def session(
    session_id,
    connection_id,
    state,
    *,
    failure=None,
    exit_info=None,
    created_at=None,
):
    return SessionSummary(
        id=session_id,
        connection_id=connection_id,
        state=state,
        created_at=created_at or datetime.now(timezone.utc),
        failure=failure,
        exit_info=exit_info,
    )


def sftp(service_id, connection_id, state, *, failure=None, created_at=None):
    return SftpServiceSummary(
        id=service_id,
        connection_id=connection_id,
        state=state,
        created_at=created_at or datetime.now(timezone.utc),
        failure=failure,
    )


class Subscription:
    def __init__(self):
        self.closed = False

    def unsubscribe(self):
        self.closed = True


class Client:
    def __init__(self, instance_id, sessions, sftp_services=()):
        self.server_instance_id = instance_id
        self.sessions = list(sessions)
        self.sftp_services = list(sftp_services)
        self.callback = None
        self.subscription = Subscription()

    def subscribe_events(self, callback):
        self.callback = callback
        return self.subscription

    def list_sessions(self):
        return list(self.sessions)

    def list_sftp_services(self):
        return list(self.sftp_services)

    def emit(self, event_type, payload, sequence, *, session_id=None):
        self.callback(
            CoreEvent(
                event_type,
                payload,
                sequence,
                datetime.now(timezone.utc),
                session_id=session_id,
            )
        )


def connection(connection_id="connection-1"):
    return ConnectionSummary(
        id=connection_id,
        nickname="example",
        host="example",
        hostname="example.test",
        username="alice",
        port=22,
    )


def test_initial_session_snapshot_aggregates_by_connection_id():
    dto = connection()
    client = Client(
        "daemon-a",
        [session("session-1", dto.id, SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore()

    store.attach_client(client)

    assert store.status_for(dto) == ConnectionRuntimeStatus(
        ConnectionState.CONNECTED,
        "Connected",
    )
    # Runtime status is separate; the immutable connection projection is unchanged.
    assert not hasattr(dto, "is_connected")


def test_live_session_events_follow_connection_wide_priority():
    changed = []
    client = Client(
        "daemon-a",
        [session("starting", "connection-1", SessionState.STARTING)],
    )
    store = ConnectionRuntimeStatusStore(
        on_changed=lambda connection_id, status: changed.append(
            (connection_id, status)
        )
    )
    store.attach_client(client)
    assert store.status_for("connection-1").state is ConnectionState.CONNECTING

    running = session("starting", "connection-1", SessionState.RUNNING)
    client.emit(EventType.SESSION_STATE_CHANGED, running, 1)
    assert store.status_for("connection-1").state is ConnectionState.CONNECTED

    failed = session(
        "failed",
        "connection-1",
        SessionState.FAILED,
        failure=SessionFailure("session_startup_failed", "Authentication failed"),
    )
    client.emit(EventType.SESSION_CREATED, failed, 2)
    # Any running session wins over another failed attempt.
    assert store.status_for("connection-1").state is ConnectionState.CONNECTED

    client.emit(
        EventType.SESSION_EXITED,
        SessionExitInfo(exit_code=255, reason="Connection lost"),
        3,
        session_id="starting",
    )
    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.FAILED,
        "Authentication failed",
    )

    closed = session("failed", "connection-1", SessionState.CLOSED)
    client.emit(EventType.SESSION_CLOSED, closed, 4)
    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.DISCONNECTED,
        "Connection lost",
    )
    assert changed[-1][0] == "connection-1"


def test_closed_only_sessions_return_to_unknown():
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [session("closed", "connection-1", SessionState.CLOSED)],
        )
    )

    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN


def test_closed_failed_session_retains_its_authoritative_failure():
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [
                session(
                    "closed",
                    "connection-1",
                    SessionState.CLOSED,
                    failure=SessionFailure(
                        "session_startup_failed",
                        "permission denied (publickey,password).",
                    ),
                    exit_info=SessionExitInfo(exit_code=255, reason="process_exit"),
                )
            ],
        )
    )

    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.FAILED,
        "permission denied (publickey,password).",
    )


def test_plugin_session_failure_uses_frontend_formatter(monkeypatch):
    import sshpilot.gtk.connection_runtime_status as runtime_status

    monkeypatch.setattr(
        runtime_status,
        "format_plugin_session_failure",
        lambda failure: f"translated:{failure.parameters['program']}",
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [
                session(
                    "failed",
                    "connection-1",
                    SessionState.FAILED,
                    failure=PluginSessionFailure(
                        PluginSessionFailureCode.KUBECTL_UNAVAILABLE,
                        ErrorCode.SESSION_STARTUP_FAILED,
                        parameters={"program": "kubectl"},
                    ),
                )
            ],
        )
    )

    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.FAILED,
        "translated:kubectl",
    )


def test_newer_clean_close_supersedes_retained_failure():
    now = datetime.now(timezone.utc)
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [
                session(
                    "failed",
                    "connection-1",
                    SessionState.CLOSED,
                    failure=SessionFailure(
                        "session_startup_failed",
                        "permission denied",
                    ),
                    exit_info=SessionExitInfo(exit_code=255, reason="process_exit"),
                    created_at=now,
                ),
                session(
                    "clean",
                    "connection-1",
                    SessionState.CLOSED,
                    exit_info=SessionExitInfo(exit_code=0, reason="clean close"),
                    created_at=now + timedelta(seconds=1),
                ),
            ],
        )
    )

    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.DISCONNECTED,
        "clean close",
    )


def test_reconnect_replaces_status_and_ignores_old_daemon_events():
    old = Client(
        "daemon-old",
        [session("old", "connection-1", SessionState.RUNNING)],
    )
    new = Client("daemon-new", [])
    store = ConnectionRuntimeStatusStore()
    store.attach_client(old)
    old_callback = old.callback

    store.attach_client(new)
    old_callback(
        CoreEvent(
            EventType.SESSION_STATE_CHANGED,
            session("stale", "connection-1", SessionState.RUNNING),
            99,
            datetime.now(timezone.utc),
        )
    )

    assert old.subscription.closed
    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN


def test_sidebar_resolves_daemon_runtime_status_without_mutating_dto():
    from sshpilot.sidebar import ConnectionRow

    dto = connection()
    row = ConnectionRow.__new__(ConnectionRow)
    row.connection = dto
    row._status_resolver = lambda _connection: ConnectionRuntimeStatus(
        ConnectionState.CONNECTED,
        "Connected",
    )

    assert row._resolve_status() == (ConnectionState.CONNECTED, "Connected")
    assert row._is_online() is True


# --- SFTP services (GH #1193) -----------------------------------------------


def test_ready_sftp_service_marks_the_connection_connected():
    """A file manager is a connection to the host, same as a terminal is."""
    dto = connection()
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [],
            [sftp("sftp-1", dto.id, SftpServiceState.READY)],
        )
    )

    assert store.status_for(dto) == ConnectionRuntimeStatus(
        ConnectionState.CONNECTED,
        "Connected",
    )


def test_sftp_lifecycle_events_drive_the_indicator():
    changed = []
    client = Client("daemon-a", [])
    store = ConnectionRuntimeStatusStore(
        on_changed=lambda connection_id, status: changed.append(
            (connection_id, status)
        )
    )
    store.attach_client(client)
    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN

    client.emit(
        EventType.SFTP_CREATED,
        sftp("sftp-1", "connection-1", SftpServiceState.STARTING),
        1,
    )
    assert store.status_for("connection-1").state is ConnectionState.CONNECTING

    client.emit(
        EventType.SFTP_STATE_CHANGED,
        sftp("sftp-1", "connection-1", SftpServiceState.READY),
        2,
    )
    assert store.status_for("connection-1").state is ConnectionState.CONNECTED

    # Closing the file manager is an intentional close: report nothing rather
    # than a red "disconnected" for something the user closed on purpose.
    client.emit(
        EventType.SFTP_CLOSED,
        sftp("sftp-1", "connection-1", SftpServiceState.CLOSED),
        3,
    )
    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN
    assert changed[-1] == ("connection-1", ConnectionRuntimeStatus())


def test_failed_sftp_service_reports_its_reason():
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [],
            [
                sftp(
                    "sftp-1",
                    "connection-1",
                    SftpServiceState.FAILED,
                    failure=SftpFailure(
                        SftpFailureCode.SESSION_ESTABLISHMENT_FAILED,
                        ErrorCode.SFTP_SERVICE_NOT_READY,
                        diagnostic="Permission denied",
                    ),
                )
            ],
        )
    )

    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.FAILED,
        "The SFTP session could not be established\n\nPermission denied",
    )


def test_live_sftp_service_outranks_a_failed_session():
    """Either kind being live means the host is connected."""
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [
                session(
                    "session-1",
                    "connection-1",
                    SessionState.FAILED,
                    failure=SessionFailure("session_startup_failed", "no route"),
                )
            ],
            [sftp("sftp-1", "connection-1", SftpServiceState.READY)],
        )
    )

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED


def test_closing_a_terminal_keeps_a_live_file_manager_connected():
    """Closing one tab must not clear a host the file manager still holds."""
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
        [sftp("sftp-1", "connection-1", SftpServiceState.READY)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)

    client.emit(
        EventType.SESSION_CLOSED,
        session("session-1", "connection-1", SessionState.CLOSED),
        1,
    )

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED


def test_sftp_services_are_kept_apart_by_connection():
    store = ConnectionRuntimeStatusStore()
    store.attach_client(
        Client(
            "daemon-a",
            [],
            [
                sftp("sftp-1", "connection-1", SftpServiceState.READY),
                sftp("sftp-2", "connection-2", SftpServiceState.CLOSED),
            ],
        )
    )

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED
    assert store.status_for("connection-2").state is ConnectionState.UNKNOWN


def test_client_without_sftp_listing_still_tracks_sessions():
    """A daemon too old to list SFTP services must not break the store."""

    class SessionOnlyClient(Client):
        list_sftp_services = None

    client = SessionOnlyClient(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED
