"""Selector-driven Unix-domain daemon server."""

from __future__ import annotations

import errno
import logging
import os
import queue
import secrets
import selectors
import socket
import stat
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, Optional, Set, Tuple, Union

from sshpilot.runtime_identity import new_server_instance_id

from sshpilot import __version__ as sshpilot_version
from sshpilot.core.errors import CoreError
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.ssh_overrides_service import SshOverridesService
from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import CoreEvent, EventType, EventPublisher, Subscription
from sshpilot.api.models.common import (
    ForwardId,
    InteractionId,
    RequestId,
    SessionId,
    SftpServiceId,
    TransferId,
)
from sshpilot.api.models.daemon import (
    DaemonDiagnostics,
    DaemonLifecycleState,
    DaemonResourceCounts,
    DaemonStatus,
)
from sshpilot.api.models.interactions import InteractionState
from sshpilot.api.models.operations import (
    ForwardState,
    OperationId,
    SftpServiceState,
)
from sshpilot.api.models.sessions import SessionState
from sshpilot.api.models.terminal import TerminalInput, TerminalOutput
from sshpilot.api.models.transfers import TransferState
from sshpilot.api.transport.codec import (
    decode_envelope,
    encode_envelope,
    error_to_wire,
    public_event_to_envelope,
)
from sshpilot.api.transport.envelopes import (
    ErrorResponseEnvelope,
    RequestEnvelope,
    SuccessResponseEnvelope,
)
from sshpilot.api.transport.framing import (
    FramingError,
    MultiplexedFrameDecoder,
    encode_binary_frame,
    encode_frame,
)
from sshpilot.api.transport.terminal_frames import (
    TerminalFrame,
    TerminalFrameFlags,
    TerminalFrameKind,
    encode_terminal_payload,
)
from sshpilot.api.transport.secret_frames import (
    SecretFrame,
    SecretFrameKind,
    encode_secret_payload,
)
from sshpilot.api.version import PROTOCOL_VERSION
from sshpilot.logging_support import copy_log_context, log_context

from .command_executor import (
    DEFAULT_SESSION_COMMAND_QUEUE_LIMIT,
    DEFAULT_SESSION_COMMAND_WORKERS,
    BoundedCommandExecutor,
    DeferredCommand,
)
from .config_reload import (
    AuthoritativeConfigurationBackend,
    ConfigurationReloadCoordinator,
    ConfigurationWatcher,
)
from .dispatch import (
    ClientProtocolState,
    DeferredResult,
    ImmediateResult,
    RequestDispatcher,
    SecretResponseResult,
)
from .forward_runtime import ForwardRuntime, SubprocessForwardProcessRunner
from .interaction_broker import InteractionBroker
from .key_service import DaemonKeyService
from .known_hosts_service import KnownHostsService
from .lifecycle import (
    DaemonAlreadyRunningError,
    SocketSecurityError,
    ensure_secure_socket_directory,
    prepare_socket_path,
    resolve_socket_path,
    unlink_owned_socket,
    verify_bound_socket,
)
from .lifecycle_policy import DaemonLifecycleController, _IDLE_SHUTDOWN_UNSET
from .runtime_cleanup import (
    sweep_runtime_directory_on_startup,
    sweep_stale_askpass_sockets,
)
from .session_runtime import SessionRuntime
from .sftp_runtime import SftpServiceRuntime, SubprocessSftpProcessRunner
from .ssh_readiness import (
    DEFAULT_READINESS_GRACE_SECONDS as DEFAULT_SSH_READINESS_GRACE_SECONDS,
)
from .transfer_runtime import TransferRuntime

logger = logging.getLogger(__name__)

DEFAULT_CLIENT_EVENT_QUEUE_LIMIT = 256
DEFAULT_MAX_CLIENT_OUTBOUND_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_CLIENT_TERMINAL_BYTES = 1024 * 1024
DEFAULT_SESSION_SHUTDOWN_SECONDS = 3.0
_FORWARDED_EVENT_TYPES = frozenset(
    {
        EventType.CONNECTION_CREATED,
        EventType.CONNECTION_UPDATED,
        EventType.CONNECTION_DELETED,
        EventType.CONNECTION_STORE_CHANGED,
        EventType.SESSION_CREATED,
        EventType.SESSION_STATE_CHANGED,
        EventType.SESSION_EXITED,
        EventType.SESSION_CLOSED,
        EventType.INTERACTION_CREATED,
        EventType.INTERACTION_STATE_CHANGED,
        EventType.SFTP_CREATED,
        EventType.SFTP_STATE_CHANGED,
        EventType.SFTP_CLOSED,
        EventType.SFTP_FAILED,
        EventType.TRANSFER_CREATED,
        EventType.TRANSFER_STARTED,
        EventType.TRANSFER_PROGRESS,
        EventType.TRANSFER_ITEM_COMPLETED,
        EventType.TRANSFER_COMPLETED,
        EventType.TRANSFER_CANCELLED,
        EventType.TRANSFER_FAILED,
        EventType.FORWARD_CREATED,
        EventType.FORWARD_STARTING,
        EventType.FORWARD_ACTIVE,
        EventType.FORWARD_CLOSED,
        EventType.FORWARD_FAILED,
        EventType.DAEMON_STATE_CHANGED,
        EventType.BROADCAST_OUTPUT,
    }
)
_INTERACTION_CANCELLING_EVENT_TYPES = frozenset(
    {
        EventType.SESSION_EXITED,
        EventType.SESSION_CLOSED,
        EventType.SFTP_CLOSED,
        EventType.SFTP_FAILED,
        EventType.FORWARD_CLOSED,
        EventType.FORWARD_FAILED,
    }
)


@dataclass(frozen=True)
class _OutboundFrame:
    data: bytes
    is_event: bool = False
    is_terminal: bool = False
    session_id: Optional[SessionId] = None
    terminal_sequence: Optional[int] = None


@dataclass(frozen=True)
class CoreServices:
    """Daemon-owned application services injected directly into API dispatch."""

    connections: ConnectionApplicationService
    configuration_backend: Optional[AuthoritativeConfigurationBackend] = None
    known_hosts: Optional[KnownHostsService] = None
    keys: Optional[DaemonKeyService] = None
    ssh_overrides: Optional[SshOverridesService] = None
    secrets: Any = None
    identity: Any = None
    operations: Any = None
    scp_backend: Any = None
    plugin_settings: Any = None


@dataclass
class _ClientConnection:
    sock: socket.socket
    token: int = 0
    decoder: MultiplexedFrameDecoder = field(default_factory=MultiplexedFrameDecoder)
    output: Deque[_OutboundFrame] = field(default_factory=deque)
    output_offset: int = 0
    queued_event_count: int = 0
    queued_outbound_bytes: int = 0
    queued_terminal_bytes: int = 0
    protocol: ClientProtocolState = field(default_factory=ClientProtocolState)
    close_after_write: bool = False
    continuity_lost: bool = False
    terminal_continuity_lost: Set[SessionId] = field(default_factory=set)
    closed: bool = False
    pending_request_ids: Set[RequestId] = field(default_factory=set)


@dataclass(frozen=True)
class _DeferredCompletion:
    peer_token: int
    protocol_version: str
    request_id: RequestId
    result: Any = None
    error: Optional[SshPilotError] = None


