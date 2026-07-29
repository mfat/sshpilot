"""Test terminal session controller state machine with mocked client/bridge."""

from unittest.mock import Mock, patch

import pytest

from sshpilot.api.models.common import AttachmentId, ConnectionId, SessionId
from sshpilot.api.models.sessions import AttachmentInfo, SessionSummary, SessionState
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
    assert not controller.input_owner


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


def test_continuity_lost_callback(controller):
    """Test continuity lost handling."""
    continuity_lost_calls = []
    
    def capture_continuity_lost():
        continuity_lost_calls.append(True)
    
    controller._on_continuity_lost = capture_continuity_lost
    
    controller._handle_continuity_lost("test-session", 100, 50)
    
    assert len(continuity_lost_calls) == 1


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