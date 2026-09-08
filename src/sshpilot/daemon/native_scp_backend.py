"""Daemon-owned native OpenSSH SCP execution backend."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Sequence

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import SessionId, TransferId
from sshpilot.api.models.operations import ScpFailureCode
from sshpilot.api.models.transfers import StartScpTransferRequest
from sshpilot.transfer_scp import (
    assemble_scp_transfer_args,
    classify_sftp_error,
    insert_legacy_scp_flag,
    legacy_scp_flag_unsupported,
)
from .process_registry import forget_owned_process
from .ssh_launch import IO_STDERR_ONLY, LaunchStartError, ScpLaunch, SshLauncher

_MAX_STDERR_BYTES = 64 * 1024
_DRAIN_CHUNK_BYTES = 8192


@dataclass(frozen=True)
class ScpProcessResult:
    returncode: int
    stderr: str


class _BoundedStderr:
    def __init__(self, stream) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._tail = bytearray()
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._drain,
            name="sshpilot-scp-stderr",
            daemon=True,
        )
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(_DRAIN_CHUNK_BYTES)
                if not chunk:
                    return
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", "replace")
                with self._lock:
                    self._tail.extend(chunk)
                    if len(self._tail) > _MAX_STDERR_BYTES:
                        del self._tail[:-_MAX_STDERR_BYTES]
        finally:
            self._done.set()

    def finish(self, timeout: float) -> str:
        self._done.wait(max(0.0, timeout))
        self._thread.join(timeout=max(0.0, timeout))
        with self._lock:
            return bytes(self._tail).decode("utf-8", "replace")


class NativeScpBackend:
    """Execute one typed SCP request without owning transfer lifecycle state."""

    supported = True

    def __init__(
        self,
        launch_provider,
        interaction_broker,
        *,
        popen: Callable[..., object] = subprocess.Popen,
        wait_timeout: float = 5.0,
    ) -> None:
        self._launch_provider = launch_provider
        self._interaction_broker = interaction_broker
        self._popen = popen
        self._wait_timeout = float(wait_timeout)
        # Resolve ``self._popen`` at spawn time so a test can replace it on a
        # live backend.
        self._launcher = SshLauncher(
            launch_provider,
            interaction_broker,
            popen=lambda *args, **kwargs: self._popen(*args, **kwargs),
        )

    def build_operands(
        self,
        request: StartScpTransferRequest,
        connection_target: str,
    ) -> tuple[tuple[str, ...], str]:
        sources, destination = assemble_scp_transfer_args(
            connection_target,
            request.sources,
            request.destination,
            request.direction.value,
        )
        return tuple(sources), destination

    def build_argv(
        self,
        request: StartScpTransferRequest,
        connection_target: str,
        base_argv: Sequence[str],
    ) -> tuple[str, ...]:
        sources, _destination = self.build_operands(request, connection_target)
        if not base_argv:
            raise ValueError("SCP launch argv is empty")
        return tuple((*base_argv[:-1], *sources, base_argv[-1]))

    def target_for_connection(self, connection_id):
        return self._launch_provider.prepare_daemon_scp_target(connection_id)

    def run(
        self,
        request: StartScpTransferRequest,
        *,
        connection_target: str,
        connection_id,
        transfer_id: TransferId,
        cancel_event,
    ) -> ScpProcessResult:
        if request.conflict_policy.value != "overwrite":
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST,
                ScpFailureCode.TRANSFER_PREPARATION_FAILED.value,
                details={
                    "scp_failure_code": (
                        ScpFailureCode.TRANSFER_PREPARATION_FAILED.value
                    )
                },
            )
        sources, destination = self.build_operands(request, connection_target)
        # Only scp flags belong in extra_args. Path operands must be inserted
        # after every builder option (including preference ssh_overrides): if a
        # local path lands before ``-o``/``-v``, OpenSSH treats those flags as
        # more source filenames (``stat local "-o"``) after auth.
        flag_args: tuple[str, ...] = ("-r",) if request.recursive else ()
        # One public scope per daemon resource: the interaction scope of an
        # SCP transfer IS its public TransferId. The frontend learns that ID
        # from TransferSummary and binds its interaction presenter to it; a
        # private random scope (``scp-<connection>-<id(cancel_event)>``) would
        # be unknowable and its prompts would never be presented.
        with self._launcher.open(
            scope_id=SessionId(str(transfer_id)),
            connection_id=connection_id,
        ) as scope:
            prepared = scope.prepare(
                ScpLaunch(
                    extra_args=flag_args,
                    target_override=destination,
                ),
                connection_id=connection_id,
                hostname=connection_target,
            )
            prepared = prepared.with_argv(
                self.build_argv(request, connection_target, prepared.argv)
            )
            result = self._run_attempt(
                prepared,
                cancel_event=cancel_event,
            )
            if result.returncode == 0:
                scope.authenticated()
                return result
            if cancel_event.is_set():
                raise SshPilotError(
                    ErrorCode.OPERATION_CANCELLED,
                    "The SCP transfer was cancelled",
                )
            if classify_sftp_error(result.stderr):
                # The retry stays on the same scope, so it cannot escape the
                # credentials the first attempt already brokered.
                legacy = self._run_attempt(
                    prepared.with_argv(insert_legacy_scp_flag(list(prepared.argv))),
                    cancel_event=cancel_event,
                )
                if legacy.returncode == 0:
                    scope.authenticated()
                    return legacy
                if cancel_event.is_set():
                    raise SshPilotError(
                        ErrorCode.OPERATION_CANCELLED,
                        "The SCP transfer was cancelled",
                    )
                if legacy_scp_flag_unsupported(legacy.stderr):
                    raise self._failure(result.stderr)
                raise self._failure(legacy.stderr)
            raise self._failure(result.stderr)

    def _run_attempt(
        self,
        prepared,
        *,
        cancel_event,
    ) -> ScpProcessResult:
        process = None
        stderr_reader = None
        try:
            # The scope starts the child and records it as daemon-owned; the
            # cancel/drain/reap loop below is this backend's own concern.
            process = prepared.spawn(IO_STDERR_ONLY)
            stderr_reader = _BoundedStderr(process.stderr)
            while True:
                returncode = process.poll()
                if returncode is not None:
                    break
                if cancel_event.is_set():
                    if process.poll() is None:
                        self._terminate(process)
                    returncode = self._reap(process)
                    stderr = stderr_reader.finish(self._wait_timeout)
                    raise SshPilotError(
                        ErrorCode.OPERATION_CANCELLED,
                        "The SCP transfer was cancelled",
                    )
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    continue
            returncode = self._reap(process)
            stderr = stderr_reader.finish(self._wait_timeout)
            return ScpProcessResult(returncode=returncode, stderr=stderr)
        except LaunchStartError as exc:
            # Restate the scope's start failure as a transfer failure: the
            # frontend classifies SCP problems by ``scp_failure_code``.
            raise SshPilotError(
                ErrorCode.TRANSFER_IO_FAILED,
                ScpFailureCode.PROCESS_START_FAILED.value,
                details={
                    "scp_failure_code": ScpFailureCode.PROCESS_START_FAILED.value,
                    "diagnostic": str(exc.os_error),
                },
            ) from exc
        except SshPilotError:
            raise
        except OSError as exc:
            raise SshPilotError(
                ErrorCode.TRANSFER_IO_FAILED,
                ScpFailureCode.PROCESS_START_FAILED.value,
                details={
                    "scp_failure_code": ScpFailureCode.PROCESS_START_FAILED.value,
                    "diagnostic": str(exc),
                },
            ) from exc
        finally:
            if stderr_reader is not None:
                stderr_reader.finish(self._wait_timeout)

    def _reap(self, process) -> int:
        result = process.poll()
        if result is not None:
            return int(result)
        try:
            return int(process.wait(timeout=self._wait_timeout))
        except Exception:
            self._terminate(process)
            try:
                return int(process.wait(timeout=0.5))
            except Exception:
                return int(process.poll() or -9)

    @staticmethod
    def _failure(stderr: str) -> SshPilotError:
        code = (
            ScpFailureCode.REMOTE_SFTP_UNAVAILABLE
            if classify_sftp_error(stderr)
            else ScpFailureCode.TRANSFER_FAILED
        )
        return SshPilotError(
            ErrorCode.TRANSFER_IO_FAILED,
            code.value,
            details={
                "scp_failure_code": code.value,
                "diagnostic": (stderr or "").strip(),
            },
        )

    def _terminate(self, process) -> None:
        forget_owned_process(process.pid)
        if process.poll() is not None:
            return
        if getattr(process, "_sshpilot_process_group", False):
            try:
                os.killpg(int(process.pid), signal.SIGTERM)
            except Exception:
                pass
        try:
            process.terminate()
        except Exception:
            pass
        try:
            process.wait(timeout=self._wait_timeout)
            return
        except Exception:
            pass
        if getattr(process, "_sshpilot_process_group", False):
            try:
                os.killpg(int(process.pid), signal.SIGKILL)
            except Exception:
                pass
        try:
            process.kill()
        except Exception:
            pass
        try:
            process.wait(timeout=0.5)
        except Exception:
            pass
