"""Test terminal session controller state machine with mocked client/bridge."""

from unittest.mock import Mock

import pytest

from sshpilot.api.models.common import AttachmentId, ConnectionId, SessionId
from sshpilot.api.models.sessions import AttachmentInfo
from sshpilot.api.models.terminal import TerminalDimensions, TerminalOutput
from sshpilot.terminal_session_controller import (
    DaemonTerminalSessionController,
    TerminalSessionState,
    daemon_terminal_capabilities_missing,
    required_daemon_terminal_capabilities,
)


@pytest.fixture
def mock_client():
    """Mock daemon client with required capabilities."""
    client = Mock()
    capabilities = Mock()
    capabilities.supported = required_daemon_terminal_capabilities()
    client.get_capabilities.return_value = capabilities
    client.server_instance_id = "test-instance"
    return client


@pytest.fixture
def mock_bridge():
    """Mock GTK client bridge."""
    return Mock()


@pytest.fixture
def connection_id():
    """Test connection ID."""
    return ConnectionId("test-connection")


@pytest.fixture
def controller(mock_client, mock_bridge, connection_id):
    """Create a terminal session controller."""
    return DaemonTerminalSessionController(
        client=mock_client,
        bridge=mock_bridge,
        connection_id=connection_id,
        view_id="test-view",
    )


def test_initial_state(controller):
    """Test initial controller state."""
    assert controller.state == TerminalSessionState.IDLE
    assert not controller.input_owner
    assert controller.tab_state.session_id is None
    assert controller.tab_state.attachment_id is None
    assert controller.tab_state.expected_sequence == 0


def test_missing_capabilities():
    """Test controller creation with missing capabilities."""
    client = Mock()
    capabilities = Mock()
    capabilities.supported = frozenset()  # No capabilities
    client.get_capabilities.return_value = capabilities
    
    bridge = Mock()
    connection_id = ConnectionId("test")
    
    with pytest.raises(RuntimeError, match="Required daemon terminal capabilities unavailable"):
        DaemonTerminalSessionController(
            client=client,
            bridge=bridge,
            connection_id=connection_id,
            view_id="test-view",
        )


def test_open_session_success(controller, mock_client, mock_bridge):
    """Test successful session opening."""
    session_id = SessionId("test-session")
    session_summary = Mock()
    session_summary.id = session_id
    session_summary.state = None
    mock_client.subscribe_events = Mock(return_value=Mock(unsubscribe=Mock()))
    
    # Configure mocks
    mock_client.open_session.return_value = session_summary
    
    # Start opening
    dimensions = TerminalDimensions(rows=24, columns=80)
    controller.open(ConnectionId("test"), dimensions)
    
    # Should transition to opening state
    assert controller.state == TerminalSessionState.OPENING
    
    # Simulate successful open callback
    controller._on_session_opened(session_summary)
    
    # Should have stored session ID and attempt attach
    assert controller.tab_state.session_id == session_id
    
    # Bridge should have been called to submit open request
    mock_bridge.submit.assert_called()


def test_open_session_wrong_state(controller):
    """Test opening session in wrong state."""
    # Change to non-idle state
    controller._tab_state.state = TerminalSessionState.ACTIVE
    
    with pytest.raises(RuntimeError, match="Cannot open session in state"):
        controller.open(ConnectionId("test"))


def test_attach_session_success(controller, mock_client, mock_bridge):
    """Test successful session attachment."""
    session_id = SessionId("test-session")
    attachment_id = AttachmentId("test-attachment")
    
    # Set up session
    controller._tab_state.session_id = session_id
    controller._tab_state.state = TerminalSessionState.OPENING
    
    # Mock attachment result
    attachment_info = AttachmentInfo(
        id=attachment_id,
        session_id=session_id,
        client_id="test-client",
        input_owner=True,
    )
    attach_result = Mock()
    attach_result.attachment = attachment_info
    attach_result.available_start = 0
    attach_result.live_sequence = 0  # No replay data
    
    # Call attach
    controller.attach()
    
    # Should transition to attaching state
    assert controller.state == TerminalSessionState.ATTACHING
    
    # Simulate successful attach callback
    controller._on_session_attached(attach_result)
    
    # Should update state
    assert controller.tab_state.attachment_id == attachment_id
    assert controller.input_owner
    assert controller.tab_state.expected_sequence == 0
    # Should be in active state since available_start == live_sequence (no replay)
    assert controller.state == TerminalSessionState.ACTIVE


