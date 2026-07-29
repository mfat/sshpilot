"""Daemon-owned typed interaction state and one-use secret delivery."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from time import monotonic
from typing import Callable, Deque, Dict, Iterable, List, Optional, Sequence

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import (
    CoreEventCallback,
    EventPublisher,
    EventType,
    Subscription,
)
from sshpilot.api.interaction_identity import new_interaction_id
from sshpilot.api.models import (
    ClientId,
    ConnectionId,
    HostKeyDecision,
    HostKeyPrompt,
    HostKeyStatus,
    InteractionClaim,
    InteractionDecisionRequest,
    InteractionId,
    InteractionPrompt,
    InteractionState,
    InteractionSummary,
    InteractionType,
    PassphrasePrompt,
    PasswordPrompt,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.transport.secret_frames import SecretFrame

logger = logging.getLogger(__name__)
from sshpilot.askpass_utils import classify_prompt
from sshpilot.daemon.session_runtime import SessionLaunchSpec

DEFAULT_SECRET_INTERACTION_TIMEOUT = 120.0
DEFAULT_HOST_KEY_INTERACTION_TIMEOUT = 180.0
DEFAULT_COMPLETED_INTERACTION_LIMIT = 100
DEFAULT_ASKPASS_WORKERS = 4
DEFAULT_ASKPASS_QUEUE_LIMIT = 32
_ASKPASS_LENGTH = struct.Struct(">I")
_MAX_ASKPASS_REQUEST = 8 * 1024
_MAX_SECRET_SIZE = 16 * 1024
_KEY_PATH_PATTERN = re.compile(r"""['"]([^'"]+)['"]""")

_FINAL_STATES = frozenset(
    {
        InteractionState.ANSWERED,
        InteractionState.CANCELLED,
        InteractionState.EXPIRED,
        InteractionState.FAILED,
    }
)
_SECRET_TYPES = frozenset(
    {
        InteractionType.PASSWORD,
        InteractionType.PRIVATE_KEY_PASSPHRASE,
    }
)


@dataclass
class InteractionResult:
    """Private one-use broker result; callers must clear secret after use."""

    decision: HostKeyDecision | SecretDecision
    remember_policy: RememberPolicy
    secret: Optional[bytearray] = None

    def clear(self) -> None:
        if self.secret is not None:
            self.secret[:] = b"\0" * len(self.secret)
            self.secret.clear()
            self.secret = None


@dataclass
class _InteractionRecord:
    summary: InteractionSummary
    deadline: float
    claim_nonce: Optional[str] = None
    awaiting_secret: bool = False
    result: Optional[InteractionResult] = None
    result_taken: bool = False


@dataclass
class _PendingRemember:
    interaction_type: InteractionType
    key: str
    secret: bytearray

    def clear(self) -> None:
        self.secret[:] = b"\0" * len(self.secret)
        self.secret.clear()


@dataclass
class _AskpassContext:
    token: str
    session_id: SessionId
    connection_id: ConnectionId
    hostname: str
    username: str
    port: int
    control_path: str
    control_target: str
    control_argv: tuple[str, ...]
    attempts: Dict[str, int]
    stored_attempted: set[str]
    pending_remember: list[_PendingRemember]
    closed: bool = False


class InteractionBroker:
    """Serialize typed interaction lifecycle and secret handoff."""

    def __init__(
        self,
        *,
        client_is_eligible: Optional[
            Callable[[SessionId, ClientId], bool]
        ] = None,
        secret_timeout: float = DEFAULT_SECRET_INTERACTION_TIMEOUT,
        host_key_timeout: float = DEFAULT_HOST_KEY_INTERACTION_TIMEOUT,
        completed_limit: int = DEFAULT_COMPLETED_INTERACTION_LIMIT,
        password_lookup: Optional[Callable[[ConnectionId], Optional[str]]] = None,
        password_store: Optional[Callable[[ConnectionId, str], bool]] = None,
        passphrase_lookup: Optional[Callable[[str], Optional[str]]] = None,
        passphrase_store: Optional[Callable[[str, str], bool]] = None,
        askpass_workers: int = DEFAULT_ASKPASS_WORKERS,
        askpass_queue_limit: int = DEFAULT_ASKPASS_QUEUE_LIMIT,
    ) -> None:
        if secret_timeout <= 0 or host_key_timeout <= 0:
            raise ValueError("interaction timeouts must be positive")
        if completed_limit < 1:
            raise ValueError("completed interaction limit must be positive")
        if askpass_workers < 1 or askpass_queue_limit < 1:
            raise ValueError("askpass worker and queue limits must be positive")
        self._client_is_eligible = client_is_eligible or (lambda _session, _client: True)
        self._secret_timeout = float(secret_timeout)
        self._host_key_timeout = float(host_key_timeout)
        self._completed_limit = completed_limit
        self._password_lookup = password_lookup
        self._password_store = password_store
        self._passphrase_lookup = passphrase_lookup
        self._passphrase_store = passphrase_store
        self._condition = threading.Condition(threading.RLock())
        self._records: Dict[InteractionId, _InteractionRecord] = {}
        self._completed: Deque[InteractionId] = deque()
        self._closed = False
        self._askpass_contexts: Dict[str, _AskpassContext] = {}
        self._askpass_transports: set[socket.socket] = set()
        self._askpass_queue: queue.Queue[Optional[socket.socket]] = queue.Queue(
            maxsize=askpass_queue_limit
        )
        self._private_dir = Path(
            tempfile.mkdtemp(prefix=f"sshpilot-interaction-{os.getpid()}-")
        )
        os.chmod(self._private_dir, 0o700)
        self._askpass_socket_path = self._private_dir / "askpass.sock"
        self._askpass_helper_path = self._private_dir / "askpass"
        self._write_helper_launcher()
        self._askpass_listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._askpass_listener.bind(str(self._askpass_socket_path))
        os.chmod(self._askpass_socket_path, 0o600, follow_symlinks=False)
        self._askpass_listener.listen(askpass_queue_limit)
        self._askpass_listener.settimeout(0.2)
        self._askpass_workers = tuple(
            threading.Thread(
                target=self._askpass_worker_main,
                name=f"sshpilot-askpass-{index}",
                daemon=False,
            )
            for index in range(askpass_workers)
        )
        self._askpass_acceptor = threading.Thread(
            target=self._askpass_accept_main,
            name="sshpilot-askpass-accept",
            daemon=False,
        )
        self._auth_monitor = threading.Thread(
            target=self._auth_monitor_main,
            name="sshpilot-auth-monitor",
            daemon=False,
        )
        self._publisher = EventPublisher()
        self._scheduler = threading.Thread(
            target=self._scheduler_main,
            name="sshpilot-interaction-deadlines",
            daemon=False,
        )
        for worker in self._askpass_workers:
            worker.start()
        self._askpass_acceptor.start()
        self._auth_monitor.start()
        self._scheduler.start()

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        return self._publisher.subscribe(callback)

    def prepare_launch(
        self,
        spec: SessionLaunchSpec,
        launch_builder: Callable[..., tuple[Sequence[str], dict[str, str]]],
        *,
        trailing_args: Sequence[str] = (),
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Prepare canonical SSH argv, strict trust, and daemon askpass.

        ``trailing_args`` are appended after the target host once broker
        trust/auth options have been inserted (e.g. ``("sftp",)`` for the
        SFTP subsystem request) — the host must stay ``argv[-1]`` while the
        broker computes host-key pinning and the ControlMaster check.
        """

        argv_value, environment_value = launch_builder(
            spec.connection_id,
            interaction_policy="broker",
        )
        argv = tuple(argv_value)
        environment = dict(environment_value)
        if not argv or len(argv) < 2:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH launch command is invalid",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        target = argv[-1]
        effective = self._effective_ssh_config(argv)
        hostname = effective.get("hostname", spec.hostname)
        username = effective.get("user", spec.username)
        try:
            port = int(effective.get("port", spec.port))
        except (TypeError, ValueError):
            port = spec.port
        pinned_file, key_type = self._prepare_host_key(
            spec,
            hostname=hostname,
            port=port,
            effective=effective,
        )
        token = secrets.token_urlsafe(32)
        control_path = str(self._private_dir / f"c-{spec.session_id[-12:]}")
        options = (
            "-o",
            "BatchMode=no",
            "-o",
            "KbdInteractiveAuthentication=no",
            "-o",
            "NumberOfPasswordPrompts=3",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={pinned_file}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"HostKeyAlgorithms={key_type}",
            "-o",
            "ControlMaster=yes",
            "-o",
            "ControlPersist=no",
            "-o",
            f"ControlPath={control_path}",
        )
        # OpenSSH keeps the first obtained value for each option. Insert broker
        # trust/auth controls before any preference overrides and strip
        # conflicting earlier copies so they cannot weaken the pin.
        argv = self._with_broker_options(argv, options)
        control_argv = (
            argv[0],
            "-S",
            control_path,
            "-O",
            "check",
            target,
        )
        context = _AskpassContext(
            token=token,
            session_id=spec.session_id,
            connection_id=spec.connection_id,
            hostname=hostname,
            username=username,
            port=port,
            control_path=control_path,
            control_target=target,
            control_argv=control_argv,
            attempts={},
            stored_attempted=set(),
            pending_remember=[],
        )
        with self._condition:
            self._require_open_locked()
            self._askpass_contexts[token] = context
            self._condition.notify_all()
        environment["SSH_ASKPASS"] = str(self._askpass_helper_path)
        environment["SSH_ASKPASS_REQUIRE"] = "force"
        environment["DISPLAY"] = environment.get("DISPLAY") or ":sshpilot-daemon"
        environment["SSHPILOT_DAEMON_ASKPASS_SOCKET"] = str(
            self._askpass_socket_path
        )
        environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"] = token
        source_root = str(Path(__file__).resolve().parents[2])
        current_pythonpath = environment.get("PYTHONPATH", "")
        environment["PYTHONPATH"] = (
            source_root
            if not current_pythonpath
            else os.pathsep.join((source_root, current_pythonpath))
        )
        if trailing_args:
            argv = (*argv, *trailing_args)
        return argv, environment

    _BROKER_OPTION_PREFIXES = (
        "BatchMode=",
        "KbdInteractiveAuthentication=",
        "NumberOfPasswordPrompts=",
        "StrictHostKeyChecking=",
        "UserKnownHostsFile=",
        "GlobalKnownHostsFile=",
        "HostKeyAlgorithms=",
        "ControlMaster=",
        "ControlPersist=",
        "ControlPath=",
    )

    @classmethod
    def _with_broker_options(
        cls,
        argv: Sequence[str],
        options: Sequence[str],
    ) -> tuple[str, ...]:
        if len(argv) < 2:
            raise ValueError("SSH launch argv is incomplete")
        target = argv[-1]
        head: list[str] = []
        index = 0
        end = len(argv) - 1
        while index < end:
            argument = argv[index]
            if argument == "-o" and index + 1 < end:
                value = argv[index + 1]
                if any(
                    value.startswith(prefix) for prefix in cls._BROKER_OPTION_PREFIXES
                ):
                    index += 2
                    continue
                head.extend((argument, value))
                index += 2
                continue
            if argument.startswith("-o") and len(argument) > 2:
                value = argument[2:]
                if any(
                    value.startswith(prefix) for prefix in cls._BROKER_OPTION_PREFIXES
                ):
                    index += 1
                    continue
            head.append(argument)
            index += 1
        insert_at = 1
        if len(head) >= 3 and head[1] == "-F":
            insert_at = 3
        return tuple((*head[:insert_at], *options, *head[insert_at:], target))

    def _effective_ssh_config(self, argv: Sequence[str]) -> dict[str, str]:
        target = argv[-1]
        # ``ssh … -s <host> sftp`` (and any pre-host ``-s``) must not be fed to
        # ``ssh -G`` — OpenSSH treats ``-s`` as a subsystem request and the
        # probe can hang instead of dumping config.
        probe = tuple(argument for argument in argv[:-1] if argument != "-s")
        try:
            completed = subprocess.run(
                (*probe, "-G", target),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
                text=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        result: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition(" ")
            if separator and key and key not in result:
                result[key.lower()] = value.strip()
        return result

    def _prepare_host_key(
        self,
        spec: SessionLaunchSpec,
        *,
        hostname: str,
        port: int,
        effective: dict[str, str],
    ) -> tuple[str, str]:
        keyscan = shutil.which("ssh-keyscan")
        if keyscan is None:
            raise SshPilotError(
                ErrorCode.ASKPASS_HELPER_UNAVAILABLE,
                "Host-key verification support is unavailable",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        try:
            scan = subprocess.run(
                (keyscan, "-T", "5", "-p", str(port), hostname),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=7,
                text=True,
                env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            )
        except (OSError, subprocess.SubprocessError):
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH host key could not be retrieved",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            ) from None
        candidates = []
        for line in scan.stdout.splitlines():
            parts = line.split()
            if len(parts) == 3 and parts[1].startswith(("ssh-", "ecdsa-")):
                candidates.append((parts[1], parts[2]))
        if not candidates:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH host did not present a supported key",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        preference = {"ssh-ed25519": 0, "ecdsa-sha2-nistp256": 1, "ssh-rsa": 2}
        candidates.sort(key=lambda item: preference.get(item[0], 99))
        host_token = hostname if port == 22 else f"[{hostname}]:{port}"
        known_paths = self._known_hosts_paths(effective)
        known_entries = self._known_entries(known_paths, host_token)
        matching = next(
            (
                candidate
                for candidate in candidates
                if candidate in known_entries
            ),
            None,
        )
        if matching is not None:
            selected = matching
        else:
            selected = candidates[0]
            status = HostKeyStatus.CHANGED if known_entries else HostKeyStatus.UNKNOWN
            fingerprint = self._fingerprint(selected[1])
            interaction = self.create(
                session_id=spec.session_id,
                connection_id=spec.connection_id,
                interaction_type=InteractionType.HOST_KEY_CONFIRMATION,
                prompt=HostKeyPrompt(
                    hostname=hostname,
                    port=port,
                    key_type=selected[0],
                    fingerprint=fingerprint,
                    status=status,
                ),
            )
            result = self.wait_for_result(interaction.id)
            if (
                result is None
                or not isinstance(result.decision, HostKeyDecision)
                or result.decision is HostKeyDecision.REJECT
                or status is not HostKeyStatus.UNKNOWN
            ):
                if result is not None:
                    result.clear()
                raise SshPilotError(
                    ErrorCode.SESSION_STARTUP_FAILED,
                    "The SSH host key was not trusted",
                    connection_id=spec.connection_id,
                    session_id=spec.session_id,
                )
            if result.decision is HostKeyDecision.ACCEPT_AND_STORE:
                self._persist_host_key(
                    known_paths[0],
                    host_token,
                    selected,
                    hash_host=effective.get("hashknownhosts", "no") == "yes",
                )
            result.clear()
        pin_path = self._private_dir / f"k-{spec.session_id[-12:]}"
        self._atomic_write(
            pin_path,
            f"{host_token} {selected[0]} {selected[1]}\n".encode(),
        )
        return str(pin_path), selected[0]

    @staticmethod
    def _known_hosts_paths(effective: dict[str, str]) -> tuple[Path, ...]:
        raw = effective.get("userknownhostsfile", "~/.ssh/known_hosts")
        paths = tuple(
            Path(item).expanduser()
            for item in raw.split()
            if item and item.lower() != "none"
        )
        return paths or (Path("~/.ssh/known_hosts").expanduser(),)

    @staticmethod
    def _known_entries(
        paths: Sequence[Path],
        host_token: str,
    ) -> set[tuple[str, str]]:
        keygen = shutil.which("ssh-keygen")
        entries: set[tuple[str, str]] = set()
        if keygen is None:
            return entries
        for path in paths:
            if not path.is_file():
                continue
            try:
                result = subprocess.run(
                    (keygen, "-F", host_token, "-f", str(path)),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=False,
                    timeout=3,
                    text=True,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            for line in result.stdout.splitlines():
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) >= 3:
                    entries.add((parts[-2], parts[-1]))
        return entries

    @staticmethod
    def _fingerprint(key_data: str) -> str:
        try:
            digest = hashlib.sha256(base64.b64decode(key_data, validate=True)).digest()
        except (ValueError, TypeError):
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH host key was malformed",
            ) from None
        return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")

    def _persist_host_key(
        self,
        path: Path,
        host_token: str,
        selected: tuple[str, str],
        *,
        hash_host: bool,
    ) -> None:
        path = path.expanduser()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.exists():
            current_stat = path.lstat()
            if stat.S_ISLNK(current_stat.st_mode):
                raise SshPilotError(
                    ErrorCode.HOST_KEY_PERSISTENCE_FAILED,
                    "The known-hosts file is unsafe",
                )
            existing = path.read_bytes()
        else:
            existing = b""
        stored_host = host_token
        if hash_host:
            salt = secrets.token_bytes(20)
            digest = hmac.new(salt, host_token.encode(), hashlib.sha1).digest()
            stored_host = (
                "|1|"
                + base64.b64encode(salt).decode()
                + "|"
                + base64.b64encode(digest).decode()
            )
        line = f"{stored_host} {selected[0]} {selected[1]}\n".encode()
        try:
            self._atomic_write(path, existing + line)
        except OSError:
            raise SshPilotError(
                ErrorCode.HOST_KEY_PERSISTENCE_FAILED,
                "The known-hosts file could not be updated",
            ) from None

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            dir=path.parent,
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                descriptor = -1
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600, follow_symlinks=False)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def create(
        self,
        *,
        session_id: SessionId,
        connection_id: ConnectionId,
        interaction_type: InteractionType,
        prompt: InteractionPrompt,
        attempt: int = 1,
        timeout: Optional[float] = None,
    ) -> InteractionSummary:
        duration = timeout
        if duration is None:
            duration = (
                self._host_key_timeout
                if interaction_type is InteractionType.HOST_KEY_CONFIRMATION
                else self._secret_timeout
            )
        if duration <= 0:
            raise ValueError("interaction timeout must be positive")
        now = datetime.now(timezone.utc)
        summary = InteractionSummary(
            id=new_interaction_id(),
            session_id=session_id,
            connection_id=connection_id,
            type=interaction_type,
            state=InteractionState.PENDING,
            created_at=now,
            expires_at=now + timedelta(seconds=duration),
            attempt=attempt,
            prompt=prompt,
        )
        with self._condition:
            self._require_open_locked()
            self._records[summary.id] = _InteractionRecord(
                summary=summary,
                deadline=monotonic() + duration,
            )
            self._condition.notify_all()
        self._publish(EventType.INTERACTION_CREATED, summary)
        return summary

    def list(self, client_id: ClientId) -> List[InteractionSummary]:
        with self._condition:
            return [
                record.summary
                for record in self._records.values()
                if self._visible_locked(record, client_id)
            ]

    def get(
        self,
        interaction_id: InteractionId,
        client_id: ClientId,
    ) -> InteractionSummary:
        with self._condition:
            record = self._record_locked(interaction_id)
            self._require_visible_locked(record, client_id)
            return record.summary

    def claim(
        self,
        interaction_id: InteractionId,
        client_id: ClientId,
    ) -> InteractionClaim:
        changed: Optional[InteractionSummary] = None
        with self._condition:
            record = self._record_locked(interaction_id)
            self._require_pending_locked(record)
            self._require_visible_locked(record, client_id)
            existing = record.summary.responder_client_id
            if existing is not None and existing != client_id:
                raise self._error(
                    ErrorCode.INTERACTION_CLAIM_CONFLICT,
                    "Another client owns this interaction",
                    record,
                    retryable=True,
                )
            if existing is None:
                record.claim_nonce = secrets.token_hex(16)
                changed = self._replace_summary(
                    record,
                    state=InteractionState.CLAIMED,
                    responder_client_id=client_id,
                )
            assert record.claim_nonce is not None
            claim = InteractionClaim(
                interaction_id=interaction_id,
                responder_client_id=client_id,
                nonce=record.claim_nonce,
                expires_at=record.summary.expires_at,
            )
        if changed is not None:
            self._publish(EventType.INTERACTION_STATE_CHANGED, changed)
        return claim

    def release(self, interaction_id: InteractionId, client_id: ClientId) -> None:
        changed: Optional[InteractionSummary] = None
        with self._condition:
            record = self._record_locked(interaction_id)
            self._require_pending_locked(record)
            if record.summary.responder_client_id not in (None, client_id):
                raise self._unauthorised(record)
            if record.awaiting_secret:
                raise self._error(
                    ErrorCode.INTERACTION_SECRET_EXPECTED,
                    "A secret response is already reserved",
                    record,
                )
            if record.summary.responder_client_id is not None:
                record.claim_nonce = None
                changed = self._replace_summary(
                    record,
                    state=InteractionState.PENDING,
                    responder_client_id=None,
                )
        if changed is not None:
            self._publish(EventType.INTERACTION_STATE_CHANGED, changed)

    def respond(
        self,
        request: InteractionDecisionRequest,
        client_id: ClientId,
    ) -> None:
        publish: Optional[InteractionSummary] = None
        with self._condition:
            record = self._record_locked(request.interaction_id)
            self._require_pending_locked(record)
            self._require_responder_locked(record, client_id)
            if record.summary.type is InteractionType.HOST_KEY_CONFIRMATION:
                decision = request.host_key_decision
                if decision is None:
                    raise self._invalid_decision(record)
                if request.remember_policy is not RememberPolicy.DO_NOT_STORE:
                    raise self._invalid_decision(record)
                record.result = InteractionResult(
                    decision=decision,
                    remember_policy=RememberPolicy.DO_NOT_STORE,
                )
                publish = self._finish_locked(record, InteractionState.ANSWERED)
            else:
                decision = request.secret_decision
                if decision is None:
                    raise self._invalid_decision(record)
                if decision is SecretDecision.CANCEL:
                    record.result = InteractionResult(
                        decision=decision,
                        remember_policy=RememberPolicy.DO_NOT_STORE,
                    )
                    publish = self._finish_locked(
                        record,
                        InteractionState.CANCELLED,
                    )
                elif record.awaiting_secret:
                    raise self._error(
                        ErrorCode.INTERACTION_SECRET_DUPLICATE,
                        "A secret response is already reserved",
                        record,
                    )
                else:
                    record.awaiting_secret = True
                    record.result = InteractionResult(
                        decision=decision,
                        remember_policy=request.remember_policy,
                    )
            self._condition.notify_all()
        if publish is not None:
            self._publish(EventType.INTERACTION_STATE_CHANGED, publish)

    def submit_secret(self, frame: SecretFrame, client_id: ClientId) -> None:
        publish: Optional[InteractionSummary] = None
        try:
            with self._condition:
                record = self._record_locked(frame.interaction_id)
                self._require_pending_locked(record)
                self._require_responder_locked(record, client_id)
                if record.summary.type not in _SECRET_TYPES or not record.awaiting_secret:
                    raise self._error(
                        ErrorCode.INTERACTION_SECRET_EXPECTED,
                        "This interaction is not awaiting a secret",
                        record,
                    )
                if (
                    record.claim_nonce is None
                    or bytes.fromhex(record.claim_nonce) != frame.nonce
                ):
                    raise self._unauthorised(record)
                if record.result is None or record.result.secret is not None:
                    raise self._error(
                        ErrorCode.INTERACTION_SECRET_DUPLICATE,
                        "The secret response has already been supplied",
                        record,
                    )
                record.result.secret = bytearray(frame.secret)
                publish = self._finish_locked(record, InteractionState.ANSWERED)
                self._condition.notify_all()
        finally:
            frame.clear()
        if publish is not None:
            self._publish(EventType.INTERACTION_STATE_CHANGED, publish)

    def wait_for_result(
        self,
        interaction_id: InteractionId,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[InteractionResult]:
        changed_summary: Optional[InteractionSummary] = None
        with self._condition:
            record = self._record_locked(interaction_id)
            while record.summary.state not in _FINAL_STATES and not self._closed:
                if cancel_check is not None and cancel_check():
                    self._clear_result_locked(record)
                    changed_summary = self._finish_locked(
                        record,
                        InteractionState.CANCELLED,
                    )
                    self._condition.notify_all()
                    break
                remaining = record.deadline - monotonic()
                if remaining <= 0:
                    self._clear_result_locked(record)
                    changed_summary = self._finish_locked(
                        record,
                        InteractionState.EXPIRED,
                    )
                    self._condition.notify_all()
                    break
                self._condition.wait(
                    min(remaining, 0.1)
                    if cancel_check is not None
                    else remaining
                )
            if record.summary.state is InteractionState.EXPIRED:
                result = None
            elif record.summary.state in {
                InteractionState.CANCELLED,
                InteractionState.FAILED,
            }:
                result = None
            elif record.result_taken or record.result is None:
                result = None
            else:
                record.result_taken = True
                result = record.result
                record.result = None
        if changed_summary is not None:
            self._publish(EventType.INTERACTION_STATE_CHANGED, changed_summary)
        return result

    @staticmethod
    def _transport_is_closed(transport: socket.socket) -> bool:
        timeout = transport.gettimeout()
        try:
            transport.setblocking(False)
            return transport.recv(1, socket.MSG_PEEK) == b""
        except BlockingIOError:
            return False
        except OSError:
            return True
        finally:
            try:
                transport.settimeout(timeout)
            except OSError:
                pass

    def cancel(
        self,
        interaction_id: InteractionId,
        *,
        client_id: Optional[ClientId] = None,
    ) -> None:
        publish: Optional[InteractionSummary] = None
        with self._condition:
            record = self._record_locked(interaction_id)
            if client_id is not None:
                self._require_responder_locked(record, client_id)
            if record.summary.state in _FINAL_STATES:
                return
            self._clear_result_locked(record)
            publish = self._finish_locked(record, InteractionState.CANCELLED)
            self._condition.notify_all()
        self._publish(EventType.INTERACTION_STATE_CHANGED, publish)

    def disconnect_client(self, client_id: ClientId) -> None:
        changed: list[InteractionSummary] = []
        with self._condition:
            for record in self._records.values():
                if (
                    record.summary.responder_client_id == client_id
                    and record.summary.state not in _FINAL_STATES
                ):
                    if record.awaiting_secret:
                        record.awaiting_secret = False
                        self._clear_result_locked(record)
                    record.claim_nonce = None
                    changed.append(
                        self._replace_summary(
                            record,
                            state=InteractionState.PENDING,
                            responder_client_id=None,
                        )
                    )
            self._condition.notify_all()
        for summary in changed:
            self._publish(EventType.INTERACTION_STATE_CHANGED, summary)

    def cancel_session(self, session_id: SessionId) -> None:
        changed: list[InteractionSummary] = []
        with self._condition:
            for token, context in tuple(self._askpass_contexts.items()):
                if context.session_id == session_id:
                    self._clear_context_locked(context)
                    del self._askpass_contexts[token]
            for record in self._records.values():
                if (
                    record.summary.session_id == session_id
                    and record.summary.state not in _FINAL_STATES
                ):
                    self._clear_result_locked(record)
                    changed.append(
                        self._finish_locked(record, InteractionState.CANCELLED)
                    )
            self._condition.notify_all()
        for summary in changed:
            self._publish(EventType.INTERACTION_STATE_CHANGED, summary)

    def _clear_context_locked(self, context: _AskpassContext) -> None:
        context.closed = True
        for pending in context.pending_remember:
            pending.clear()
        context.pending_remember.clear()
        for name in (
            context.control_path,
            str(self._private_dir / f"k-{context.session_id[-12:]}"),
        ):
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    def close(self, timeout: float = 2.0) -> None:
        changed: list[InteractionSummary] = []
        with self._condition:
            if self._closed:
                return
            self._closed = True
            contexts = tuple(self._askpass_contexts.values())
            self._askpass_contexts.clear()
            for context in contexts:
                self._clear_context_locked(context)
            for record in self._records.values():
                if record.summary.state not in _FINAL_STATES:
                    self._clear_result_locked(record)
                    changed.append(
                        self._finish_locked(record, InteractionState.CANCELLED)
                    )
            transports = tuple(self._askpass_transports)
            self._condition.notify_all()
        try:
            self._askpass_listener.close()
        except OSError:
            pass
        for transport in transports:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                transport.close()
            except OSError:
                pass
        shutdown_deadline = monotonic() + max(0.0, timeout)
        for _worker in self._askpass_workers:
            while True:
                remaining = shutdown_deadline - monotonic()
                if remaining <= 0:
                    break
                try:
                    self._askpass_queue.put(None, timeout=min(0.05, remaining))
                    break
                except queue.Full:
                    continue
        for summary in changed:
            self._publish(EventType.INTERACTION_STATE_CHANGED, summary)
        if threading.current_thread() is not self._scheduler:
            self._scheduler.join(max(0.0, shutdown_deadline - monotonic()))
        for thread in (
            self._askpass_acceptor,
            self._auth_monitor,
            *self._askpass_workers,
        ):
            if threading.current_thread() is not thread:
                thread.join(max(0.0, shutdown_deadline - monotonic()))
        self._publisher.close()
        shutil.rmtree(self._private_dir, ignore_errors=True)

    def _write_helper_launcher(self) -> None:
        source = (
            f"#!{sys.executable}\n"
            "from sshpilot.daemon.askpass_helper import main\n"
            "raise SystemExit(main())\n"
        ).encode()
        descriptor = os.open(
            self._askpass_helper_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o700,
        )
        try:
            os.write(descriptor, source)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _askpass_accept_main(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
            try:
                transport, _address = self._askpass_listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self._same_user_peer(transport):
                transport.close()
                continue
            with self._condition:
                if self._closed:
                    transport.close()
                    return
                self._askpass_transports.add(transport)
            try:
                self._askpass_queue.put_nowait(transport)
            except queue.Full:
                with self._condition:
                    self._askpass_transports.discard(transport)
                transport.close()

    @staticmethod
    def _same_user_peer(transport: socket.socket) -> bool:
        if not hasattr(socket, "SO_PEERCRED") or not hasattr(os, "getuid"):
            return True
        try:
            credentials = transport.getsockopt(
                socket.SOL_SOCKET,
                socket.SO_PEERCRED,
                struct.calcsize("3i"),
            )
            _pid, uid, _gid = struct.unpack("3i", credentials)
            return uid == os.getuid()
        except OSError:
            return False

    def _askpass_worker_main(self) -> None:
        while True:
            transport = self._askpass_queue.get()
            if transport is None:
                return
            secret: Optional[bytearray] = None
            try:
                transport.settimeout(
                    max(self._secret_timeout, self._host_key_timeout) + 5
                )
                request_size = _ASKPASS_LENGTH.unpack(
                    self._receive_exact(transport, _ASKPASS_LENGTH.size)
                )[0]
                if request_size < 1 or request_size > _MAX_ASKPASS_REQUEST:
                    continue
                value = json.loads(
                    self._receive_exact(transport, request_size).decode("utf-8")
                )
                if type(value) is not dict or set(value) != {"token", "prompt"}:
                    continue
                token = value["token"]
                prompt = value["prompt"]
                if (
                    type(token) is not str
                    or type(prompt) is not str
                    or len(token) > 256
                    or len(prompt) > 4096
                ):
                    continue
                secret = self._resolve_askpass_secret(
                    token,
                    prompt,
                    helper_transport=transport,
                )
                if secret is None or not secret or len(secret) > _MAX_SECRET_SIZE:
                    continue
                transport.sendall(
                    _ASKPASS_LENGTH.pack(len(secret)) + bytes(secret)
                )
            except (
                EOFError,
                OSError,
                UnicodeError,
                ValueError,
                json.JSONDecodeError,
            ):
                pass
            finally:
                if secret is not None:
                    secret[:] = b"\0" * len(secret)
                    secret.clear()
                try:
                    transport.close()
                except OSError:
                    pass
                with self._condition:
                    self._askpass_transports.discard(transport)

    @staticmethod
    def _receive_exact(transport: socket.socket, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = transport.recv(size - len(chunks))
            if not chunk:
                raise EOFError
            chunks.extend(chunk)
        return bytes(chunks)

    def _resolve_askpass_secret(
        self,
        token: str,
        raw_prompt: str,
        *,
        helper_transport: Optional[socket.socket] = None,
    ) -> Optional[bytearray]:
        prompt_type = classify_prompt(raw_prompt)
        if prompt_type not in {"password", "passphrase"}:
            return None
        with self._condition:
            context = self._askpass_contexts.get(token)
            if context is None or context.closed or self._closed:
                return None
            key_path = self._passphrase_key(raw_prompt) if prompt_type == "passphrase" else ""
            attempt_key = f"{prompt_type}:{key_path}"
            attempt = context.attempts.get(attempt_key, 0) + 1
            context.attempts[attempt_key] = attempt
            if attempt > 3:
                return None
            try_stored = attempt_key not in context.stored_attempted
            context.stored_attempted.add(attempt_key)
            interaction_type = (
                InteractionType.PASSWORD
                if prompt_type == "password"
                else InteractionType.PRIVATE_KEY_PASSPHRASE
            )
            session_id = context.session_id
            connection_id = context.connection_id
            hostname = context.hostname
            username = context.username
            port = context.port
        stored: Optional[str] = None
        if try_stored:
            try:
                if interaction_type is InteractionType.PASSWORD:
                    if self._password_lookup is not None:
                        stored = self._password_lookup(connection_id)
                elif self._passphrase_lookup is not None and key_path:
                    stored = self._passphrase_lookup(key_path)
            except Exception:
                stored = None
        if stored:
            encoded = stored.encode("utf-8")
            if b"\0" not in encoded and len(encoded) <= _MAX_SECRET_SIZE:
                return bytearray(encoded)
        if interaction_type is InteractionType.PASSWORD:
            public_prompt: InteractionPrompt = PasswordPrompt(
                username=username or "unknown",
                hostname=hostname,
                port=port,
                attempt=attempt,
                can_remember=self._password_store is not None,
                stored_secret_available=bool(stored),
            )
        else:
            public_prompt = PassphrasePrompt(
                key_display_name=Path(key_path).name if key_path else "SSH key",
                key_fingerprint=None,
                attempt=attempt,
                can_remember=self._passphrase_store is not None and bool(key_path),
                stored_secret_available=bool(stored),
            )
        interaction = self.create(
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=interaction_type,
            prompt=public_prompt,
            attempt=attempt,
        )
        result = self.wait_for_result(
            interaction.id,
            cancel_check=(
                None
                if helper_transport is None
                else lambda: self._transport_is_closed(helper_transport)
            ),
        )
        if (
            result is None
            or result.decision is not SecretDecision.SUBMIT
            or result.secret is None
        ):
            if result is not None:
                result.clear()
            return None
        secret = result.secret
        result.secret = None
        if result.remember_policy in {
            RememberPolicy.STORE_AFTER_SUCCESS,
            RememberPolicy.REPLACE_STORED_AFTER_SUCCESS,
        }:
            with self._condition:
                current = self._askpass_contexts.get(token)
                if current is not None and not current.closed:
                    # Retries (wrong password then correct) must not keep the
                    # failed secret queued for remember-after-success.
                    if interaction_type is InteractionType.PASSWORD:
                        retained = []
                        for pending in current.pending_remember:
                            if pending.interaction_type is InteractionType.PASSWORD:
                                pending.clear()
                            else:
                                retained.append(pending)
                        current.pending_remember.clear()
                        current.pending_remember.extend(retained)
                    current.pending_remember.append(
                        _PendingRemember(
                            interaction_type=interaction_type,
                            key=key_path,
                            secret=bytearray(secret),
                        )
                    )
        result.clear()
        return secret

    @staticmethod
    def _passphrase_key(raw_prompt: str) -> str:
        match = _KEY_PATH_PATTERN.search(raw_prompt)
        if match is None:
            return ""
        value = match.group(1)
        if len(value) > 4096 or "\0" in value:
            return ""
        return value

    def _auth_monitor_main(self) -> None:
        while True:
            with self._condition:
                if self._closed:
                    return
                candidates = tuple(
                    context
                    for context in self._askpass_contexts.values()
                    if not context.closed and context.pending_remember
                )
                if not candidates:
                    self._condition.wait(0.2)
                    continue
            for context in candidates:
                if not os.path.exists(context.control_path):
                    continue
                try:
                    result = subprocess.run(
                        context.control_argv,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=1,
                        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
                    )
                except (OSError, subprocess.SubprocessError):
                    continue
                if result.returncode == 0:
                    self._store_authenticated_secrets(context.token)
            with self._condition:
                if not self._closed:
                    self._condition.wait(0.1)

    def _store_authenticated_secrets(self, token: str) -> None:
        with self._condition:
            context = self._askpass_contexts.get(token)
            if context is None or context.closed:
                return
            pending = tuple(context.pending_remember)
            context.pending_remember.clear()
            connection_id = context.connection_id
        for item in pending:
            try:
                value = item.secret.decode("utf-8")
                stored = False
                if item.interaction_type is InteractionType.PASSWORD:
                    if self._password_store is not None:
                        stored = bool(self._password_store(connection_id, value))
                elif self._passphrase_store is not None and item.key:
                    stored = bool(self._passphrase_store(item.key, value))
                if not stored:
                    raise RuntimeError("credential storage did not commit")
            except Exception:
                logger.warning(
                    "Remembered SSH credential could not be stored type=%s",
                    item.interaction_type.value,
                )
            finally:
                item.clear()

    def _scheduler_main(self) -> None:
        while True:
            expired: list[InteractionSummary] = []
            with self._condition:
                if self._closed:
                    return
                now = monotonic()
                deadlines: list[float] = []
                for record in self._records.values():
                    if record.summary.state in _FINAL_STATES:
                        continue
                    if record.deadline <= now:
                        self._clear_result_locked(record)
                        expired.append(
                            self._finish_locked(record, InteractionState.EXPIRED)
                        )
                    else:
                        deadlines.append(record.deadline)
                if not expired:
                    wait = None if not deadlines else max(0.0, min(deadlines) - now)
                    self._condition.wait(wait)
                    continue
                self._condition.notify_all()
            for summary in expired:
                self._publish(EventType.INTERACTION_STATE_CHANGED, summary)

    def _finish_locked(
        self,
        record: _InteractionRecord,
        state: InteractionState,
    ) -> InteractionSummary:
        if record.summary.state in _FINAL_STATES:
            return record.summary
        record.awaiting_secret = False
        record.claim_nonce = None
        summary = self._replace_summary(record, state=state)
        self._completed.append(summary.id)
        self._evict_completed_locked()
        return summary

    def _evict_completed_locked(self) -> None:
        while len(self._completed) > self._completed_limit:
            interaction_id = self._completed.popleft()
            record = self._records.get(interaction_id)
            if record is None or record.summary.state not in _FINAL_STATES:
                continue
            self._clear_result_locked(record)
            del self._records[interaction_id]

    @staticmethod
    def _clear_result_locked(record: _InteractionRecord) -> None:
        if record.result is not None:
            record.result.clear()
            record.result = None
        record.result_taken = False
        record.awaiting_secret = False

    @staticmethod
    def _replace_summary(
        record: _InteractionRecord,
        *,
        state: InteractionState,
        responder_client_id: Optional[ClientId] = None,
    ) -> InteractionSummary:
        old = record.summary
        record.summary = InteractionSummary(
            id=old.id,
            session_id=old.session_id,
            connection_id=old.connection_id,
            type=old.type,
            state=state,
            created_at=old.created_at,
            expires_at=old.expires_at,
            attempt=old.attempt,
            prompt=old.prompt,
            responder_client_id=responder_client_id,
        )
        return record.summary

    def _publish(self, event_type: EventType, summary: InteractionSummary) -> None:
        self._publisher.publish(
            event_type,
            summary,
            connection_id=summary.connection_id,
            session_id=summary.session_id,
        )

    def _visible_locked(
        self,
        record: _InteractionRecord,
        client_id: ClientId,
    ) -> bool:
        return self._client_is_eligible(record.summary.session_id, client_id)

    def _require_visible_locked(
        self,
        record: _InteractionRecord,
        client_id: ClientId,
    ) -> None:
        if not self._visible_locked(record, client_id):
            raise self._unauthorised(record)

    def _require_responder_locked(
        self,
        record: _InteractionRecord,
        client_id: ClientId,
    ) -> None:
        self._require_visible_locked(record, client_id)
        if record.summary.responder_client_id != client_id:
            raise self._unauthorised(record)

    @staticmethod
    def _require_pending_locked(record: _InteractionRecord) -> None:
        if record.summary.state in _FINAL_STATES:
            code = (
                ErrorCode.INTERACTION_EXPIRED
                if record.summary.state is InteractionState.EXPIRED
                else ErrorCode.INTERACTION_ALREADY_ANSWERED
            )
            raise InteractionBroker._error(
                code,
                "The interaction is no longer pending",
                record,
            )

    def _record_locked(self, interaction_id: InteractionId) -> _InteractionRecord:
        record = self._records.get(interaction_id)
        if record is None:
            raise SshPilotError(
                ErrorCode.INTERACTION_NOT_FOUND,
                "The interaction was not found",
                details={"interaction_id": interaction_id},
            )
        return record

    def _require_open_locked(self) -> None:
        if self._closed:
            raise SshPilotError(
                ErrorCode.DAEMON_SHUTTING_DOWN,
                "The interaction broker is shutting down",
                retryable=True,
            )

    @staticmethod
    def _error(
        code: ErrorCode,
        message: str,
        record: _InteractionRecord,
        *,
        retryable: bool = False,
    ) -> SshPilotError:
        return SshPilotError(
            code,
            message,
            details={"interaction_id": record.summary.id},
            retryable=retryable,
            connection_id=record.summary.connection_id,
            session_id=record.summary.session_id,
        )

    @staticmethod
    def _unauthorised(record: _InteractionRecord) -> SshPilotError:
        return InteractionBroker._error(
            ErrorCode.INTERACTION_RESPONDER_UNAUTHORIZED,
            "This client is not authorised to respond",
            record,
        )

    @staticmethod
    def _invalid_decision(record: _InteractionRecord) -> SshPilotError:
        return InteractionBroker._error(
            ErrorCode.INVALID_REQUEST,
            "The interaction decision does not match its type",
            record,
        )

    def __enter__(self) -> InteractionBroker:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def active_ids(self) -> Iterable[InteractionId]:
        with self._condition:
            return tuple(
                identifier
                for identifier, record in self._records.items()
                if record.summary.state not in _FINAL_STATES
            )
