"""Frontend-neutral GTK-facing session controller abstraction and daemon implementation."""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Optional, Protocol

from .api.capabilities import Capability
from .api.errors import ErrorCode, SshPilotError
from .api.events import EventType
from .api.models.common import AttachmentId, ConnectionId, SessionId
from .api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    DetachSessionRequest,
    OpenSessionRequest,
    PluginSessionFailure,
    SessionExitInfo,
    SessionSummary,
    SessionState,
)
from .api.models.terminal import (
    ReplayRequest,
    ResizeTerminalRequest,
    TerminalDimensions,
    TerminalInput,
)
from .gtk.plugin_session_failure_messages import format_plugin_session_failure

logger = logging.getLogger(__name__)

_RECOVERY_REPLAY_CHUNK_BYTES = 512 * 1024


def _session_failure_presentation(failure) -> tuple[ErrorCode, str]:
    if type(failure) is PluginSessionFailure:
        return failure.error_code, format_plugin_session_failure(failure)
    try:
        code = ErrorCode(failure.code)
    except ValueError:
        code = ErrorCode.SESSION_STARTUP_FAILED
    return code, failure.message


class TerminalSessionState(str, Enum):
    """GTK-facing session state tracking."""
    IDLE = "idle"
    OPENING = "opening"
    ATTACHING = "attaching"
    REPLAYING = "replaying"
    RECOVERING = "recovering"
    ACTIVE = "active"
    DETACHED = "detached"
    CLOSING = "closing"
    FAILED = "failed"
    CLOSED = "closed"


@dataclass
class DaemonTerminalTabState:
    """State tracking for daemon terminal tab."""
    view_id: str
    session_id: SessionId | None
    attachment_id: AttachmentId | None
    connection_id: ConnectionId
    daemon_instance_id: str
    expected_sequence: int = 0
    input_owner: bool = False
    state: TerminalSessionState = TerminalSessionState.IDLE
    exit_info: Optional[SessionExitInfo] = None
    session_running: bool = False


class TerminalSessionController(Protocol):
    """Frontend-neutral session controller interface."""

    def open(
        self,
        connection_id: ConnectionId,
        dimensions: Optional[TerminalDimensions] = None,
    ) -> None:
        """Start opening a new session. Async operation."""
        ...

    def attach(
        self,
        want_output: bool = True,
        request_input: bool = True,
        from_sequence: int = 0,
    ) -> None:
        """Attach to the session. Requires session to be opened first."""
        ...

    def detach(self) -> None:
        """Detach from session without terminating it."""
        ...

    def close(self) -> None:
        """Terminate the session."""
        ...

    def send_input(self, data: bytes) -> None:
        """Send input data to the session. Requires input ownership."""
        ...

    def resize(self, dimensions: TerminalDimensions) -> None:
        """Resize terminal. Requires input ownership (resize authority)."""
        ...

    def subscribe_output(
        self,
        callback: Callable[[bytes], None],
    ) -> None:
        """Subscribe to terminal output."""
        ...

    @property
    def tab_state(self) -> DaemonTerminalTabState:
        """Current tab state."""
        ...

    @property
    def state(self) -> TerminalSessionState:
        """Current session state."""
        ...

    @property
    def session_running(self) -> bool:
        """Whether the daemon session has reached RUNNING (authenticated)."""
        ...

    @property
    def session_events_subscribed(self) -> bool:
        """Whether daemon session state events are being observed."""
        ...

    @property
    def input_owner(self) -> bool:
        """Whether this controller owns input."""
        ...


