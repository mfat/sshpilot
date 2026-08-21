"""Helpers for Phase 10.1 daemon ↔ real OpenSSH integration tests.

Wires an ephemeral ``DaemonServer`` (never the production socket) to a
:class:`~tests.daemon.phase10_sshd.Phase10SshdEnvironment` via a password-aware
connection manager subclass. Prefer stored-password autofill through the
interaction broker; optionally auto-answer password prompts as a fallback.
"""

from __future__ import annotations

import secrets
import shutil
import socket
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, List, Optional

import pytest

from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.api import DaemonClient
from sshpilot.api.models import (
    HostKeyDecision,
    InteractionDecisionRequest,
    InteractionState,
    InteractionType,
    RememberPolicy,
    SecretDecision,
    SftpServiceState,
)
from sshpilot.api.models.operations import ForwardState
from sshpilot.api.models.transfers import TransferState
from sshpilot.daemon import DaemonServer

from tests.daemon.conftest import TestConnection, TestConnectionManager
from tests.daemon.phase10_sshd import (
    Phase10SshdEnvironment,
    container_runtime,
    start_phase10_sshd,
)
from tests.daemon.password_sshd import container_runtime as _password_runtime
from tests.daemon.integration_environment import skip_or_fail

_TERMINAL_TRANSFER = frozenset(
    {TransferState.COMPLETED, TransferState.CANCELLED, TransferState.FAILED}
)


def require_phase10_container(tmp_path: Path) -> Phase10SshdEnvironment:
    """Start Phase 10, skipping only outside the canonical test environment."""

    if container_runtime() is None and _password_runtime() is None:
        skip_or_fail("Phase 10.1 requires podman or docker")
    try:
        return start_phase10_sshd(tmp_path)
    except RuntimeError as error:
        skip_or_fail(f"Phase 10 OpenSSH fixture unavailable: {error}")


def free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 30.0,
    interval: float = 0.05,
    message: str = "condition not met in time",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    if not predicate():
        raise AssertionError(message)


class Phase10Connection(TestConnection):
    """Test connection that forces ``ssh -F`` against the fixture config."""

    def __init__(
        self,
        *,
        nickname: str,
        hostname: str,
        username: str,
        port: int,
        auth_method: int,
        config_path: Path,
        keyfile: str = "",
    ):
        self.id = nickname
        self.nickname = nickname
        self.hostname = hostname
        self.host = hostname
        self.username = username
        self.aliases = ()
        self.uuid = nickname
        self.protocol = "ssh"
        self.port = port
        self.auth_method = auth_method
        self.keyfile = keyfile
        self.identity_files = [keyfile] if keyfile else []
        self.password = None  # never leak the TestConnection sentinel
        self.config_root = str(config_path)
        self.source = str(config_path)
        self.data = {}
        self.data.update(
            {
                "nickname": nickname,
                "hostname": hostname,
                "username": username,
                "port": port,
                "protocol": "ssh",
                
                "auth_method": auth_method,
                "keyfile": keyfile,
            }
        )

    def _resolve_config_override_path(self) -> Optional[str]:
        path = Path(self.config_root)
        return str(path) if path.is_file() else None

    def collect_identity_file_candidates(self):
        if self.keyfile and Path(self.keyfile).is_file():
            return [self.keyfile]
        return []


class Phase10ConnectionManager(TestConnectionManager):
    """In-memory manager with password/passphrase APIs for ConnectionApplicationService."""

    def __init__(self):
        super().__init__()
        self.connections = []
        self._passwords = {}
        self._passphrases = {}

    def get_connection_password(self, connection) -> Optional[str]:
        if connection is None:
            return None
        return self._passwords.get(str(getattr(connection, "id", getattr(connection, "uuid", ""))))

    def lookup_connection_password(self, connection_id: str) -> Optional[str]:
        return self._passwords.get(str(connection_id))

    def lookup_key_passphrase(self, key_path: str) -> Optional[str]:
        return self._passphrases.get(str(key_path))

    def store_connection_password(self, connection, password: str) -> bool:
        self._passwords[str(getattr(connection, "id", connection.uuid))] = password
        return True

    def get_password(self, host: str, username: str) -> Optional[str]:
        for connection in self.connections:
            if getattr(connection, "username", None) != username:
                continue
            if host in {
                getattr(connection, "hostname", None),
                getattr(connection, "nickname", None),
                getattr(connection, "host", None),
            }:
                return self.get_connection_password(connection)
        return None

    def get_key_passphrase(self, key_path: str) -> Optional[str]:
        return self._passphrases.get(str(key_path))

    def store_key_passphrase(self, key_path: str, passphrase: str) -> bool:
        self._passphrases[str(key_path)] = passphrase
        return True


