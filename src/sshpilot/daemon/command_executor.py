"""Bounded keyed worker pool for daemon-owned blocking commands."""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Hashable

logger = logging.getLogger(__name__)

DEFAULT_SESSION_COMMAND_WORKERS = 4
DEFAULT_SESSION_COMMAND_QUEUE_LIMIT = 64


@dataclass(frozen=True)
class DeferredCommand:
    """One daemon-created operation in a serial command lane."""

    key: Hashable
    operation: Callable[[], object]
    on_complete: Callable[[object], None]
    on_error: Callable[[BaseException], None]
    on_cancel: Callable[[], None]


class BoundedCommandExecutor:
    """Execute bounded commands while serializing equal command keys."""

    def __init__(self, *, max_workers: int, max_commands: int) -> None:
        if type(max_workers) is not int or max_workers < 1:
            raise ValueError("command executor worker count must be positive")
        if type(max_commands) is not int or max_commands < 1:
            raise ValueError("command executor queue limit must be positive")
        self.max_workers = max_workers
        self.max_commands = max_commands
        self._condition = threading.Condition()
        self._ready: Deque[DeferredCommand] = deque()
        self._waiting: Dict[Hashable, Deque[DeferredCommand]] = defaultdict(deque)
        self._occupied_keys: set[Hashable] = set()
        self._running_keys: set[Hashable] = set()
        self._outstanding = 0
        self._accepting = True
        self._stopping = False
        self._threads = tuple(
            threading.Thread(
                target=self._worker_main,
                name=f"sshpilot-session-command-{index + 1}",
                daemon=True,
            )
            for index in range(max_workers)
        )
        for thread in self._threads:
            thread.start()

    @property
    def outstanding(self) -> int:
        with self._condition:
            return self._outstanding

    def submit(self, command: DeferredCommand) -> bool:
        """Reserve bounded capacity without waiting for a worker."""

        if type(command) is not DeferredCommand:
            raise TypeError("deferred command is required")
        with self._condition:
            if not self._accepting or self._outstanding >= self.max_commands:
                return False
            self._outstanding += 1
            if command.key in self._occupied_keys:
                self._waiting[command.key].append(command)
            else:
                self._occupied_keys.add(command.key)
                self._ready.append(command)
                self._condition.notify()
            return True

    def submit_background(
        self,
        *,
        key: Hashable,
        operation: Callable[[], object],
        on_error: Callable[[BaseException], None],
        on_cancel: Callable[[], None],
    ) -> bool:
        """Accept fire-and-report-state work without an RPC completion callback."""

        return self.submit(
            DeferredCommand(
                key=key,
                operation=operation,
                on_complete=lambda _value: None,
                on_error=on_error,
                on_cancel=on_cancel,
            )
        )

    def stop_accepting(self, *, cancel_pending: bool) -> int:
        """Reject new work and optionally cancel commands not yet running."""

        cancelled: list[DeferredCommand] = []
        with self._condition:
            self._accepting = False
            if cancel_pending:
                while self._ready:
                    command = self._ready.popleft()
                    if command.key not in self._running_keys:
                        self._occupied_keys.discard(command.key)
                    cancelled.append(command)
                for key, commands in tuple(self._waiting.items()):
                    cancelled.extend(commands)
                    commands.clear()
                    if key not in self._running_keys:
                        self._occupied_keys.discard(key)
                self._waiting.clear()
                self._outstanding -= len(cancelled)
            self._condition.notify_all()
        for command in cancelled:
            try:
                command.on_cancel()
            except Exception as error:
                logger.error(
                    "Deferred command cancellation failed type=%s",
                    type(error).__name__,
                )
        return len(cancelled)

    def shutdown(self, *, timeout: float) -> bool:
        """Stop workers within a monotonic bound.

        Running operations must cooperate through their owned runner's
        ``close()`` implementation. Threads are daemon threads as a final
        process-exit safeguard.
        """

        deadline = time.monotonic() + max(0.0, timeout)
        self.stop_accepting(cancel_pending=True)
        with self._condition:
            while self._outstanding:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._condition.wait(remaining)
            drained = self._outstanding == 0
            self._stopping = True
            self._condition.notify_all()
        for thread in self._threads:
            if thread is threading.current_thread():
                continue
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        return drained and all(not thread.is_alive() for thread in self._threads)

    def _worker_main(self) -> None:
        while True:
            with self._condition:
                while not self._ready and not self._stopping:
                    self._condition.wait()
                if self._stopping and not self._ready:
                    return
                command = self._ready.popleft()
                self._running_keys.add(command.key)
            try:
                result = command.operation()
            except BaseException as error:
                try:
                    command.on_error(error)
                except Exception as callback_error:
                    logger.error(
                        "Deferred command error completion failed type=%s",
                        type(callback_error).__name__,
                    )
            else:
                try:
                    command.on_complete(result)
                except Exception as error:
                    logger.error(
                        "Deferred command completion failed type=%s",
                        type(error).__name__,
                    )
            finally:
                self._finish(command.key)

    def _finish(self, key: Hashable) -> None:
        with self._condition:
            self._running_keys.discard(key)
            self._outstanding -= 1
            waiting = self._waiting.get(key)
            if waiting:
                self._ready.append(waiting.popleft())
                if not waiting:
                    self._waiting.pop(key, None)
                self._condition.notify()
            else:
                self._waiting.pop(key, None)
                self._occupied_keys.discard(key)
            self._condition.notify_all()