class DaemonTerminalSessionController:
    """Production controller using DaemonClient + GtkClientBridge.

    - Requires capabilities: sessions.*, terminal.*, interactions needed
    - open(connection_id, dimensions) -> sessions.open then attach
    - Successful open with state STARTING is normal; RUNNING is async
    - State machine: idle→opening→attaching→replaying→active→detached|closing|failed→closed
    - One open request per activation; cancel while opening supported
    - If closed before open completes, reconcile via on_discard closing the session
    - attach establishes replay/live boundary while session may still be STARTING
    - Interactive only after attachment exists
    - Continuity loss: call on_continuity_lost callback (GTK shows local marker; never send marker to daemon)
    - detach() detaches attachment without terminating
    - close() terminates session
    - send_input requires input_owner
    - resize only when input_owner (resize authority)
    - Uses bridge.submit / bind_terminal
    - Tracks DaemonTerminalTabState
    - Records server_instance_id from client.server_instance_id
    - Does NOT spawn local SSH / askpass / secrets
    """

    def __init__(
        self,
        client,
        bridge,
        connection_id: ConnectionId,
        view_id: str,
        on_output: Optional[Callable[[bytes], None]] = None,
        on_continuity_lost: Optional[Callable[[], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_state_changed: Optional[Callable[[], None]] = None,
    ) -> None:
        self._client = client
        self._bridge = bridge
        self._connection_id = connection_id
        self._view_id = view_id
        self._on_output = on_output
        self._on_continuity_lost = on_continuity_lost
        self._on_error = on_error or self._default_error_handler
        self._on_state_changed = on_state_changed

        # Validate required capabilities
        missing = daemon_terminal_capabilities_missing(client)
        if missing:
            raise RuntimeError(
                f"Required daemon terminal capabilities unavailable: {missing}"
            )

        self._tab_state = DaemonTerminalTabState(
            view_id=view_id,
            session_id=None,
            attachment_id=None,
            connection_id=connection_id,
            daemon_instance_id=getattr(client, 'server_instance_id', ''),
            expected_sequence=0,
            input_owner=False,
            state=TerminalSessionState.IDLE,
        )

        self._stream = None
        self._closed = False
        self._opening_session_id: Optional[SessionId] = None
        self._event_subscription = None
        self._session_events_subscribed = False
        self._restoring_existing = False
        self._attach_from_sequence = 0
        self._replay_catchup_target: Optional[int] = None
        self._stream_generation = 0
        self._recovery_catchup_target: Optional[int] = None
        self._recovery_replay_pending = None
        self._resize_in_flight = False
        self._resize_sent: Optional[TerminalDimensions] = None
        self._pending_resize: Optional[TerminalDimensions] = None
        self._recovery_input = deque()
        self._recovery_input_bytes = 0
        self._max_recovery_input_bytes = 256 * 1024

    @property
    def tab_state(self) -> DaemonTerminalTabState:
        """Current tab state."""
        return self._tab_state

    @property
    def state(self) -> TerminalSessionState:
        """Current session state."""
        return self._tab_state.state

    @property
    def session_running(self) -> bool:
        """Whether the daemon session has reached RUNNING (authenticated)."""
        return self._tab_state.session_running

    @property
    def session_events_subscribed(self) -> bool:
        """Whether daemon session state events are being observed.

        When True, ``session_running`` is authoritative; when False (e.g. a
        restored/detached attach without an event subscription) callers must
        fall back to local evidence such as terminal output.
        """
        return self._session_events_subscribed

    @property
    def input_owner(self) -> bool:
        """Whether this controller owns input."""
        return self._tab_state.input_owner

    @property
    def exit_info(self) -> Optional[SessionExitInfo]:
        """Exit details once the daemon session has exited, if reported."""
        return self._tab_state.exit_info

    def set_client(self, client) -> None:
        """Rebind to a replaced daemon client after a transport reconnect.

        The daemon owns session/attachment state, so it survives a transport
        swap — only this controller's client handle needs to move. Without
        this, a deferred callback that fires after reconnect (e.g. a
        session-opened success racing the old transport's shutdown) still
        calls through the closed client and raises "The client is closed"
        (``subscribe_terminal``/``attach_session`` etc.) uncaught.
        """
        if client is self._client:
            return
        self._client = client
        session_id = self._tab_state.session_id
        if session_id is not None:
            self._subscribe_session_events(session_id)
        if (
            self._stream is not None
            and self._tab_state.state
            in {
                TerminalSessionState.ACTIVE,
                TerminalSessionState.REPLAYING,
                TerminalSessionState.RECOVERING,
            }
        ):
            self._begin_output_recovery(reattach=True)

    def open(
        self,
        connection_id: ConnectionId,
        dimensions: Optional[TerminalDimensions] = None,
        remote_command: Optional[str] = None,
        force_tty: bool = False,
    ) -> None:
        """Start opening a new session. Async operation."""
        if self._closed:
            raise RuntimeError("Controller is closed")

        if self._tab_state.state != TerminalSessionState.IDLE:
            raise RuntimeError(f"Cannot open session in state: {self._tab_state.state}")

        self._tab_state.state = TerminalSessionState.OPENING
        self._tab_state.connection_id = connection_id
        self._restoring_existing = False

        self._bridge.submit(
            lambda: self._client.open_session(
                OpenSessionRequest(
                    connection_id=connection_id,
                    dimensions=dimensions,
                    remote_command=remote_command,
                    force_tty=force_tty,
                )
            ),
            on_success=self._on_session_opened,
            on_error=self._on_open_error,
            on_discard=self._discard_opened_session,
        )

    def attach(
        self,
        want_output: bool = True,
        request_input: bool = True,
        from_sequence: int = 0,
    ) -> None:
        """Attach to the session. Requires session to be opened first."""
        if self._closed:
            raise RuntimeError("Controller is closed")

        if not self._tab_state.session_id:
            raise RuntimeError("No session to attach to")

        restoring_existing = self._tab_state.state is TerminalSessionState.DETACHED
        if self._tab_state.state not in {TerminalSessionState.OPENING, TerminalSessionState.DETACHED}:
            raise RuntimeError(f"Cannot attach in state: {self._tab_state.state}")

        self._restoring_existing = restoring_existing
        self._tab_state.state = TerminalSessionState.ATTACHING
        self._attach_from_sequence = max(0, int(from_sequence or 0))

        # Set up terminal output stream first
        if want_output and not self._stream:
            self._replace_output_binding(self._client, paused=False)

        # Then attach to the session
        self._bridge.submit(
            lambda: self._client.attach_session(
                AttachSessionRequest(
                    session_id=self._tab_state.session_id,
                    request_input=request_input,
                    want_terminal_output=want_output,
                    from_sequence=from_sequence,
                )
            ),
            on_success=self._on_session_attached,
            on_error=self._on_attach_error,
        )

    def detach(self) -> None:
        """Detach from session without terminating it."""
        if self._closed or not self._tab_state.attachment_id:
            return

        self._tab_state.state = TerminalSessionState.DETACHED
        self._tab_state.input_owner = False
        self._invalidate_output_binding()
        self._clear_pending_terminal_control()

        if self._stream:
            self._stream.close()
            self._stream = None

        self._bridge.submit(
            lambda: self._client.detach_session(
                DetachSessionRequest(
                    session_id=self._tab_state.session_id,
                    attachment_id=self._tab_state.attachment_id,
                )
            ),
            on_success=lambda _: None,
            on_error=self._on_error,
        )

    def close(self) -> None:
        """Terminate the session."""
        if self._closed:
            return

        self._closed = True
        self._tab_state.state = TerminalSessionState.CLOSING
        self._invalidate_output_binding()
        self._clear_pending_terminal_control()
        self._unsubscribe_events()

        # Close output stream first
        if self._stream:
            self._stream.close()
            self._stream = None

        # Terminate session if we have one
        if self._tab_state.session_id:
            try:
                self._bridge.submit(
                    lambda: self._client.close_session(
                        CloseSessionRequest(session_id=self._tab_state.session_id)
                    ),
                    on_success=self._on_session_closed,
                    on_error=self._on_error,
                )
            except RuntimeError:
                # Bridge might be closed
                self._tab_state.state = TerminalSessionState.CLOSED
        else:
            self._tab_state.state = TerminalSessionState.CLOSED

    def send_input(self, data: bytes) -> None:
        """Send input data to the session. Requires input ownership."""
        if self._closed or not self._tab_state.input_owner:
            return

        if not self._tab_state.session_id:
            return

        if not self._tab_state.attachment_id:
            if self._tab_state.state is TerminalSessionState.RECOVERING:
                self._queue_recovery_input(data)
            return

        submit = (
            self._bridge.submit_terminal_input
            if callable(
                getattr(type(self._bridge), "submit_terminal_input", None)
            )
            else self._bridge.submit
        )
        try:
            submit(
                lambda: self._client.send_terminal_input(
                    TerminalInput(
                        session_id=self._tab_state.session_id,
                        attachment_id=self._tab_state.attachment_id,
                        data=data,
                    )
                ),
                on_success=lambda _: None,
                on_error=self._on_input_error,
            )
        except BaseException as error:
            self._on_input_error(error)

    def resize(self, dimensions: TerminalDimensions) -> None:
        """Resize terminal. Requires input ownership (resize authority)."""
        if self._closed or not self._tab_state.input_owner:
            return

        if not (self._tab_state.session_id and self._tab_state.attachment_id):
            return

        if self._resize_in_flight:
            self._pending_resize = dimensions
            return
        self._resize_in_flight = True
        self._submit_resize(dimensions)

    def _submit_resize(self, dimensions: TerminalDimensions) -> None:
        self._resize_sent = dimensions
        try:
            self._bridge.submit(
                lambda: self._client.resize_terminal(
                    ResizeTerminalRequest(
                        session_id=self._tab_state.session_id,
                        attachment_id=self._tab_state.attachment_id,
                        dimensions=dimensions,
                    )
                ),
                on_success=lambda _: self._finish_resize(None),
                on_error=self._finish_resize,
            )
        except BaseException as error:
            self._finish_resize(error)

    def _finish_resize(self, error) -> None:
        if error is not None:
            self._on_input_error(error)
        if self._closed or not self._tab_state.input_owner:
            self._resize_in_flight = False
            self._pending_resize = None
            return
        pending = self._pending_resize
        self._pending_resize = None
        if pending is not None and pending != self._resize_sent:
            self._submit_resize(pending)
            return
        self._resize_in_flight = False

    def subscribe_output(
        self,
        callback: Callable[[bytes], None],
    ) -> None:
        """Subscribe to terminal output."""
        self._on_output = callback

    def _on_session_opened(self, summary) -> None:
        """Handle accepted session open (normally STARTING)."""
        if self._closed:
            self._discard_opened_session(summary)
            return

        state = getattr(summary, "state", None)
        if isinstance(state, SessionState) and state not in {
            SessionState.STARTING,
            SessionState.RUNNING,
        }:
            self._tab_state.session_id = summary.id
            self._tab_state.state = TerminalSessionState.FAILED
            failure = getattr(summary, "failure", None)
            if failure is not None:
                code, message = _session_failure_presentation(failure)
                self._on_error(
                    SshPilotError(
                        code,
                        message,
                        session_id=summary.id,
                    )
                )
            else:
                self._on_error(
                    SshPilotError(
                        ErrorCode.SESSION_STARTUP_FAILED,
                        "The session could not be started",
                        session_id=summary.id,
                    )
                )
            return

        self._tab_state.session_id = summary.id
        self._tab_state.session_running = state is SessionState.RUNNING
        self._opening_session_id = None
        self._subscribe_session_events(summary.id)

        # Auto-attach after opening; RUNNING is observed asynchronously.
        self.attach()

    def _discard_opened_session(self, summary) -> None:
        """Close a session accepted after the controller was cancelled."""
        try:
            self._bridge.submit(
                lambda: self._client.close_session(
                    CloseSessionRequest(session_id=summary.id)
                ),
                on_success=lambda _: None,
                on_error=lambda _: None,
            )
        except RuntimeError:
            pass

    def _subscribe_session_events(self, session_id: SessionId) -> None:
        subscribe = getattr(self._client, "subscribe_events", None)
        if not callable(subscribe):
            return
        self._unsubscribe_events()

        def _on_event(event) -> None:
            if self._closed or event.session_id != session_id:
                return
            payload = event.payload
            if event.type is EventType.SESSION_STATE_CHANGED:
                state = getattr(payload, "state", None)
                if state not in {
                    SessionState.RUNNING,
                    SessionState.FAILED,
                    SessionState.EXITED,
                    SessionState.CLOSED,
                }:
                    return
            elif event.type is EventType.SESSION_EXITED:
                # The daemon emits SESSION_EXITED (SessionExitInfo payload) for
                # process exit — e.g. a remote reboot killing ssh or the user
                # typing exit — instead of SESSION_STATE_CHANGED.
                if not isinstance(payload, SessionExitInfo):
                    return
            elif event.type is EventType.SESSION_CLOSED:
                if not isinstance(payload, SessionSummary) or payload.state is not SessionState.CLOSED:
                    return
            else:
                return
            try:
                self._bridge.submit(
                    lambda: payload,
                    on_success=self._on_async_session_state,
                    on_error=lambda _: None,
                )
            except RuntimeError:
                pass

        try:
            self._event_subscription = subscribe(_on_event)
            self._session_events_subscribed = self._event_subscription is not None
        except SshPilotError:
            pass

    def _unsubscribe_events(self) -> None:
        subscription = self._event_subscription
        self._event_subscription = None
        self._session_events_subscribed = False
        if subscription is None:
            return
        unsubscribe = getattr(subscription, "unsubscribe", None)
        if callable(unsubscribe):
            try:
                unsubscribe()
            except Exception:
                logger.debug("Daemon session event unsubscription failed", exc_info=True)

    def _on_async_session_state(self, summary) -> None:
        """Apply asynchronous daemon session failure/exit to the open tab."""
        if self._closed:
            return
        if isinstance(summary, SessionExitInfo):
            # SESSION_EXITED carries the exit details directly.
            self._tab_state.exit_info = summary
            if self._tab_state.state not in {
                TerminalSessionState.CLOSING,
                TerminalSessionState.CLOSED,
            }:
                self._tab_state.state = TerminalSessionState.CLOSED
                self._notify_state_changed()
            return
        state = getattr(summary, "state", None)
        if state is SessionState.RUNNING:
            if not self._tab_state.session_running:
                self._tab_state.session_running = True
                self._notify_state_changed()
            return
        if state is SessionState.FAILED:
            self._tab_state.state = TerminalSessionState.FAILED
            failure = getattr(summary, "failure", None)
            code = ErrorCode.SESSION_STARTUP_FAILED
            message = "The session process could not be started"
            if failure is not None:
                code, message = _session_failure_presentation(failure)
            self._on_error(
                SshPilotError(
                    code,
                    message,
                    session_id=self._tab_state.session_id,
                )
            )
        elif state in {SessionState.EXITED, SessionState.CLOSED}:
            exit_info = getattr(summary, "exit_info", None)
            if isinstance(exit_info, SessionExitInfo):
                self._tab_state.exit_info = exit_info
            if self._tab_state.state not in {
                TerminalSessionState.CLOSING,
                TerminalSessionState.CLOSED,
            }:
                self._tab_state.state = TerminalSessionState.CLOSED
                self._notify_state_changed()

    def _on_session_attached(self, result) -> None:
        """Handle session attach completion."""
        if self._closed:
            return

        self._tab_state.attachment_id = result.attachment.id
        self._tab_state.input_owner = result.attachment.input_owner

        # The daemon replays [min(from_sequence, live), live). REPLAYING is
        # only valid while replay frames are actually in flight — reattaching
        # to an idle session whose replay slice is empty delivers no frames at
        # all, so waiting for output here would stick the tab on "Connecting".
        replay_from = min(self._attach_from_sequence, result.live_sequence)
        # This is the next byte represented by the existing frontend state.
        # Do not advance to live_sequence until replay bytes have actually
        # passed through the terminal output callback.
        self._tab_state.expected_sequence = replay_from
        if replay_from < result.live_sequence:
            self._replay_catchup_target = result.live_sequence
            self._tab_state.state = TerminalSessionState.REPLAYING
        else:
            self._replay_catchup_target = None
            self._tab_state.state = TerminalSessionState.ACTIVE
        self._notify_state_changed()

    def _notify_state_changed(self) -> None:
        callback = self._on_state_changed
        if callback is not None:
            callback()

    def _replace_output_binding(self, client, *, paused: bool):
        self._stream_generation += 1
        generation = self._stream_generation
        previous = self._stream
        self._stream = None
        if previous is not None:
            previous.close()
        binding = self._bridge.bind_terminal(
            client,
            self._tab_state.session_id,
            on_output=lambda output: self._handle_bound_output(generation, output),
            on_continuity_lost=lambda session_id, expected, available: (
                self._handle_bound_continuity(
                    generation,
                    session_id,
                    expected,
                    available,
                )
            ),
            on_error=lambda error: self._handle_bound_error(generation, error),
            start_paused=paused,
            recovery_sequence=(
                self._tab_state.expected_sequence if paused else None
            ),
        )
        if generation != self._stream_generation or self._closed:
            binding.close()
            return generation, None
        self._stream = binding
        return generation, binding

    def _invalidate_output_binding(self) -> None:
        self._stream_generation += 1
        self._recovery_catchup_target = None
        self._recovery_replay_pending = None

    def _handle_bound_output(self, generation: int, output) -> None:
        if generation != self._stream_generation or self._closed:
            return
        self._handle_output(output)

    def _handle_bound_continuity(
        self,
        generation: int,
        session_id,
        expected,
        available,
    ) -> None:
        if generation != self._stream_generation or self._closed:
            return
        self._handle_continuity_lost(session_id, expected, available)

    def _handle_bound_error(self, generation: int, error) -> None:
        if generation != self._stream_generation or self._closed:
            return
        self._on_error(error)

    def _begin_output_recovery(self, *, reattach: bool) -> None:
        if self._closed or self._tab_state.session_id is None:
            return
        safe_sequence = self._tab_state.expected_sequence
        self._tab_state.state = TerminalSessionState.RECOVERING
        self._notify_state_changed()
        generation, binding = self._replace_output_binding(
            self._client,
            paused=True,
        )
        if binding is None:
            return
        recovery_client = self._client

        if reattach:
            self._tab_state.attachment_id = None

            def operation():
                return recovery_client.attach_session(
                    AttachSessionRequest(
                        session_id=self._tab_state.session_id,
                        request_input=True,
                        want_terminal_output=True,
                        from_sequence=safe_sequence,
                    )
                )

            def on_success(result):
                self._finish_output_reattach(
                    generation,
                    binding,
                    safe_sequence,
                    result,
                    recovery_client,
                )
        else:
            attachment_id = self._tab_state.attachment_id
            if attachment_id is None:
                self._fail_output_recovery(
                    generation,
                    "The terminal output attachment is unavailable",
                )
                return
            self._submit_output_replay(
                generation,
                binding,
                safe_sequence,
                recovery_client,
                attachment_id,
            )
            return
        try:
            self._bridge.submit(
                operation,
                on_success=on_success,
                on_error=lambda error: self._fail_output_recovery(
                    generation,
                    str(error),
                ),
            )
        except BaseException as error:
            self._fail_output_recovery(generation, str(error))

    def _submit_output_replay(
        self,
        generation: int,
        binding,
        start_sequence: int,
        recovery_client,
        attachment_id,
    ) -> None:
        try:
            self._bridge.submit(
                lambda: recovery_client.replay_terminal(
                    ReplayRequest(
                        session_id=self._tab_state.session_id,
                        attachment_id=attachment_id,
                        after_sequence=start_sequence,
                        max_bytes=_RECOVERY_REPLAY_CHUNK_BYTES,
                    )
                ),
                on_success=lambda result: self._finish_output_replay(
                    generation,
                    binding,
                    start_sequence,
                    result,
                    recovery_client,
                    attachment_id,
                ),
                on_error=lambda error: self._fail_output_recovery(
                    generation,
                    str(error),
                ),
            )
        except BaseException as error:
            self._fail_output_recovery(generation, str(error))

    def _finish_output_replay(
        self,
        generation: int,
        binding,
        safe_sequence: int,
        result,
        recovery_client,
        attachment_id,
    ) -> None:
        if not self._recovery_is_current(generation, binding):
            return
        if (
            result.first_sequence != safe_sequence
            or result.next_sequence < safe_sequence
        ):
            self._fail_output_recovery(
                generation,
                "The retained terminal output cannot restore this view",
            )
            return
        if result.truncated and result.next_sequence == safe_sequence:
            self._fail_output_recovery(
                generation,
                "The retained terminal output cannot restore this view",
            )
            return
        self._recovery_replay_pending = (
            generation,
            binding,
            result.next_sequence,
            result.truncated,
            recovery_client,
            attachment_id,
        )
        binding.resume(
            replay_end=result.next_sequence,
            allow_live=not result.truncated,
        )
        self._continue_replay_if_chunk_delivered()

    def _continue_replay_if_chunk_delivered(self) -> None:
        pending = self._recovery_replay_pending
        if pending is None:
            return
        (
            generation,
            binding,
            end_sequence,
            truncated,
            recovery_client,
            attachment_id,
        ) = pending
        if not self._recovery_is_current(generation, binding):
            self._recovery_replay_pending = None
            return
        if self._tab_state.expected_sequence < end_sequence:
            return
        self._recovery_replay_pending = None
        if truncated:
            self._submit_output_replay(
                generation,
                binding,
                end_sequence,
                recovery_client,
                attachment_id,
            )
            return
        self._recovery_catchup_target = end_sequence
        self._finish_output_recovery_if_caught_up()

    def _finish_output_reattach(
        self,
        generation: int,
        binding,
        safe_sequence: int,
        result,
        recovery_client,
    ) -> None:
        if not self._recovery_is_current(generation, binding):
            self._discard_recovery_attachment(recovery_client, result)
            return
        if (
            result.replay_truncated
            or result.available_start > safe_sequence
            or result.live_sequence < safe_sequence
        ):
            self._fail_output_recovery(
                generation,
                "The retained terminal output cannot restore this view",
            )
            return
        self._tab_state.attachment_id = result.attachment.id
        self._tab_state.input_owner = result.attachment.input_owner
        self._validate_and_resume_recovery(binding, result.live_sequence)
        self._flush_recovery_input()

    def _discard_recovery_attachment(self, client, result) -> None:
        """Release an attachment created by a superseded reconnect."""

        try:
            self._bridge.submit(
                lambda: client.detach_session(
                    DetachSessionRequest(
                        session_id=result.attachment.session_id,
                        attachment_id=result.attachment.id,
                    )
                ),
                on_success=lambda _result: None,
                on_error=lambda _error: None,
            )
        except BaseException:
            logger.debug("Discarding stale recovery attachment failed", exc_info=True)

    def _recovery_is_current(self, generation: int, binding) -> bool:
        return (
            not self._closed
            and generation == self._stream_generation
            and binding is self._stream
            and self._tab_state.state is TerminalSessionState.RECOVERING
        )

    def _validate_and_resume_recovery(self, binding, target: int) -> None:
        self._recovery_catchup_target = target
        binding.resume(replay_end=target)
        if self._tab_state.expected_sequence >= target:
            self._finish_output_recovery_if_caught_up()

    def _finish_output_recovery_if_caught_up(self) -> None:
        target = self._recovery_catchup_target
        if (
            self._tab_state.state is TerminalSessionState.RECOVERING
            and target is not None
            and self._tab_state.expected_sequence >= target
        ):
            self._recovery_catchup_target = None
            self._tab_state.state = TerminalSessionState.ACTIVE
            self._notify_state_changed()

    def _fail_output_recovery(self, generation: int, message: str) -> None:
        if generation != self._stream_generation or self._closed:
            return
        binding = self._stream
        self._invalidate_output_binding()
        self._stream = None
        if binding is not None:
            binding.close()
        had_pending_input = bool(self._recovery_input)
        self._tab_state.input_owner = False
        self._tab_state.state = TerminalSessionState.FAILED
        self._clear_pending_terminal_control()
        self._notify_state_changed()
        if self._on_continuity_lost is not None:
            self._on_continuity_lost()
        if had_pending_input:
            self._on_error(
                SshPilotError(
                    ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                    "Terminal input was not delivered during output recovery",
                    session_id=self._tab_state.session_id,
                )
            )
        self._on_error(
            SshPilotError(
                ErrorCode.TERMINAL_REPLAY_UNAVAILABLE,
                message or "Terminal output continuity could not be restored",
                session_id=self._tab_state.session_id,
            )
        )

    def _queue_recovery_input(self, data: bytes) -> None:
        if self._recovery_input_bytes + len(data) > self._max_recovery_input_bytes:
            self._on_error(
                SshPilotError(
                    ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
                    "Terminal input was not delivered while output recovered",
                    session_id=self._tab_state.session_id,
                )
            )
            return
        self._recovery_input.append(data)
        self._recovery_input_bytes += len(data)

    def _flush_recovery_input(self) -> None:
        pending = tuple(self._recovery_input)
        self._recovery_input.clear()
        self._recovery_input_bytes = 0
        for data in pending:
            self.send_input(data)

    def _clear_pending_terminal_control(self) -> None:
        self._pending_resize = None
        self._resize_in_flight = False
        self._recovery_input.clear()
        self._recovery_input_bytes = 0

    def _on_session_closed(self, _result) -> None:
        """Handle session close completion."""
        self._unsubscribe_events()
        self._tab_state.state = TerminalSessionState.CLOSED

    def _handle_output(self, output) -> None:
        """Handle terminal output."""
        if self._closed:
            return

        # A same-transport replay request may race already-queued live frames.
        # Until replay reaches its response-time live boundary, only replay
        # frames can rebuild the parser state from expected_sequence.
        if (
            self._tab_state.state is TerminalSessionState.RECOVERING
            and self._recovery_catchup_target is not None
            and self._tab_state.expected_sequence
            < self._recovery_catchup_target
            and not output.replay
        ):
            return

        if self._on_output:
            self._on_output(output.data)

        # Advance only after the VTE-facing callback returned successfully.
        # This remains the authoritative replay start for this presentation.
        self._tab_state.expected_sequence = output.next_sequence

        # Transition from replaying to active when we reach live data, or when
        # replay frames have caught up to the attach-time live sequence (an
        # idle session produces no live frame to end REPLAYING otherwise).
        if self._tab_state.state == TerminalSessionState.REPLAYING:
            target = self._replay_catchup_target
            if not output.replay or (
                target is not None and output.next_sequence >= target
            ):
                self._replay_catchup_target = None
                self._tab_state.state = TerminalSessionState.ACTIVE
                self._notify_state_changed()

        # This sequence now identifies bytes delivered through the VTE-facing
        # callback, rather than merely received or spooled by the binding.
        if self._tab_state.state is TerminalSessionState.RECOVERING:
            self._continue_replay_if_chunk_delivered()
            self._finish_output_recovery_if_caught_up()

    def _handle_continuity_lost(self, session_id, expected, available) -> None:
        """Handle terminal continuity loss."""
        if self._closed or session_id != self._tab_state.session_id:
            return
        self._begin_output_recovery(reattach=False)

    def _on_open_error(self, error) -> None:
        """Handle session open error."""
        self._tab_state.state = TerminalSessionState.FAILED
        self._on_error(error)

    def _on_attach_error(self, error) -> None:
        """Handle session attach error."""
        stale_session = getattr(getattr(error, "code", None), "value", None) == "session_already_closed"
        if self._restoring_existing and stale_session:
            # Restore metadata can outlive a daemon session that failed or was
            # closed between discovery and the asynchronous attach request.
            # This is stale restore state, not a connection failure.
            logger.info("Discarding stale daemon session attachment")
            self._tab_state.state = TerminalSessionState.CLOSED
            self._tab_state.attachment_id = None
            self._tab_state.input_owner = False
            if self._stream is not None:
                self._stream.close()
                self._stream = None
            self._notify_state_changed()
            return
        if stale_session and self._tab_state.session_id is not None:
            # The attach raced a startup failure. Fetch the session summary so
            # the UI reports the daemon's actual launch failure instead of the
            # generic attachment-state error.
            try:
                self._bridge.submit(
                    lambda: self._client.get_session(self._tab_state.session_id),
                    on_success=self._on_failed_attach_session,
                    on_error=lambda _: self._finish_attach_error(error),
                )
                return
            except RuntimeError:
                pass
        self._finish_attach_error(error)

    def _on_failed_attach_session(self, summary) -> None:
        failure = getattr(summary, "failure", None)
        if failure is None:
            self._finish_attach_error(
                SshPilotError(
                    ErrorCode.SESSION_STARTUP_FAILED,
                    "The daemon session failed before attachment",
                    session_id=self._tab_state.session_id,
                )
            )
            return
        code, message = _session_failure_presentation(failure)
        self._finish_attach_error(
            SshPilotError(
                code,
                message,
                session_id=self._tab_state.session_id,
            )
        )

    def _finish_attach_error(self, error) -> None:
        self._tab_state.state = TerminalSessionState.FAILED
        self._on_error(error)

    def _on_input_error(self, error) -> None:
        """Handle input/resize faults without failing the whole session."""
        code = getattr(error, "code", None)
        if code in {
            ErrorCode.TERMINAL_INPUT_BACKPRESSURE,
            ErrorCode.TERMINAL_INPUT_OWNER_REQUIRED,
            ErrorCode.TERMINAL_ATTACHMENT_REQUIRED,
            ErrorCode.SESSION_INVALID_STATE,
            ErrorCode.SERVER_BUSY,
        }:
            logger.debug(
                "Transient terminal input/resize error: %s",
                getattr(code, "value", code),
            )
            # Still surface to the widget so it can classify (non-fatal).
            if self._on_error is not self._default_error_handler:
                self._on_error(error)
            return
        self._on_error(error)

    @staticmethod
    def _default_error_handler(error) -> None:
        """Default error handler."""
        logger.warning(
            "Daemon terminal session operation failed: %s",
            getattr(getattr(error, "code", None), "value", "internal_error"),
        )


def required_daemon_terminal_capabilities() -> frozenset[Capability]:
    """Required capabilities for daemon terminal sessions."""
    return frozenset({
        Capability.SESSIONS_READ,
        Capability.SESSIONS_WRITE,
        Capability.SESSIONS_EVENTS,
        Capability.TERMINAL_OUTPUT,
        Capability.TERMINAL_INPUT,
        Capability.TERMINAL_RESIZE,
        Capability.TERMINAL_REPLAY,
        Capability.INTERACTIONS_READ,
        Capability.INTERACTIONS_RESPOND,
        Capability.INTERACTIONS_EVENTS,
        Capability.INTERACTIONS_HOST_KEY,
        Capability.INTERACTIONS_PASSWORD,
        Capability.INTERACTIONS_PASSPHRASE,
    })


def daemon_terminal_capabilities_missing(client) -> frozenset[Capability]:
    """Return missing required capabilities for daemon terminal sessions."""
    required = required_daemon_terminal_capabilities()
    supported = client.get_capabilities().supported
    return required - supported
