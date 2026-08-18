"""Bounded on-demand launcher for the experimental local daemon mode."""

from __future__ import annotations

import logging
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from sshpilot.api.capabilities import Capability
from sshpilot.api.daemon_client import DEFAULT_REQUEST_TIMEOUT, DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError

from .lifecycle import (
    SocketSecurityError,
    ensure_private_runtime_directory,
    evict_socket_owner,
    probe_socket_owner,
    remove_dead_socket,
    resolve_socket_path,
    validate_client_socket_path,
    wait_until_socket_free,
)


logger = logging.getLogger(__name__)


DEFAULT_STARTUP_TIMEOUT = 3.0
# A frozen build starts a whole application bundle, not an interpreter: code
# signature validation, dyld, and a cold page cache put first-launch startup
# well past the source-tree budget on macOS in particular. Timing out there
# reports a broken daemon when the daemon was merely still starting.
FROZEN_STARTUP_TIMEOUT = 20.0
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_PROBE_TIMEOUT = 0.25
# How long the graceful ``daemon.stop`` RPC itself may take to be answered.
# Short: an unusable peer is by definition one we cannot rely on.
DEFAULT_EVICTION_STOP_TIMEOUT = 2.0
# How long a daemon that *accepted* the stop gets to finish leaving. The stop
# RPC replies as soon as the request is accepted, while the daemon then drains
# (its own drain budget is 5s), so escalating on the reply alone would mean
# routinely killing a daemon that was shutting down correctly — and a killed
# daemon cannot reap the ssh children the drain existed to clean up.
DEFAULT_EVICTION_DRAIN_TIMEOUT = 6.0

_SENSITIVE_CHILD_ENVIRONMENT = frozenset(
    {
        "BW_SESSION",
        "BITWARDENCLI_APPDATA_DIR",
        "SSHPILOT_ASKPASS_TOKEN",
        "SSHPILOT_KDBX_DATABASE",
        "SSHPILOT_KDBX_KEY",
        "SSHPILOT_KDBX_KEYFILE",
        "SSHPILOT_PASSWORD_HOSTS",
        "SSHPILOT_PASSWORD_USER",
        "SSHPILOT_SESSION_PASSPHRASE_FILE",
        "SSHPILOT_SESSION_PASSWORD",
        "SSHPILOT_SESSION_PASSWORD_FILE",
        "SSHPILOT_SESSION_PASSWORD_ID",
        "SSH_ASKPASS",
        "SSH_ASKPASS_REQUIRE",
    }
)


class DaemonStartupFailure(str, Enum):
    """Stable local categories used by composition and tests."""

    PROCESS_EXITED = "process_exited"
    STARTUP_TIMEOUT = "startup_timeout"
    HANDSHAKE_FAILED = "handshake_failed"
    INCOMPATIBLE_PROTOCOL = "incompatible_protocol"
    API_VERSION_MISMATCH = "api_version_mismatch"
    MISSING_CAPABILITY = "missing_capability"
    UNSAFE_SOCKET = "unsafe_socket"
    INTERNAL_ERROR = "internal_error"


class DaemonLaunchError(RuntimeError):
    """A safe launcher failure with no command, path, or raw exception text."""

    def __init__(self, reason: DaemonStartupFailure) -> None:
        self.reason = reason
        super().__init__(f"local daemon startup failed ({reason.value})")


# Failures that describe a *resident peer this build cannot use* rather than a
# failure to start one. Every one of them is recoverable by replacing that
# peer, so the launcher evicts it instead of surfacing a dead end: a daemon
# left over from a previous app build must never be able to keep the
# application from starting.
EVICTABLE_FAILURES = frozenset(
    {
        DaemonStartupFailure.API_VERSION_MISMATCH,
        DaemonStartupFailure.INCOMPATIBLE_PROTOCOL,
        DaemonStartupFailure.MISSING_CAPABILITY,
        DaemonStartupFailure.HANDSHAKE_FAILED,
    }
)


@dataclass(frozen=True)
class DaemonProcessHandle:
    """Proof that this launcher created one exact child process."""

    process: subprocess.Popen
    command: Sequence[str]
    started_by_frontend: bool = True


