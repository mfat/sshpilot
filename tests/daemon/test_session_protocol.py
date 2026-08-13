import threading
import time

import pytest

from sshpilot.api import Capability, DaemonClient, ErrorCode, SshPilotError
from sshpilot.api.events import EventType
from sshpilot.api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    SessionExitInfo,
    SessionState,
)


def _wait_until(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return bool(predicate())


class _Handle:
    def __init__(self, on_exit):
        self._on_exit = on_exit
        self._exit = None
        self.terminated = 0
        self.killed = 0

    def terminate(self):
        self.terminated += 1
        self.exit(SessionExitInfo(exit_code=0, reason="terminated"))

    def kill(self):
        self.killed += 1
        self.exit(SessionExitInfo(signal=9, reason="killed"))

    def wait(self, timeout):
        del timeout
        return self._exit

    def exit(self, info):
        if self._exit is not None:
            return
        self._exit = info
        self._on_exit(info)


class _Runner:
    def __init__(self):
        self.handles = []
        self.closed = False

    def start(self, _spec, on_exit):
        handle = _Handle(on_exit)
        self.handles.append(handle)
        return handle

    def close(self):
        self.closed = True


@pytest.mark.parametrize("_repeat", range(5))
def test_daemon_client_session_methods_and_multi_client_events(
    daemon_factory,
    _repeat,
):
    runner = _Runner()
    server, _manager = daemon_factory(session_runner=runner)
    client_a = DaemonClient(socket_path=server.socket_path, client_id="client:a")
    client_b = DaemonClient(socket_path=server.socket_path, client_id="client:b")
    events_a = []
    events_b = []
    client_a.subscribe_events(events_a.append)
    client_b.subscribe_events(events_b.append)
    try:
        assert {
            Capability.SESSIONS_READ,
            Capability.SESSIONS_WRITE,
            Capability.SESSIONS_EVENTS,
        } <= client_a.get_capabilities().supported
        connection_id = client_a.list_connections()[0].id

        opened = client_a.open_session(OpenSessionRequest(connection_id=connection_id))

        assert opened.state is SessionState.STARTING
        assert _wait_until(
            lambda: client_b.get_session(opened.id).state is SessionState.RUNNING
        )
        assert client_b.list_sessions()[0].id == opened.id
        assert _wait_until(
            lambda: (
                len([event for event in events_a if event.session_id == opened.id]) >= 3
                and len([event for event in events_b if event.session_id == opened.id]) >= 3
            )
        )
        session_events_a = [event for event in events_a if event.session_id == opened.id]
        session_events_b = [event for event in events_b if event.session_id == opened.id]
        assert [event.sequence for event in session_events_a] == [
            event.sequence for event in session_events_b
        ]
        assert [event.type for event in session_events_a] == [
            EventType.SESSION_CREATED,
            EventType.SESSION_STATE_CHANGED,
            EventType.SESSION_STATE_CHANGED,
        ]

        attachment_a = client_a.attach_session(
            AttachSessionRequest(session_id=opened.id)
        ).attachment
        attachment_b = client_b.attach_session(
            AttachSessionRequest(session_id=opened.id)
        ).attachment
        assert attachment_a.client_id == "client:a"
        assert attachment_b.client_id == "client:b"
        assert client_a.get_session(opened.id).attachment_count == 2

        client_b.close()
        assert _wait_until(lambda: client_a.get_session(opened.id).attachment_count == 1)
        client_a.detach_session(
            DetachSessionRequest(
                session_id=opened.id,
                attachment_id=attachment_a.id,
            )
        )
        assert client_a.get_session(opened.id).attachment_count == 0

        client_a.close_session(CloseSessionRequest(session_id=opened.id))
        assert client_a.get_session(opened.id).state is SessionState.CLOSED
        assert _wait_until(
            lambda: any(
                event.type is EventType.SESSION_CLOSED and event.session_id == opened.id
                for event in events_a
            )
        )
        assert runner.handles[0].terminated == 1
    finally:
        client_a.close()
        client_b.close()


def test_default_production_runner_fails_truthfully_without_terminal_transport(
    daemon_factory,
):
    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    events = []
    client.subscribe_events(events.append)
    try:
        opened = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )
        assert opened.state is SessionState.STARTING
        assert _wait_until(
            lambda: client.get_session(opened.id).state is SessionState.FAILED
        )
        failed = client.get_session(opened.id)
        assert failed.failure.code == ErrorCode.SESSION_STARTUP_FAILED.value
        assert failed.capabilities.supported == frozenset()
        assert _wait_until(
            lambda: any(
                event.type is EventType.SESSION_STATE_CHANGED
                and event.payload.state is SessionState.FAILED
                for event in events
            )
        )
    finally:
        client.close()