def test_attach_with_replay(controller):
    """Test attachment with replay data."""
    session_id = SessionId("test-session")
    attachment_id = AttachmentId("test-attachment")
    
    controller._tab_state.session_id = session_id
    controller._tab_state.state = TerminalSessionState.OPENING
    
    # Mock attachment result with replay
    attachment_info = AttachmentInfo(
        id=attachment_id,
        session_id=session_id,
        client_id="test-client",
        input_owner=False,
    )
    attach_result = Mock()
    attach_result.attachment = attachment_info
    attach_result.available_start = 0
    attach_result.live_sequence = 100  # Has replay data
    
    controller._on_session_attached(attach_result)
    
    # Should be in replaying state since available_start < live_sequence
    assert controller.state == TerminalSessionState.REPLAYING
    assert controller.tab_state.expected_sequence == 0
    assert not controller.input_owner


def _attach_result(session_id, attachment_id, *, available_start, live_sequence):
    attachment_info = AttachmentInfo(
        id=attachment_id,
        session_id=session_id,
        client_id="test-client",
        input_owner=True,
    )
    attach_result = Mock()
    attach_result.attachment = attachment_info
    attach_result.available_start = available_start
    attach_result.live_sequence = live_sequence
    return attach_result


def test_reattach_idle_session_already_caught_up_goes_active(controller):
    """Reattach to a retained but idle session must not wait for replay frames
    that never arrive.

    Scenario: the tab quit while fully caught up (from_sequence == live) and
    the shell produced no output since. The daemon replay slice is empty, so
    no terminal frames will follow attach — REPLAYING would be terminal and
    the widget would show "Connecting" forever.
    """
    session_id = SessionId("test-session")
    controller._tab_state.session_id = session_id
    controller._tab_state.state = TerminalSessionState.DETACHED

    controller.attach(from_sequence=100)
    controller._on_session_attached(
        _attach_result(
            session_id,
            AttachmentId("test-attachment"),
            available_start=0,  # history is retained…
            live_sequence=100,  # …but the tab was already caught up
        )
    )

    assert controller.state == TerminalSessionState.ACTIVE


def test_replay_caught_up_at_live_boundary_activates_without_live_frame(controller):
    """Replay frames reaching the attach-time live sequence must end REPLAYING
    even when every frame is replay-flagged (idle session after reattach)."""
    session_id = SessionId("test-session")
    controller._tab_state.session_id = session_id
    controller._tab_state.state = TerminalSessionState.DETACHED

    controller.attach(from_sequence=0)
    controller._on_session_attached(
        _attach_result(
            session_id,
            AttachmentId("test-attachment"),
            available_start=0,
            live_sequence=100,
        )
    )
    assert controller.state == TerminalSessionState.REPLAYING

    controller._handle_output(
        TerminalOutput(session_id=session_id, sequence=0, data=b"x" * 60, replay=True)
    )
    # Not yet caught up to the live boundary (60 < 100).
    assert controller.state == TerminalSessionState.REPLAYING

    controller._handle_output(
        TerminalOutput(session_id=session_id, sequence=60, data=b"y" * 40, replay=True)
    )
    # Replay reached the attach-time live sequence: stream is caught up even
    # though no live (non-replay) frame has arrived.
    assert controller.state == TerminalSessionState.ACTIVE


def test_output_handling(controller):
    """Test terminal output handling."""
    controller._tab_state.state = TerminalSessionState.REPLAYING
    
    output_data = b"test output"
    on_output_calls = []
    
    def capture_output(data):
        on_output_calls.append(data)
    
    controller._on_output = capture_output
    
    # Simulate replay output
    replay_output = TerminalOutput(
        session_id="test-session",
        sequence=0,
        data=output_data,
        replay=True,
    )
    
    controller._handle_output(replay_output)
    
    # Should still be replaying
    assert controller.state == TerminalSessionState.REPLAYING
    assert on_output_calls == [output_data]
    
    # Simulate live output
    live_output = TerminalOutput(
        session_id="test-session",
        sequence=len(output_data),
        data=b"live data",
        replay=False,
    )
    
    controller._handle_output(live_output)
    
    # Should transition to active
    assert controller.state == TerminalSessionState.ACTIVE
    assert len(on_output_calls) == 2