@dataclass(frozen=True)
class DaemonLaunchResult:
    client: DaemonClient
    process: Optional[DaemonProcessHandle]


def _child_environment(source: Optional[Mapping[str, str]] = None) -> dict:
    """Copy the runtime environment while dropping known session secrets."""

    environment = dict(os.environ if source is None else source)
    for name in _SENSITIVE_CHILD_ENVIRONMENT:
        environment.pop(name, None)
    # ``sys.executable -m sshpilot.daemon`` must also work from a source tree,
    # where run.py placed ``src`` on this process's sys.path without exporting
    # PYTHONPATH. Installed builds already have this directory discoverable;
    # prepending it remains harmless and keeps launcher behavior deterministic.
    source_root = str(Path(__file__).resolve().parents[2])
    python_path = environment.get("PYTHONPATH", "")
    entries = [entry for entry in python_path.split(os.pathsep) if entry]
    if source_root not in entries:
        entries.insert(0, source_root)
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    if getattr(sys, "frozen", False):
        # A frozen child started through sys.executable is a new PyInstaller
        # application instance, not a Python interpreter invocation.  Reset
        # the bootloader's inherited onefile/child state before dispatching
        # the daemon entrypoint from the shared application executable.
        environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
        # Tell the daemon it is running inside a packaged app so it picks the
        # packaged idle-shutdown default. Nothing else set this, so shipped
        # builds silently used the longer development timeout and left a
        # resident daemon for minutes after the last client disconnected.
        environment.setdefault("SSHPILOT_PACKAGED", "1")
    return environment


