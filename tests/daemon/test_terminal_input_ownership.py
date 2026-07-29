"""Test terminal input ownership claim/release with multi-attachment support."""

import threading
import uuid

import pytest

from sshpilot.api import InProcessClient
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import AttachmentId, ClientId, ConnectionId
from sshpilot.api.models.sessions import AttachSessionRequest, OpenSessionRequest, SessionExitInfo, SessionState
from sshpilot.api.models.terminal import TerminalDimensions
from sshpilot.connection_identity import new_connection_uuid
from sshpilot.daemon.session_runtime import SessionRuntime


class _Connection:
    def __init__(self):
        self.nickname = "test"
        self.uuid = new_connection_uuid()
        self.host = "test"
        self.hostname = "test.example"
        self.username = "alice"
        self.port = 22
        self.protocol = "ssh"
        self.aliases = []
        self.auth_method = 0
        self.keyfile = ""
        self.identity_files = []
        self.certificate = ""
        self.certificate_files = []
        self.x11_forwarding = False
        self.forwarding_rules = []
        self.proxy_jump = []
        self.data = {}


class _Manager:
    def __init__(self):
        self.connection = _Connection()

    def get_connections(self):
        return [self.connection]

    def connect(self, _name, _callback):
        return 1

    def disconnect(self, _handler_id):
        return None


class ControlledHandle:
    def __init__(self, on_exit, *, exit_on_terminate=True):
        self._on_exit = on_exit
        self._exit_on_terminate = exit_on_terminate
        self._exit_info = None
        self.terminated = 0
        self.killed = 0
        self._lock = threading.Lock()

    def terminate(self):
        self.terminated += 1
        if self._exit_on_terminate:
            self.exit(SessionExitInfo(exit_code=0, reason="terminated"))

    def kill(self):
        self.killed += 1
        self.exit(SessionExitInfo(signal=9, reason="killed"))

    def wait(self, timeout):
        del timeout
        with self._lock:
            return self._exit_info

    def exit(self, exit_info):
        with self._lock:
            if self._exit_info is not None:
                return
            self._exit_info = exit_info
        self._on_exit(exit_info)


class ControlledRunner:
    def __init__(self, *, fail=False, exit_on_terminate=True):
        self.fail = fail
        self.exit_on_terminate = exit_on_terminate
        self.handles = []
        self.specs = []
        self.closed = False
        self.terminal_capable = True  # Enable terminal support

    def start(self, spec, on_exit, on_output=None, on_eof=None):
        self.specs.append(spec)
        if self.fail:
            raise RuntimeError("sensitive runner failure")
        handle = ControlledHandle(
            on_exit,
            exit_on_terminate=self.exit_on_terminate,
        )
        self.handles.append(handle)
        return handle
    
    def close(self):
        self.closed = True


@pytest.fixture
def session_runtime():
    """Create a session runtime for testing."""
    manager = _Manager()
    core = InProcessClient(manager)
    runner = ControlledRunner()
    runtime = SessionRuntime(core, runner=runner)
    yield runtime, core
    runtime.shutdown()
    core.close()


@pytest.fixture
def connection_id():
    """Test connection ID."""
    return ConnectionId("test-connection")


@pytest.fixture
def client_id():
    """Test client ID."""
    return ClientId("test-client")


@pytest.fixture
def session_with_attachment(session_runtime, client_id):
    """Create a session with one attachment."""
    runtime, core = session_runtime
    connection_id = core.list_connections()[0].id
    
    # Open session
    session = runtime.open_session(
        OpenSessionRequest(
            connection_id=connection_id,
            dimensions=TerminalDimensions(rows=24, columns=80),
        ),
        client_id=client_id,
    )
    
    # Check session state - debug info
    if session.state != SessionState.RUNNING:
        print(f"Session failure: {session.failure}")
        print(f"Session exit info: {session.exit_info}")
    
    assert session.state == SessionState.RUNNING, f"Session state is {session.state}, expected RUNNING"
    
    # Attach to session
    result = runtime.attach_session(
        AttachSessionRequest(
            session_id=session.id,
            request_input=True,
            want_terminal_output=True,
            from_sequence=0,
        ),
        client_id=client_id,
    )
    
    return {
        'session_id': session.id,
        'attachment_id': result.attachment.id,
        'client_id': client_id,
        'runtime': runtime,
    }