@dataclass
class Phase10Stack:
    """Ephemeral daemon + client + OpenSSH fixture (production socket untouched)."""

    env: Phase10SshdEnvironment
    server: DaemonServer
    manager: Phase10ConnectionManager
    connection: Phase10Connection
    client: DaemonClient
    _owns_env: bool = True
    _socket_root: Optional[Path] = None
    _clients: List[DaemonClient] = field(default_factory=list)
    _password_responder: Optional[threading.Thread] = field(default=None, repr=False)
    _stop_responder: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def connection_id(self):
        from sshpilot.api.models.common import ConnectionId

        return ConnectionId(self.connection.id)

    def connect_client(self, *, client_id: str = "client:phase10") -> DaemonClient:
        client = DaemonClient(
            socket_path=self.server.socket_path,
            client_id=client_id,
            timeout=60,
        )
        self._clients.append(client)
        return client

    def start_password_auto_answer(self, password: Optional[str] = None) -> None:
        """Fallback: claim/submit password interactions if the broker still prompts."""

        secret = password if password is not None else self.env.password
        self._start_secret_auto_answer(
            InteractionType.PASSWORD,
            secret,
            name="phase10-password-auto",
        )

    def start_passphrase_auto_answer(self, passphrase: Optional[str] = None) -> None:
        secret = passphrase if passphrase is not None else self.env.key_passphrase
        self._start_secret_auto_answer(
            InteractionType.PRIVATE_KEY_PASSPHRASE,
            secret,
            name="phase10-passphrase-auto",
        )

    def start_password_decline(self) -> None:
        """Cancel pending password prompts via the in-process broker."""

        stop = self._stop_responder
        owner = self.client._client_id

        def _loop() -> None:
            from sshpilot.api.models.common import ClientId

            client_id = ClientId(str(owner))
            while not stop.is_set():
                broker = getattr(self.server, "_interaction_broker", None)
                if broker is not None:
                    try:
                        for summary in broker.list(client_id):
                            if summary.type is not InteractionType.PASSWORD:
                                continue
                            if summary.state is not InteractionState.PENDING:
                                continue
                            try:
                                broker.claim(summary.id, client_id)
                                broker.respond(
                                    InteractionDecisionRequest(
                                        interaction_id=summary.id,
                                        secret_decision=SecretDecision.CANCEL,
                                    ),
                                    client_id,
                                )
                            except Exception:
                                continue
                    except Exception:
                        pass
                stop.wait(0.05)

        self._password_responder = threading.Thread(
            target=_loop, name="phase10-password-decline", daemon=True
        )
        self._password_responder.start()

    def _start_secret_auto_answer(
        self,
        interaction_type: InteractionType,
        secret: str,
        *,
        name: str,
    ) -> None:
        helper = DaemonClient(
            socket_path=self.server.socket_path,
            client_id="client:phase10-auth-helper",
            timeout=60,
        )
        self._clients.append(helper)
        stop = self._stop_responder

        def _loop() -> None:
            while not stop.is_set():
                try:
                    for summary in helper.list_interactions():
                        if summary.type is not interaction_type:
                            continue
                        if summary.state is not InteractionState.PENDING:
                            continue
                        try:
                            claim = helper.claim_interaction(summary.id)
                            helper.respond_to_interaction(
                                InteractionDecisionRequest(
                                    interaction_id=summary.id,
                                    secret_decision=SecretDecision.SUBMIT,
                                    remember_policy=RememberPolicy.DO_NOT_STORE,
                                )
                            )
                            helper.send_interaction_secret(
                                summary.id,
                                claim.nonce,
                                bytearray(secret.encode()),
                            )
                        except Exception:
                            continue
                except Exception:
                    pass
                stop.wait(0.1)

        self._password_responder = threading.Thread(
            target=_loop, name=name, daemon=True
        )
        self._password_responder.start()

    def answer_pending_host_keys(self, *, accept: bool = True) -> int:
        """Accept/reject pending host-key prompts via the in-process broker."""

        broker = getattr(self.server, "_interaction_broker", None)
        if broker is None:
            return 0
        from sshpilot.api.models.common import ClientId

        client_id = ClientId(str(self.client._client_id))
        answered = 0
        for summary in broker.list(client_id):
            if summary.type is not InteractionType.HOST_KEY_CONFIRMATION:
                continue
            if summary.state is not InteractionState.PENDING:
                continue
            broker.claim(summary.id, client_id)
            broker.respond(
                InteractionDecisionRequest(
                    interaction_id=summary.id,
                    host_key_decision=(
                        HostKeyDecision.ACCEPT
                        if accept
                        else HostKeyDecision.REJECT
                    ),
                ),
                client_id,
            )
            answered += 1
        return answered

    def wait_sftp_ready(self, service_id, *, timeout: float = 45.0):
        """Poll until READY; answer host-key prompts via the in-process broker.

        Avoid DaemonClient event subscriptions here — they share the transport
        with RPCs and have caused ``transport is closed`` races in these tests.
        """

        def _poll() -> bool:
            try:
                self.answer_pending_host_keys()
            except Exception:
                pass
            summary = self.client.get_sftp_service(service_id)
            if summary.state is SftpServiceState.READY:
                return True
            if summary.state in {SftpServiceState.FAILED, SftpServiceState.CLOSED}:
                failure = getattr(summary, "failure", None)
                detail = getattr(failure, "message", None) or summary.state.value
                raise AssertionError(f"SFTP did not become READY: {detail}")
            return False

        wait_until(_poll, timeout=timeout, message="SFTP never reached READY")
        return self.client.get_sftp_service(service_id)

    def wait_forward_active(self, forward_id, *, timeout: float = 30.0):
        def _poll() -> bool:
            try:
                self.answer_pending_host_keys()
            except Exception:
                pass
            summary = self.client.get_forward(forward_id)
            if summary.state is ForwardState.ACTIVE:
                return True
            if summary.state in {ForwardState.FAILED, ForwardState.CLOSED}:
                failure = getattr(summary, "failure", None)
                detail = getattr(failure, "message", None) or summary.state.value
                raise AssertionError(f"forward did not become ACTIVE: {detail}")
            return False

        wait_until(_poll, timeout=timeout, message="forward never reached ACTIVE")
        return self.client.get_forward(forward_id)

    def wait_transfer_terminal(self, transfer_id, *, timeout: float = 60.0):
        def _poll() -> bool:
            return self.client.get_transfer(transfer_id).state in _TERMINAL_TRANSFER

        wait_until(_poll, timeout=timeout, message="transfer never finished")
        return self.client.get_transfer(transfer_id)

    def close(self, *, destroy_env: Optional[bool] = None) -> None:
        self._stop_responder.set()
        if self._password_responder is not None:
            self._password_responder.join(timeout=2.0)
        for client in list(self._clients):
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
        try:
            self.client.close()
        except Exception:
            pass
        try:
            self.server.shutdown()
            self.server.wait_stopped()
        except Exception:
            pass
        if self._socket_root is not None:
            shutil.rmtree(self._socket_root, ignore_errors=True)
            self._socket_root = None
        should_destroy = self._owns_env if destroy_env is None else destroy_env
        if should_destroy:
            self.env.destroy()