def test_send_input_without_ownership(controller):
    """Test sending input without ownership."""
    controller._tab_state.input_owner = False
    controller._tab_state.session_id = SessionId("test")
    controller._tab_state.attachment_id = AttachmentId("test")
    
    # Should not send input
    controller.send_input(b"test")
    
    # Bridge should not be called
    controller._bridge.submit.assert_not_called()


def test_send_input_with_ownership(controller, mock_bridge):
    """Test sending input with ownership."""
    controller._tab_state.input_owner = True
    controller._tab_state.session_id = SessionId("test")
    controller._tab_state.attachment_id = AttachmentId("test")
    
    # Send input
    test_data = b"test input"
    controller.send_input(test_data)
    
    # Bridge should be called
    mock_bridge.submit.assert_called()


def test_resize_without_ownership(controller):
    """Test resize without ownership."""
    controller._tab_state.input_owner = False
    
    dimensions = TerminalDimensions(rows=30, columns=100)
    controller.resize(dimensions)
    
    # Bridge should not be called
    controller._bridge.submit.assert_not_called()


def test_resize_with_ownership(controller, mock_bridge):
    """Test resize with ownership."""
    controller._tab_state.input_owner = True
    controller._tab_state.session_id = SessionId("test")
    controller._tab_state.attachment_id = AttachmentId("test")
    
    dimensions = TerminalDimensions(rows=30, columns=100)
    controller.resize(dimensions)
    
    # Bridge should be called
    mock_bridge.submit.assert_called()


def test_detach_session(controller, mock_bridge):
    """Test session detachment."""
    controller._tab_state.session_id = SessionId("test")
    controller._tab_state.attachment_id = AttachmentId("test")
    controller._tab_state.input_owner = True
    
    # Set up a mock stream
    mock_stream = Mock()
    controller._stream = mock_stream
    
    controller.detach()
    
    # Should update state
    assert controller.state == TerminalSessionState.DETACHED
    assert not controller.input_owner
    
    # Should close stream
    mock_stream.close.assert_called_once()
    assert controller._stream is None
    
    # Should call detach on bridge
    mock_bridge.submit.assert_called()


def test_close_session(controller, mock_bridge):
    """Test session termination."""
    controller._tab_state.session_id = SessionId("test")
    
    # Set up a mock stream
    mock_stream = Mock()
    controller._stream = mock_stream
    
    controller.close()
    
    # Should update state
    assert controller.state == TerminalSessionState.CLOSING
    assert controller._closed
    
    # Should close stream
    mock_stream.close.assert_called_once()
    
    # Should call close on bridge
    mock_bridge.submit.assert_called()


def test_close_during_opening(controller, mock_client, mock_bridge):
    """Test closing while session is opening."""
    controller._tab_state.state = TerminalSessionState.OPENING
    controller._opening_session_id = SessionId("test")
    # Set session_id so it tries to close
    controller._tab_state.session_id = SessionId("test")
    
    controller.close()
    
    # Should set closed flag
    assert controller._closed
    # State should transition to closing first
    assert controller.state == TerminalSessionState.CLOSING


def test_continuity_lost_starts_recovery(controller, mock_bridge):
    """A damaged stream is replaced and replayed, not marked then continued."""
    continuity_lost_calls = []
    
    def capture_continuity_lost():
        continuity_lost_calls.append(True)
    
    controller._on_continuity_lost = capture_continuity_lost
    controller._tab_state.session_id = SessionId("test-session")
    controller._tab_state.attachment_id = AttachmentId("attachment")
    controller._tab_state.state = TerminalSessionState.ACTIVE
    controller._stream = Mock()
    mock_bridge.bind_terminal.return_value = Mock()

    controller._handle_continuity_lost("test-session", 100, 50)

    assert controller.state is TerminalSessionState.RECOVERING
    assert len(continuity_lost_calls) == 0
    mock_bridge.submit.assert_called_once()


def test_error_handling(controller):
    """Test error handling."""
    error_calls = []
    
    def capture_error(error):
        error_calls.append(error)
    
    controller._on_error = capture_error
    
    test_error = Exception("Test error")
    controller._on_open_error(test_error)
    
    # Should transition to failed state
    assert controller.state == TerminalSessionState.FAILED
    assert len(error_calls) == 1


