import pytest
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
    SessionFailureCode,
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
        failure=SessionFailure(
            SessionFailureCode.AUTH_INCOMPLETE,
            ErrorCode.SESSION_STARTUP_FAILED,
        ),
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
        "The session did not complete authentication",
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
                        SessionFailureCode.SSH_DIAGNOSTIC,
                        ErrorCode.SESSION_STARTUP_FAILED,
                        diagnostic="permission denied (publickey,password).",
                    ),
                    exit_info=SessionExitInfo(exit_code=255, reason="process_exit"),
                )
            ],
        )
    )

    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.FAILED,
        "The SSH session failed.\n\npermission denied (publickey,password).",
    )


def test_session_failure_uses_frontend_presenter(monkeypatch):
    import sshpilot.gtk.connection_runtime_status as runtime_status

    seen = []
    monkeypatch.setattr(
        runtime_status,
        "format_session_failure",
        lambda failure, *, include_diagnostic: (
            seen.append((failure.code, include_diagnostic)), "translated reason"
        )[1],
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
                    failure=SessionFailure(
                        SessionFailureCode.COMMAND_QUEUE_FULL,
                        ErrorCode.SERVER_BUSY,
                    ),
                )
            ],
        )
    )

    assert store.status_for("connection-1").reason == "translated reason"
    assert seen == [(SessionFailureCode.COMMAND_QUEUE_FULL, True)]


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
                        SessionFailureCode.SSH_DIAGNOSTIC,
                        ErrorCode.SESSION_STARTUP_FAILED,
                        diagnostic="permission denied",
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

    # Status 0 is the user typing ``exit``: the tab closes itself, and the
    # host has nothing to report, not even the older failure.
    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN


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
                    failure=SessionFailure(
                        SessionFailureCode.SSH_DIAGNOSTIC,
                        ErrorCode.SESSION_STARTUP_FAILED,
                        diagnostic="no route",
                    ),
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


# --- in-process sessions (embedded RDP tab) ---------------------------------


def test_in_process_session_lights_and_clears_the_indicator():
    """The embedded RDP tab is no daemon session; it reports itself."""
    changed = []
    store = ConnectionRuntimeStatusStore(
        on_changed=lambda cid, status: changed.append((cid, status.state))
    )
    store.attach_client(Client("daemon-a", []))

    store.set_local_status(
        "rdp-1", "connection-1",
        ConnectionRuntimeStatus(ConnectionState.CONNECTING, "Connecting"),
    )
    store.set_local_status(
        "rdp-1", "connection-1",
        ConnectionRuntimeStatus(ConnectionState.CONNECTED, "Connected"),
    )
    assert store.status_for("connection-1").state is ConnectionState.CONNECTED

    store.set_local_status("rdp-1", "connection-1", None)
    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN
    assert changed == [
        ("connection-1", ConnectionState.CONNECTING),
        ("connection-1", ConnectionState.CONNECTED),
        ("connection-1", ConnectionState.UNKNOWN),
    ]


def test_live_in_process_session_outranks_a_failed_daemon_session():
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.FAILED)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)
    assert store.status_for("connection-1").state is ConnectionState.FAILED

    store.set_local_status(
        "rdp-1", "connection-1",
        ConnectionRuntimeStatus(ConnectionState.CONNECTED, "Connected"),
    )

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED


def test_live_daemon_session_outranks_a_failed_in_process_one():
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)

    store.set_local_status(
        "rdp-1", "connection-1",
        ConnectionRuntimeStatus(ConnectionState.FAILED, "Could Not Connect"),
    )

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED


def test_in_process_failure_keeps_its_reason():
    store = ConnectionRuntimeStatusStore()
    store.attach_client(Client("daemon-a", []))

    store.set_local_status(
        "rdp-1", "connection-1",
        ConnectionRuntimeStatus(ConnectionState.FAILED, "Authentication Failed"),
    )

    assert store.status_for("connection-1") == ConnectionRuntimeStatus(
        ConnectionState.FAILED, "Authentication Failed"
    )


def test_in_process_sessions_survive_a_daemon_reconnect():
    """A new daemon snapshot does not erase a remote desktop held in-process."""
    store = ConnectionRuntimeStatusStore()
    store.attach_client(Client("daemon-a", []))
    store.set_local_status(
        "rdp-1", "connection-1",
        ConnectionRuntimeStatus(ConnectionState.CONNECTED, "Connected"),
    )

    store.attach_client(Client("daemon-b", []))

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED


def test_in_process_sessions_are_kept_apart_by_key_and_connection():
    store = ConnectionRuntimeStatusStore()
    store.attach_client(Client("daemon-a", []))
    live = ConnectionRuntimeStatus(ConnectionState.CONNECTED, "Connected")
    store.set_local_status("rdp-1", "connection-1", live)
    store.set_local_status("rdp-2", "connection-1", live)
    store.set_local_status("rdp-3", "connection-2", live)

    store.set_local_status("rdp-1", "connection-1", None)

    assert store.status_for("connection-1").state is ConnectionState.CONNECTED
    assert store.status_for("connection-2").state is ConnectionState.CONNECTED


