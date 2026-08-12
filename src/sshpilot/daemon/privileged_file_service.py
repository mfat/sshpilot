"""Daemon-owned privileged (sudo) remote file operations.

The privileged file path runs the canonical native SSH launch
(``ssh <alias> <remote_command>``) exactly like daemon authorized-key
operations, so the saved connection's SSH configuration (ProxyJump,
identities, ports, host-key policy) applies unchanged. The remote command is a
sudo read or write built daemon-side from a validated remote path — never a
client-supplied shell string.

Sudo passwords never cross the wire as DTOs or event payloads. The daemon
presents a protected ``PASSWORD`` interaction through the
:class:`~sshpilot.daemon.interaction_broker.InteractionBroker`, awaits the
one-use secret, feeds it directly to the child stdin, and clears it afterwards.
Stored sudo passwords reuse the existing per-connection secret schema
(``sudo_password_spec``); a stored password that proves wrong is cleared and
the daemon re-prompts through the broker.

``sudo tee`` writes through an existing file's inode, so ownership and mode of
an existing root-owned file are preserved without the daemon needing to know
them. Replacement is revision-safe: the daemon re-reads the authoritative
current content (through sudo), compares ``expected_revision``, and refuses to
write on ``FILE_REVISION_CONFLICT`` — the caller holds the per-target lock.

The sudo stderr contract is kept locale-stable by prefixing every remote
command with ``env LC_ALL=C``, so the password-required / wrong-password /
denied classification does not depend on the host's language. A single
``replace()`` (its re-read, backup, and write are separate SSH sessions) reuses
one in-memory sudo password so a password host prompts once per save, and a host
without usable sudo (``sudo: command not found``) fails fast with
``UNSUPPORTED_CAPABILITY`` instead of a misleading "missing file".
"""

from __future__ import annotations

import logging
import os
import selectors
import shlex
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import ConnectionId, SessionId
from sshpilot.api.models.interactions import (
    InteractionType,
    PasswordPrompt,
    RememberPolicy,
    SecretDecision,
)
from sshpilot.askpass_utils import (
    clear_sudo_password,
    is_sudo_denied_error,
    lookup_sudo_password,
    store_sudo_password,
)

logger = logging.getLogger(__name__)

REMOTE_COMMAND_TIMEOUT = 30.0
SSH_ERROR_EXIT = 255
MAX_SUDO_ATTEMPTS = 3
MAX_READ_BYTES = 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024

_SUDO_READ_HEAD = "cat"
_SUDO_WRITE_HEAD = "tee"

_PASSWORD_REQUIRED_MARKERS = (
    "a password is required",
    "password required",
)
_WRONG_PASSWORD_MARKERS = (
    "sorry, try again",
    "incorrect password",
)
_NOT_FOUND_MARKERS = (
    "no such file",
    "not found",
)
_SUDO_UNAVAILABLE_MARKERS = (
    "sudo: command not found",
    "sudo: not found",
    "must have a tty to run sudo",
)


class _SudoPasswordContext:
    """In-memory sudo password reuse scoped to one ``replace()`` call.

    A single save runs up to three sudo invocations (the authoritative
    re-read, the ``cp -a`` backup, and the ``tee`` write) as separate SSH
    sessions, so sudo's own credential timestamp never carries between them.
    Reusing the just-validated password for the sibling commands spares the
    user one prompt per extra invocation. The value lives only in daemon
    memory for the duration of the operation — it is never logged, persisted,
    or sent anywhere except the ssh child's stdin.
    """

    __slots__ = ("password",)

    def __init__(self) -> None:
        self.password: Optional[str] = None


@dataclass(frozen=True)
class PrivilegedFileContent:
    content: bytes
    exists: bool
    mode: Optional[int]


@dataclass(frozen=True)
class PrivilegedReplaceResult:
    path: str
    revision: str
    size: int
    backup_path: Optional[str]


@dataclass
class _CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    exists: bool = True


@dataclass
class _PromptResult:
    password: Optional[str]
    remember_policy: RememberPolicy
    from_stored: bool = False