def test_required_capabilities():
    """Test required capabilities helper."""
    required = required_daemon_terminal_capabilities()
    
    # Should include key terminal capabilities
    from sshpilot.api.capabilities import Capability
    assert Capability.SESSIONS_READ in required
    assert Capability.SESSIONS_WRITE in required
    assert Capability.TERMINAL_INPUT in required
    assert Capability.TERMINAL_OUTPUT in required


def test_missing_capabilities_helper():
    """Test missing capabilities helper."""
    client = Mock()
    capabilities = Mock()
    capabilities.supported = frozenset()
    client.get_capabilities.return_value = capabilities
    
    missing = daemon_terminal_capabilities_missing(client)

    # Should return all required capabilities as missing
    required = required_daemon_terminal_capabilities()
    assert missing == required


def _run_submit(fn, on_success=None, on_error=None, **kwargs):
    """Bridge stub that executes the operation synchronously."""
    try:
        result = fn()
    except Exception as exc:  # pragma: no cover - defensive
        if on_error is not None:
            on_error(exc)
        return
    if on_success is not None:
        on_success(result)


@pytest.fixture
def active_session(mock_client, mock_bridge, connection_id):
    """Controller on a live daemon session with its event callback captured.

    Returns (controller, on_event, on_state_changed) where on_event is the
    callback the controller registered via client.subscribe_events.
    """
    on_state_changed = Mock()
    controller = DaemonTerminalSessionController(
        client=mock_client,
        bridge=mock_bridge,
        connection_id=connection_id,
        view_id="test-view",
        on_state_changed=on_state_changed,
    )
    mock_client.subscribe_events = Mock(return_value=Mock(unsubscribe=Mock()))
    mock_bridge.submit.side_effect = _run_submit
    session_id = SessionId("test-session")
    controller._tab_state.session_id = session_id
    controller._tab_state.state = TerminalSessionState.ACTIVE
    controller._subscribe_session_events(session_id)
    on_event = mock_client.subscribe_events.call_args[0][0]
    return controller, on_event, on_state_changed


def _session_event(event_type, payload, session_id):
    from sshpilot.api.events import CoreEvent

    return CoreEvent(
        type=event_type,
        payload=payload,
        sequence=1,
        session_id=session_id,
    )


def test_session_exited_event_closes_tab_state(active_session):
    """Remote exit (e.g. host reboot) must close the tab state and notify."""
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import SessionExitInfo

    controller, on_event, on_state_changed = active_session
    info = SessionExitInfo(exit_code=255, reason="process_exit")

    on_event(_session_event(EventType.SESSION_EXITED, info, controller.tab_state.session_id))

    assert controller.state == TerminalSessionState.CLOSED
    assert controller.exit_info == info
    on_state_changed.assert_called_once()


def test_session_exited_clean_exit_records_exit_code(active_session):
    """A clean exit (user typed exit) is reported with exit code 0."""
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import SessionExitInfo

    controller, on_event, on_state_changed = active_session
    info = SessionExitInfo(exit_code=0, reason="process_exit")

    on_event(_session_event(EventType.SESSION_EXITED, info, controller.tab_state.session_id))

    assert controller.state == TerminalSessionState.CLOSED
    assert controller.exit_info is not None
    assert controller.exit_info.exit_code == 0
    on_state_changed.assert_called_once()


def test_session_closed_event_closes_tab_state(active_session):
    """SESSION_CLOSED carries a SessionSummary and must also notify."""
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import SessionState, SessionSummary

    controller, on_event, on_state_changed = active_session
    summary = SessionSummary(
        id=controller.tab_state.session_id,
        connection_id=ConnectionId("test-connection"),
        state=SessionState.CLOSED,
    )

    on_event(_session_event(EventType.SESSION_CLOSED, summary, controller.tab_state.session_id))

    assert controller.state == TerminalSessionState.CLOSED
    on_state_changed.assert_called_once()