# --- a close the user asked for ---------------------------------------------


def _close_sequence(client, session_id, connection_id, start):
    """What the daemon sends when a tab is closed: CLOSING, the exit, CLOSED."""
    client.emit(
        EventType.SESSION_STATE_CHANGED,
        session(session_id, connection_id, SessionState.CLOSING),
        start,
    )
    client.emit(
        EventType.SESSION_EXITED,
        SessionExitInfo(signal=15, reason="process_signal"),
        start + 1,
        session_id=session_id,
    )
    client.emit(
        EventType.SESSION_CLOSED,
        session(
            session_id, connection_id, SessionState.CLOSED,
            exit_info=SessionExitInfo(signal=15, reason="process_signal"),
        ),
        start + 2,
    )


def test_closing_a_tab_clears_the_indicator():
    """The user closed it: the SIGTERM exit it causes is not a lost session."""
    changed = []
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore(
        on_changed=lambda cid, status: changed.append(status.state)
    )
    store.attach_client(client)

    _close_sequence(client, "session-1", "connection-1", 1)

    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN
    assert ConnectionState.DISCONNECTED not in changed
    assert ConnectionState.FAILED not in changed


def test_session_ending_by_itself_still_reports_disconnected():
    """No CLOSING step: the remote end went away, which is worth showing."""
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)

    client.emit(
        EventType.SESSION_EXITED,
        SessionExitInfo(exit_code=255, reason="process_exit"),
        1,
        session_id="session-1",
    )

    assert store.status_for("connection-1").state is ConnectionState.DISCONNECTED


def test_a_close_that_failed_is_still_reported():
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)
    client.emit(
        EventType.SESSION_STATE_CHANGED,
        session("session-1", "connection-1", SessionState.CLOSING),
        1,
    )

    client.emit(
        EventType.SESSION_STATE_CHANGED,
        session(
            "session-1", "connection-1", SessionState.FAILED,
            failure=SessionFailure(
                SessionFailureCode.TERMINATION_FAILED,
                ErrorCode.SESSION_TERMINATION_FAILED,
            ),
        ),
        2,
    )

    assert store.status_for("connection-1").state is ConnectionState.FAILED


def test_closing_a_newer_tab_supersedes_an_earlier_lost_session():
    """As with any newer result: the latest end of a session decides."""
    client = Client(
        "daemon-a",
        [
            session("lost", "connection-1", SessionState.CLOSED,
                    exit_info=SessionExitInfo(exit_code=255, reason="process_exit")),
            session("open", "connection-1", SessionState.RUNNING),
        ],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)

    _close_sequence(client, "open", "connection-1", 1)

    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN


def test_close_seen_while_refreshing_still_counts():
    """Events queued during a snapshot are applied together; CLOSING must stick."""
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)
    with store._lock:
        store._refreshing = True
    _close_sequence(client, "session-1", "connection-1", 1)
    with store._lock:
        store._refreshing = False
        for event in store._pending_events:
            store._apply_event_locked(event)
        store._pending_events = []
        store._statuses = store._aggregate_locked()

    assert store.status_for("connection-1").state is ConnectionState.UNKNOWN


def test_requested_closes_are_forgotten_for_a_new_daemon():
    store = ConnectionRuntimeStatusStore()
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store.attach_client(client)
    _close_sequence(client, "session-1", "connection-1", 1)

    store.attach_client(Client(
        "daemon-b",
        [session("session-1", "connection-1", SessionState.CLOSED,
                 exit_info=SessionExitInfo(exit_code=255, reason="process_exit"))],
    ))

    assert store.status_for("connection-1").state is ConnectionState.DISCONNECTED


@pytest.mark.parametrize(
    ("exit_info", "expected"),
    [
        # ``exit``: the shell left with 0, the tab closes itself.
        (SessionExitInfo(exit_code=0, reason="process_exit"), ConnectionState.UNKNOWN),
        # ``false; exit`` passes 1 on: the tab keeps its banner, and so the dot.
        (SessionExitInfo(exit_code=1, reason="process_exit"), ConnectionState.DISCONNECTED),
        # ssh's own "connection lost" status.
        (SessionExitInfo(exit_code=255, reason="process_exit"), ConnectionState.DISCONNECTED),
        (SessionExitInfo(signal=9, reason="process_signal"), ConnectionState.DISCONNECTED),
    ],
)
def test_typed_exit_follows_the_tab(exit_info, expected):
    """The dot agrees with the tab: it closes on status 0 and nothing else."""
    client = Client(
        "daemon-a",
        [session("session-1", "connection-1", SessionState.RUNNING)],
    )
    store = ConnectionRuntimeStatusStore()
    store.attach_client(client)

    client.emit(EventType.SESSION_EXITED, exit_info, 1, session_id="session-1")
    client.emit(
        EventType.SESSION_CLOSED,
        session("session-1", "connection-1", SessionState.CLOSED, exit_info=exit_info),
        2,
    )

    assert store.status_for("connection-1").state is expected
