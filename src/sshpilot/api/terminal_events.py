"""Frontend-neutral terminal stream subscriptions."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .models.common import SessionId
from .models.terminal import TerminalOutput
from .errors import SshPilotError

TerminalOutputCallback = Callable[[TerminalOutput], None]
TerminalContinuityCallback = Callable[[SessionId, int, int], None]
TerminalEofCallback = Callable[[SessionId, int], None]
TerminalErrorCallback = Callable[[SshPilotError], None]


@dataclass
class TerminalSubscription:
    """Idempotent handle for one local terminal callback registration."""

    _cancel: Callable[[], None]

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._closed = False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self._cancel()

    unsubscribe = close