def _sudo_read_command(path: str, *, passwordless: bool) -> str:
    sudo = "sudo -n" if passwordless else "sudo -S -p ''"
    return f"env LC_ALL=C {sudo} -- {_SUDO_READ_HEAD} -- {shlex.quote(path)}"


def _sudo_write_command(path: str, *, passwordless: bool) -> str:
    sudo = "sudo -n" if passwordless else "sudo -S -p ''"
    return f"env LC_ALL=C {sudo} -- {_SUDO_WRITE_HEAD} -- {shlex.quote(path)} > /dev/null"


def _sudo_backup_command(path: str, backup_path: str, *, passwordless: bool) -> str:
    """Best-effort ``sudo cp -a`` backup; path positional args avoid shell quoting.

    Uses the same passwordless/``-S -p ''`` pattern as reads and writes so
    password hosts can create backups too.
    """
    sudo = "sudo -n" if passwordless else "sudo -S -p ''"
    return (
        f"env LC_ALL=C {sudo} -- sh -c 'cp -a -- \"$1\" \"$2\"' sh "
        f"{shlex.quote(path)} {shlex.quote(backup_path)}"
    )


def _file_revision(content: bytes, exists: bool) -> str:
    if not exists:
        return "absent"
    import hashlib

    return hashlib.sha256(content).hexdigest()


def _is_password_required(stderr: bytes) -> bool:
    text = (stderr or b"").decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _PASSWORD_REQUIRED_MARKERS)


def _is_wrong_password(stderr: bytes) -> bool:
    text = (stderr or b"").decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _WRONG_PASSWORD_MARKERS)


def _is_missing(stderr: bytes) -> bool:
    text = (stderr or b"").decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _NOT_FOUND_MARKERS)


def _is_sudo_unavailable(returncode: int, stderr: bytes) -> bool:
    """True when sudo itself cannot run on the host.

    Covers sudo not being installed (the remote shell reports
    ``sudo: command not found`` with exit 127) and the legacy ``requiretty``
    policy that rejects the non-interactive SSH channel. Must be classified
    *before* the missing-file markers: ``sudo: command not found`` contains
    "not found", so treating it as an absent file would silently surface a
    privileged read as an empty document instead of a typed error.
    """
    if returncode == 127:
        return True
    text = (stderr or b"").decode("utf-8", errors="replace").lower()
    return any(marker in text for marker in _SUDO_UNAVAILABLE_MARKERS)


def _denied_error(connection_id: ConnectionId) -> SshPilotError:
    return SshPilotError(
        ErrorCode.REMOTE_PERMISSION_DENIED,
        "The user is not allowed to run sudo on this host",
        connection_id=connection_id,
    )


def _sudo_unavailable_error(connection_id: ConnectionId) -> SshPilotError:
    return SshPilotError(
        ErrorCode.UNSUPPORTED_CAPABILITY,
        "sudo is unavailable on this host",
        connection_id=connection_id,
    )