def test_session_open_and_close_transport_failures_are_not_retried(monkeypatch):
    client = object.__new__(DaemonClient)
    calls = []

    def fail_request(method, params, **kwargs):
        calls.append((method, params, kwargs))
        raise SshPilotError(
            ErrorCode.MUTATION_AMBIGUOUS,
            "The session change may have completed",
        )

    monkeypatch.setattr(client, "_request", fail_request)
    monkeypatch.setattr(client, "_require_capability", lambda _capability: None)
    request = OpenSessionRequest(connection_id="test")

    with pytest.raises(SshPilotError) as caught:
        client.open_session(request)

    assert caught.value.code is ErrorCode.MUTATION_AMBIGUOUS
    assert len(calls) == 1
    assert calls[0][2]["session_mutation"] is True


def test_malformed_session_wire_params_are_rejected_without_payload_logging(
    daemon_factory,
    caplog,
):
    server, _manager = daemon_factory()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        with pytest.raises(SshPilotError) as caught:
            client._request(
                "sessions.open",
                {
                    "connection_id": client.list_connections()[0].id,
                    "unexpected": "must-not-log",
                },
            )
        assert caught.value.code is ErrorCode.INVALID_REQUEST
        assert "must-not-log" not in caplog.text
        assert "must-not-log" not in repr(caught.value)
    finally:
        client.close()


@pytest.mark.parametrize("_repeat", range(5))
def test_client_disconnect_during_attach_does_not_close_session(
    daemon_factory,
    _repeat,
):
    runner = _Runner()
    server, _manager = daemon_factory(session_runner=runner)
    owner = DaemonClient(socket_path=server.socket_path, client_id="client:owner")
    observer = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:observer",
    )
    try:
        session = owner.open_session(
            OpenSessionRequest(connection_id=owner.list_connections()[0].id)
        )
        owner.attach_session(AttachSessionRequest(session_id=session.id))
        observer.attach_session(AttachSessionRequest(session_id=session.id))
        barrier = threading.Event()
        barrier.set()
        observer.close()
        assert barrier.is_set()
        assert _wait_until(lambda: owner.get_session(session.id).attachment_count == 1)
        assert owner.get_session(session.id).state is SessionState.RUNNING
    finally:
        owner.close()
        observer.close()


def test_session_and_connection_events_share_one_monotonic_sequence(
    daemon_factory,
):
    runner = _Runner()
    server, manager = daemon_factory(session_runner=runner)
    client = DaemonClient(socket_path=server.socket_path)
    received = []
    client.subscribe_events(received.append)
    try:
        manager.update_connection("demo", {"hostname": "updated.example"})
        session = client.open_session(
            OpenSessionRequest(connection_id=client.list_connections()[0].id)
        )

        assert _wait_until(
            lambda: (
                len(
                    [
                        event
                        for event in received
                        if event.type
                        in {
                            EventType.CONNECTION_UPDATED,
                            EventType.SESSION_CREATED,
                            EventType.SESSION_STATE_CHANGED,
                        }
                        and (
                            event.type is EventType.CONNECTION_UPDATED
                            or event.session_id == session.id
                        )
                    ]
                )
                >= 4
            )
        )
        relevant = [
            event
            for event in received
            if event.type is EventType.CONNECTION_UPDATED or event.session_id == session.id
        ]
        assert [event.sequence for event in relevant] == sorted(
            event.sequence for event in relevant
        )
        assert len({event.sequence for event in relevant}) == len(relevant)
        assert relevant[0].type is EventType.CONNECTION_UPDATED
    finally:
        client.close()