def test_exited_then_closed_notifies_once(active_session):
    """The daemon sends EXITED then CLOSED; the tab must be notified once."""
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import (
        SessionExitInfo,
        SessionState,
        SessionSummary,
    )

    controller, on_event, on_state_changed = active_session
    info = SessionExitInfo(exit_code=255, reason="process_exit")

    on_event(_session_event(EventType.SESSION_EXITED, info, controller.tab_state.session_id))
    on_event(
        _session_event(
            EventType.SESSION_CLOSED,
            SessionSummary(
                id=controller.tab_state.session_id,
                connection_id=ConnectionId("test-connection"),
                state=SessionState.CLOSED,
                exit_info=info,
            ),
            controller.tab_state.session_id,
        )
    )

    assert controller.state == TerminalSessionState.CLOSED
    assert controller.exit_info == info
    on_state_changed.assert_called_once()


def test_session_events_after_close_ignored(active_session):
    """User-initiated close must not be overwritten by late daemon events."""
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import SessionExitInfo

    controller, on_event, on_state_changed = active_session
    controller.close()

    on_event(
        _session_event(
            EventType.SESSION_EXITED,
            SessionExitInfo(exit_code=255, reason="process_exit"),
            controller.tab_state.session_id,
        )
    )

    assert controller.exit_info is None
    on_state_changed.assert_not_called()


def test_session_state_changed_non_terminal_ignored(active_session):
    """Non-terminal session states must not disturb the tab."""
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import SessionState, SessionSummary

    controller, on_event, on_state_changed = active_session
    summary = SessionSummary(
        id=controller.tab_state.session_id,
        connection_id=ConnectionId("test-connection"),
        state=SessionState.STARTING,
    )

    on_event(_session_event(EventType.SESSION_STATE_CHANGED, summary, controller.tab_state.session_id))

    assert controller.state == TerminalSessionState.ACTIVE
    assert controller.session_running is False
    on_state_changed.assert_not_called()


def test_session_state_changed_running_observed(active_session):
    """RUNNING is surfaced so automated feeds can wait for authentication."""
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import SessionState, SessionSummary

    controller, on_event, on_state_changed = active_session
    summary = SessionSummary(
        id=controller.tab_state.session_id,
        connection_id=ConnectionId("test-connection"),
        state=SessionState.RUNNING,
    )

    on_event(_session_event(EventType.SESSION_STATE_CHANGED, summary, controller.tab_state.session_id))

    assert controller.session_running is True
    assert controller.state == TerminalSessionState.ACTIVE
    on_state_changed.assert_called_once()


def test_plugin_startup_failure_is_formatted_when_frontend_handles_event(
    active_session,
    monkeypatch,
):
    from sshpilot.api.errors import ErrorCode
    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import (
        PluginSessionFailure,
        PluginSessionFailureCode,
        SessionState,
        SessionSummary,
    )
    import sshpilot.terminal_session_controller as controller_module

    controller, on_event, _on_state_changed = active_session
    errors = []
    controller._on_error = errors.append
    monkeypatch.setattr(
        controller_module,
        "format_plugin_session_failure",
        lambda failure: f"translated:{failure.parameters['runtime']}",
    )
    summary = SessionSummary(
        id=controller.tab_state.session_id,
        connection_id=ConnectionId("test-connection"),
        state=SessionState.FAILED,
        failure=PluginSessionFailure(
            code=PluginSessionFailureCode.CONTAINER_RUNTIME_UNAVAILABLE,
            error_code=ErrorCode.SESSION_STARTUP_FAILED,
            parameters={"runtime": "podman"},
        ),
    )

    on_event(
        _session_event(
            EventType.SESSION_STATE_CHANGED,
            summary,
            controller.tab_state.session_id,
        )
    )

    assert controller.state is TerminalSessionState.FAILED
    assert len(errors) == 1
    assert errors[0].code is ErrorCode.SESSION_STARTUP_FAILED
    assert errors[0].message == "translated:podman"


def test_session_closed_event_with_wrong_payload_ignored(active_session):
    """Malformed SESSION_CLOSED payloads must not affect the tab."""
    from types import SimpleNamespace

    from sshpilot.api.events import EventType
    from sshpilot.api.models.sessions import SessionState

    controller, on_event, on_state_changed = active_session

    on_event(
        SimpleNamespace(
            type=EventType.SESSION_CLOSED,
            payload=Mock(state=SessionState.CLOSED),
            session_id=controller.tab_state.session_id,
        )
    )

    assert controller.state == TerminalSessionState.ACTIVE
    on_state_changed.assert_not_called()