def start_phase10_stack(
    tmp_path: Path,
    *,
    auth: str = "password",
    store_secret: bool = True,
    auto_answer_password: bool = False,
    env: Optional[Phase10SshdEnvironment] = None,
) -> Phase10Stack:
    """Start fixture + ephemeral DaemonServer + DaemonClient.

    Uses a unique unix socket under ``tmp_path`` — never the production daemon
    socket. Pass a shared ``env`` to avoid one container per test.
    """

    owns_env = env is None
    if env is None:
        env = require_phase10_container(tmp_path)
    if auth == "key":
        env.write_client_config("key")
    else:
        env.write_client_config("password")

    manager = Phase10ConnectionManager()
    connection = Phase10Connection(
        nickname=env.host_alias,
        hostname="127.0.0.1",
        username=env.username,
        port=env.port,
        auth_method=1 if auth == "password" else 0,
        config_path=env.ssh_config,
        keyfile=str(env.key_path) if auth == "key" else "",
    )
    manager.connections = [connection]
    if store_secret:
        if auth == "password":
            manager.store_connection_password(connection, env.password)
        else:
            manager.store_key_passphrase(str(env.key_path), env.key_passphrase)

    from sshpilot.daemon.connection_launch_provider import DaemonConnectionLaunchProvider

    launch_provider = DaemonConnectionLaunchProvider(
        lambda connection_id: next(
            (
                connection
                for connection in manager.connections
                if connection.id == str(connection_id)
            ),
            None,
        ),
        secret_provider=manager,
    )

    # AF_UNIX paths are ~108 bytes; pytest tmp paths can exceed that when nested.
    socket_root = Path(
        tempfile.mkdtemp(prefix=f"sshpilot-p10d-{secrets.token_hex(4)}-")
    )
    socket_path = socket_root / "sshpilotd.sock"
    server = DaemonServer(
        lambda: ConnectionApplicationService(
            manager,
            launch_provider=launch_provider,
            secret_provider=manager,
            client_name="sshpilotd",
        ),
        socket_path=socket_path,
    )
    server.start_in_thread()
    client = DaemonClient(
        socket_path=server.socket_path,
        client_id="client:phase10",
        timeout=60,
    )
    stack = Phase10Stack(
        env=env,
        server=server,
        manager=manager,
        connection=connection,
        client=client,
        _owns_env=owns_env,
        _socket_root=socket_root,
    )
    stack._clients.append(client)
    if auto_answer_password and auth == "password":
        stack.start_password_auto_answer()
    elif auto_answer_password and auth == "key":
        stack.start_passphrase_auto_answer()
    return stack