class DaemonServer:
    """Own one core client and serve all local clients on its owner thread."""

    def __init__(
        self,
        core_factory: Callable[[], Union[ConnectionApplicationService, CoreServices]],
        *,
        socket_path: Optional[os.PathLike] = None,
        client_event_queue_limit: int = DEFAULT_CLIENT_EVENT_QUEUE_LIMIT,
        max_client_outbound_bytes: int = DEFAULT_MAX_CLIENT_OUTBOUND_BYTES,
        max_client_terminal_bytes: int = DEFAULT_MAX_CLIENT_TERMINAL_BYTES,
        session_runtime_factory: Optional[
            Callable[[ConnectionApplicationService], SessionRuntime]
        ] = None,
        session_command_workers: int = DEFAULT_SESSION_COMMAND_WORKERS,
        session_command_queue_limit: int = DEFAULT_SESSION_COMMAND_QUEUE_LIMIT,
        session_shutdown_timeout: float = DEFAULT_SESSION_SHUTDOWN_SECONDS,
        configuration_watcher_factory: Optional[
            Callable[[], ConfigurationWatcher]
        ] = None,
        configuration_reload_debounce: float = 0.2,
        configuration_poll_interval: float = 1.0,
        interaction_broker_factory: Optional[
            Callable[[SessionRuntime], InteractionBroker]
        ] = None,
        idle_shutdown_seconds: object = _IDLE_SHUTDOWN_UNSET,
        service_mode: bool = False,
        packaged: bool = False,
        drain_timeout_seconds: float = 5.0,
        ssh_readiness_grace_seconds: float = DEFAULT_SSH_READINESS_GRACE_SECONDS,
    ) -> None:
        if (
            type(client_event_queue_limit) is not int
            or client_event_queue_limit < 1
        ):
            raise ValueError("client event queue limit must be positive")
        if (
            type(max_client_outbound_bytes) is not int
            or max_client_outbound_bytes < 1
        ):
            raise ValueError("maximum client outbound bytes must be positive")
        if (
            type(max_client_terminal_bytes) is not int
            or max_client_terminal_bytes < 1
        ):
            raise ValueError("maximum client terminal bytes must be positive")
        if type(session_command_workers) is not int or session_command_workers < 1:
            raise ValueError("session command worker count must be positive")
        if (
            type(session_command_queue_limit) is not int
            or session_command_queue_limit < 1
        ):
            raise ValueError("session command queue limit must be positive")
        if session_shutdown_timeout < 0:
            raise ValueError("session shutdown timeout must not be negative")
        if configuration_reload_debounce < 0:
            raise ValueError("configuration reload debounce must not be negative")
        if configuration_poll_interval <= 0:
            raise ValueError("configuration poll interval must be positive")
        if ssh_readiness_grace_seconds <= 0:
            raise ValueError("ssh readiness grace seconds must be positive")
        self.socket_path = resolve_socket_path(socket_path)
        self.client_event_queue_limit = client_event_queue_limit
        self.max_client_outbound_bytes = max_client_outbound_bytes
        self.max_client_terminal_bytes = max_client_terminal_bytes
        self._core_factory = core_factory
        self._session_runtime_factory = session_runtime_factory
        self.session_command_workers = session_command_workers
        self.session_command_queue_limit = session_command_queue_limit
        self.session_shutdown_timeout = float(session_shutdown_timeout)
        self.ssh_readiness_grace_seconds = float(ssh_readiness_grace_seconds)
        self._configuration_watcher_factory = configuration_watcher_factory
        self.configuration_reload_debounce = float(
            configuration_reload_debounce
        )
        self.configuration_poll_interval = float(configuration_poll_interval)
        self._interaction_broker_factory = interaction_broker_factory
        self._idle_shutdown_seconds = idle_shutdown_seconds
        self._service_mode = bool(service_mode)
        self._packaged = bool(packaged)
        self._drain_timeout_seconds = float(drain_timeout_seconds)
        self._lifecycle: Optional[DaemonLifecycleController] = None
        self._selector: Optional[selectors.BaseSelector] = None
        self._listener: Optional[socket.socket] = None
        self._wakeup_read: Optional[socket.socket] = None
        self._wakeup_write: Optional[socket.socket] = None
        self._connection_service: Optional[ConnectionApplicationService] = None
        self._known_hosts_service: Optional[KnownHostsService] = None
        self._key_service: Optional[DaemonKeyService] = None
        self._ssh_overrides_service: Optional[SshOverridesService] = None
        self._secrets_service: Any = None
        self._identity_service: Any = None
        self._operation_runtime: Any = None
        self._session_runtime: Optional[SessionRuntime] = None
        self._sftp_runtime: Optional[SftpServiceRuntime] = None
        self._transfer_runtime: Optional[TransferRuntime] = None
        self._forward_runtime: Optional[ForwardRuntime] = None
        self._interaction_broker: Optional[InteractionBroker] = None
        self._dispatcher: Optional[RequestDispatcher] = None
        self._session_executor: Optional[BoundedCommandExecutor] = None
        self._configuration_reload: Optional[
            ConfigurationReloadCoordinator
        ] = None
        self._completion_queue: queue.Queue[_DeferredCompletion] = queue.Queue(
            maxsize=session_command_queue_limit
        )
        self._clients: Dict[int, _ClientConnection] = {}
        self._clients_by_token: Dict[int, _ClientConnection] = {}
        self._next_peer_token = 1
        self._socket_identity: Optional[Tuple[int, int]] = None
        self._stopping = threading.Event()
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._startup_error: Optional[BaseException] = None
        self._thread: Optional[threading.Thread] = None
        self._event_lock = threading.Lock()
        self._accepting_core_events = False
        self._core_subscription: Optional[Subscription] = None
        self._session_subscription: Optional[Subscription] = None
        self._terminal_subscription: Optional[Subscription] = None
        self._interaction_subscription: Optional[Subscription] = None
        self._sftp_subscription: Optional[Subscription] = None
        self._transfer_subscription: Optional[Subscription] = None
        self._forward_subscription: Optional[Subscription] = None
        self._operation_subscription: Optional[Subscription] = None
        self._broadcast_publisher = EventPublisher()
        self._broadcast_subscription: Optional[Subscription] = None
        self._command_input_condition = threading.Condition()
        self._command_inputs: Dict[str, bytearray] = {}
        self._terminal_queue: queue.Queue[TerminalOutput] = queue.Queue(maxsize=1024)
        self._terminal_publication_lost: Set[SessionId] = set()
        self._next_event_sequence = 0
        self._session_shutdown_started = False
        self._started_at_wall: Optional[datetime] = None
        self._instance_id = new_server_instance_id()

    @property
    def ready(self) -> bool:
        return self._ready.is_set() and self._startup_error is None

    @property
    def stopped(self) -> bool:
        return self._stopped.is_set()

    def start_in_thread(self, *, timeout: float = 5.0) -> threading.Thread:
        """Start the production server loop in one ownership thread."""

        if self._thread is not None:
            raise RuntimeError("daemon server has already been started")
        thread = threading.Thread(
            target=self.serve_forever,
            name="sshpilot-daemon",
            daemon=True,
        )
        self._thread = thread
        thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("daemon server did not become ready")
        if self._startup_error is not None:
            raise self._startup_error
        return thread

    def serve_forever(self) -> None:
        """Construct the core, bind securely, and dispatch until shutdown."""

        try:
            self._setup()
        except BaseException as exc:
            if isinstance(exc, CoreError) and exc.code.value == "CONNECTION_STATE_IO_ERROR":
                logger.error(
                    "connection state rejected category=%s reason=%s",
                    exc.diagnostic_category,
                    exc.diagnostic_reason,
                )
            else:
                logger.error("Daemon startup failed type=%s", type(exc).__name__)
            frames = traceback.extract_tb(exc.__traceback__)
            if frames:
                logger.debug(
                    "Daemon startup traceback (exception text redacted): %s",
                    " -> ".join(
                        f"{frame.filename}:{frame.lineno} in {frame.name}"
                        for frame in frames
                    ),
                )
            self._startup_error = exc
            if self._lifecycle is not None:
                self._lifecycle.mark_failed()
            self._ready.set()
            self._cleanup()
            if self._lifecycle is not None:
                self._lifecycle.mark_stopped()
            self._stopped.set()
            return
        if self._lifecycle is not None:
            self._lifecycle.mark_ready()
        with self._event_lock:
            self._accepting_core_events = True
        self._ready.set()
        try:
            while not self._stopping.is_set():
                selector = self._selector
                if selector is None:
                    break
                timeout = None
                if self._lifecycle is not None:
                    timeout = self._lifecycle.select_timeout()
                    if self._lifecycle.poll_idle():
                        self.shutdown()
                        continue
                for key, mask in selector.select(timeout):
                    if key.data == "listener":
                        self._accept_client()
                    elif key.data == "wakeup":
                        self._drain_wakeup()
                    else:
                        state = key.data
                        if mask & selectors.EVENT_READ:
                            self._read_client(state)
                        if (
                            state.sock.fileno() >= 0
                            and mask & selectors.EVENT_WRITE
                        ):
                            self._write_client(state)
                if self._lifecycle is not None and self._lifecycle.poll_idle():
                    self.shutdown()
        finally:
            if self._lifecycle is not None:
                self._lifecycle.begin_stopping()
            self._flush_outbound_before_shutdown(timeout=0.5)
            self._stop_configuration_reload()
            self._stop_core_events()
            if self._dispatcher is not None:
                self._dispatcher.begin_shutdown()
            self._shutdown_session_commands()
            self._cleanup()
            if self._lifecycle is not None:
                self._lifecycle.mark_stopped()
            self._stopped.set()

    def _flush_outbound_before_shutdown(self, *, timeout: float) -> None:
        """Best-effort write of queued RPC replies before tearing sockets down."""

        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            with self._event_lock:
                pending = [
                    state
                    for state in self._clients.values()
                    if not state.closed and state.output
                ]
            if not pending:
                return
            for state in pending:
                try:
                    self._write_client(state)
                except Exception:
                    logger.debug("flush-before-shutdown write failed", exc_info=True)
            time.sleep(0.01)

    def shutdown(self) -> None:
        """Request shutdown without blocking the caller."""

        if self._stopping.is_set():
            return
        if self._lifecycle is not None and not self._lifecycle.is_draining_or_stopping():
            self._lifecycle.begin_drain(reason="clean_shutdown")
        self._stop_core_events()
        if self._dispatcher is not None:
            self._dispatcher.begin_shutdown()
        self._stopping.set()
        wakeup = self._wakeup_write
        if wakeup is not None:
            try:
                wakeup.send(b"x")
            except OSError:
                pass

    def wait_stopped(self, *, timeout: float = 5.0) -> bool:
        return self._stopped.wait(timeout)

    def _setup(self) -> None:
        configuration_backend: Optional[
            AuthoritativeConfigurationBackend
        ] = None
        scp_backend = None
        # Bounded startup hygiene for orphaned askpass sockets.
        try:
            sweep_runtime_directory_on_startup(self.socket_path.parent)
        except Exception as exc:
            logger.debug("startup runtime sweep skipped: %s", type(exc).__name__)
        self._started_at_wall = datetime.now(timezone.utc)
        self._lifecycle = DaemonLifecycleController(
            server_instance_id=self._instance_id,
            started_at=self._started_at_wall,
            daemon_version=sshpilot_version,
            development_revision=os.environ.get("SSHPILOT_DEV_REVISION", "").strip(),
            idle_shutdown_seconds=self._idle_shutdown_seconds,
            service_mode=self._service_mode,
            packaged=self._packaged,
            drain_timeout_seconds=self._drain_timeout_seconds,
            on_shutdown=self._on_lifecycle_shutdown_request,
            on_state_changed=self._on_lifecycle_state_changed,
        )
        self._lifecycle.set_resource_provider(self.collect_resource_counts)
        prepare_socket_path(self.socket_path)
        selector = selectors.DefaultSelector()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        gate_terminal_evidence = False
        self._readiness_manager: Optional[Any] = None
        try:
            old_umask = os.umask(0o177)
            try:
                try:
                    listener.bind(str(self.socket_path))
                except OSError as exc:
                    if exc.errno == errno.EADDRINUSE:
                        raise DaemonAlreadyRunningError(
                            "The daemon socket is already in use"
                        ) from None
                    raise
            finally:
                os.umask(old_umask)
            initial = self.socket_path.lstat()
            if (
                stat.S_ISSOCK(initial.st_mode)
                and not stat.S_ISLNK(initial.st_mode)
                and (
                    not hasattr(os, "getuid")
                    or initial.st_uid == os.getuid()
                )
            ):
                self._socket_identity = (initial.st_dev, initial.st_ino)
            else:
                raise SocketSecurityError("The daemon path changed during bind")
            ensure_secure_socket_directory(self.socket_path)
            os.chmod(self.socket_path, 0o600, follow_symlinks=False)
            verified_identity = verify_bound_socket(self.socket_path)
            if verified_identity != self._socket_identity:
                raise SocketSecurityError("The daemon socket changed during bind")
            listener.listen()
            listener.setblocking(False)
            # Own the single-instance socket before loading/migrating the core.
            # Readiness remains false until construction and migration finish.
            core = self._core_factory()
            if isinstance(core, CoreServices):
                self._connection_service = core.connections
                configuration_backend = core.configuration_backend
                self._known_hosts_service = core.known_hosts
                self._key_service = core.keys
                self._ssh_overrides_service = core.ssh_overrides
                self._secrets_service = core.secrets
                self._identity_service = core.identity
                self._operation_runtime = core.operations
                scp_backend = core.scp_backend
                plugin_settings = core.plugin_settings
            else:
                self._connection_service = core
                plugin_settings = None
            enable_workers = getattr(
                self._connection_service,
                "enable_serialized_command_threads",
                None,
            )
            if callable(enable_workers):
                enable_workers()
            if self._session_runtime_factory is None:
                launch_builder = getattr(
                    self._connection_service,
                    "prepare_daemon_terminal_launch",
                    None,
                )
                if callable(launch_builder):
                    from .pty_runner import PtySessionProcessRunner

                    runner = PtySessionProcessRunner(
                        lambda spec: self._prepare_session_launch(
                            spec,
                            launch_builder,
                        )
                    )
                    self._readiness_manager = self._build_readiness_manager()
                    self._session_runtime = SessionRuntime(
                        self._connection_service,
                        runner=runner,
                        readiness_manager=self._readiness_manager,
                    )
                    gate_terminal_evidence = True
                else:
                    self._session_runtime = SessionRuntime(self._connection_service)
            else:
                self._readiness_manager = self._build_readiness_manager()
                self._session_runtime = self._session_runtime_factory(
                    self._connection_service
                )
                if (
                    self._readiness_manager is not None
                    and isinstance(self._session_runtime, SessionRuntime)
                ):
                    self._session_runtime._readiness_manager = (
                        self._readiness_manager
                    )
            self._interaction_broker = (
                self._interaction_broker_factory(self._session_runtime)
                if self._interaction_broker_factory is not None
                else InteractionBroker(
                    client_is_eligible=self._client_can_interact,
                    password_lookup=getattr(
                        self._connection_service,
                        "lookup_daemon_password",
                        None,
                    ),
                    password_store=getattr(
                        self._connection_service,
                        "store_daemon_password",
                        None,
                    ),
                    passphrase_lookup=getattr(
                        self._connection_service,
                        "lookup_daemon_passphrase",
                        None,
                    ),
                    passphrase_store=getattr(
                        self._connection_service,
                        "store_daemon_passphrase",
                        None,
                    ),
                )
            )
            sftp_builder = getattr(
                self._connection_service, "prepare_daemon_sftp_launch", None
            )
            if self._operation_runtime is None:
                from .operation_runtime import OperationRuntime

                self._operation_runtime = OperationRuntime()
            if callable(sftp_builder):
                sftp_runner = SubprocessSftpProcessRunner(
                    lambda spec: self._prepare_sftp_launch(spec, sftp_builder)
                )
                privileged_runner = self._build_privileged_file_runner()
                self._sftp_runtime = SftpServiceRuntime(
                    self._connection_service,
                    runner=sftp_runner,
                    privileged_file_runner=privileged_runner,
                    operation_lifecycle=self._operation_runtime,
                )
            else:
                self._sftp_runtime = SftpServiceRuntime(
                    self._connection_service,
                    operation_lifecycle=self._operation_runtime,
                )
            forward_builder = getattr(
                self._connection_service, "prepare_daemon_forward_launch", None
            )
            if callable(forward_builder):
                forward_runner = SubprocessForwardProcessRunner(
                    lambda spec: self._prepare_forward_launch(spec, forward_builder)
                )
                self._forward_runtime = ForwardRuntime(
                    self._connection_service, runner=forward_runner
                )
            else:
                self._forward_runtime = ForwardRuntime(self._connection_service)
            if scp_backend is None:
                scp_provider = getattr(
                    getattr(self._connection_service, "_launch_provider", None),
                    "prepare_scp_launch",
                    None,
                )
                scp_target = getattr(
                    getattr(self._connection_service, "_launch_provider", None),
                    "scp_target",
                    None,
                )
                if callable(scp_provider) and callable(scp_target):
                    from .native_scp_backend import NativeScpBackend

                    scp_backend = NativeScpBackend(
                        self._connection_service,
                        self._interaction_broker,
                    )
            self._transfer_runtime = TransferRuntime(
                self._sftp_runtime,
                scp_backend=scp_backend,
            )
            if self._secrets_service is not None and hasattr(
                self._secrets_service, "attach_interaction_broker"
            ):
                self._secrets_service.attach_interaction_broker(self._interaction_broker)
            if self._identity_service is not None and hasattr(
                self._identity_service, "attach_interaction_broker"
            ):
                self._identity_service.attach_interaction_broker(
                    self._interaction_broker
                )
            if self._key_service is not None and hasattr(
                self._key_service, "attach_interaction_broker"
            ):
                self._key_service.attach_interaction_broker(
                    self._interaction_broker
                )
            if gate_terminal_evidence:
                runtime = self._session_runtime
                broker = self._interaction_broker
                runtime._auth_failure_classifier = broker.classify_startup_failure
                runtime.enable_connection_evidence_gate()
                runtime.set_authenticated_callback(broker.mark_authenticated)
            from .broadcast_service import BroadcastCommandService
            launch_provider = getattr(self._connection_service, "_launch_provider", None)
            self._broadcast_service = None
            if self._operation_runtime is not None and callable(
                getattr(launch_provider, "prepare_remote_command_launch", None)
            ):
                self._broadcast_service = BroadcastCommandService(
                    self._operation_runtime,
                    launch_provider,
                    interaction_broker=self._interaction_broker,
                    output_publisher=self._publish_broadcast_output,
                )
            self._dispatcher = RequestDispatcher(
                self._connection_service,
                self._session_runtime,
                self._interaction_broker,
                sftp_runtime=self._sftp_runtime,
                transfer_runtime=self._transfer_runtime,
                forward_runtime=self._forward_runtime,
                known_hosts_service=self._known_hosts_service,
                key_service=self._key_service,
                ssh_overrides_service=self._ssh_overrides_service,
                secrets_service=self._secrets_service,
                identity_service=self._identity_service,
                operation_runtime=self._operation_runtime,
                broadcast_service=self._broadcast_service,
                plugin_settings=plugin_settings,
                command_input_waiter=self._wait_command_input,
                lifecycle_controller=self._lifecycle,
                diagnostics_provider=self.build_diagnostics,
            )
            self._session_executor = BoundedCommandExecutor(
                max_workers=self.session_command_workers,
                max_commands=self.session_command_queue_limit,
            )
            wakeup_read, wakeup_write = socket.socketpair()
            wakeup_read.setblocking(False)
            wakeup_write.setblocking(False)
            selector.register(listener, selectors.EVENT_READ, "listener")
            selector.register(wakeup_read, selectors.EVENT_READ, "wakeup")
        except Exception:
            listener.close()
            selector.close()
            raise
        self._selector = selector
        self._listener = listener
        self._wakeup_read = wakeup_read
        self._wakeup_write = wakeup_write
        subscribe_events = getattr(self._connection_service, "subscribe_events", None)
        if callable(subscribe_events):
            self._core_subscription = subscribe_events(self._on_core_event)
        self._session_subscription = self._session_runtime.subscribe_events(
            self._on_core_event
        )
        self._interaction_subscription = self._interaction_broker.subscribe_events(
            self._on_core_event
        )
        self._terminal_subscription = self._session_runtime.subscribe_terminal(
            self._on_terminal_output
        )
        self._sftp_subscription = self._sftp_runtime.subscribe_events(
            self._on_core_event
        )
        self._transfer_subscription = self._transfer_runtime.subscribe_events(
            self._on_core_event
        )
        self._forward_subscription = self._forward_runtime.subscribe_events(
            self._on_core_event
        )
        self._operation_subscription = self._operation_runtime.subscribe_events(
            self._on_core_event
        )
        self._broadcast_subscription = self._broadcast_publisher.subscribe(
            self._on_core_event
        )
        # Defer `_accepting_core_events` until after `mark_ready()` so the
        # initial READY lifecycle publication is not sequenced onto the
        # shared event bus (clients still poll `daemon.status`).
        if configuration_backend is not None:
            watcher = (
                self._configuration_watcher_factory()
                if self._configuration_watcher_factory is not None
                else None
            )
            self._configuration_reload = ConfigurationReloadCoordinator(
                configuration_backend,
                self._session_executor,
                watcher=watcher,
                debounce=self.configuration_reload_debounce,
                poll_interval=self.configuration_poll_interval,
            )
            self._configuration_reload.start()

    def _build_readiness_manager(self) -> Optional[Any]:
        """Construct the OpenSSH diagnostics readiness owner for this instance.

        ``None`` when the daemon lacks a private runtime directory (the
        session runtime then relies on the PTY connection-evidence path).
        """
        from .ssh_readiness import SshReadinessManager

        try:
            return SshReadinessManager(
                instance_id=self._instance_id,
                grace_seconds=self.ssh_readiness_grace_seconds,
            )
        except (OSError, ValueError, RuntimeError):
            logger.debug("ssh readiness is unavailable", exc_info=True)
            return None

    def _prepare_session_launch(
        self,
        spec: Any,
        launch_builder: Callable[..., tuple],
    ) -> tuple:
        broker = self._interaction_broker
        if broker is None:
            raise SshPilotError(
                ErrorCode.ASKPASS_HELPER_UNAVAILABLE,
                "Typed SSH interactions are unavailable",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        argv, environment = broker.prepare_launch(spec, launch_builder)
        readiness = self._readiness_manager
        if readiness is not None:
            diagnostics_path = readiness.prepare_launch(
                spec.session_id, argv, environment
            )
            if diagnostics_path is not None:
                from .ssh_readiness import insert_ssh_diagnostics_options

                argv = insert_ssh_diagnostics_options(argv, diagnostics_path)
        return argv, environment

    def _prepare_sftp_launch(
        self,
        spec: Any,
        launch_builder: Callable[..., tuple],
    ) -> tuple:
        broker = self._interaction_broker
        if broker is None:
            raise SshPilotError(
                ErrorCode.ASKPASS_HELPER_UNAVAILABLE,
                "Typed SSH interactions are unavailable",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        return broker.prepare_launch(
            spec, launch_builder, trailing_args=("sftp",), headless=True
        )

    def _build_privileged_file_runner(self) -> Optional[Any]:
        """Wire the daemon-owned sudo file runner when the launch provider is set.

        Returns None when the daemon cannot prepare privileged launches; the
        SFTP runtime then rejects ``access=SUDO`` reads/replacements with a
        stable ``UNSUPPORTED_CAPABILITY`` error instead of falling back to a
        frontend-owned SSH path.
        """
        launch_provider = getattr(self._connection_service, "_launch_provider", None)
        broker = self._interaction_broker
        if (
            launch_provider is None
            or not callable(
                getattr(launch_provider, "prepare_remote_command_launch", None)
            )
            or broker is None
        ):
            return None
        from .privileged_file_service import PrivilegedFileService

        return PrivilegedFileService(launch_provider, broker)


    def _prepare_forward_launch(
        self,
        spec: Any,
        launch_builder: Callable[..., tuple],
    ) -> tuple:
        broker = self._interaction_broker
        if broker is None:
            raise SshPilotError(
                ErrorCode.ASKPASS_HELPER_UNAVAILABLE,
                "Typed SSH interactions are unavailable",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        forward_runtime = self._forward_runtime
        if forward_runtime is None:
            raise SshPilotError(
                ErrorCode.ASKPASS_HELPER_UNAVAILABLE,
                "Port forwarding is unavailable",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        summary = forward_runtime.get_forward(ForwardId(str(spec.session_id)))

        def _wrapped_builder(connection_id: Any, *, interaction_policy: str = "broker") -> tuple:
            return launch_builder(
                connection_id,
                forward_type=summary.type.value,
                bind_host=summary.bind_host,
                bind_port=summary.bind_port,
                destination_host=summary.destination_host,
                destination_port=summary.destination_port,
                interaction_policy=interaction_policy,
            )

        return broker.prepare_launch(spec, _wrapped_builder, headless=True)

    def _client_can_interact(self, session_id: SessionId, client_id: Any) -> bool:
        broker = self._interaction_broker
        if broker is not None and broker.client_owns_direct_scope(
            session_id,
            client_id,
        ):
            return True
        session_runtime = self._session_runtime
        if session_runtime is not None and session_runtime.client_can_interact(
            session_id, client_id
        ):
            return True
        text = str(session_id)
        if text.startswith("sftp-") and self._sftp_runtime is not None:
            return self._sftp_runtime.client_can_interact(
                SftpServiceId(text), client_id
            )
        if text.startswith("forward-") and self._forward_runtime is not None:
            return self._forward_runtime.client_can_interact(
                ForwardId(text), client_id
            )
        if text.startswith("operation-") and self._operation_runtime is not None:
            # Long-running operations (public-key deployment, authorized-key
            # removal) scope their interactions to the public OperationId; the
            # owning client must be able to see and answer them.
            return self._operation_runtime.client_can_interact(
                OperationId(text), client_id
            )
        if text.startswith("transfer-") and self._transfer_runtime is not None:
            # Native SCP transfers scope their interactions to the public
            # TransferId; the owning client must be able to see and answer them.
            return self._transfer_runtime.client_can_interact(
                TransferId(text), client_id
            )
        if text.startswith("key-operation-") and self._key_service is not None:
            return self._key_service.client_can_interact(session_id, client_id)
        return False

    def _accept_client(self) -> None:
        listener = self._listener
        selector = self._selector
        if listener is None or selector is None or self._stopping.is_set():
            return
        try:
            client_socket, _address = listener.accept()
        except BlockingIOError:
            return
        client_socket.setblocking(False)
        peer_token = self._next_peer_token
        self._next_peer_token += 1
        state = _ClientConnection(client_socket, token=peer_token)
        with self._event_lock:
            self._clients[client_socket.fileno()] = state
            self._clients_by_token[peer_token] = state
        selector.register(client_socket, selectors.EVENT_READ, state)
        self._note_lifecycle_activity()

    def _read_client(self, state: _ClientConnection) -> None:
        try:
            chunk = state.sock.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self._close_client(state)
            return
        if not chunk:
            try:
                state.decoder.finish()
            except FramingError:
                logger.debug("Client closed during a partial daemon frame")
            self._close_client(state)
            return
        try:
            messages = state.decoder.feed(chunk)
        except FramingError as exc:
            self._queue_protocol_error(state, exc.code, exc.message)
            return
        for message in messages:
            if isinstance(message, TerminalFrame):
                self._handle_terminal_frame(state, message)
            elif isinstance(message, SecretFrame):
                self._handle_secret_frame(state, message)
            else:
                if not isinstance(message, dict):
                    self._queue_protocol_error(
                        state,
                        ErrorCode.PROTOCOL_ERROR,
                        "The transport frame type is invalid",
                    )
                    continue
                self._handle_message(state, message)

    def _handle_secret_frame(
        self,
        state: _ClientConnection,
        frame: SecretFrame,
    ) -> None:
        try:
            if frame.kind is SecretFrameKind.COMMAND_INPUT:
                protocol = state.protocol
                if (
                    not protocol.handshake_completed
                    or protocol.client_info is None
                    or "binary-secret-v1" not in protocol.client_info.supported_frame_types
                ):
                    raise SshPilotError(
                        ErrorCode.UNSUPPORTED_CAPABILITY,
                        "Binary secret transport was not negotiated",
                    )
                with self._command_input_condition:
                    self._command_inputs[str(frame.interaction_id)] = bytearray(frame.secret)
                    self._command_input_condition.notify_all()
                frame.clear()
                return
            if frame.kind is not SecretFrameKind.RESPONSE:
                raise SshPilotError(
                    ErrorCode.PROTOCOL_ERROR,
                    "The daemon accepts client secret response frames only",
                )
            protocol = state.protocol
            if (
                not protocol.handshake_completed
                or protocol.client_id is None
                or protocol.client_info is None
                or "binary-secret-v1"
                not in protocol.client_info.supported_frame_types
            ):
                raise SshPilotError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    "Binary secret transport was not negotiated",
                )
            broker = self._interaction_broker
            if broker is None:
                raise SshPilotError(
                    ErrorCode.UNSUPPORTED_CAPABILITY,
                    "Typed interactions are unavailable",
                )
            broker.submit_secret(frame, protocol.client_id)
        except SshPilotError as error:
            frame.clear()
            self._queue_protocol_error(state, error.code, error.message)
        except Exception:
            frame.clear()
            self._queue_protocol_error(
                state,
                ErrorCode.INTERNAL_ERROR,
                "The daemon rejected the secret response",
            )

    def _wait_command_input(self, request_id, timeout: float = 30.0):
        deadline = time.monotonic() + timeout
        with self._command_input_condition:
            while str(request_id) not in self._command_inputs and not self._stopping.is_set():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._command_input_condition.wait(remaining)
            return self._command_inputs.pop(str(request_id), None)

    def _handle_terminal_frame(
        self,
        state: _ClientConnection,
        frame: TerminalFrame,
    ) -> None:
        runtime = self._session_runtime
        client_id = state.protocol.client_id
        if (
            runtime is None
            or client_id is None
            or not state.protocol.handshake_completed
            or state.protocol.client_info is None
            or "binary-terminal-v1"
            not in state.protocol.client_info.supported_frame_types
            or frame.kind is not TerminalFrameKind.INPUT
        ):
            self._queue_protocol_error(
                state,
                ErrorCode.PROTOCOL_ERROR,
                "The binary terminal frame is not permitted",
            )
            return
        try:
            attachment_id = frame.attachment_id
            if attachment_id is None:
                raise ValueError("terminal input attachment is missing")
            runtime.send_terminal_input(
                TerminalInput(
                    session_id=frame.session_id,
                    attachment_id=attachment_id,
                    data=frame.data,
                ),
                client_id=client_id,
            )
        except SshPilotError as error:
            # Binary input is fire-and-report. Return only the stable code and
            # safe identifiers; never echo terminal input bytes.
            self._queue_terminal_frame(
                state,
                TerminalFrame(
                    kind=TerminalFrameKind.INPUT_ERROR,
                    session_id=frame.session_id,
                    attachment_id=frame.attachment_id,
                    sequence=frame.sequence,
                    data=error.code.value.encode("ascii"),
                ),
            )

    def _handle_message(self, state: _ClientConnection, message: dict) -> None:
        request_id = message.get("request_id")
        if type(request_id) is not str or not request_id.strip():
            request_id = RequestId("protocol")
        else:
            request_id = RequestId(request_id)
        try:
            envelope = decode_envelope(message)
            if not isinstance(envelope, RequestEnvelope):
                raise ValueError("clients may send request envelopes only")
            dispatcher = self._dispatcher
            if dispatcher is None:
                raise SshPilotError(
                    ErrorCode.DAEMON_SHUTTING_DOWN,
                    "The daemon is shutting down",
                    retryable=True,
                    request_id=envelope.request_id,
                )
            with log_context(
                request=envelope.request_id,
                client=state.protocol.client_id or envelope.client_id,
                method=envelope.method,
            ):
                logger.debug("daemon request dispatch")
                request_context = copy_log_context()
                result = dispatcher.dispatch(envelope, state.protocol)
            if isinstance(result, DeferredResult):
                self._submit_deferred(state, envelope, result, context=request_context)
                return
            if not isinstance(result, ImmediateResult):
                raise RuntimeError("dispatcher returned an unknown result type")
            response = SuccessResponseEnvelope(
                protocol_version=(
                    state.protocol.selected_protocol_version or PROTOCOL_VERSION
                ),
                request_id=envelope.request_id,
                result=result.value,
            )
        except SshPilotError as exc:
            response = self._error_response(request_id, exc)
        except (TypeError, ValueError):
            response = self._error_response(
                request_id,
                SshPilotError(
                    ErrorCode.PROTOCOL_ERROR,
                    "The transport envelope is malformed",
                    request_id=request_id,
                ),
            )
        self._queue_response(state, response)
        if (
            "result" in locals()
            and isinstance(result, ImmediateResult)
            and result.terminal_replay is not None
        ):
            self._queue_replay(
                state,
                result.terminal_session_id,
                result.terminal_replay,
            )

    def _submit_deferred(
        self,
        state: _ClientConnection,
        request: RequestEnvelope,
        result: DeferredResult,
        *,
        context=None,
    ) -> None:
        executor = self._session_executor
        protocol_version = (
            state.protocol.selected_protocol_version or PROTOCOL_VERSION
        )
        peer_token = state.token
        if executor is None:
            result.on_rejected()
            self._queue_response(
                state,
                self._error_response(
                    request.request_id,
                    SshPilotError(
                        ErrorCode.DAEMON_SHUTTING_DOWN,
                        "The daemon is shutting down",
                        retryable=True,
                        request_id=request.request_id,
                        connection_id=result.connection_id,
                        session_id=result.session_id,
                    ),
                ),
            )
            return

        respond_on_accept = result.respond_on_accept

        def _complete(value: object) -> None:
            if respond_on_accept:
                return
            self._enqueue_completion(
                _DeferredCompletion(
                    peer_token=peer_token,
                    protocol_version=protocol_version,
                    request_id=request.request_id,
                    result=value,
                )
            )

        def _error(error: BaseException) -> None:
            if respond_on_accept:
                callback = result.on_background_error
                if callback is not None:
                    try:
                        callback(error)
                    except Exception as callback_error:
                        logger.error(
                            "Background deferred error handler failed type=%s",
                            type(callback_error).__name__,
                        )
                return
            if isinstance(error, SshPilotError):
                safe_error = SshPilotError(
                    error.code,
                    error.message,
                    details=error.details,
                    retryable=error.retryable,
                    request_id=request.request_id,
                    connection_id=error.connection_id or result.connection_id,
                    session_id=error.session_id or result.session_id,
                )
            else:
                logger.error(
                    "Deferred daemon method %s failed type=%s",
                    request.method,
                    type(error).__name__,
                )
                safe_error = SshPilotError(
                    ErrorCode.INTERNAL_ERROR,
                    "The daemon could not complete the deferred request",
                    retryable=True,
                    request_id=request.request_id,
                    connection_id=result.connection_id,
                    session_id=result.session_id,
                )
            self._enqueue_completion(
                _DeferredCompletion(
                    peer_token=peer_token,
                    protocol_version=protocol_version,
                    request_id=request.request_id,
                    error=safe_error,
                )
            )

        def _cancel() -> None:
            callback = result.on_cancel or result.on_rejected
            try:
                callback()
            except Exception as callback_error:
                logger.error(
                    "Deferred command cancellation failed type=%s",
                    type(callback_error).__name__,
                )

        request_context = context or copy_log_context()

        def _operation():
            return request_context.run(result.operation)

        if respond_on_accept:
            accepted = executor.submit_background(
                key=result.command_key,
                operation=_operation,
                on_error=_error,
                on_cancel=_cancel,
            )
        else:
            command = DeferredCommand(
                key=result.command_key,
                operation=_operation,
                on_complete=_complete,
                on_error=_error,
                on_cancel=lambda: None,
            )
            accepted = executor.submit(command)
            if accepted:
                state.pending_request_ids.add(request.request_id)

        if accepted:
            logger.debug(
                "Deferred daemon command accepted method=%s respond_on_accept=%s",
                request.method,
                respond_on_accept,
            )
            if respond_on_accept:
                self._queue_response(
                    state,
                    SuccessResponseEnvelope(
                        protocol_version=protocol_version,
                        request_id=request.request_id,
                        result=result.accepted_result,
                    ),
                )
            return

        result.on_rejected()
        self._queue_response(
            state,
            self._error_response(
                request.request_id,
                SshPilotError(
                    ErrorCode.SERVER_BUSY,
                    "The daemon command queue is full",
                    retryable=True,
                    request_id=request.request_id,
                    connection_id=result.connection_id,
                    session_id=result.session_id,
                ),
            ),
        )

    def _enqueue_completion(self, completion: _DeferredCompletion) -> None:
        while not self._stopping.is_set():
            try:
                self._completion_queue.put(completion, timeout=0.05)
            except queue.Full:
                self._wake_selector()
                continue
            self._wake_selector()
            return

    def _queue_protocol_error(
        self,
        state: _ClientConnection,
        code: ErrorCode,
        message: str,
    ) -> None:
        self._queue_response(
            state,
            self._error_response(
                RequestId("protocol"),
                SshPilotError(code, message, request_id=RequestId("protocol")),
            ),
        )
        state.close_after_write = True

    @staticmethod
    def _error_response(
        request_id: RequestId,
        error: SshPilotError,
    ) -> ErrorResponseEnvelope:
        safe_error = error
        if error.request_id != request_id:
            safe_error = SshPilotError(
                error.code,
                error.message,
                details=error.details,
                retryable=error.retryable,
                request_id=request_id,
                connection_id=error.connection_id,
                session_id=error.session_id,
            )
        return ErrorResponseEnvelope(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            error=error_to_wire(safe_error),
        )

    def _queue_secret_response(
        self,
        state: _ClientConnection,
        request_id: RequestId,
        secret: Optional[bytearray],
    ) -> None:
        if secret is None:
            return
        frame = None
        try:
            frame = encode_binary_frame(
                encode_secret_payload(
                    SecretFrame(
                        kind=SecretFrameKind.REVEAL_RESPONSE,
                        interaction_id=InteractionId(str(request_id)),
                        nonce=secrets.token_bytes(16),
                        secret=secret,
                    )
                )
            )
            outbound = _OutboundFrame(frame)
            with self._event_lock:
                if state.closed or state.continuity_lost:
                    return
                overflow = not self._enqueue_frame_locked(
                    state, outbound, priority=True
                )
            if overflow:
                self._close_client(state)
                return
            self._refresh_client_interest(state)
        except (FramingError, TypeError, ValueError):
            self._close_client(state)
        finally:
            secret[:] = b"\0" * len(secret)
            secret.clear()

    def _queue_response(
        self,
        state: _ClientConnection,
        response: Union[SuccessResponseEnvelope, ErrorResponseEnvelope],
    ) -> None:
        try:
            frame = _OutboundFrame(
                encode_frame(encode_envelope(response)),
                is_event=False,
            )
        except FramingError:
            logger.exception("Daemon generated an invalid response frame")
            self._close_client(state)
            return
        with self._event_lock:
            if state.closed or state.continuity_lost:
                return
            dropped_terminal = self._make_control_room_locked(
                state,
                len(frame.data),
            )
            overflow = not self._enqueue_frame_locked(state, frame, priority=True)
        if overflow:
            logger.warning("Disconnecting daemon peer after outbound queue overflow")
            self._close_client(state)
            return
        for session_id, sequence in dropped_terminal.items():
            self._queue_terminal_status(state, session_id, sequence)
        selector = self._selector
        if selector is not None and state.sock.fileno() >= 0:
            self._refresh_client_interest(state)

    def _write_client(self, state: _ClientConnection) -> None:
        with self._event_lock:
            if state.closed or state.continuity_lost:
                return
            if not state.output:
                frame = None
                offset = 0
            else:
                frame = state.output[0]
                offset = state.output_offset
        if frame is None:
            self._finish_write(state)
            return
        try:
            sent = state.sock.send(frame.data[offset:])
        except BlockingIOError:
            return
        except OSError:
            self._close_client(state)
            return
        with self._event_lock:
            if (
                state.closed
                or state.continuity_lost
                or not state.output
                or state.output[0] is not frame
            ):
                return
            state.output_offset += sent
            state.queued_outbound_bytes = max(
                0,
                state.queued_outbound_bytes - sent,
            )
            if frame.is_terminal:
                state.queued_terminal_bytes = max(
                    0,
                    state.queued_terminal_bytes - sent,
                )
            if state.output_offset >= len(frame.data):
                completed = state.output.popleft()
                state.output_offset = 0
                if completed.is_event:
                    state.queued_event_count -= 1
            empty = not state.output
        if empty:
            self._finish_write(state)

    def _finish_write(self, state: _ClientConnection) -> None:
        if state.close_after_write:
            self._close_client(state)
            return
        self._refresh_client_interest(state)

    def _drain_wakeup(self) -> None:
        wakeup = self._wakeup_read
        if wakeup is not None:
            try:
                while wakeup.recv(1024):
                    pass
            except BlockingIOError:
                pass
            except OSError:
                pass
        self._drain_completions()
        self._drain_terminal_output()
        with self._event_lock:
            states = tuple(self._clients.values())
        for state in states:
            if state.continuity_lost:
                logger.warning("Disconnecting daemon peer after event queue overflow")
                self._close_client(state)
            else:
                self._refresh_client_interest(state)

    def _drain_completions(self) -> None:
        while True:
            try:
                completion = self._completion_queue.get_nowait()
            except queue.Empty:
                return
            state = self._clients_by_token.get(completion.peer_token)
            if (
                state is None
                or state.closed
                or completion.request_id not in state.pending_request_ids
            ):
                logger.debug("Discarded deferred completion for a closed peer")
                continue
            state.pending_request_ids.discard(completion.request_id)
            secret_result = (
                completion.result
                if isinstance(completion.result, SecretResponseResult)
                else None
            )
            secret_value = (
                secret_result.secret
                if secret_result is not None and secret_result.secret
                else None
            )
            if completion.error is not None:
                response = self._error_response(
                    completion.request_id,
                    completion.error,
                )
            else:
                response = SuccessResponseEnvelope(
                    protocol_version=completion.protocol_version,
                    request_id=completion.request_id,
                    result=bool(secret_value) if secret_result is not None else completion.result,
                )
            self._queue_response(state, response)
            if secret_result is not None:
                self._queue_secret_response(
                    state,
                    completion.request_id,
                    secret_value,
                )

    def _close_client(self, state: _ClientConnection) -> None:
        file_descriptor = state.sock.fileno()
        with self._event_lock:
            if state.closed:
                return
            state.closed = True
            state.output.clear()
            state.output_offset = 0
            state.queued_event_count = 0
            state.queued_outbound_bytes = 0
            state.queued_terminal_bytes = 0
            state.pending_request_ids.clear()
            self._clients.pop(file_descriptor, None)
            self._clients_by_token.pop(state.token, None)
        selector = self._selector
        if selector is not None and file_descriptor >= 0:
            try:
                selector.unregister(state.sock)
            except (KeyError, ValueError):
                pass
        try:
            state.sock.close()
        except OSError:
            pass
        session_runtime = self._session_runtime
        if session_runtime is not None:
            session_runtime.detach_client(state.protocol.client_id)
        sftp_runtime = self._sftp_runtime
        if sftp_runtime is not None:
            sftp_runtime.detach_client(state.protocol.client_id)
        forward_runtime = self._forward_runtime
        if forward_runtime is not None:
            forward_runtime.detach_client(state.protocol.client_id)
        transfer_runtime = self._transfer_runtime
        if transfer_runtime is not None:
            transfer_runtime.detach_client(state.protocol.client_id)
        broker = self._interaction_broker
        key_service = self._key_service
        if key_service is not None and state.protocol.client_id is not None:
            key_service.disconnect_client(state.protocol.client_id)
        if broker is not None and state.protocol.client_id is not None:
            broker.disconnect_client(state.protocol.client_id)
        self._note_lifecycle_activity()

    def _on_terminal_output(self, output: TerminalOutput) -> None:
        if self._stopping.is_set():
            return
        try:
            self._terminal_queue.put_nowait(output)
        except queue.Full:
            logger.warning(
                "Terminal publication queue overflow session=%s",
                output.session_id,
            )
            with self._event_lock:
                self._terminal_publication_lost.add(output.session_id)
        self._wake_selector()

    def _drain_terminal_output(self) -> None:
        runtime = self._session_runtime
        if runtime is None:
            return
        with self._event_lock:
            lost_sessions = tuple(self._terminal_publication_lost)
            self._terminal_publication_lost.clear()
            states = tuple(self._clients.values())
        for session_id in lost_sessions:
            for state in states:
                client_id = state.protocol.client_id
                if (
                    client_id is not None
                    and runtime.receives_terminal(session_id, client_id)
                ):
                    self._queue_terminal_status(state, session_id, 0)
        while True:
            try:
                output = self._terminal_queue.get_nowait()
            except queue.Empty:
                return
            flags = TerminalFrameFlags.EOF if output.eof else TerminalFrameFlags.NONE
            frame = TerminalFrame(
                kind=TerminalFrameKind.OUTPUT,
                session_id=output.session_id,
                sequence=output.sequence,
                data=output.data,
                flags=flags,
            )
            with self._event_lock:
                states = tuple(self._clients.values())
            for state in states:
                client_id = state.protocol.client_id
                if (
                    client_id is None
                    or state.closed
                    or not state.protocol.handshake_completed
                    or state.protocol.client_info is None
                    or "binary-terminal-v1"
                    not in state.protocol.client_info.supported_frame_types
                    or not runtime.receives_terminal(output.session_id, client_id)
                ):
                    continue
                self._queue_terminal_frame(state, frame)

    def _queue_replay(
        self,
        state: _ClientConnection,
        session_id: Optional[SessionId],
        replay,
    ) -> None:
        if session_id is None:
            return
        with self._event_lock:
            if state.closed or state.continuity_lost:
                return
            state.terminal_continuity_lost.discard(session_id)
        flags = TerminalFrameFlags.REPLAY
        if replay.truncated:
            flags |= TerminalFrameFlags.TRUNCATED
        maximum = 64 * 1024
        for sequence, data in replay.chunks:
            for offset in range(0, len(data), maximum):
                chunk = data[offset : offset + maximum]
                self._queue_terminal_frame(
                    state,
                    TerminalFrame(
                        kind=TerminalFrameKind.OUTPUT,
                        session_id=session_id,
                        sequence=sequence + offset,
                        data=chunk,
                        flags=flags,
                    ),
                )
        if replay.eof:
            self._queue_terminal_frame(
                state,
                TerminalFrame(
                    kind=TerminalFrameKind.OUTPUT,
                    session_id=session_id,
                    sequence=replay.returned_end,
                    flags=TerminalFrameFlags.REPLAY | TerminalFrameFlags.EOF,
                ),
            )

    def _queue_terminal_frame(
        self,
        state: _ClientConnection,
        terminal_frame: TerminalFrame,
    ) -> None:
        try:
            encoded = encode_binary_frame(
                encode_terminal_payload(terminal_frame)
            )
        except (FramingError, TypeError, ValueError):
            self._close_client(state)
            return
        outbound = _OutboundFrame(
            encoded,
            is_terminal=True,
            session_id=terminal_frame.session_id,
            terminal_sequence=terminal_frame.sequence,
        )
        with self._event_lock:
            if state.closed or state.continuity_lost:
                return
            if (
                terminal_frame.session_id in state.terminal_continuity_lost
                and not terminal_frame.flags & TerminalFrameFlags.REPLAY
            ):
                return
            if (
                state.queued_terminal_bytes + len(encoded)
                > self.max_client_terminal_bytes
                or state.queued_outbound_bytes + len(encoded)
                > self.max_client_outbound_bytes
            ):
                self._drop_terminal_session_locked(
                    state,
                    terminal_frame.session_id,
                )
                overflow = True
            else:
                overflow = not self._enqueue_frame_locked(state, outbound)
        if overflow:
            self._queue_terminal_status(
                state,
                terminal_frame.session_id,
                terminal_frame.sequence,
            )
        self._refresh_client_interest(state)

    def _queue_terminal_status(
        self,
        state: _ClientConnection,
        session_id: SessionId,
        sequence: int,
    ) -> None:
        try:
            encoded = encode_binary_frame(
                encode_terminal_payload(
                    TerminalFrame(
                        kind=TerminalFrameKind.CONTINUITY_LOST,
                        session_id=session_id,
                        sequence=sequence,
                    )
                )
            )
        except (FramingError, TypeError, ValueError):
            return
        with self._event_lock:
            if state.closed or state.continuity_lost:
                return
            state.terminal_continuity_lost.add(session_id)
            if not self._enqueue_frame_locked(
                state,
                _OutboundFrame(encoded, session_id=session_id),
                priority=True,
            ):
                state.continuity_lost = True
        self._wake_selector()

    @staticmethod
    def _drop_terminal_session_locked(
        state: _ClientConnection,
        session_id: SessionId,
    ) -> Optional[int]:
        kept: Deque[_OutboundFrame] = deque()
        terminal_removed = 0
        outbound_removed = 0
        first_dropped_sequence: Optional[int] = None
        for index, frame in enumerate(state.output):
            if (
                frame.is_terminal
                and frame.session_id == session_id
                and not (index == 0 and state.output_offset)
            ):
                terminal_removed += len(frame.data)
                outbound_removed += len(frame.data)
                if (
                    first_dropped_sequence is None
                    and frame.terminal_sequence is not None
                ):
                    first_dropped_sequence = frame.terminal_sequence
                continue
            kept.append(frame)
        state.output = kept
        state.queued_terminal_bytes = max(
            0,
            state.queued_terminal_bytes - terminal_removed,
        )
        state.queued_outbound_bytes = max(
            0,
            state.queued_outbound_bytes - outbound_removed,
        )
        return first_dropped_sequence

    def _make_control_room_locked(
        self,
        state: _ClientConnection,
        required: int,
    ) -> dict[SessionId, int]:
        dropped: dict[SessionId, int] = {}
        if state.queued_outbound_bytes + required <= self.max_client_outbound_bytes:
            return dropped
        session_ids = tuple(
            dict.fromkeys(
                frame.session_id
                for frame in state.output
                if frame.is_terminal and frame.session_id is not None
            )
        )
        for session_id in session_ids:
            sequence = self._drop_terminal_session_locked(state, session_id)
            if sequence is not None:
                dropped[session_id] = sequence
            if (
                state.queued_outbound_bytes + required
                <= self.max_client_outbound_bytes
            ):
                return dropped
        return dropped

    def _cleanup(self) -> None:
        self._stop_configuration_reload()
        operation_runtime = self._operation_runtime
        self._operation_runtime = None
        if operation_runtime is not None:
            try:
                operation_runtime.shutdown()
            except Exception:
                logger.debug("operation runtime shutdown failed", exc_info=True)
        secrets = self._secrets_service
        if secrets is not None and hasattr(secrets, "shutdown"):
            try:
                secrets.shutdown()
            except Exception:
                logger.debug("secret service shutdown failed", exc_info=True)
        broker = self._interaction_broker
        self._interaction_broker = None
        if broker is not None:
            broker.close(timeout=self.session_shutdown_timeout)
        self._shutdown_session_commands()
        self._stop_core_events()
        with self._event_lock:
            states = tuple(self._clients.values())
        for state in states:
            self._close_client(state)
        selector = self._selector
        for sock in (self._listener, self._wakeup_read, self._wakeup_write):
            if sock is None:
                continue
            if selector is not None:
                try:
                    selector.unregister(sock)
                except (KeyError, ValueError):
                    pass
            sock.close()
        if selector is not None:
            selector.close()
        self._selector = None
        self._listener = None
        self._wakeup_read = None
        self._wakeup_write = None
        connection_service = self._connection_service
        self._connection_service = None
        self._session_runtime = None
        self._sftp_runtime = None
        self._transfer_runtime = None
        self._forward_runtime = None
        try:
            if connection_service is not None:
                connection_service.close()
        except Exception as error:
            logger.error(
                "Daemon core cleanup failed (%s)",
                type(error).__name__,
            )
        finally:
            unlink_owned_socket(self.socket_path, self._socket_identity)
            self._socket_identity = None
            try:
                sweep_stale_askpass_sockets(self.socket_path.parent)
            except Exception:
                logger.debug("shutdown askpass sweep failed", exc_info=True)

    def collect_resource_counts(self) -> DaemonResourceCounts:
        with self._event_lock:
            clients = len(self._clients)
        sessions_active = sessions_retained = 0
        if self._session_runtime is not None:
            for item in self._session_runtime.list_sessions():
                if item.state in {
                    SessionState.CREATED,
                    SessionState.STARTING,
                    SessionState.RUNNING,
                    SessionState.CLOSING,
                }:
                    sessions_active += 1
                else:
                    sessions_retained += 1
        sftp_active = sftp_retained = 0
        if self._sftp_runtime is not None:
            for item in self._sftp_runtime.list_services():
                if item.state in {
                    SftpServiceState.CREATED,
                    SftpServiceState.STARTING,
                    SftpServiceState.READY,
                    SftpServiceState.CLOSING,
                }:
                    sftp_active += 1
                else:
                    sftp_retained += 1
        transfers_queued = transfers_starting = transfers_running = transfers_retained = 0
        if self._transfer_runtime is not None:
            for item in self._transfer_runtime.list_transfers():
                if item.state is TransferState.QUEUED:
                    transfers_queued += 1
                elif item.state is TransferState.STARTING:
                    transfers_starting += 1
                elif item.state in {TransferState.RUNNING, TransferState.CANCELLING}:
                    transfers_running += 1
                else:
                    transfers_retained += 1
        forwards_active = forwards_retained = 0
        if self._forward_runtime is not None:
            for item in self._forward_runtime.list_forwards():
                if item.state in {
                    ForwardState.CREATED,
                    ForwardState.STARTING,
                    ForwardState.ACTIVE,
                    ForwardState.CLOSING,
                }:
                    forwards_active += 1
                else:
                    forwards_retained += 1
        interactions_pending = 0
        if self._interaction_broker is not None:
            # Broker list requires a client id; count via internal records when available.
            records = getattr(self._interaction_broker, "_records", None)
            if isinstance(records, dict):
                for record in records.values():
                    summary = getattr(record, "summary", None)
                    state = getattr(summary, "state", None)
                    if state in {
                        InteractionState.PENDING,
                        InteractionState.CLAIMED,
                    }:
                        interactions_pending += 1
        return DaemonResourceCounts(
            clients=clients,
            sessions_active=sessions_active,
            sessions_retained=sessions_retained,
            sftp_active=sftp_active,
            sftp_retained=sftp_retained,
            transfers_queued=transfers_queued,
            transfers_starting=transfers_starting,
            transfers_running=transfers_running,
            transfers_retained=transfers_retained,
            forwards_active=forwards_active,
            forwards_retained=forwards_retained,
            interactions_pending=interactions_pending,
        )

    def build_diagnostics(self) -> DaemonDiagnostics:
        if self._lifecycle is None:
            raise RuntimeError("lifecycle controller is not ready")
        status = self._lifecycle.status()
        started = self._started_at_wall or status.started_at
        uptime = max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
        executor_depth = 0
        if self._session_executor is not None:
            depth = getattr(self._session_executor, "queued_count", None)
            if callable(depth):
                executor_depth = int(depth())
            else:
                queue_obj = getattr(self._session_executor, "_queue", None)
                if queue_obj is not None and hasattr(queue_obj, "qsize"):
                    try:
                        executor_depth = int(queue_obj.qsize())
                    except NotImplementedError:
                        executor_depth = 0
        roles: Dict[str, int] = {}
        for thread in threading.enumerate():
            name = thread.name or "unnamed"
            role = name.split("-", 1)[0] if name.startswith("sshpilot") else "other"
            roles[role] = roles.get(role, 0) + 1
        open_fds = None
        rss = None
        try:
            open_fds = len(os.listdir(f"/proc/{os.getpid()}/fd"))
        except OSError:
            open_fds = None
        try:
            with open(f"/proc/{os.getpid()}/statm", encoding="utf-8") as handle:
                pages = int(handle.read().split()[1])
            rss = pages * os.sysconf("SC_PAGE_SIZE")
        except (OSError, IndexError, ValueError):
            rss = None
        return DaemonDiagnostics(
            status=status,
            uptime_seconds=uptime,
            executor_queue_depth=executor_depth,
            thread_counts_by_role=roles,
            open_descriptor_count=open_fds,
            rss_bytes=rss,
            socket_bound=self._listener is not None,
            keep_alive_lease=self._lifecycle.keep_alive_lease,
        )

    def _on_lifecycle_shutdown_request(self) -> None:
        """Enter drain mode without tearing sockets down mid-RPC reply."""

        if self._dispatcher is not None:
            self._dispatcher.begin_shutdown()
        self._wake_selector()

    def _on_lifecycle_state_changed(self, status: DaemonStatus) -> None:
        # Skip the initial READY publication so client event sequences still
        # start at zero for the first domain event after handshake.
        if status.state is DaemonLifecycleState.READY and not self._accepting_core_events:
            return
        if status.state in {
            DaemonLifecycleState.STARTING,
            DaemonLifecycleState.STOPPED,
        }:
            return
        event = CoreEvent(
            type=EventType.DAEMON_STATE_CHANGED,
            payload=status,
            sequence=0,
        )
        self._on_core_event(event)

    def _note_lifecycle_activity(self) -> None:
        if self._lifecycle is not None:
            self._lifecycle.note_activity()

    def _stop_configuration_reload(self) -> None:
        coordinator = self._configuration_reload
        self._configuration_reload = None
        if coordinator is not None:
            coordinator.close()

    def _run_bounded_shutdown(
        self, label: str, target: Optional[Any], deadline: float
    ) -> None:
        if target is None:
            return

        def _shutdown_target() -> None:
            try:
                target.shutdown()
            except Exception as error:
                logger.error(
                    "%s shutdown failed type=%s",
                    label,
                    type(error).__name__,
                )

        thread = threading.Thread(
            target=_shutdown_target,
            name=f"sshpilot-{label.lower().replace(' ', '-')}-shutdown",
            daemon=True,
        )
        thread.start()
        thread.join(max(0.0, deadline - time.monotonic()))
        if thread.is_alive():
            logger.error("%s shutdown exceeded its time bound", label)

    def _shutdown_session_commands(self) -> None:
        if self._session_shutdown_started:
            return
        self._session_shutdown_started = True
        deadline = time.monotonic() + self.session_shutdown_timeout
        executor = self._session_executor
        runtime = self._session_runtime
        if executor is not None:
            executor.stop_accepting(cancel_pending=True)

        # Reject new commands and cancel in-flight transfers before tearing
        # down the SFTP handles/forwards they depend on, then sessions last.
        self._run_bounded_shutdown(
            "Transfer runtime", self._transfer_runtime, deadline
        )
        self._run_bounded_shutdown("SFTP runtime", self._sftp_runtime, deadline)
        self._run_bounded_shutdown(
            "Forward runtime", self._forward_runtime, deadline
        )

        runtime_thread = None
        if runtime is not None:
            def _shutdown_runtime() -> None:
                try:
                    runtime.shutdown()
                except Exception as error:
                    logger.error(
                        "Session runtime shutdown failed type=%s",
                        type(error).__name__,
                    )

            runtime_thread = threading.Thread(
                target=_shutdown_runtime,
                name="sshpilot-session-runtime-shutdown",
                daemon=True,
            )
            runtime_thread.start()
        if executor is not None:
            executor.shutdown(
                timeout=max(0.0, deadline - time.monotonic()),
            )
        if runtime_thread is not None:
            runtime_thread.join(max(0.0, deadline - time.monotonic()))
            if runtime_thread.is_alive():
                logger.error("Session runtime shutdown exceeded its time bound")
        self._session_executor = None
        readiness = self._readiness_manager
        self._readiness_manager = None
        if readiness is not None:
            try:
                readiness.close()
            except Exception as error:
                logger.error(
                    "SSH readiness shutdown failed type=%s",
                    type(error).__name__,
                )
        while True:
            try:
                self._completion_queue.get_nowait()
            except queue.Empty:
                break

    def _publish_broadcast_output(self, operation_id, connection_id, stream, text):
        from sshpilot.api.models.broadcast import BroadcastCommandOutput

        self._broadcast_publisher.publish(
            EventType.BROADCAST_OUTPUT,
            BroadcastCommandOutput(operation_id, connection_id, stream, text),
            connection_id=connection_id,
        )

    def _on_core_event(self, event: CoreEvent) -> None:
        """Encode once and enqueue without performing socket I/O."""

        if event.type not in _FORWARDED_EVENT_TYPES:
            return
        if event.type is not EventType.DAEMON_STATE_CHANGED:
            self._note_lifecycle_activity()
        if (
            event.type in _INTERACTION_CANCELLING_EVENT_TYPES
            and event.session_id is not None
            and self._interaction_broker is not None
        ):
            self._interaction_broker.cancel_session(event.session_id)
        with self._event_lock:
            if not self._accepting_core_events or self._stopping.is_set():
                return
            sequence = self._next_event_sequence
            try:
                envelope = public_event_to_envelope(
                    event,
                    sequence=sequence,
                    protocol_version=PROTOCOL_VERSION,
                )
                frame = encode_frame(encode_envelope(envelope))
            except (FramingError, TypeError, ValueError):
                logger.error(
                    "Daemon rejected invalid core event type=%s",
                    event.type.value,
                )
                return
            self._next_event_sequence += 1
            for state in self._clients.values():
                if (
                    state.closed
                    or state.continuity_lost
                    or not state.protocol.handshake_completed
                ):
                    continue
                if (
                    event.type
                    in {


                        EventType.INTERACTION_CREATED,
                        EventType.INTERACTION_STATE_CHANGED,
                    }
                    and event.session_id is not None
                    and (
                        state.protocol.client_id is None
                        or not self._client_can_interact(
                            event.session_id,
                            state.protocol.client_id,
                        )
                    )
                ):
                    continue
                if state.queued_event_count >= self.client_event_queue_limit:
                    state.continuity_lost = True
                    state.output.clear()
                    state.output_offset = 0
                    state.queued_event_count = 0
                    state.queued_outbound_bytes = 0
                    state.queued_terminal_bytes = 0
                    continue
                if not self._enqueue_frame_locked(
                    state,
                    _OutboundFrame(frame, is_event=True),
                    priority=True,
                ):
                    continue
        self._wake_selector()

    def _enqueue_frame_locked(
        self,
        state: _ClientConnection,
        frame: _OutboundFrame,
        *,
        priority: bool = False,
    ) -> bool:
        """Queue one complete frame while enforcing total per-peer memory."""

        if (
            state.queued_outbound_bytes + len(frame.data)
            > self.max_client_outbound_bytes
        ):
            state.continuity_lost = True
            state.output.clear()
            state.output_offset = 0
            state.queued_event_count = 0
            state.queued_outbound_bytes = 0
            state.queued_terminal_bytes = 0
            return False
        if priority and state.output:
            index = 1 if state.output_offset else 0
            if frame.is_event:
                # Preserve lifecycle FIFO behind control/status frames while
                # overtaking queued terminal data.
                while index < len(state.output) and not state.output[index].is_terminal:
                    index += 1
            state.output.insert(index, frame)
        else:
            state.output.append(frame)
        state.queued_outbound_bytes += len(frame.data)
        if frame.is_terminal:
            state.queued_terminal_bytes += len(frame.data)
        if frame.is_event:
            state.queued_event_count += 1
        return True

    def _refresh_client_interest(self, state: _ClientConnection) -> None:
        selector = self._selector
        if selector is None or state.closed or state.sock.fileno() < 0:
            return
        with self._event_lock:
            has_output = bool(state.output)
        events = selectors.EVENT_READ
        if has_output:
            events |= selectors.EVENT_WRITE
        try:
            selector.modify(state.sock, events, state)
        except (KeyError, ValueError, OSError):
            self._close_client(state)

    def _wake_selector(self) -> None:
        wakeup = self._wakeup_write
        if wakeup is None:
            return
        try:
            wakeup.send(b"e")
        except (BlockingIOError, OSError):
            pass

    def _stop_core_events(self) -> None:
        with self._event_lock:
            self._accepting_core_events = False
            subscription = self._core_subscription
            self._core_subscription = None
            session_subscription = self._session_subscription
            self._session_subscription = None
            terminal_subscription = self._terminal_subscription
            self._terminal_subscription = None
            interaction_subscription = self._interaction_subscription
            self._interaction_subscription = None
            sftp_subscription = self._sftp_subscription
            self._sftp_subscription = None
            transfer_subscription = self._transfer_subscription
            self._transfer_subscription = None
            forward_subscription = self._forward_subscription
            self._forward_subscription = None
            operation_subscription = self._operation_subscription
            self._operation_subscription = None
        if subscription is not None:
            subscription.unsubscribe()
        if session_subscription is not None:
            session_subscription.unsubscribe()
        if terminal_subscription is not None:
            terminal_subscription.unsubscribe()
        if interaction_subscription is not None:
            interaction_subscription.unsubscribe()
        if sftp_subscription is not None:
            sftp_subscription.unsubscribe()
        if transfer_subscription is not None:
            transfer_subscription.unsubscribe()
        if forward_subscription is not None:
            forward_subscription.unsubscribe()
        if operation_subscription is not None:
            operation_subscription.unsubscribe()