def test_claim_input_success(session_with_attachment):
    """Test successful input ownership claim."""
    session_id = session_with_attachment['session_id']
    attachment_id = session_with_attachment['attachment_id']
    client_id = session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Initially attachment should own input (from attach with request_input=True)
    summary = runtime.get_session(session_id)
    assert summary.input_owner is not None
    assert summary.input_owner.attachment_id == attachment_id
    
    # Release input first
    runtime.release_input(session_id, attachment_id, client_id)
    
    # Verify input is released
    summary = runtime.get_session(session_id)
    assert summary.input_owner is None
    
    # Claim input back
    runtime.claim_input(session_id, attachment_id, client_id)
    
    # Verify input is claimed
    summary = runtime.get_session(session_id)
    assert summary.input_owner is not None
    assert summary.input_owner.attachment_id == attachment_id


def test_claim_input_already_owned(session_with_attachment):
    """Test claiming input when already owned by another attachment."""
    session_id = session_with_attachment['session_id']
    attachment_id = session_with_attachment['attachment_id']
    session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Create a second attachment
    client_id_2 = ClientId("test-client-2")
    result_2 = runtime.attach_session(
        AttachSessionRequest(
            session_id=session_id,
            request_input=False,  # Don't request input
            want_terminal_output=True,
            from_sequence=0,
        ),
        client_id=client_id_2,
    )
    attachment_id_2 = result_2.attachment.id
    
    # First attachment should still own input
    summary = runtime.get_session(session_id)
    assert summary.input_owner is not None
    assert summary.input_owner.attachment_id == attachment_id
    
    # Second attachment should not be able to claim input
    with pytest.raises(SshPilotError) as exc_info:
        runtime.claim_input(session_id, attachment_id_2, client_id_2)
    
    assert exc_info.value.code == ErrorCode.TERMINAL_INPUT_OWNER_EXISTS


def test_claim_input_invalid_attachment(session_with_attachment):
    """Test claiming input with invalid attachment."""
    session_id = session_with_attachment['session_id']
    client_id = session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Use non-existent attachment ID
    invalid_attachment = AttachmentId(f"attachment:{uuid.uuid4()}")
    
    with pytest.raises(SshPilotError) as exc_info:
        runtime.claim_input(session_id, invalid_attachment, client_id)
    
    assert exc_info.value.code == ErrorCode.TERMINAL_ATTACHMENT_REQUIRED


def test_claim_input_wrong_client(session_with_attachment):
    """Test claiming input with wrong client ID."""
    session_id = session_with_attachment['session_id']
    attachment_id = session_with_attachment['attachment_id']
    runtime = session_with_attachment['runtime']
    
    # Use wrong client ID
    wrong_client = ClientId("wrong-client")
    
    with pytest.raises(SshPilotError) as exc_info:
        runtime.claim_input(session_id, attachment_id, wrong_client)
    
    assert exc_info.value.code == ErrorCode.TERMINAL_ATTACHMENT_REQUIRED


def test_release_input_success(session_with_attachment):
    """Test successful input ownership release."""
    session_id = session_with_attachment['session_id']
    attachment_id = session_with_attachment['attachment_id']
    client_id = session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Initially attachment should own input
    summary = runtime.get_session(session_id)
    assert summary.input_owner is not None
    assert summary.input_owner.attachment_id == attachment_id
    
    # Release input
    runtime.release_input(session_id, attachment_id, client_id)
    
    # Verify input is released
    summary = runtime.get_session(session_id)
    assert summary.input_owner is None


