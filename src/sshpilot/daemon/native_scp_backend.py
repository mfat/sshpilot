"""Daemon-owned native OpenSSH SCP execution backend."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import SessionId, TransferId
from sshpilot.api.models.transfers import StartScpTransferRequest
from sshpilot.transfer_scp import (
    assemble_scp_transfer_args,
    classify_sftp_error,
    insert_legacy_scp_flag,
    legacy_scp_flag_unsupported,
)

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
                "native SCP supports overwrite only",
            )
        sources, destination = self.build_operands(request, connection_target)
        extra_args = list(sources)
        if request.recursive:
            extra_args.insert(0, "-r")
        base_argv, base_env = self._launch_provider.prepare_daemon_scp_launch(
            connection_id,
            extra_args=extra_args,
            interaction_policy="broker",
            target_override=destination,
        )
        # One public scope per daemon resource: the interaction scope of an
        # SCP transfer IS its public TransferId. The frontend learns that ID
        # from TransferSummary and binds its interaction presenter to it; a
        # private random scope (``scp-<connection>-<id(cancel_event)>``) would
        # be unknowable and its prompts would never be presented.
        scope_id = SessionId(str(transfer_id))
        argv, env = self._interaction_broker.prepare_operation_launch(
            tuple(base_argv),
            dict(base_env),
            scope_id=scope_id,
            connection_id=connection_id,
            hostname=connection_target,
        )
        succeeded = False
        try:
            result = self._run_attempt(
                argv,
                env,
                cancel_event=cancel_event,
            )
            if result.returncode == 0:
                succeeded = True
                return result
            if cancel_event.is_set():
                raise SshPilotError(
                    ErrorCode.OPERATION_CANCELLED,
                    "The SCP transfer was cancelled",
                )
            if classify_sftp_error(result.stderr):
                legacy_argv = insert_legacy_scp_flag(list(argv))
                legacy = self._run_attempt(
                    legacy_argv,
                    env,
                    cancel_event=cancel_event,
                )
                if legacy.returncode == 0:
                    succeeded = True
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
        finally:
            if succeeded:
                # Commit credentials the user chose to remember AFTER a
                # successful run — BEFORE the interaction scope is torn down.
                # ``cancel_session`` destroys the askpass context and clears
                # pending remembered secrets, so ``mark_authenticated`` must
                # run first. On failure/cancellation the raises above skip it
                # and the ``finally`` still cleans the scope up.
                self._interaction_broker.mark_authenticated(scope_id)
            self._interaction_broker.cancel_session(scope_id)

    def _run_attempt(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        cancel_event,
    ) -> ScpProcessResult:
        process = None
        stderr_reader = None
        try:
            process = self._popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=dict(env),
                start_new_session=(os.name != "nt"),
                shell=False,
            )
            setattr(process, "_sshpilot_process_group", os.name != "nt")
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
        except SshPilotError:
            raise
        except OSError as exc:
            raise SshPilotError(
                ErrorCode.TRANSFER_IO_FAILED,
                "The SCP process could not be started",
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
        message = classify_sftp_error(stderr) or "The SCP transfer failed"
        return SshPilotError(ErrorCode.TRANSFER_IO_FAILED, message)

    def _terminate(self, process) -> None:
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
