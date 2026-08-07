"""Daemon-owned native OpenSSH SCP execution backend."""

from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.common import SessionId
from sshpilot.api.models.transfers import StartScpTransferRequest
from sshpilot.transfer_scp import (
    assemble_scp_transfer_args,
    classify_sftp_error,
    insert_legacy_scp_flag,
    legacy_scp_flag_unsupported,
)

_MAX_STDERR_BYTES = 64 * 1024


@dataclass(frozen=True)
class ScpProcessResult:
    returncode: int
    stderr: str


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
        cancel_event,
    ) -> ScpProcessResult:
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
        argv = tuple(base_argv)
        scope_id = SessionId(f"scp-{connection_id}-{id(cancel_event)}")
        env = dict(base_env)
        argv, env = self._interaction_broker.prepare_operation_launch(
            argv,
            env,
            scope_id=scope_id,
            connection_id=connection_id,
            hostname=connection_target,
        )
        try:
            return self._run_once(
                argv,
                env,
                scope_id=scope_id,
                cancel_event=cancel_event,
            )
        finally:
            self._interaction_broker.cancel_session(scope_id)

    def _run_once(
        self,
        argv: Sequence[str],
        env: Mapping[str, str],
        *,
        scope_id: str,
        cancel_event,
    ) -> ScpProcessResult:
        process = None
        try:
            process = self._popen(
                list(argv),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=dict(env),
                start_new_session=(os.name != "nt"),
                shell=False,
            )
            setattr(process, "_sshpilot_process_group", os.name != "nt")
            while True:
                if cancel_event.is_set():
                    self._terminate(process)
                    raise SshPilotError(
                        ErrorCode.OPERATION_CANCELLED,
                        "The SCP transfer was cancelled",
                    )
                returncode = process.poll()
                if returncode is not None:
                    break
                if hasattr(process, "wait"):
                    try:
                        returncode = process.wait(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        continue
            stderr = self._read_stderr(process)
            if returncode != 0:
                friendly = classify_sftp_error(stderr)
                if friendly and not legacy_scp_flag_unsupported(stderr):
                    legacy_argv = insert_legacy_scp_flag(list(argv))
                    return self._run_legacy(
                        legacy_argv,
                        env,
                        cancel_event=cancel_event,
                    )
                raise self._failure(stderr)
            return ScpProcessResult(returncode=0, stderr=stderr)
        except SshPilotError:
            raise
        except OSError as exc:
            raise SshPilotError(
                ErrorCode.TRANSFER_IO_FAILED,
                "The SCP process could not be started",
            ) from exc

    def _run_legacy(self, argv, env, *, cancel_event):
        process = self._popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
            start_new_session=(os.name != "nt"),
            shell=False,
        )
        try:
            while True:
                if cancel_event.is_set():
                    self._terminate(process)
                    raise SshPilotError(
                        ErrorCode.OPERATION_CANCELLED,
                        "The SCP transfer was cancelled",
                    )
                try:
                    returncode = process.wait(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
            stderr = self._read_stderr(process)
            if returncode != 0:
                raise self._failure(stderr)
            return ScpProcessResult(returncode=0, stderr=stderr)
        finally:
            if process.poll() is None and cancel_event.is_set():
                self._terminate(process)

    @staticmethod
    def _read_stderr(process) -> str:
        stream = getattr(process, "stderr", None)
        if stream is None:
            return ""
        data = stream.read(_MAX_STDERR_BYTES)
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        return str(data or "")[-_MAX_STDERR_BYTES:]

    @staticmethod
    def _failure(stderr: str) -> SshPilotError:
        message = "The SCP transfer failed"
        if classify_sftp_error(stderr):
            message = classify_sftp_error(stderr)
        return SshPilotError(ErrorCode.TRANSFER_IO_FAILED, message)

    def _terminate(self, process) -> None:
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
        except Exception:
            if getattr(process, "_sshpilot_process_group", False):
                try:
                    os.killpg(int(process.pid), signal.SIGKILL)
                except Exception:
                    pass
            try:
                process.kill()
            except Exception:
                pass