class DaemonLauncher:
    """Reuse a compatible daemon or make one bounded, race-safe launch attempt."""

    def __init__(
        self,
        *,
        socket_path: Optional[os.PathLike] = None,
        startup_timeout: Optional[float] = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        executable: Optional[str] = None,
        environment: Optional[Mapping[str, str]] = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
        verbose: Optional[bool] = None,
        quiet: Optional[bool] = None,
    ) -> None:
        if startup_timeout is None:
            startup_timeout = (
                FROZEN_STARTUP_TIMEOUT
                if getattr(sys, "frozen", False)
                else DEFAULT_STARTUP_TIMEOUT
            )
        if startup_timeout <= 0 or poll_interval <= 0 or probe_timeout <= 0:
            raise ValueError("daemon launcher timeouts must be positive")
        self.socket_path = resolve_socket_path(socket_path)
        self.startup_timeout = float(startup_timeout)
        self.poll_interval = float(poll_interval)
        self.probe_timeout = float(probe_timeout)
        self.executable = executable or sys.executable
        self._environment = environment
        self._popen = popen
        self._lock = threading.Lock()
        if verbose is None:
            env = environment if environment is not None else os.environ
            verbose = str(env.get("SSHPILOT_DAEMON_VERBOSE", "")).strip() in {
                "1",
                "true",
                "yes",
            }
        self._verbose = bool(verbose)
        if quiet is None:
            env = environment if environment is not None else os.environ
            quiet = str(env.get("SSHPILOT_DAEMON_QUIET", "")).strip().lower() in {
                "1", "true", "yes"
            }
        self._quiet = bool(quiet) and not self._verbose

    @staticmethod
    def _ensure_gtk_askpass_log_forwarder() -> None:
        """Tail the shared askpass log into this (GTK) process logger.

        The daemon broker writes ASKPASS lines into ``sshpilot-askpass.log``;
        without a forwarder in the frontend those never reach ``--verbose``
        console output or ``sshpilot.log``. GTK-free: only imports askpass_utils.
        """

        try:
            from sshpilot.askpass_utils import ensure_askpass_log_forwarder

            ensure_askpass_log_forwarder()
        except Exception:
            pass

    def connect_or_start(self) -> DaemonLaunchResult:
        """Return one compatible client, launching at most once per call.

        A resident daemon this build cannot speak to is replaced rather than
        reported: startup does not depend on the previous build having exited
        cleanly.
        """

        with self._lock:
            self._prepare_socket_location()

            client = self._connect_to_usable_peer()
            if client is not None:
                self._ensure_gtk_askpass_log_forwarder()
                self._ensure_daemon_log_forwarder()
                self._synchronize_explicit_log_level(client)
                return DaemonLaunchResult(client=client, process=None)

            if getattr(sys, "frozen", False):
                command = (
                    self.executable,
                    "--daemon",
                    "--socket",
                    str(self.socket_path),
                    *(("--verbose",) if self._verbose else ()),
                    *(("--quiet",) if self._quiet else ()),
                )
            else:
                command = (
                    self.executable,
                    "-m",
                    "sshpilot.daemon",
                    "--socket",
                    str(self.socket_path),
                    *(("--verbose",) if self._verbose else ()),
                    *(("--quiet",) if self._quiet else ()),
                )
            # Begin verbose forwarding before the child exists so startup
            # failures still contribute their daemon diagnostics.
            self._ensure_daemon_log_forwarder()
            try:
                process = self._popen(
                    list(command),
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                    start_new_session=True,
                    env=_child_environment(self._environment),
                )
            except OSError as error:
                raise DaemonLaunchError(
                    DaemonStartupFailure.PROCESS_EXITED
                ) from error

            handle = DaemonProcessHandle(process=process, command=command)
            try:
                client = self._wait_until_ready(process)
            except BaseException as error:
                self._stop_failed_child(process)
                logger.debug(
                    "Daemon launch attempt failed type=%s",
                    type(error).__name__,
                )
                raise
            self._ensure_gtk_askpass_log_forwarder()
            self._ensure_daemon_log_forwarder()
            self._synchronize_explicit_log_level(client)
            return DaemonLaunchResult(client=client, process=handle)

    def _prepare_socket_location(self) -> None:
        """Validate the socket location, repairing what is safely repairable.

        A runtime directory left at the wrong mode by an unguarded ``makedirs``
        under a lax umask, or a leftover non-socket file at the endpoint, are
        both self-inflicted local states that used to fail startup outright
        with ``unsafe_socket``. Neither is a security signal on a path this
        user already owns, so both are repaired. Anything that is *not* ours
        (foreign owner, symlinked directory) still fails closed.
        """

        try:
            validate_client_socket_path(self.socket_path)
            return
        except SocketSecurityError as first_error:
            initial_error: SocketSecurityError = first_error

        try:
            ensure_private_runtime_directory(self.socket_path.parent)
        except (SocketSecurityError, OSError) as error:
            raise DaemonLaunchError(DaemonStartupFailure.UNSAFE_SOCKET) from error

        self._discard_unusable_endpoint()

        try:
            validate_client_socket_path(self.socket_path)
        except SocketSecurityError as error:
            raise DaemonLaunchError(DaemonStartupFailure.UNSAFE_SOCKET) from error
        # These messages are curated and path-free, so they are safe to log.
        logger.info(
            "Repaired the daemon socket location before startup (was: %s)",
            initial_error,
        )

    def _discard_unusable_endpoint(self) -> None:
        """Remove an endpoint that is not a usable socket and answers nobody."""

        try:
            info = self.socket_path.lstat()
        except (FileNotFoundError, OSError):
            return
        if stat.S_ISSOCK(info.st_mode) and not stat.S_ISLNK(info.st_mode):
            if not stat.S_IMODE(info.st_mode) & 0o177:
                return  # A well-formed socket; not this method's business.
            accepting, _pid = probe_socket_owner(self.socket_path)
            if accepting:
                return  # Live peer; eviction, not deletion, is the answer.
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            return
        try:
            self.socket_path.unlink()
        except (FileNotFoundError, OSError):
            return
        logger.info("Removed an unusable daemon socket endpoint before startup")

    def _connect_to_usable_peer(self) -> Optional[DaemonClient]:
        """Return a client for a resident daemon, or ``None`` to launch one.

        Evicts (once) a peer that answers but cannot serve this build, so the
        caller falls through to launching a current daemon instead of failing.
        """

        for attempt in (0, 1):
            try:
                return self._connect(self.probe_timeout)
            except DaemonLaunchError as error:
                reason = error.reason
            except SshPilotError as error:
                if error.code is ErrorCode.DAEMON_UNAVAILABLE:
                    remove_dead_socket(self.socket_path)
                    return None
                reason = self._classify_handshake_error(error).reason

            if attempt or reason not in EVICTABLE_FAILURES:
                raise DaemonLaunchError(reason)
            logger.warning(
                "Resident daemon cannot serve this build reason=%s; replacing it",
                reason.value,
            )
            # The outcome is advisory. Eviction stops rather than guessing
            # when the socket changes hands mid-flight, so the next probe —
            # not this return value — decides what is actually there now: a
            # daemon we can use, a free socket to launch into, or the same
            # unusable peer, which then fails for real.
            if not self._evict_unusable_peer():
                logger.warning("Could not free the daemon socket; re-probing")
        return None

    def _evict_unusable_peer(self) -> bool:
        """Replace the daemon holding the socket. True when the socket is free.

        Graceful first: an administrative ``daemon.stop`` lets the old daemon
        close its own sessions, children and ControlMasters. A daemon that
        accepts it is then given time to actually finish — the stop RPC
        replies on acceptance, not on exit — because signalling one that is
        already leaving correctly buys nothing and costs the cleanup it was
        in the middle of. Signals remain the fallback for a peer that will
        not answer or will not go: a wedged process, or one whose reply DTOs
        this build can no longer decode.
        """

        if self._request_peer_stop():
            if wait_until_socket_free(
                self.socket_path, timeout=DEFAULT_EVICTION_DRAIN_TIMEOUT
            ):
                logger.info("The outgoing daemon stopped on request")
                return remove_dead_socket(self.socket_path)
        return evict_socket_owner(self.socket_path)

    def _request_peer_stop(self) -> bool:
        """Ask an incompatible resident daemon to stop. True when it accepted.

        Deliberately issues no other RPC: the peer's DTOs are not guaranteed
        to match this build, and every failure mode here simply escalates to
        the signal path.
        """

        from sshpilot.api.models.daemon import StopDaemonRequest

        client: Optional[DaemonClient] = None
        try:
            client = DaemonClient(
                socket_path=self.socket_path,
                timeout=DEFAULT_EVICTION_STOP_TIMEOUT,
                connect_timeout=self.probe_timeout,
                client_name="sshpilot-gtk",
                frontend_type="gtk",
                allow_api_mismatch=True,
            )
            result = client.stop_daemon(StopDaemonRequest(force=True))
            accepted = bool(getattr(result, "accepted", False))
            logger.info(
                "Requested graceful stop of the outgoing daemon accepted=%s",
                accepted,
            )
            return accepted
        except BaseException:
            logger.debug("Graceful stop of the outgoing daemon failed", exc_info=True)
            return False
        finally:
            if client is not None:
                try:
                    client.close()
                except BaseException:
                    pass

    def _connect(self, timeout: float) -> DaemonClient:
        # ``timeout`` here is the launcher probe budget for socket connect only.
        # RPC waits must use DEFAULT_REQUEST_TIMEOUT — otherwise slow methods
        # like sftp.list falsely trip transport_timeout (~probe 0.25s).
        client = DaemonClient(
            socket_path=self.socket_path,
            timeout=DEFAULT_REQUEST_TIMEOUT,
            connect_timeout=timeout,
            client_name="sshpilot-gtk",
            frontend_type="gtk",
        )
        capabilities = client.get_capabilities()
        # Normal UI initialization requires the complete production baseline.
        # Optional renderer features may add their own gates, but none of these
        # backend domains may disappear behind a partial/legacy daemon.
        required = {
            Capability.CONNECTIONS_READ,
            Capability.CONNECTIONS_EVENTS,
            Capability.CONNECTIONS_WRITE,
            Capability.CONNECTIONS_CONFIG_READ,
            Capability.CONNECTIONS_CONFIG_WRITE,
            Capability.CONNECTIONS_SECRETS_WRITE,
            Capability.CONNECTIONS_METADATA_WRITE,
            Capability.CONNECTIONS_GROUPS,
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
            Capability.SFTP_READ,
            Capability.SFTP_WRITE,
            Capability.SFTP_EVENTS,
            Capability.SFTP_METADATA,
            Capability.SFTP_MUTATE,
            Capability.TRANSFERS_READ,
            Capability.TRANSFERS_WRITE,
            Capability.TRANSFERS_EVENTS,
            Capability.FORWARDS_READ,
            Capability.FORWARDS_WRITE,
            Capability.FORWARDS_EVENTS,
            Capability.DAEMON_STATUS,
            Capability.DAEMON_CONTROL,
            Capability.DAEMON_EVENTS,
        }
        if not required <= capabilities.supported:
            client.close()
            raise DaemonLaunchError(DaemonStartupFailure.MISSING_CAPABILITY)
        return client

    def _ensure_daemon_log_forwarder(self) -> None:
        if not self._verbose:
            return
        try:
            from sshpilot.logging_support import ensure_daemon_log_forwarder
            from sshpilot.platform_utils import get_state_dir

            ensure_daemon_log_forwarder(
                Path(get_state_dir()) / "daemon.log",
                enabled=True,
            )
        except Exception:
            logger.debug("Could not start daemon log forwarder", exc_info=True)

    def _synchronize_explicit_log_level(self, client: DaemonClient) -> None:
        """Apply only an explicit launcher verbosity override to the daemon."""

        if not (self._verbose or self._quiet):
            return
        setter = getattr(client, "set_daemon_log_level", None)
        if not callable(setter):
            # Lightweight launcher test doubles and older compatible clients
            # need not expose the additive control method.
            return
        from sshpilot.api.models.daemon import DaemonLogLevel, SetDaemonLogLevelRequest

        level = DaemonLogLevel.DEBUG if self._verbose else DaemonLogLevel.WARNING
        try:
            setter(SetDaemonLogLevelRequest(level=level))
        except SshPilotError as error:
            logger.warning(
                "Could not synchronize daemon log level code=%s",
                error.code.value,
            )
        except Exception as error:
            logger.debug(
                "Could not synchronize daemon log level type=%s",
                type(error).__name__,
            )

    def _wait_until_ready(self, process: subprocess.Popen) -> DaemonClient:
        deadline = time.monotonic() + self.startup_timeout
        saw_socket = False
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                reason = (
                    DaemonStartupFailure.HANDSHAKE_FAILED
                    if saw_socket
                    else DaemonStartupFailure.STARTUP_TIMEOUT
                )
                raise DaemonLaunchError(reason)

            try:
                socket_exists = validate_client_socket_path(self.socket_path)
            except SocketSecurityError as error:
                raise DaemonLaunchError(
                    DaemonStartupFailure.UNSAFE_SOCKET
                ) from error
            saw_socket = saw_socket or socket_exists

            if socket_exists:
                try:
                    return self._connect(min(self.probe_timeout, remaining))
                except SshPilotError as error:
                    if error.code is not ErrorCode.DAEMON_UNAVAILABLE:
                        raise self._classify_handshake_error(error) from error

            if process.poll() is not None:
                # A racing launcher may have won even though this child exited.
                try:
                    return self._connect(min(self.probe_timeout, remaining))
                except SshPilotError as error:
                    if error.code is not ErrorCode.DAEMON_UNAVAILABLE:
                        raise self._classify_handshake_error(error) from error
                raise DaemonLaunchError(DaemonStartupFailure.PROCESS_EXITED)

            threading.Event().wait(min(self.poll_interval, remaining))

    @staticmethod
    def _classify_handshake_error(error: SshPilotError) -> DaemonLaunchError:
        if error.code is ErrorCode.API_VERSION_MISMATCH:
            reason = DaemonStartupFailure.API_VERSION_MISMATCH
        elif error.code is ErrorCode.PROTOCOL_VERSION_UNSUPPORTED:
            reason = DaemonStartupFailure.INCOMPATIBLE_PROTOCOL
        elif error.code in {
            ErrorCode.PROTOCOL_ERROR,
            ErrorCode.INVALID_FRAME,
            ErrorCode.FRAME_TOO_LARGE,
            ErrorCode.TRANSPORT_CLOSED,
            ErrorCode.TRANSPORT_TIMEOUT,
        }:
            reason = DaemonStartupFailure.HANDSHAKE_FAILED
        else:
            reason = DaemonStartupFailure.INTERNAL_ERROR
        return DaemonLaunchError(reason)

    @staticmethod
    def _stop_failed_child(process: subprocess.Popen) -> None:
        """Stop only the exact child object created by this launcher."""

        if process.poll() is not None:
            return
        try:
            process.terminate()
            process.wait(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