class PrivilegedFileService:
    """Run sudo file reads/writes over the canonical native SSH launch.

    ``launch_provider`` is the daemon ``ConnectionLaunchProvider`` (its
    ``prepare_remote_command_launch`` builds ``ssh <alias> <remote_command>``)
    and ``broker`` is the daemon InteractionBroker used for both the SSH
    child's own auth prompts (via ``prepare_operation_launch``) and the
    protected sudo-password prompt. Password lookup/store/clear callables are
    injectable for tests and default to the existing ``sudo_password_spec``
    secret backend.
    """

    def __init__(
        self,
        launch_provider: Any,
        broker: Any,
        *,
        popen: Callable[..., "subprocess.Popen"] = subprocess.Popen,
        environ: Optional[dict] = None,
        command_timeout: float = REMOTE_COMMAND_TIMEOUT,
        max_password_attempts: int = MAX_SUDO_ATTEMPTS,
        max_read_bytes: int = MAX_READ_BYTES,
        secret_lookup: Callable[[str, str], str] = lookup_sudo_password,
        secret_store: Callable[[str, str, str], bool] = store_sudo_password,
        secret_clear: Callable[[str, str], bool] = clear_sudo_password,
    ) -> None:
        if launch_provider is None or broker is None:
            raise TypeError("a launch provider and interaction broker are required")
        if command_timeout <= 0:
            raise ValueError("the privileged command timeout must be positive")
        if type(max_password_attempts) is not int or max_password_attempts < 1:
            raise ValueError("the sudo attempt limit must be positive")
        if type(max_read_bytes) is not int or max_read_bytes < 1:
            raise ValueError("the privileged read limit must be positive")
        self._launch_provider = launch_provider
        self._broker = broker
        self._popen = popen
        self._environ = dict(environ if environ is not None else os.environ)
        self._command_timeout = float(command_timeout)
        self._max_password_attempts = max_password_attempts
        self._max_read_bytes = max_read_bytes
        self._secret_lookup = secret_lookup
        self._secret_store = secret_store
        self._secret_clear = secret_clear

    # -- public operations -------------------------------------------------

    def read(
        self,
        *,
        connection_id: ConnectionId,
        scope_id: SessionId,
        hostname: str,
        username: str,
        port: int,
        path: str,
    ) -> PrivilegedFileContent:
        """Read ``path`` as root. Passwordless sudo is attempted first; a
        stored sudo password is tried next; then a protected prompt."""
        return self._read_content(
            connection_id=connection_id,
            scope_id=scope_id,
            hostname=hostname,
            username=username,
            port=port,
            path=path,
            password_context=None,
        )

    def _read_content(
        self,
        *,
        connection_id: ConnectionId,
        scope_id: SessionId,
        hostname: str,
        username: str,
        port: int,
        path: str,
        password_context: Optional[_SudoPasswordContext],
    ) -> PrivilegedFileContent:
        result = self._execute(
            connection_id,
            scope_id,
            lambda passwordless: _sudo_read_command(path, passwordless=passwordless),
            _stdin_for_password,
            hostname=hostname,
            username=username,
            port=port,
            allow_missing=True,
            read_limit=self._max_read_bytes,
            password_context=password_context,
        )
        if not result.exists:
            return PrivilegedFileContent(content=b"", exists=False, mode=None)
        return PrivilegedFileContent(content=result.stdout, exists=True, mode=None)

    def replace(
        self,
        *,
        connection_id: ConnectionId,
        scope_id: SessionId,
        hostname: str,
        username: str,
        port: int,
        path: str,
        payload: bytes,
        expected_revision: str,
        backup: bool,
    ) -> PrivilegedReplaceResult:
        """Write ``payload`` to ``path`` as root, revision-safe.

        The caller holds the per-target lock covering this authoritative read,
        revision comparison, backup, and write. ``sudo tee`` preserves an
        existing file's owner/mode by writing through its inode. The read,
        backup, and write share one in-memory password context so a password
        host prompts once per save instead of once per sudo invocation.
        """
        password_context = _SudoPasswordContext()
        current = self._read_content(
            connection_id=connection_id,
            scope_id=scope_id,
            hostname=hostname,
            username=username,
            port=port,
            path=path,
            password_context=password_context,
        )
        if not current.exists:
            raise SshPilotError(
                ErrorCode.FILE_REVISION_CONFLICT,
                "The remote file no longer exists",
                connection_id=connection_id,
            )
        current_revision = _file_revision(current.content, current.exists)
        if current_revision != expected_revision:
            raise SshPilotError(
                ErrorCode.FILE_REVISION_CONFLICT,
                "The remote file changed since it was read",
                connection_id=connection_id,
            )
        backup_path: Optional[str] = None
        if backup and current.exists:
            backup_path = f"{path}.bak-{time.time_ns()}"
            self._execute(
                connection_id,
                scope_id,
                lambda passwordless: _sudo_backup_command(
                    path, backup_path, passwordless=passwordless
                ),
                _stdin_for_password,
                hostname=hostname,
                username=username,
                port=port,
                password_context=password_context,
            )

        def _stdin(password: Optional[str]) -> Optional[bytes]:
            return _stdin_with_payload(password, payload)

        result = self._execute(
            connection_id,
            scope_id,
            lambda passwordless: _sudo_write_command(path, passwordless=passwordless),
            _stdin,
            hostname=hostname,
            username=username,
            port=port,
            password_context=password_context,
        )
        if not result.exists:
            raise SshPilotError(
                ErrorCode.FILE_REPLACEMENT_FAILED,
                "The remote file could not be replaced",
                connection_id=connection_id,
            )
        return PrivilegedReplaceResult(
            path=path,
            revision=_file_revision(payload, True),
            size=len(payload),
            backup_path=backup_path,
        )
    # -- sudo execution ----------------------------------------------------

    def _execute(
        self,
        connection_id: ConnectionId,
        scope_id: SessionId,
        command_builder: Callable[[bool], str],
        stdin_builder: Callable[[Optional[str]], Optional[bytes]],
        *,
        hostname: str,
        username: str,
        port: int,
        allow_missing: bool = False,
        read_limit: Optional[int] = None,
        password_context: Optional[_SudoPasswordContext] = None,
    ) -> _CommandResult:
        """Run a sudo command, resolving the password when required.

        A validated password already used earlier in the same operation
        (``password_context``) is tried before the stored secret and the
        protected prompt; successful passwords are remembered back into it.

        Returns a ``_CommandResult`` with ``exists`` reflecting whether the
        target path is present (missing marker classification). Raises
        ``SshPilotError`` for denied/cancelled/exhausted/timeout/oversized
        conditions.
        """
        result = self._run(
            connection_id,
            scope_id,
            command_builder(passwordless=True),
            stdin_builder(None),
            read_limit=read_limit,
        )
        if result.returncode == 0:
            result.exists = True
            return result
        if _is_sudo_unavailable(result.returncode, result.stderr):
            raise _sudo_unavailable_error(connection_id)
        if is_sudo_denied_error(result.stderr.decode("utf-8", errors="replace")):
            raise _denied_error(connection_id)
        if allow_missing and _is_missing(result.stderr):
            result.exists = False
            return result
        if result.returncode == SSH_ERROR_EXIT:
            raise SshPilotError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "The SSH connection could not run the remote command",
                connection_id=connection_id,
            )
        if not _is_password_required(result.stderr):
            raise SshPilotError(
                ErrorCode.REMOTE_COMMAND_FAILED,
                "The remote command failed",
                connection_id=connection_id,
            )

        context_password = (
            password_context.password if password_context is not None else None
        )
        password = context_password or self._secret_lookup(hostname, username)
        from_stored = bool(password) and not context_password
        from_context = bool(context_password)
        attempt = 1
        while attempt <= self._max_password_attempts:
            if not password:
                prompt = self._prompt_password(
                    scope_id, connection_id, hostname, username, port, attempt
                )
                if prompt.password is None:
                    raise SshPilotError(
                        ErrorCode.OPERATION_CANCELLED,
                        "The sudo password prompt was cancelled",
                        connection_id=connection_id,
                    )
                password = prompt.password
                remember_policy = prompt.remember_policy
            else:
                remember_policy = RememberPolicy.DO_NOT_STORE
            stdin = stdin_builder(password)
            result = self._run(
                connection_id,
                scope_id,
                command_builder(passwordless=False),
                stdin,
                read_limit=read_limit,
            )
            if result.returncode == 0:
                result.exists = True
                if password_context is not None:
                    password_context.password = password
                self._remember_password(
                    hostname,
                    username,
                    password,
                    remember_policy,
                    from_stored,
                )
                return result
            if _is_sudo_unavailable(result.returncode, result.stderr):
                raise _sudo_unavailable_error(connection_id)
            if is_sudo_denied_error(result.stderr.decode("utf-8", errors="replace")):
                raise _denied_error(connection_id)
            if allow_missing and _is_missing(result.stderr):
                result.exists = False
                return result
            if result.returncode == SSH_ERROR_EXIT:
                raise SshPilotError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    "The SSH connection could not run the remote command",
                    connection_id=connection_id,
                )
            if not _is_password_required(result.stderr) and not _is_wrong_password(
                result.stderr
            ):
                raise SshPilotError(
                    ErrorCode.REMOTE_COMMAND_FAILED,
                    "The remote command failed",
                    connection_id=connection_id,
                )
            if from_stored:
                try:
                    self._secret_clear(hostname, username)
                except Exception:
                    logger.debug("Failed to clear a wrong stored sudo password", exc_info=True)
                from_stored = False
            if from_context:
                if password_context is not None:
                    password_context.password = None
                from_context = False
            password = None
            attempt += 1
        raise SshPilotError(
            ErrorCode.AUTHENTICATION_ATTEMPTS_EXHAUSTED,
            "The sudo password was not accepted",
            connection_id=connection_id,
        )

    def _remember_password(
        self,
        hostname: str,
        username: str,
        password: str,
        policy: RememberPolicy,
        from_stored: bool,
    ) -> None:
        if policy in {
            RememberPolicy.STORE_AFTER_SUCCESS,
            RememberPolicy.REPLACE_STORED_AFTER_SUCCESS,
        }:
            try:
                self._secret_store(hostname, username, password)
            except Exception:
                logger.debug("Could not store the sudo password", exc_info=True)
        elif policy is RememberPolicy.DELETE_STORED_SECRET and from_stored:
            try:
                self._secret_clear(hostname, username)
            except Exception:
                logger.debug("Could not clear the stored sudo password", exc_info=True)

    def _prompt_password(
        self,
        scope_id: SessionId,
        connection_id: ConnectionId,
        hostname: str,
        username: str,
        port: int,
        attempt: int,
    ) -> _PromptResult:
        """Present a protected PASSWORD interaction and collect the one-use secret."""
        summary = self._broker.create(
            session_id=scope_id,
            connection_id=connection_id,
            interaction_type=InteractionType.PASSWORD,
            prompt=PasswordPrompt(
                username=username or "unknown",
                hostname=hostname,
                port=int(port or 22),
                attempt=attempt,
                can_remember=True,
                stored_secret_available=False,
            ),
            attempt=attempt,
        )
        result = self._broker.wait_for_result(summary.id)
        if (
            result is None
            or result.decision is not SecretDecision.SUBMIT
            or result.secret is None
        ):
            if result is not None:
                result.clear()
            return _PromptResult(password=None, remember_policy=RememberPolicy.DO_NOT_STORE)
        secret = result.secret
        result.secret = None
        policy = result.remember_policy
        result.clear()
        try:
            password = bytes(secret).decode("utf-8", errors="strict")
        except (UnicodeDecodeError, ValueError):
            password = ""
        if not password or "\x00" in password:
            return _PromptResult(password=None, remember_policy=policy)
        del secret
        return _PromptResult(password=password, remember_policy=policy)

    def _run(
        self,
        connection_id: ConnectionId,
        scope_id: SessionId,
        remote_command: str,
        stdin_data: Optional[bytes],
        read_limit: Optional[int] = None,
    ) -> _CommandResult:
        argv, environment = self._launch_provider.prepare_remote_command_launch(
            connection_id, remote_command
        )
        argv, environment = self._broker.prepare_operation_launch(
            argv,
            environment,
            scope_id=scope_id,
            connection_id=connection_id,
        )
        try:
            process = self._popen(
                list(argv),
                stdin=(
                    subprocess.PIPE
                    if stdin_data is not None
                    else subprocess.DEVNULL
                ),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(environment),
                start_new_session=True,
            )
        except OSError as exc:
            raise SshPilotError(
                ErrorCode.SESSION_STARTUP_FAILED,
                "The SSH command could not be started",
                connection_id=connection_id,
            ) from exc
        try:
            if read_limit is not None:
                stdout, stderr = self._read_bounded(
                    process, stdin_data, read_limit, connection_id
                )
            else:
                stdout, stderr = process.communicate(
                    stdin_data, timeout=self._command_timeout
                )
        except subprocess.TimeoutExpired:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
            raise SshPilotError(
                ErrorCode.OPERATION_TIMED_OUT,
                "The remote command timed out",
                connection_id=connection_id,
            ) from None
        return _CommandResult(
            returncode=process.returncode,
            stdout=stdout or b"",
            stderr=stderr or b"",
        )

    def _read_bounded(
        self,
        process: "subprocess.Popen",
        stdin_data: Optional[bytes],
        read_limit: int,
        connection_id: ConnectionId,
    ) -> tuple[bytes, bytes]:
        """Stream a privileged read, failing fast past ``read_limit`` bytes.

        A bounded reader (instead of ``communicate``) means a remote file that
        exceeds the contract fails with a typed ``FILE_CONTENT_TOO_LARGE``
        error and the child is killed instead of buffering unbounded output.
        Real pipe FDs are drained through a selector so the deadline is always
        enforced and stdout/stderr are drained concurrently, so a child that
        fills stderr while the parent reads stdout cannot deadlock.
        """
        if stdin_data is not None:
            try:
                process.stdin.write(stdin_data)
                process.stdin.flush()
                process.stdin.close()
            except (OSError, ValueError):
                pass
        deadline = time.monotonic() + self._command_timeout
        out = bytearray()
        err = bytearray()

        streams = {}
        for label, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            try:
                streams[label] = stream.fileno()
            except (AttributeError, OSError, ValueError):
                streams = {}
                break

        if not streams:
            return self._read_bounded_in_memory(
                process, read_limit, connection_id, deadline
            )

        selector = selectors.DefaultSelector()
        try:
            for stream in streams.values():
                selector.register(stream, selectors.EVENT_READ)
            while streams:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._terminate_process(process)
                    raise subprocess.TimeoutExpired("ssh", self._command_timeout)
                try:
                    events = selector.select(timeout=max(0.0, remaining))
                except OSError:
                    break
                if not events:
                    self._terminate_process(process)
                    raise subprocess.TimeoutExpired("ssh", self._command_timeout)
                for key, _mask in events:
                    label = _stream_label(streams, key.fd)
                    if label is None:
                        continue
                    try:
                        chunk = os.read(key.fd, _READ_CHUNK_BYTES)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        selector.unregister(key.fd)
                        del streams[label]
                        continue
                    if not chunk:
                        selector.unregister(key.fd)
                        del streams[label]
                        continue
                    if label == "stdout":
                        out.extend(chunk)
                        if len(out) > read_limit:
                            self._terminate_process(process)
                            raise SshPilotError(
                                ErrorCode.FILE_CONTENT_TOO_LARGE,
                                "The remote file is too large",
                                connection_id=connection_id,
                            )
                    else:
                        err.extend(chunk)
        finally:
            selector.close()
        self._wait_reap(process, deadline)
        return bytes(out), bytes(err)

    def _read_bounded_in_memory(
        self,
        process: "subprocess.Popen",
        read_limit: int,
        connection_id: ConnectionId,
        deadline: float,
    ) -> tuple[bytes, bytes]:
        """Fallback for non-pipe streams (test doubles) that cannot deadlock."""
        out = bytearray()
        while True:
            try:
                chunk = process.stdout.read(_READ_CHUNK_BYTES)
            except (OSError, ValueError):
                chunk = b""
            if not chunk:
                break
            out.extend(chunk)
            if len(out) > read_limit:
                self._terminate_process(process)
                raise SshPilotError(
                    ErrorCode.FILE_CONTENT_TOO_LARGE,
                    "The remote file is too large",
                    connection_id=connection_id,
                )
        try:
            stderr = process.stderr.read()
        except (OSError, ValueError):
            stderr = b""
        self._wait_reap(process, deadline)
        return bytes(out), stderr or b""

    def _wait_reap(self, process: "subprocess.Popen", deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._terminate_process(process)
                raise
        else:
            self._terminate_process(process)
            raise subprocess.TimeoutExpired("ssh", self._command_timeout)

    @staticmethod
    def _terminate_process(process: "subprocess.Popen") -> None:
        try:
            process.kill()
        except OSError:
            pass
        try:
            process.wait()
        except Exception:
            pass


def _stream_label(streams: dict, fd: int) -> Optional[str]:
    for label, registered_fd in streams.items():
        if registered_fd == fd:
            return label
    return None


def _stdin_for_password(password: Optional[str]) -> Optional[bytes]:
    if password is None:
        return None
    return password.encode("utf-8") + b"\n"


def _stdin_with_payload(password: Optional[str], payload: bytes) -> Optional[bytes]:
    if password is None:
        return payload
    return password.encode("utf-8") + b"\n" + payload
