"""Bounded per-session terminal replay state."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, Iterable, Tuple

DEFAULT_SESSION_REPLAY_BYTES = 2 * 1024 * 1024
DEFAULT_GLOBAL_REPLAY_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class ReplaySlice:
    requested_sequence: int
    available_start: int
    returned_start: int
    returned_end: int
    live_sequence: int
    chunks: Tuple[tuple[int, bytes], ...]
    truncated: bool
    eof: bool


class TerminalReplayBuffer:
    """Chunked byte ring with absolute byte-offset sequences."""

    def __init__(self, max_bytes: int = DEFAULT_SESSION_REPLAY_BYTES) -> None:
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("terminal replay byte limit must be positive")
        self.max_bytes = max_bytes
        self._chunks: Deque[tuple[int, bytes]] = deque()
        self._start = 0
        self._end = 0
        self._retained = 0
        self._eof = False

    @property
    def start_sequence(self) -> int:
        return self._start

    @property
    def end_sequence(self) -> int:
        return self._end

    @property
    def retained_bytes(self) -> int:
        return self._retained

    @property
    def eof(self) -> bool:
        return self._eof

    def append(self, data: bytes) -> tuple[int, int]:
        if not isinstance(data, bytes):
            raise TypeError("terminal replay data must be bytes")
        if self._eof:
            raise RuntimeError("terminal output cannot follow EOF")
        start = self._end
        if not data:
            return start, start
        self._end += len(data)
        if len(data) >= self.max_bytes:
            retained = data[-self.max_bytes :]
            retained_start = self._end - len(retained)
            self._chunks.clear()
            self._chunks.append((retained_start, retained))
            self._retained = len(retained)
            self._start = retained_start
            return start, self._end
        self._chunks.append((start, data))
        self._retained += len(data)
        self._evict()
        return start, self._end

    def mark_eof(self) -> int:
        self._eof = True
        return self._end

    def replay(self, from_sequence: int, maximum_bytes: int) -> ReplaySlice:
        if type(from_sequence) is not int or from_sequence < 0:
            raise ValueError("terminal replay sequence must not be negative")
        if type(maximum_bytes) is not int or maximum_bytes < 1:
            raise ValueError("terminal replay maximum must be positive")
        if from_sequence > self._end:
            raise ValueError("terminal replay sequence exceeds live output")
        returned_start = max(from_sequence, self._start)
        returned_end = min(self._end, returned_start + maximum_bytes)
        chunks = tuple(
            self._slice_chunks(returned_start, returned_end)
        )
        return ReplaySlice(
            requested_sequence=from_sequence,
            available_start=self._start,
            returned_start=returned_start,
            returned_end=returned_end,
            live_sequence=self._end,
            chunks=chunks,
            truncated=from_sequence < self._start or returned_end < self._end,
            eof=self._eof and returned_end == self._end,
        )

    def clear(self) -> None:
        self._chunks.clear()
        self._retained = 0
        self._start = self._end

    def trim_to(self, max_retained: int) -> int:
        """Evict oldest bytes until retained_bytes <= max_retained.

        Returns the number of bytes freed. Sequence offsets advance so
        subsequent replay reports truncation correctly.
        """

        if type(max_retained) is not int or max_retained < 0:
            raise ValueError("trim target must be a non-negative int")
        before = self._retained
        if max_retained == 0:
            self.clear()
            return before - self._retained
        previous_limit = self.max_bytes
        self.max_bytes = max_retained
        try:
            self._evict()
        finally:
            self.max_bytes = previous_limit
        return before - self._retained

    def _evict(self) -> None:
        excess = self._retained - self.max_bytes
        while excess > 0 and self._chunks:
            sequence, chunk = self._chunks[0]
            if len(chunk) <= excess:
                self._chunks.popleft()
                self._retained -= len(chunk)
                self._start = sequence + len(chunk)
                excess -= len(chunk)
                continue
            self._chunks[0] = (sequence + excess, chunk[excess:])
            self._retained -= excess
            self._start = sequence + excess
            excess = 0
        if not self._chunks:
            self._start = self._end

    def _slice_chunks(
        self,
        start: int,
        end: int,
    ) -> Iterable[tuple[int, bytes]]:
        if start >= end:
            return
        for sequence, chunk in self._chunks:
            chunk_end = sequence + len(chunk)
            if chunk_end <= start:
                continue
            if sequence >= end:
                break
            slice_start = max(start, sequence)
            slice_end = min(end, chunk_end)
            yield (
                slice_start,
                chunk[slice_start - sequence : slice_end - sequence],
            )
