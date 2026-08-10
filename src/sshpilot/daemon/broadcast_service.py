"""Daemon-owned bounded one-shot SSH broadcast execution.

This module is deliberately GTK-free.  It accepts saved connection ids and
always delegates SSH construction/authentication to ConnectionLaunchProvider.
"""

from __future__ import annotations

import selectors
import subprocess
import threading
import time
from collections import OrderedDict
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Callable, Dict, Optional

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.broadcast import (
    BroadcastCommandRequest,
    BroadcastCommandSummary,
    HostCommandResult,
    HostCommandState,
)
from sshpilot.api.models.common import ClientId, ConnectionId
from sshpilot.api.models.operations import (
    OperationId,
    OperationKind,
    OperationState,
    ServiceFailure,
)
from sshpilot.daemon.operation_runtime import OperationCancelled, OperationHandle, OperationRuntime

_CHUNK_BYTES = 16_384
_TERMINATION_GRACE = 1.0


class NativeSshCommandRunner:
    """Own a native child and drain both output pipes with bounded retention."""

    def run(
        self,
        argv,
        environment,
        policy,
        *,
        cancel_event,
        on_process=None,
        on_output=None,
        input_data=None,
    ):
        process = subprocess.Popen(
            tuple(argv),
            env=dict(environment),
            shell=False,
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE if policy.capture_stdout else subprocess.DEVNULL,
            stderr=subprocess.PIPE if policy.capture_stderr else subprocess.DEVNULL,
            bufsize=0,
        )
        if on_process:
            on_process(process)
        if input_data is not None and process.stdin is not None:
            try:
                process.stdin.write(bytes(input_data))
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        selector = selectors.DefaultSelector()
        retained = {"stdout": bytearray(), "stderr": bytearray()}
        truncated = False
        for name in ("stdout", "stderr"):
            pipe = getattr(process, name)
            if pipe is not None:
                selector.register(pipe, selectors.EVENT_READ, name)
        started = time.monotonic()
        timed_out = False
        try:
            while selector.get_map() or process.poll() is None:
                if cancel_event.is_set():
                    self._stop(process)
                    raise OperationCancelled()
                if policy.timeout_seconds is not None and time.monotonic() - started >= float(
                    policy.timeout_seconds
                ):
                    timed_out = True
                    self._stop(process)
                for key, _ in selector.select(0.05):
                    chunk = key.fileobj.read(_CHUNK_BYTES)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        key.fileobj.close()
                        continue
                    if on_output:
                        on_output(key.data, chunk.decode("utf-8", "replace"))
                    room = policy.output_limit_bytes - sum(
                        len(value) for value in retained.values()
                    )
                    if room > 0:
                        retained[key.data].extend(chunk[:room])
                    if len(chunk) > max(room, 0):
                        truncated = True
                if timed_out and process.poll() is not None:
                    break
            exit_code = process.wait()
        finally:
            selector.close()
            if process.poll() is None:
                self._stop(process)
            process.wait()
            if on_process:
                on_process(None)
        return (
            exit_code,
            retained["stdout"].decode("utf-8", "replace"),
            retained["stderr"].decode("utf-8", "replace"),
            truncated,
            timed_out,
        )

    @staticmethod
    def _stop(process) -> None:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=_TERMINATION_GRACE)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class BroadcastCommandService:
    """Coordinate one parent operation and deterministic per-host results."""

    def __init__(
        self,
        operation_runtime: OperationRuntime,
        launch_provider,
        *,
        interaction_broker,
        runner=None,
        terminal_retention: int = 128,
        output_publisher: Optional[Callable[[OperationId, ConnectionId, str, str], None]] = None,
    ):
        if interaction_broker is None:
            raise ValueError("broadcast execution requires an interaction broker")
        if type(terminal_retention) is not int or terminal_retention < 0:
            raise ValueError("terminal_retention must be non-negative")
        self._operations = operation_runtime
        self._launch_provider = launch_provider
        self._interaction_broker = interaction_broker
        self._runner = runner or NativeSshCommandRunner()
        self._output_publisher = output_publisher
        self._lock = threading.RLock()
        self._targets: Dict[OperationId, list[HostCommandResult]] = {}
        self._cancels: Dict[OperationId, threading.Event] = {}
        self._processes: Dict[OperationId, set[object]] = {}
        self._entered: set[OperationId] = set()
        self._terminal_records: OrderedDict[OperationId, None] = OrderedDict()
        self._terminal_retention = terminal_retention

    def start(
        self,
        request: BroadcastCommandRequest,
        *,
        owner_client_id: ClientId,
        input_data=None,
    ) -> BroadcastCommandSummary:
        if type(request) is not BroadcastCommandRequest:
            raise SshPilotError(
                ErrorCode.INVALID_REQUEST, "A broadcast command request is required"
            )
        initial = [
            HostCommandResult(cid, HostCommandState.PENDING) for cid in request.connection_ids
        ]
        operation_id_box = []

        def body(handle: OperationHandle) -> str:
            operation_id_box.append(handle.operation_id)
            return self._execute(handle, request, input_data)

        summary = self._operations.start_operation(
            OperationKind.BROADCAST_COMMAND,
            body,
            owner_client_id=owner_client_id,
            message="Broadcast command queued",
        )
        with self._lock:
            self._targets[summary.operation_id] = initial
            self._cancels[summary.operation_id] = threading.Event()
            self._processes[summary.operation_id] = set()
        return BroadcastCommandSummary(summary, tuple(initial))

    def get(self, operation_id: OperationId, *, client_id: ClientId) -> BroadcastCommandSummary:
        operation = self._operations.get_operation(operation_id)
        if (
            operation.owner_client_id != client_id
            or operation.kind is not OperationKind.BROADCAST_COMMAND
        ):
            raise SshPilotError(
                ErrorCode.OPERATION_NOT_FOUND, "The requested broadcast does not exist"
            )
        with self._lock:
            targets = tuple(self._targets.get(operation_id, ()))
        return BroadcastCommandSummary(operation, targets)

    def cancel(self, operation_id: OperationId, *, client_id: ClientId) -> BroadcastCommandSummary:
        current = self.get(operation_id, client_id=client_id)
        with self._lock:
            event = self._cancels.get(operation_id)
            if event:
                event.set()
        # Target workers own terminate -> grace -> kill -> reap.  The request
        # dispatcher must never wait serially on every running SSH process.
        operation = self._operations.cancel_operation(operation_id)
        if operation.state is OperationState.CANCELLED:
            with self._lock:
                if operation_id not in self._entered:
                    self._targets[operation_id] = [
                        replace(target, state=HostCommandState.CANCELLED)
                        if target.state is HostCommandState.PENDING
                        else target
                        for target in self._targets.get(operation_id, ())
                    ]
                    self._remember_terminal_locked(operation_id)
                    current = BroadcastCommandSummary(
                        operation, tuple(self._targets.get(operation_id, ()))
                    )
        return BroadcastCommandSummary(operation, current.targets)

    def _execute(self, handle: OperationHandle, request: BroadcastCommandRequest, input_data=None) -> str:
        operation_id = handle.operation_id
        with self._lock:
            self._entered.add(operation_id)
        # start_operation can schedule immediately, before start() records the DTOs.
        while operation_id not in self._targets:
            time.sleep(0.001)
        cancel = self._cancels[operation_id]
        handle.set_cancel_hook(cancel.set)
        pending = iter(enumerate(request.connection_ids))
        futures = {}
        try:
            with ThreadPoolExecutor(
                max_workers=request.policy.concurrency_limit,
                thread_name_prefix="sshpilot-broadcast",
            ) as pool:
                while True:
                    while not cancel.is_set() and len(futures) < request.policy.concurrency_limit:
                        try:
                            index, connection_id = next(pending)
                        except StopIteration:
                            break
                        self._set(
                            operation_id,
                            index,
                            HostCommandResult(connection_id, HostCommandState.RUNNING),
                        )
                        futures[
                            pool.submit(
                                self._run_target,
                                operation_id,
                                connection_id,
                                request,
                                cancel,
                                input_data,
                            )
                        ] = index
                    if not futures:
                        break
                    done, _ = wait(futures, return_when=FIRST_COMPLETED, timeout=0.05)
                    for future in done:
                        index = futures.pop(future)
                        try:
                            result = future.result()
                        except OperationCancelled:
                            result = HostCommandResult(
                                request.connection_ids[index], HostCommandState.CANCELLED
                            )
                        self._set(operation_id, index, result)
                        terminal = sum(
                            item.state
                            in (
                                HostCommandState.SUCCEEDED,
                                HostCommandState.FAILED,
                                HostCommandState.CANCELLED,
                            )
                            for item in self._targets[operation_id]
                        )
                        handle.report(
                            "Broadcast command running",
                            terminal / len(request.connection_ids),
                        )
                    if cancel.is_set() and not futures:
                        break
                if cancel.is_set():
                    with self._lock:
                        for index, item in enumerate(self._targets[operation_id]):
                            if item.state in (
                                HostCommandState.PENDING,
                                HostCommandState.RUNNING,
                            ):
                                self._targets[operation_id][index] = replace(
                                    item, state=HostCommandState.CANCELLED
                                )
                    raise OperationCancelled()
            with self._lock:
                results = tuple(self._targets[operation_id])
        finally:
            self._interaction_broker.cancel_session(operation_id)
            self._remember_terminal(operation_id)
            if isinstance(input_data, bytearray):
                input_data[:] = b"\0" * len(input_data)
                input_data.clear()
        handle.set_result({"targets": [self._result_wire(item) for item in results]})
        if any(item.state is HostCommandState.FAILED for item in results):
            raise SshPilotError(
                ErrorCode.REMOTE_COMMAND_FAILED, "One or more broadcast targets failed"
            )
        return "Broadcast command completed"

    def _run_target(self, operation_id, connection_id, request, cancel, input_data=None):
        try:
            argv, environment = self._launch_provider.prepare_remote_command_launch(
                connection_id, request.command, interaction_policy="broker"
            )
            argv, environment = self._interaction_broker.prepare_operation_launch(
                argv,
                environment,
                scope_id=operation_id,
                connection_id=connection_id,
                hostname=str(connection_id),
                interaction_mode=request.policy.interaction_mode,
            )
            owned_process = [None]

            def process_changed(process):
                with self._lock:
                    bucket = self._processes[operation_id]
                    if process is None:
                        if owned_process[0] is not None:
                            bucket.discard(owned_process[0])
                    else:
                        owned_process[0] = process
                        bucket.add(process)

            def output(stream, text):
                if self._output_publisher:
                    self._output_publisher(operation_id, connection_id, stream, text)

            runner_kwargs = {
                "cancel_event": cancel,
                "on_process": process_changed,
                "on_output": output,
            }
            if input_data is not None:
                runner_kwargs["input_data"] = input_data
            code, stdout, stderr, truncated, timed_out = self._runner.run(
                argv, environment, request.policy, **runner_kwargs
            )
            if timed_out:
                return HostCommandResult(
                    connection_id,
                    HostCommandState.FAILED,
                    code,
                    stdout,
                    stderr,
                    truncated,
                    ServiceFailure("broadcast_timeout", "The remote command timed out"),
                )
            if code != 0:
                return HostCommandResult(
                    connection_id,
                    HostCommandState.FAILED,
                    code,
                    stdout,
                    stderr,
                    truncated,
                    ServiceFailure(
                        "broadcast_nonzero_exit", "The remote command exited unsuccessfully"
                    ),
                )
            return HostCommandResult(
                connection_id, HostCommandState.SUCCEEDED, code, stdout, stderr, truncated
            )
        except OperationCancelled:
            raise
        except SshPilotError as exc:
            return HostCommandResult(
                connection_id,
                HostCommandState.FAILED,
                failure=ServiceFailure(exc.code.value, exc.message),
            )
        except Exception:
            return HostCommandResult(
                connection_id,
                HostCommandState.FAILED,
                failure=ServiceFailure(
                    "broadcast_launch_failed", "The remote command could not be started"
                ),
            )

    def _set(self, operation_id, index, result):
        with self._lock:
            self._targets[operation_id][index] = result

    def _remember_terminal(self, operation_id: OperationId) -> None:
        """Bound retained target output alongside OperationRuntime retention."""
        with self._lock:
            self._remember_terminal_locked(operation_id)

    def _remember_terminal_locked(self, operation_id: OperationId) -> None:
        """Record terminal retention while ``self._lock`` is held."""
        self._terminal_records.pop(operation_id, None)
        self._terminal_records[operation_id] = None
        while len(self._terminal_records) > self._terminal_retention:
            expired, _ = self._terminal_records.popitem(last=False)
            self._targets.pop(expired, None)
            self._cancels.pop(expired, None)
            self._processes.pop(expired, None)
            self._entered.discard(expired)

    @staticmethod
    def _result_wire(result):
        return {
            "connection_id": result.connection_id,
            "state": result.state.value,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.truncated,
            "failure": None
            if result.failure is None
            else {"code": result.failure.code, "message": result.failure.message},
        }
