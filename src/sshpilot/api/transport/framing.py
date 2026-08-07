"""Length-prefixed JSON framing primitives."""

from __future__ import annotations

import json
import socket
import struct
from typing import Any, Dict, List, Optional, Union

from ..errors import ErrorCode

MAX_FRAME_SIZE = 1024 * 1024
_HEADER_SIZE = 4


class FramingError(Exception):
    """Safe framing failure with a stable public error code."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __repr__(self) -> str:
        return f"FramingError(code={self.code.value!r}, message={self.message!r})"


def _decode_payload(payload: bytes) -> Dict[str, Any]:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        raise FramingError(
            ErrorCode.INVALID_FRAME,
            "The transport frame is not valid UTF-8",
        ) from None
    try:
        value = json.loads(text)
    except (json.JSONDecodeError, RecursionError):
        raise FramingError(
            ErrorCode.INVALID_FRAME,
            "The transport frame does not contain valid JSON",
        ) from None
    if type(value) is not dict:
        raise FramingError(
            ErrorCode.INVALID_FRAME,
            "The transport frame must contain a JSON object",
        )
    return value


def encode_frame(message: Dict[str, Any], *, max_size: int = MAX_FRAME_SIZE) -> bytes:
    """Encode one deterministic JSON object with a 4-byte length prefix."""

    if type(message) is not dict:
        raise TypeError("transport message must be a dictionary")
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError, RecursionError):
        raise FramingError(
            ErrorCode.INVALID_FRAME,
            "The transport message is not JSON-safe",
        ) from None
    if not payload:
        raise FramingError(ErrorCode.INVALID_FRAME, "Empty frames are not permitted")
    if len(payload) > max_size:
        raise FramingError(
            ErrorCode.FRAME_TOO_LARGE,
            "The transport frame exceeds the maximum size",
        )
    return struct.pack(">I", len(payload)) + payload


def encode_binary_frame(payload: bytes, *, max_size: int = MAX_FRAME_SIZE) -> bytes:
    """Frame already-validated binary data without JSON conversion."""

    if not isinstance(payload, bytes):
        raise TypeError("binary frame payload must be bytes")
    if not payload:
        raise FramingError(ErrorCode.INVALID_FRAME, "Empty frames are not permitted")
    if len(payload) > max_size:
        raise FramingError(
            ErrorCode.FRAME_TOO_LARGE,
            "The transport frame exceeds the maximum size",
        )
    return struct.pack(">I", len(payload)) + payload


class FrameDecoder:
    """Incrementally decode complete frames from fragmented socket reads."""

    def __init__(self, *, max_size: int = MAX_FRAME_SIZE) -> None:
        self._max_size = max_size
        self._buffer = bytearray()
        self._expected_size: Optional[int] = None

    @property
    def incomplete(self) -> bool:
        return bool(self._buffer) or self._expected_size is not None

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        if not isinstance(data, bytes):
            raise TypeError("frame data must be bytes")
        self._buffer.extend(data)
        messages = []
        while True:
            if self._expected_size is None:
                if len(self._buffer) < _HEADER_SIZE:
                    break
                self._expected_size = struct.unpack(">I", self._buffer[:_HEADER_SIZE])[0]
                del self._buffer[:_HEADER_SIZE]
                if self._expected_size == 0:
                    self._expected_size = None
                    raise FramingError(
                        ErrorCode.INVALID_FRAME,
                        "Empty frames are not permitted",
                    )
                if self._expected_size > self._max_size:
                    self._expected_size = None
                    raise FramingError(
                        ErrorCode.FRAME_TOO_LARGE,
                        "The transport frame exceeds the maximum size",
                    )
            if len(self._buffer) < self._expected_size:
                break
            payload = bytes(self._buffer[: self._expected_size])
            del self._buffer[: self._expected_size]
            self._expected_size = None
            messages.append(_decode_payload(payload))
        return messages

    def finish(self) -> None:
        """Reject a peer closing in the middle of a header or body."""

        if self.incomplete:
            raise FramingError(
                ErrorCode.INVALID_FRAME,
                "The transport connection closed during a frame",
            )


class MultiplexedFrameDecoder:
    """Incrementally decode JSON and negotiated binary terminal frames."""

    def __init__(self, *, max_size: int = MAX_FRAME_SIZE) -> None:
        self._max_size = max_size
        self._buffer = bytearray()
        self._expected_size: Optional[int] = None

    @property
    def incomplete(self) -> bool:
        return bool(self._buffer) or self._expected_size is not None

    def feed(self, data: bytes) -> List[Union[Dict[str, Any], object]]:
        from .secret_frames import decode_secret_payload, is_secret_payload
        from .terminal_frames import decode_terminal_payload, is_terminal_payload

        if not isinstance(data, bytes):
            raise TypeError("frame data must be bytes")
        self._buffer.extend(data)
        messages: List[Union[Dict[str, Any], object]] = []
        while True:
            if self._expected_size is None:
                if len(self._buffer) < _HEADER_SIZE:
                    break
                self._expected_size = struct.unpack(
                    ">I", self._buffer[:_HEADER_SIZE]
                )[0]
                del self._buffer[:_HEADER_SIZE]
                if self._expected_size == 0:
                    self._expected_size = None
                    raise FramingError(
                        ErrorCode.INVALID_FRAME,
                        "Empty frames are not permitted",
                    )
                if self._expected_size > self._max_size:
                    self._expected_size = None
                    raise FramingError(
                        ErrorCode.FRAME_TOO_LARGE,
                        "The transport frame exceeds the maximum size",
                    )
            if len(self._buffer) < self._expected_size:
                break
            payload = bytes(self._buffer[: self._expected_size])
            del self._buffer[: self._expected_size]
            self._expected_size = None
            messages.append(
                decode_terminal_payload(payload)
                if is_terminal_payload(payload)
                else (
                    decode_secret_payload(payload)
                    if is_secret_payload(payload)
                    else _decode_payload(payload)
                )
            )
        return messages

    def finish(self) -> None:
        if self.incomplete:
            raise FramingError(
                ErrorCode.INVALID_FRAME,
                "The transport connection closed during a frame",
            )


def _receive_exact(sock: socket.socket, size: int, *, allow_clean_close: bool) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            if not chunks and allow_clean_close:
                raise EOFError("transport connection closed")
            raise FramingError(
                ErrorCode.INVALID_FRAME,
                "The transport connection closed during a frame",
            )
        chunks.extend(chunk)
    return bytes(chunks)


def receive_frame(
    sock: socket.socket,
    *,
    max_size: int = MAX_FRAME_SIZE,
) -> Dict[str, Any]:
    """Read exactly one complete frame from a blocking socket."""

    header = _receive_exact(sock, _HEADER_SIZE, allow_clean_close=True)
    size = struct.unpack(">I", header)[0]
    if size == 0:
        raise FramingError(ErrorCode.INVALID_FRAME, "Empty frames are not permitted")
    if size > max_size:
        raise FramingError(
            ErrorCode.FRAME_TOO_LARGE,
            "The transport frame exceeds the maximum size",
        )
    return _decode_payload(_receive_exact(sock, size, allow_clean_close=False))


def receive_multiplexed_frame(
    sock: socket.socket,
    *,
    max_size: int = MAX_FRAME_SIZE,
) -> Union[Dict[str, Any], object]:
    """Read one JSON envelope or negotiated binary terminal frame."""

    from .secret_frames import decode_secret_payload, is_secret_payload
    from .terminal_frames import decode_terminal_payload, is_terminal_payload

    header = _receive_exact(sock, _HEADER_SIZE, allow_clean_close=True)
    size = struct.unpack(">I", header)[0]
    if size == 0:
        raise FramingError(ErrorCode.INVALID_FRAME, "Empty frames are not permitted")
    if size > max_size:
        raise FramingError(
            ErrorCode.FRAME_TOO_LARGE,
            "The transport frame exceeds the maximum size",
        )
    payload = _receive_exact(sock, size, allow_clean_close=False)
    if is_terminal_payload(payload):
        return decode_terminal_payload(payload)
    if is_secret_payload(payload):
        return decode_secret_payload(payload)
    return _decode_payload(payload)