def test_release_input_not_owner(session_with_attachment):
    """Test releasing input when not owner."""
    session_id = session_with_attachment['session_id']
    session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Create a second attachment
    client_id_2 = ClientId("test-client-2")
    result_2 = runtime.attach_session(
        AttachSessionRequest(
            session_id=session_id,
            request_input=False,  # Don't request input
            want_terminal_output=True,
            from_sequence=0,
        ),
        client_id=client_id_2,
    )
    attachment_id_2 = result_2.attachment.id
    
    # Second attachment should not be able to release input (doesn't own it)
    with pytest.raises(SshPilotError) as exc_info:
        runtime.release_input(session_id, attachment_id_2, client_id_2)
    
    assert exc_info.value.code == ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED


def test_multi_attachment_per_client(session_with_attachment):
    """Test multiple attachments per client."""
    session_id = session_with_attachment['session_id']
    client_id = session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Create a second attachment for the same client
    result_2 = runtime.attach_session(
        AttachSessionRequest(
            session_id=session_id,
            request_input=False,  # Don't request input
            want_terminal_output=True,
            from_sequence=0,
        ),
        client_id=client_id,
    )
    attachment_id_2 = result_2.attachment.id
    
    # Both attachments should belong to same client but have different IDs
    assert session_with_attachment['attachment_id'] != attachment_id_2
    
    # Client should still receive output (any attachment wants it)
    assert runtime.receives_terminal(session_id, client_id)
    
    # Second attachment should not have input ownership
    assert not result_2.attachment.input_owner


def test_client_disconnect_cleans_all_attachments(session_with_attachment):
    """Test that client disconnection cleans up all attachments."""
    session_id = session_with_attachment['session_id']
    client_id = session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Create a second attachment for the same client
    runtime.attach_session(
        AttachSessionRequest(
            session_id=session_id,
            request_input=False,
            want_terminal_output=True,
            from_sequence=0,
        ),
        client_id=client_id,
    )
    
    # Verify client has output subscription
    assert runtime.receives_terminal(session_id, client_id)
    
    # Verify input ownership exists
    summary = runtime.get_session(session_id)
    assert summary.input_owner is not None
    
    # Disconnect client
    runtime.detach_client(client_id)
    
    # Verify all attachments are cleaned up
    assert not runtime.receives_terminal(session_id, client_id)
    
    # Verify input ownership is released
    summary = runtime.get_session(session_id)
    assert summary.input_owner is None
    
    # Verify attachment count is 0
    assert summary.attachment_count == 0


def test_output_subscription_with_multiple_attachments(session_with_attachment):
    """Test output subscription behavior with multiple attachments."""
    session_id = session_with_attachment['session_id']
    client_id = session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Initially client receives output
    assert runtime.receives_terminal(session_id, client_id)
    
    # Create a second attachment without output
    runtime.attach_session(
        AttachSessionRequest(
            session_id=session_id,
            request_input=False,
            want_terminal_output=False,  # No output
            from_sequence=0,
        ),
        client_id=client_id,
    )
    
    # Client should still receive output (first attachment wants it)
    assert runtime.receives_terminal(session_id, client_id)
    
    # Detach the first attachment (that wants output)
    from sshpilot.api.models.sessions import DetachSessionRequest
    runtime.detach_session(
        DetachSessionRequest(
            session_id=session_id,
            attachment_id=session_with_attachment['attachment_id'],
        ),
        client_id=client_id,
    )
    
    # Client should no longer receive output (no attachment wants it)
    assert not runtime.receives_terminal(session_id, client_id)


def test_always_create_new_attachment(session_with_attachment):
    """Test that attach_session always creates new attachments (Phase 9 behavior)."""
    session_id = session_with_attachment['session_id']
    client_id = session_with_attachment['client_id']
    runtime = session_with_attachment['runtime']
    
    # Create multiple attachments for same client
    attachment_ids = [session_with_attachment['attachment_id']]
    
    for _ in range(3):
        result = runtime.attach_session(
            AttachSessionRequest(
                session_id=session_id,
                request_input=False,
                want_terminal_output=False,
                from_sequence=0,
            ),
            client_id=client_id,
        )
        attachment_ids.append(result.attachment.id)
    
    # All attachment IDs should be unique
    assert len(set(attachment_ids)) == len(attachment_ids)
    
    # Session should show correct attachment count
    summary = runtime.get_session(session_id)
    assert summary.attachment_count == 4