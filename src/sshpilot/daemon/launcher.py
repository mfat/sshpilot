"""Bounded on-demand launcher for the experimental local daemon mode."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from sshpilot.api.capabilities import Capability
from sshpilot.api.daemon_client import DaemonClient
from sshpilot.api.errors import ErrorCode, SshPilotError

from .lifecycle import (
    SocketSecurityError,
    resolve_socket_path,
    validate_client_socket_path,
)


DEFAULT_STARTUP_TIMEOUT = 3.0
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_PROBE_TIMEOUT = 0.25

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
    MISSING_CAPABILITY = "missing_capability"
    UNSAFE_SOCKET = "unsafe_socket"
    INTERNAL_ERROR = "internal_error"


class DaemonLaunchError(RuntimeError):
    """A safe launcher failure with no command, path, or raw exception text."""

    def __init__(self, reason: DaemonStartupFailure) -> None:
        self.reason = reason
        super().__init__(f"local daemon startup failed ({reason.value})")


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
    return environment


class DaemonLauncher:
    """Reuse a compatible daemon or make one bounded, race-safe launch attempt."""

    def __init__(
        self,
        *,
        socket_path: Optional[os.PathLike] = None,
        startup_timeout: float = DEFAULT_STARTUP_TIMEOUT,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        probe_timeout: float = DEFAULT_PROBE_TIMEOUT,
        executable: Optional[str] = None,
        environment: Optional[Mapping[str, str]] = None,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
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

    def connect_or_start(self) -> DaemonLaunchResult:
        """Return one compatible client, launching at most once per call."""

        with self._lock:
            try:
                validate_client_socket_path(self.socket_path)
            except SocketSecurityError as error:
                raise DaemonLaunchError(
                    DaemonStartupFailure.UNSAFE_SOCKET
                ) from error

            try:
                client = self._connect(self.probe_timeout)
            except SshPilotError as error:
                if error.code is not ErrorCode.DAEMON_UNAVAILABLE:
                    raise self._classify_handshake_error(error) from error
            else:
                return DaemonLaunchResult(client=client, process=None)

            command = (
                self.executable,
                "-m",
                "sshpilot.daemon",
                "--socket",
                str(self.socket_path),
            )
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
            except BaseException:
                self._stop_failed_child(process)
                raise
            return DaemonLaunchResult(client=client, process=handle)

    def _connect(self, timeout: float) -> DaemonClient:
        client = DaemonClient(
            socket_path=self.socket_path,
            timeout=timeout,
            client_name="sshpilot-gtk",
            frontend_type="gtk",
        )
        capabilities = client.get_capabilities()
        required = {
            Capability.CONNECTIONS_READ,
            Capability.CONNECTIONS_EVENTS,
            Capability.CONNECTIONS_WRITE,
        }
        if not required <= capabilities.supported:
            client.close()
            raise DaemonLaunchError(DaemonStartupFailure.MISSING_CAPABILITY)
        return client

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
        if error.code is ErrorCode.PROTOCOL_VERSION_UNSUPPORTED:
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
