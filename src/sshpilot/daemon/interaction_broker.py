"""Daemon-owned typed interaction state and one-use secret delivery."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import secrets
import shutil
import socket
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
from typing import Callable, Deque, Dict, Iterable, List, Mapping, Optional, Sequence

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.events import (
    CoreEventCallback,
    EventPublisher,
    EventType,
    Subscription,
)
from sshpilot.api.interaction_identity import new_interaction_id
from sshpilot.api.models import (
    ChallengePrompt,
    ConfirmationPrompt,
    ClientId,
    ConnectionId,
    ExecutionInteractionMode,
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
    PresencePrompt,
    RememberPolicy,
    SecretDecision,
    SessionId,
)
from sshpilot.api.transport.secret_frames import SecretFrame

logger = logging.getLogger(__name__)
from sshpilot.askpass_utils import (
    _extract_key_path,
    append_askpass_log,
    classify_prompt,
)
from sshpilot.daemon.session_runtime import SessionLaunchSpec
from sshpilot.logging_support import log_context

DEFAULT_SECRET_INTERACTION_TIMEOUT = 120.0
DEFAULT_HOST_KEY_INTERACTION_TIMEOUT = 180.0
DEFAULT_PRESENCE_INTERACTION_TIMEOUT = 600.0
# Backup export/import encryption passphrase prompts are a short, self-contained
# step in an otherwise fast local operation — 120s (tuned for SSH auth retries)
# leaves the UI looking hung far longer than the prompt itself warrants.
DEFAULT_BACKUP_ENCRYPTION_INTERACTION_TIMEOUT = 30.0
DEFAULT_COMPLETED_INTERACTION_LIMIT = 100
DEFAULT_ASKPASS_WORKERS = 4
DEFAULT_ASKPASS_QUEUE_LIMIT = 32
_ASKPASS_LENGTH = struct.Struct(">I")
_MAX_ASKPASS_REQUEST = 8 * 1024
_MAX_SECRET_SIZE = 16 * 1024

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
        InteractionType.KEYBOARD_INTERACTIVE,
        InteractionType.CONFIRMATION,
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
    user_known_hosts_paths: tuple[Path, ...]
    allow_stored_secrets: bool = True
    confirm_passphrase: bool = False
    interaction_mode: ExecutionInteractionMode = ExecutionInteractionMode.INTERACTIVE
    confirmation_secret: Optional[bytearray] = None
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
        presence_timeout: float = DEFAULT_PRESENCE_INTERACTION_TIMEOUT,
        completed_limit: int = DEFAULT_COMPLETED_INTERACTION_LIMIT,
        password_lookup: Optional[Callable[[ConnectionId], Optional[str]]] = None,
        password_store: Optional[Callable[[ConnectionId, str], bool]] = None,
        passphrase_lookup: Optional[Callable[[str], Optional[str]]] = None,
        passphrase_store: Optional[Callable[[str, str], bool]] = None,
        askpass_workers: int = DEFAULT_ASKPASS_WORKERS,
        askpass_queue_limit: int = DEFAULT_ASKPASS_QUEUE_LIMIT,
    ) -> None:
        if secret_timeout <= 0 or host_key_timeout <= 0 or presence_timeout <= 0:
            raise ValueError("interaction timeouts must be positive")
        if completed_limit < 1:
            raise ValueError("completed interaction limit must be positive")
        if askpass_workers < 1 or askpass_queue_limit < 1:
            raise ValueError("askpass worker and queue limits must be positive")
        self._client_is_eligible = client_is_eligible or (lambda _session, _client: True)
        self._secret_timeout = float(secret_timeout)
        self._host_key_timeout = float(host_key_timeout)
        self._presence_timeout = float(presence_timeout)
        self._completed_limit = completed_limit
        self._password_lookup = password_lookup
        self._password_store = password_store
        self._passphrase_lookup = passphrase_lookup
        self._passphrase_store = passphrase_store
        self._condition = threading.Condition(threading.RLock())
        self._records: Dict[InteractionId, _InteractionRecord] = {}
        self._completed: Deque[InteractionId] = deque()
        self._direct_scope_owners: Dict[SessionId, ClientId] = {}
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
        self._publisher = EventPublisher()
        self._scheduler = threading.Thread(
            target=self._scheduler_main,
            name="sshpilot-interaction-deadlines",
            daemon=False,
        )
        for worker in self._askpass_workers:
            worker.start()
        self._askpass_acceptor.start()
        self._scheduler.start()

    def subscribe_events(self, callback: CoreEventCallback) -> Subscription:
        return self._publisher.subscribe(callback)

    def client_owns_direct_scope(
        self,
        session_id: SessionId,
        client_id: ClientId,
    ) -> bool:
        """Return whether *client_id* owns a direct protected interaction."""
        with self._condition:
            return self._direct_scope_owners.get(session_id) == client_id

    def prepare_launch(
        self,
        spec: SessionLaunchSpec,
        launch_builder: Callable[..., tuple[Sequence[str], dict[str, str]]],
        *,
        trailing_args: Sequence[str] = (),
        headless: bool = False,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Prepare canonical SSH argv, OpenSSH trust, and daemon askpass.

        Host-key verification is left to the real OpenSSH child. Unknown hosts
        surface as askpass prompts and are routed through
        ``HOST_KEY_CONFIRMATION``. There is no ``ssh-keyscan`` preflight on the
        connect critical path.

        ``trailing_args`` are appended after the target host once broker
        options have been inserted (e.g. ``("sftp",)`` for the SFTP subsystem
        request) — the host must stay ``argv[-1]`` while broker options are
        inserted.
        """

        kwargs = {"interaction_policy": "normal"}
        if spec.remote_command is not None:
            kwargs["remote_command"] = spec.remote_command
        if spec.force_tty:
            kwargs["force_tty"] = True
        argv_value, environment_value = launch_builder(
            spec.connection_id,
            **kwargs,
        )
        argv = tuple(argv_value)
        # Keep the normal OpenSSH child environment.  Replace only SSH Pilot's
        # private, process-local askpass transport below.
        environment = dict(environment_value)
        if not argv or len(argv) < 2:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH launch command is invalid",
                connection_id=spec.connection_id,
                session_id=spec.session_id,
            )
        target = argv[-1]
        effective = self._effective_ssh_config(argv, environment)
        hostname = effective.get("hostname", spec.hostname)
        username = effective.get("user", spec.username)
        try:
            port = int(effective.get("port", spec.port))
        except (TypeError, ValueError):
            port = spec.port
        token = secrets.token_urlsafe(32)
        # OpenSSH and its effective configuration own host-key persistence.
        context = _AskpassContext(
            token=token,
            session_id=spec.session_id,
            connection_id=spec.connection_id,
            hostname=hostname,
            username=username,
            port=port,
            control_path="",
            control_target=target,
            control_argv=(),
            attempts={},
            stored_attempted=set(),
            pending_remember=[],
            user_known_hosts_paths=self._known_hosts_paths(effective),
        )
        with self._condition:
            self._require_open_locked()
            self._askpass_contexts[token] = context
            self._condition.notify_all()
        environment.pop("SSHPILOT_DAEMON_ASKPASS_ACTIVE", None)
        for name in tuple(environment):
            if (
                name in {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
                or name.startswith("SSHPILOT_ASKPASS_")
                or name.startswith("SSHPILOT_SESSION_")
                or name.startswith("SSHPILOT_DAEMON_ASKPASS_")

            ):
                environment.pop(name, None)
        # Every daemon-owned SSH child must have a broker. Even when the auth
        # resolver found no stored secret, OpenSSH can still need an unknown
        # host decision, an unstored password/passphrase, MFA/PIN, or hardware
        # presence. The daemon PTY has no frontend able to answer those prompts.
        askpass_active = True
        if askpass_active:
            environment["SSH_ASKPASS"] = str(self._askpass_helper_path)
            environment["SSH_ASKPASS_REQUIRE"] = "force" if headless else "prefer"
            environment["DISPLAY"] = environment.get("DISPLAY") or ":sshpilot-daemon"
            environment["SSHPILOT_DAEMON_ASKPASS_SOCKET"] = str(
                self._askpass_socket_path
            )
            environment["SSHPILOT_DAEMON_ASKPASS_TOKEN"] = token
        append_askpass_log(
            f"ASKPASS: daemon broker ready for {username}@{hostname}:{port} "
            f"session={spec.session_id}"
        )
        if trailing_args:
            argv = (*argv, *trailing_args)
        return argv, environment

    def prepare_operation_launch(
        self,
        argv: Sequence[str],
        environment: Mapping[str, str],
        *,
        scope_id: SessionId,
        connection_id: Optional[ConnectionId],
        hostname: str = "",
        username: str = "",
        port: int = 22,
        allow_stored_secrets: bool = True,
        confirm_passphrase: bool = False,
        interaction_mode: ExecutionInteractionMode = ExecutionInteractionMode.INTERACTIVE,
    ) -> tuple[tuple[str, ...], dict[str, str]]:
        """Broker askpass environment for a non-session OpenSSH launch.

        Long-running daemon operations (``ssh-copy-id`` deployment, remote
        authorized-key mutations, agent key loading) run OpenSSH children
        that must surface password/passphrase/host-key prompts as typed
        interactions even though no terminal session exists.  The returned
        environment is *environment* with SSH Pilot's private askpass
        transport replaced by the broker's, exactly like
        :meth:`prepare_launch`.  The caller owns context cleanup via
        ``cancel_session(scope_id)`` once the child exits.
        """

        argv = tuple(argv)
        if not argv:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The operation launch command is invalid",
            )
        if not isinstance(interaction_mode, ExecutionInteractionMode):
            raise TypeError("interaction_mode must be an ExecutionInteractionMode")
        env = dict(environment)
        if not hostname:
            # Headless operation callers (ssh-copy-id, ssh-add, remote
            # commands) do not always know the connection's display host; the
            # launch target (``argv[-1]``) carries it. Deriving safe display
            # metadata here keeps every typed prompt (password, passphrase,
            # host-key) constructible for every headless operation — a
            # PASSWORD prompt with an empty hostname would fail model
            # validation and the askpass worker would silently drop it.
            target_hostname, target_username = self._target_identity(
                str(argv[-1])
            )
            hostname = target_hostname
            if target_username and not username:
                username = target_username
        token = secrets.token_urlsafe(32)
        context = _AskpassContext(
            token=token,
            session_id=scope_id,
            connection_id=connection_id or ConnectionId("identity-agent"),
            hostname=hostname,
            username=username,
            port=port,
            control_path="",
            control_target=str(argv[-1]),
            control_argv=(),
            attempts={},
            stored_attempted=set(),
            pending_remember=[],
            user_known_hosts_paths=(),
            allow_stored_secrets=allow_stored_secrets,
            confirm_passphrase=confirm_passphrase,
            interaction_mode=interaction_mode,
        )
        with self._condition:
            self._require_open_locked()
            self._askpass_contexts[token] = context
            self._condition.notify_all()
        env.pop("SSHPILOT_DAEMON_ASKPASS_ACTIVE", None)
        for name in tuple(env):
            if (
                name in {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
                or name.startswith("SSHPILOT_ASKPASS_")
                or name.startswith("SSHPILOT_SESSION_")
                or name.startswith("SSHPILOT_DAEMON_ASKPASS_")
            ):
                env.pop(name, None)
        # Headless OpenSSH children have no TTY: every prompt must reach the
        # broker (REQUIRE=force), never fall back to a missing terminal.
        env["SSH_ASKPASS"] = str(self._askpass_helper_path)
        env["SSH_ASKPASS_REQUIRE"] = "force"
        env["DISPLAY"] = env.get("DISPLAY") or ":sshpilot-daemon"
        env["SSHPILOT_DAEMON_ASKPASS_SOCKET"] = str(self._askpass_socket_path)
        env["SSHPILOT_DAEMON_ASKPASS_TOKEN"] = token
        append_askpass_log(
            f"ASKPASS: daemon broker ready for operation scope={scope_id}"
        )
        return argv, env

    @staticmethod
    def _target_identity(target: str) -> tuple[str, str]:
        """Best-effort ``(hostname, username)`` from a launch target.

        Headless operation callers pass the OpenSSH target as ``argv[-1]``
        (e.g. ``alice@example.com`` for ssh-copy-id). When the caller supplies
        no explicit hostname/username, this derives safe display values from
        the target so typed prompts can be constructed. The target may be
        ``user@host[:port]``, ``host[:port]``, an IPv6 bracket form, a remote
        command, or a local path — the result is sanitized to a non-empty
        display string, never validated OpenSSH config.
        """

        raw = str(target or "").split("/", 1)[0]
        if "@" in raw:
            user, _, host_part = raw.rpartition("@")
        else:
            user = ""
            host_part = raw
        if host_part.startswith("[") and "]" in host_part:
            hostname = host_part[1:].split("]", 1)[0]
        elif host_part.count(":") == 1 and host_part.rsplit(":", 1)[1].isdigit():
            hostname = host_part.rsplit(":", 1)[0]
        else:
            hostname = host_part
        cleaned = "".join(
            char for char in hostname if char == "\t" or ord(char) >= 0x20
        ).strip()
        if not cleaned or len(cleaned) > 255:
            # Last resort: a fixed safe display value. An empty or over-long
            # value would fail prompt-model validation and the askpass worker
            # would silently drop the interaction again.
            return "unknown", user
        return cleaned, user

    @staticmethod
    def _strict_host_key_mode(
        argv: Sequence[str],
        effective: dict[str, str],
    ) -> str:
        """Resolve StrictHostKeyChecking; default matches Preferences (accept-new).

        OpenSSH keeps the first obtained value for each option, so scan argv
        left-to-right and stop at the first StrictHostKeyChecking=.
        """

        index = 0
        end = len(argv) - 1
        found: Optional[str] = None
        while index < end and found is None:
            argument = argv[index]
            if argument == "-o" and index + 1 < end:
                value = argv[index + 1]
                if value.startswith("StrictHostKeyChecking="):
                    found = value.split("=", 1)[1]
                index += 2
                continue
            if argument.startswith("-o") and len(argument) > 2:
                value = argument[2:]
                if value.startswith("StrictHostKeyChecking="):
                    found = value.split("=", 1)[1]
            index += 1
        raw = found or effective.get("stricthostkeychecking") or "accept-new"
        mode = str(raw).strip().lower()
        if mode == "off":
            return "no"
        if mode in {"accept-new", "yes", "no", "ask"}:
            return mode
        return "accept-new"

    @staticmethod
    def _broker_option_prefixes(options: Sequence[str]) -> tuple[str, ...]:
        """Option name prefixes we insert — only those are stripped from argv."""

        prefixes: list[str] = []
        index = 0
        while index < len(options):
            if options[index] == "-o" and index + 1 < len(options):
                value = options[index + 1]
                if "=" in value:
                    prefixes.append(value.split("=", 1)[0] + "=")
                index += 2
                continue
            index += 1
        return tuple(prefixes)

    @classmethod
    def _with_broker_options(
        cls,
        argv: Sequence[str],
        options: Sequence[str],
    ) -> tuple[str, ...]:
        if len(argv) < 2:
            raise ValueError("SSH launch argv is incomplete")
        prefixes = cls._broker_option_prefixes(options)
        target = argv[-1]
        head: list[str] = []
        index = 0
        end = len(argv) - 1
        while index < end:
            argument = argv[index]
            if argument == "-o" and index + 1 < end:
                value = argv[index + 1]
                if any(value.startswith(prefix) for prefix in prefixes):
                    index += 2
                    continue
                head.extend((argument, value))
                index += 2
                continue
            if argument.startswith("-o") and len(argument) > 2:
                value = argument[2:]
                if any(value.startswith(prefix) for prefix in prefixes):
                    index += 1
                    continue
            head.append(argument)
            index += 1
        insert_at = 1
        if len(head) >= 3 and head[1] == "-F":
            insert_at = 3
        return tuple((*head[:insert_at], *options, *head[insert_at:], target))

    def _effective_ssh_config(
        self,
        argv: Sequence[str],
        environment: Optional[dict[str, str]] = None,
    ) -> dict[str, str]:
        target = argv[-1]
        # ``ssh … -s <host> sftp`` (and any pre-host ``-s``) must not be fed to
        # ``ssh -G`` — OpenSSH treats ``-s`` as a subsystem request and the
        # probe can hang instead of dumping config.
        probe = tuple(argument for argument in argv[:-1] if argument != "-s")
        try:
            # Probe with the same sanitized environment as the SSH child.
            # Only the broker-private askpass variables are added later.
            probe_env = dict(environment or os.environ)
            completed = subprocess.run(
                (*probe, "-G", target),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
                text=True,
                env=probe_env,
            )
        except (OSError, subprocess.SubprocessError):
            return {}
        result: dict[str, str] = {}
        for line in completed.stdout.splitlines():
            key, separator, value = line.partition(" ")
            if separator and key and key not in result:
                result[key.lower()] = value.strip()
        return result

    @staticmethod
    def _parse_host_key_askpass_prompt(
        raw_prompt: str,
        *,
        hostname: str,
        port: int,
    ) -> Optional[HostKeyPrompt]:
        """Build a typed host-key prompt from OpenSSH askpass text."""

        fingerprint_match = re.search(r"(SHA256:[A-Za-z0-9+/=]+)", raw_prompt)
        if fingerprint_match is None:
            return None
        fingerprint = fingerprint_match.group(1)
        if len(fingerprint) > 128:
            return None
        type_match = re.search(
            r"\b(ED25519|ECDSA|RSA|DSA)\b(?:\s+key)?\s+fingerprint",
            raw_prompt,
            flags=re.IGNORECASE,
        )
        raw_type = (type_match.group(1) if type_match else "UNKNOWN").upper()
        key_type = {
            "ED25519": "ssh-ed25519",
            "ECDSA": "ecdsa-sha2-nistp256",
            "RSA": "ssh-rsa",
            "DSA": "ssh-dss",
        }.get(raw_type, raw_type.lower())
        upper = raw_prompt.upper()
        if (
            "IDENTIFICATION HAS CHANGED" in upper
            or "HOST KEY VERIFICATION FAILED" in upper
            or "OFFENDING" in upper
        ):
            status = HostKeyStatus.CHANGED
        else:
            status = HostKeyStatus.UNKNOWN
        host = hostname
        host_match = re.search(
            r"authenticity of host ['\[]([^'\]\s]+)",
            raw_prompt,
            flags=re.IGNORECASE,
        )
        if host_match is not None:
            host = host_match.group(1).strip("[]")
        try:
            return HostKeyPrompt(
                hostname=host or hostname or "unknown",
                port=port,
                key_type=key_type,
                fingerprint=fingerprint,
                status=status,
            )
        except (TypeError, ValueError):
            return None

    def _resolve_host_key_askpass(
        self,
        token: str,
        raw_prompt: str,
        *,
        helper_transport: Optional[socket.socket] = None,
    ) -> Optional[bytearray]:
        """Answer OpenSSH host-key askpass via HOST_KEY_CONFIRMATION."""

        with self._condition:
            context = self._askpass_contexts.get(token)
            if context is None or context.closed or self._closed:
                return None
            session_id = context.session_id
            connection_id = context.connection_id
            hostname = context.hostname
            port = context.port
            if context.interaction_mode is ExecutionInteractionMode.AUTOFILL_ONLY:
                return None
        prompt = self._parse_host_key_askpass_prompt(
            raw_prompt,
            hostname=hostname,
            port=port,
        )
        if prompt is None:
            logger.warning(
                "askpass host-key prompt could not be parsed prompt=%r",
                raw_prompt[:200],
            )
            append_askpass_log(
                f"ASKPASS: host-key prompt unparsed prompt={raw_prompt[:200]!r}"
            )
            return None
        append_askpass_log(
            f"ASKPASS: host-key confirmation for {hostname}:{port} "
            f"status={getattr(prompt.status, 'name', prompt.status)}"
        )
        interaction = self.create(
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=InteractionType.HOST_KEY_CONFIRMATION,
            prompt=prompt,
        )
        result = self.wait_for_result(
            interaction.id,
            cancel_check=(
                None
                if helper_transport is None
                else lambda: self._transport_is_closed(helper_transport)
            ),
        )
        if result is None or not isinstance(result.decision, HostKeyDecision):
            append_askpass_log("ASKPASS: host-key confirmation cancelled or failed")
            if result is not None:
                result.clear()
            return None
        decision = result.decision
        result.clear()
        if decision is HostKeyDecision.REJECT:
            append_askpass_log("ASKPASS: host-key rejected by user")
            return None
        # CHANGED keys: dialog only offers Reject; refuse accept paths defensively.
        if prompt.status is not HostKeyStatus.UNKNOWN:
            append_askpass_log("ASKPASS: host-key accept refused (non-UNKNOWN status)")
            return None
        append_askpass_log("ASKPASS: host-key accepted")
        # OpenSSH askpass expects a yes/no line; helper appends the newline.
        return bytearray(b"yes")

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
                else self._presence_timeout
                if interaction_type is InteractionType.SECURITY_KEY_PRESENCE
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
        with log_context(
            interaction=summary.id,
            session=summary.session_id,
            connection=summary.connection_id,
        ):
            logger.info("interaction created type=%s", summary.type.value)
        return summary

    def request_client_secret(
        self,
        *,
        owner_client_id: ClientId,
        session_id: SessionId,
        connection_id: ConnectionId,
        interaction_type: InteractionType,
        prompt: InteractionPrompt,
        timeout: Optional[float] = None,
    ) -> Optional[bytearray]:
        """Collect one secret for a daemon-owned non-process operation."""
        result, _state = self._request_client_secret_result(
            owner_client_id=owner_client_id,
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=interaction_type,
            prompt=prompt,
            timeout=timeout,
        )
        return None if result is None else result.secret

    def request_client_secret_with_remember(
        self,
        *,
        owner_client_id: ClientId,
        session_id: SessionId,
        connection_id: ConnectionId,
        interaction_type: InteractionType,
        prompt: InteractionPrompt,
        timeout: Optional[float] = None,
    ) -> tuple[Optional[bytearray], RememberPolicy]:
        """Like :meth:`request_client_secret`, but also reports the
        ``remember_policy`` the client attached to its response (e.g. a
        "remember this" checkbox) — needed by callers that offer to persist
        the secret without a second prompt."""
        result, _state = self._request_client_secret_result(
            owner_client_id=owner_client_id,
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=interaction_type,
            prompt=prompt,
            timeout=timeout,
        )
        if result is None:
            return None, RememberPolicy.DO_NOT_STORE
        return result.secret, result.remember_policy

    def request_client_secret_with_status(
        self,
        *,
        owner_client_id: ClientId,
        session_id: SessionId,
        connection_id: ConnectionId,
        interaction_type: InteractionType,
        prompt: InteractionPrompt,
        timeout: Optional[float] = None,
    ) -> tuple[Optional[bytearray], InteractionState]:
        """Like :meth:`request_client_secret`, but also reports the
        interaction's final :class:`InteractionState`. Callers that need to
        tell an explicit user cancellation apart from a timed-out prompt
        (rather than collapsing both into a bare ``None``) should use this
        instead of :meth:`request_client_secret`."""
        result, state = self._request_client_secret_result(
            owner_client_id=owner_client_id,
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=interaction_type,
            prompt=prompt,
            timeout=timeout,
        )
        return (None if result is None else result.secret), state

    def _request_client_secret_result(
        self,
        *,
        owner_client_id: ClientId,
        session_id: SessionId,
        connection_id: ConnectionId,
        interaction_type: InteractionType,
        prompt: InteractionPrompt,
        timeout: Optional[float] = None,
    ) -> tuple[Optional[InteractionResult], InteractionState]:
        """Run one direct-scope interaction and return its raw result plus
        its final :class:`InteractionState`.

        Registering ``owner_client_id`` as the session's direct scope owner
        (via ``_direct_scope_owners``) is what makes the server actually
        forward this interaction's events to that client — see
        ``DaemonServer._client_can_interact``. A session id with no
        registered owner and no recognized prefix (sftp-/forward-/
        operation-/transfer-/key-operation-) is invisible to every client, so
        skipping this registration (calling ``create()`` +
        ``wait_for_result()`` directly, as every secret-backend caller used
        to) means the interaction is created but nobody is ever notified —
        it silently expires at its timeout with no dialog ever shown.
        The caller owns clearing ``result.secret``.
        """
        with self._condition:
            self._require_open_locked()
            if session_id in self._direct_scope_owners:
                raise ValueError("protected interaction scope is already active")
            self._direct_scope_owners[session_id] = owner_client_id
        result: Optional[InteractionResult] = None
        state = InteractionState.CANCELLED
        try:
            summary = self.create(
                session_id=session_id,
                connection_id=connection_id,
                interaction_type=interaction_type,
                prompt=prompt,
                attempt=1,
                timeout=timeout,
            )
            result, state = self._wait_for_result_with_state(summary.id)
            if result is None or result.secret is None:
                return None, state
            return result, state
        finally:
            with self._condition:
                self._direct_scope_owners.pop(session_id, None)
            self.cancel_session(session_id)

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
            with log_context(
                interaction=changed.id,
                session=changed.session_id,
                connection=changed.connection_id,
                client=client_id,
            ):
                logger.info("interaction claimed")
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
                if (
                    record.summary.type is InteractionType.SECURITY_KEY_PRESENCE
                    and decision is SecretDecision.SUBMIT
                ):
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

    def classify_startup_failure(self, session_id: SessionId) -> ErrorCode:
        """Map a failed auth-gate wait to a stable error code."""

        with self._condition:
            for record in reversed(list(self._records.values())):
                if record.summary.session_id != session_id:
                    continue
                if record.summary.state is InteractionState.CANCELLED:
                    return ErrorCode.OPERATION_CANCELLED
                if record.summary.state is InteractionState.EXPIRED:
                    return ErrorCode.SESSION_STARTUP_FAILED
        return ErrorCode.SESSION_STARTUP_FAILED

    def mark_authenticated(self, session_id: SessionId) -> None:
        """Commit credentials explicitly marked for storage after login."""

        context = self._context_for_session(session_id)
        if context is not None:
            self._store_authenticated_secrets(context.token)
            self._harden_known_hosts_permissions(context)

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
    def _harden_known_hosts_permissions(context: _AskpassContext) -> None:
        """Keep OpenSSH-owned known_hosts files private after acceptance."""

        for path in context.user_known_hosts_paths:
            try:
                if path.is_file() and not path.is_symlink():
                    path.chmod(0o600)
            except OSError:
                logger.warning(
                    "Could not harden OpenSSH known_hosts permissions path=%s",
                    path,
                )

    def _context_for_session(self, session_id: SessionId) -> Optional[_AskpassContext]:
        with self._condition:
            for context in self._askpass_contexts.values():
                if context.session_id == session_id and not context.closed:
                    return context
        return None


    def wait_for_result(
        self,
        interaction_id: InteractionId,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> Optional[InteractionResult]:
        result, _state = self._wait_for_result_with_state(
            interaction_id, cancel_check=cancel_check
        )
        return result

    def _wait_for_result_with_state(
        self,
        interaction_id: InteractionId,
        *,
        cancel_check: Optional[Callable[[], bool]] = None,
    ) -> tuple[Optional[InteractionResult], InteractionState]:
        """Like :meth:`wait_for_result`, but also returns the interaction's
        final state so callers can distinguish ``CANCELLED`` from
        ``EXPIRED`` instead of both collapsing to ``None``."""
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
            final_state = record.summary.state
        if changed_summary is not None:
            self._publish(EventType.INTERACTION_STATE_CHANGED, changed_summary)
        return result, final_state

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
        direct_scopes: tuple[SessionId, ...] = ()
        with self._condition:
            direct_scopes = tuple(
                scope
                for scope, owner in self._direct_scope_owners.items()
                if owner == client_id
            )
            for scope in direct_scopes:
                self._direct_scope_owners.pop(scope, None)
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
        for scope in direct_scopes:
            self.cancel_session(scope)

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
        if context.confirmation_secret is not None:
            context.confirmation_secret[:] = b"\0" * len(
                context.confirmation_secret
            )
            context.confirmation_secret.clear()
            context.confirmation_secret = None
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
            self._direct_scope_owners.clear()
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
            *self._askpass_workers,
        ):
            if threading.current_thread() is not thread:
                thread.join(max(0.0, shutdown_deadline - monotonic()))
        self._publisher.close()
        shutil.rmtree(self._private_dir, ignore_errors=True)

    def _write_helper_launcher(self) -> None:
        if getattr(sys, "frozen", False):
            # sys.executable is this application's own binary in a frozen
            # (PyInstaller) build, not a Python interpreter: a shebang of
            # "#!{sys.executable}" followed by inlined Python source (the
            # non-frozen branch below) would make OpenSSH's exec of this
            # script just relaunch the GUI instead of running the askpass
            # helper. Route through /bin/sh so the interpreter-argument
            # splitting is portable, and re-invoke this binary with the
            # internal dispatch flag handled in run.py — the daemon's own
            # source tree is already inside the bundle, so nothing needs
            # inlining here.
            source = (
                "#!/bin/sh\n"
                f'exec "{sys.executable}" --internal-askpass "$@"\n'
            )
        else:
            # Inline the helper module so the OpenSSH askpass child does not
            # depend on PYTHONPATH or an installed sshpilot package (ssh may
            # spawn with a reduced environment; import failures look like
            # missing secrets).
            helper_source = Path(__file__).with_name("askpass_helper.py").read_text(
                encoding="utf-8"
            )
            lines = []
            skipping_main_guard = False
            for line in helper_source.splitlines():
                if line.startswith("if __name__"):
                    skipping_main_guard = True
                    continue
                if skipping_main_guard:
                    if line and not line.startswith((" ", "\t")):
                        skipping_main_guard = False
                    else:
                        continue
                lines.append(line)
            source = f"#!{sys.executable}\n" + "\n".join(lines) + "\nraise SystemExit(main())\n"
        descriptor = os.open(
            self._askpass_helper_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o700,
        )
        try:
            os.write(descriptor, source.encode())
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
                    max(
                        self._secret_timeout,
                        self._host_key_timeout,
                        self._presence_timeout,
                    )
                    + 5
                )
                request_size = _ASKPASS_LENGTH.unpack(
                    self._receive_exact(transport, _ASKPASS_LENGTH.size)
                )[0]
                if request_size < 1 or request_size > _MAX_ASKPASS_REQUEST:
                    continue
                value = json.loads(
                    self._receive_exact(transport, request_size).decode("utf-8")
                )
                if type(value) is not dict or set(value) not in (
                    {"token", "prompt"},
                    {"token", "prompt", "hint"},
                ):
                    continue
                token = value["token"]
                prompt = value["prompt"]
                hint = value.get("hint", "")
                if (
                    type(token) is not str
                    or type(prompt) is not str
                    or type(hint) is not str
                    or len(token) > 256
                    or len(prompt) > 4096
                ):
                    continue
                append_askpass_log(
                    f"ASKPASS called with prompt: {prompt[:200]!r} "
                    f"hint={(os.environ.get('SSH_ASKPASS_PROMPT') or '').strip().lower()!r}"
                )
                secret = self._resolve_askpass_secret(
                    token,
                    prompt,
                    hint=hint,
                    helper_transport=transport,
                )
                if secret is None or len(secret) > _MAX_SECRET_SIZE:
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
        hint: str = "",
        helper_transport: Optional[socket.socket] = None,
    ) -> Optional[bytearray]:
        normalized_hint = hint.strip().lower()
        prompt_type = (
            "presence" if normalized_hint == "none"
            else "confirmation" if normalized_hint == "confirm"
            else classify_prompt(raw_prompt)
        )
        if prompt_type == "hostkey":
            return self._resolve_host_key_askpass(
                token,
                raw_prompt,
                helper_transport=helper_transport,
            )
        if prompt_type == "presence":
            return self._resolve_nonstored_askpass(
                token, raw_prompt, presence=True, helper_transport=helper_transport
            )
        if prompt_type == "confirmation":
            return self._resolve_nonstored_askpass(
                token, raw_prompt, presence=False, confirmation=True,
                helper_transport=helper_transport,
            )
        if prompt_type not in {"password", "passphrase"}:
            # This deliberately includes unknown prompts: the in-process helper
            # treats them as keyboard-interactive because askpass invocation
            # under REQUIRE=prefer does not reliably fall back to the PTY.
            return self._resolve_nonstored_askpass(
                token, raw_prompt, presence=False, helper_transport=helper_transport
            )
        with self._condition:
            context = self._askpass_contexts.get(token)
            if context is None or context.closed or self._closed:
                return None
            if (
                prompt_type == "passphrase"
                and context.confirm_passphrase
                and context.confirmation_secret is not None
                and self._is_passphrase_confirmation_prompt(raw_prompt)
                and context.interaction_mode is ExecutionInteractionMode.INTERACTIVE
            ):
                secret = bytearray(context.confirmation_secret)
                context.confirmation_secret[:] = b"\0" * len(
                    context.confirmation_secret
                )
                context.confirmation_secret.clear()
                context.confirmation_secret = None
                return secret
            key_path = self._passphrase_key(raw_prompt) if prompt_type == "passphrase" else ""
            attempt_key = f"{prompt_type}:{key_path}"
            attempt = context.attempts.get(attempt_key, 0) + 1
            context.attempts[attempt_key] = attempt
            # Only skip stored autofill after we already *returned* a stored
            # secret for this prompt. A miss/exception must not burn the only
            # autofill chance — secrets may not be ready on the first askpass
            # call (daemon secret init), and OpenSSH will ask again.
            try_stored = (
                context.allow_stored_secrets
                and attempt_key not in context.stored_attempted
            )
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
            confirmation_required = context.confirm_passphrase
            interaction_mode = context.interaction_mode
        stored: Optional[str] = None
        if try_stored:
            try:
                if interaction_type is InteractionType.PASSWORD:
                    if self._password_lookup is not None:
                        stored = self._password_lookup(connection_id)
                elif self._passphrase_lookup is not None and key_path:
                    stored = self._passphrase_lookup(key_path)
                elif interaction_type is InteractionType.PRIVATE_KEY_PASSPHRASE:
                    logger.warning(
                        "askpass passphrase: lookup skipped prompt=%r key=%r "
                        "has_lookup=%s",
                        raw_prompt[:200],
                        key_path,
                        self._passphrase_lookup is not None,
                    )
            except Exception:
                logger.exception(
                    "askpass stored-secret lookup failed type=%s key=%r",
                    interaction_type,
                    key_path,
                )
                stored = None
            if interaction_type is InteractionType.PRIVATE_KEY_PASSPHRASE:
                logger.info(
                    "askpass passphrase: prompt=%r key=%s stored=%s lookup=%s "
                    "attempt=%s",
                    raw_prompt[:160],
                    key_path or "<none>",
                    bool(stored),
                    self._passphrase_lookup is not None,
                    attempt,
                )
                append_askpass_log(
                    f"ASKPASS: passphrase prompt key={key_path or '<none>'} "
                    f"stored={bool(stored)} lookup={self._passphrase_lookup is not None} "
                    f"attempt={attempt}"
                )
            elif interaction_type is InteractionType.PASSWORD:
                append_askpass_log(
                    f"ASKPASS: password prompt for {username}@{hostname} "
                    f"stored={bool(stored)} attempt={attempt}"
                )
        elif interaction_type is InteractionType.PRIVATE_KEY_PASSPHRASE:
            logger.info(
                "askpass skipped stored passphrase already_offered=true "
                "prompt=%r key=%s attempt=%s",
                raw_prompt[:160],
                key_path or "<none>",
                attempt,
            )
            append_askpass_log(
                f"ASKPASS: passphrase retry (stored already offered) "
                f"key={key_path or '<none>'} attempt={attempt}"
            )
        elif interaction_type is InteractionType.PASSWORD:
            append_askpass_log(
                f"ASKPASS: password retry for {username}@{hostname} attempt={attempt}"
            )
        if stored:
            encoded = stored.encode("utf-8")
            if b"\0" not in encoded and len(encoded) <= _MAX_SECRET_SIZE:
                with self._condition:
                    context = self._askpass_contexts.get(token)
                    if context is not None:
                        context.stored_attempted.add(attempt_key)
                if interaction_type is InteractionType.PASSWORD:
                    append_askpass_log(
                        f"ASKPASS: Returning stored password for {username}@{hostname}"
                    )
                else:
                    append_askpass_log(
                        f"ASKPASS: Returning stored passphrase for {key_path or '<none>'}"
                    )
                return bytearray(encoded)
        if interaction_mode is ExecutionInteractionMode.AUTOFILL_ONLY:
            append_askpass_log(
                f"ASKPASS: no stored {prompt_type}; passive operation declined interaction"
            )
            return None
        if interaction_type is InteractionType.PASSWORD:
            public_prompt: InteractionPrompt = PasswordPrompt(
                username=username or "unknown",
                hostname=hostname,
                port=port,
                attempt=attempt,
                can_remember=self._password_store is not None,
                stored_secret_available=bool(stored),
            )
            append_askpass_log(
                f"ASKPASS: no stored password for {username}@{hostname}; asking user"
            )
        else:
            public_prompt = PassphrasePrompt(
                key_display_name=Path(key_path).name if key_path else "SSH key",
                key_fingerprint=None,
                attempt=attempt,
                can_remember=self._passphrase_store is not None and bool(key_path),
                stored_secret_available=bool(stored),
                confirmation_required=confirmation_required,
            )
            append_askpass_log(
                f"ASKPASS: no stored passphrase for {key_path or '<none>'}; asking user"
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
            append_askpass_log("ASKPASS: user cancelled or failed secret prompt")
            if result is not None:
                result.clear()
            return None
        secret = result.secret
        result.secret = None
        if interaction_type is InteractionType.PRIVATE_KEY_PASSPHRASE:
            with self._condition:
                current = self._askpass_contexts.get(token)
                if (
                    current is not None
                    and not current.closed
                    and current.confirm_passphrase
                ):
                    if not secret:
                        result.clear()
                        secret.clear()
                        return None
                    if current.confirmation_secret is not None:
                        current.confirmation_secret[:] = b"\0" * len(
                            current.confirmation_secret
                        )
                        current.confirmation_secret.clear()
                    current.confirmation_secret = bytearray(secret)
        append_askpass_log("ASKPASS: Returning secret from user dialog")
        if result.remember_policy in {
            RememberPolicy.STORE_AFTER_SUCCESS,
            RememberPolicy.REPLACE_STORED_AFTER_SUCCESS,
        }:
            with self._condition:
                current = self._askpass_contexts.get(token)
                if current is not None and not current.closed:
                    # A repeated prompt proves the previous password failed.
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

    def _resolve_nonstored_askpass(
        self,
        token: str,
        raw_prompt: str,
        *,
        presence: bool,
        confirmation: bool = False,
        helper_transport: Optional[socket.socket],
    ) -> Optional[bytearray]:
        with self._condition:
            context = self._askpass_contexts.get(token)
            if context is None or context.closed or self._closed:
                return None
            if context.interaction_mode is ExecutionInteractionMode.AUTOFILL_ONLY:
                return None
            key = (
                "presence"
                if presence
                else "confirmation"
                if confirmation
                else "interactive"
            )
            attempt = context.attempts.get(key, 0) + 1
            context.attempts[key] = attempt
            interaction_type = (
                InteractionType.SECURITY_KEY_PRESENCE if presence
                else InteractionType.CONFIRMATION if confirmation
                else InteractionType.KEYBOARD_INTERACTIVE
            )
            prompt: InteractionPrompt = (
                PresencePrompt(text=raw_prompt.strip() or "Touch your security key")
                if presence
                else ConfirmationPrompt(
                    text=raw_prompt.strip() or "Confirm SSH operation"
                )
                if confirmation
                else ChallengePrompt(text=raw_prompt.strip() or "Authentication response", attempt=attempt)
            )
            session_id = context.session_id
            connection_id = context.connection_id
        interaction = self.create(
            session_id=session_id,
            connection_id=connection_id,
            interaction_type=interaction_type,
            prompt=prompt,
            attempt=attempt,
        )
        if presence:
            # Presence is a passive notification.  OpenSSH ends its askpass
            # helper when the hardware operation finishes; no answer frame is
            # required (or meaningful).
            self.wait_for_result(
                interaction.id,
                cancel_check=(
                    None
                    if helper_transport is None
                    else lambda: self._transport_is_closed(helper_transport)
                ),
            )
            return None
        result = self.wait_for_result(
            interaction.id,
            cancel_check=(
                None
                if helper_transport is None
                else lambda: self._transport_is_closed(helper_transport)
            ),
        )
        if result is None or result.decision is not SecretDecision.SUBMIT or result.secret is None:
            if result is not None:
                result.clear()
            return None
        secret = result.secret
        result.secret = None
        result.clear()
        return secret

    @staticmethod
    def _passphrase_key(raw_prompt: str) -> str:
        # Same quoted + unquoted OpenSSH forms as askpass_utils._extract_key_path
        # (e.g. "Enter passphrase for /home/u/.ssh/id_rsa: ").
        value = _extract_key_path(raw_prompt)
        if not value or len(value) > 4096 or "\0" in value:
            return ""
        return value

    @staticmethod
    def _is_passphrase_confirmation_prompt(raw_prompt: str) -> bool:
        value = (raw_prompt or "").strip().lower()
        return any(marker in value for marker in ("again", "same passphrase", "confirm"))

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
        with log_context(
            interaction=summary.id,
            session=summary.session_id,
            connection=summary.connection_id,
        ):
            logger.info("interaction state changed to=%s", state.value)
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
        direct_owner = self._direct_scope_owners.get(record.summary.session_id)
        return direct_owner == client_id if direct_owner is not None else (
            self._client_is_eligible(record.summary.session_id, client_id)
        )

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
