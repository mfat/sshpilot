"""OSC 52 clipboard-write detection for terminal output (GTK-free).

Neither emulator acts on OSC 52: VTE parses it and drops it (it sits in the
ignored list in ``vteseq.cc``), and the vendored xterm.js has no clipboard
addon. Every byte a daemon-backed tab or a PyXterm local shell paints goes
through Python first, so ``Osc52Scanner`` watches that stream and reports
clipboard writes. It never alters the bytes; the emulator still gets them.

Handled::

    ESC ] 52 ; <selection> ; <base64> BEL
    ESC ] 52 ; <selection> ; <base64> ESC \\

Deliberately not handled:

* Reads (``?`` payload) — a remote must never see the local clipboard.
* Empty payloads — xterm clears the selection; we leave the clipboard alone.
* 8-bit C1 OSC/ST (0x9D/0x9C) — those bytes are UTF-8 continuation bytes too,
  and VTE ignores them in UTF-8 mode as well.

Some senders (e.g. the ``osc52`` PyPI tool) split large copies into several
sequences written back to back. Sequences for the same selection with no byte
between them form one run, and each completed sequence reports the whole run
so far; callers coalesce the reports briefly and act on the last one.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from typing import Optional, Union

OSC52_POLICY_NEVER = "never"
OSC52_POLICY_ASK = "ask"
OSC52_POLICY_ALWAYS = "always"
OSC52_POLICIES = (OSC52_POLICY_NEVER, OSC52_POLICY_ASK, OSC52_POLICY_ALWAYS)
OSC52_DEFAULT_POLICY = OSC52_POLICY_ASK

# Limit on the base64 text of one run, in KiB.
OSC52_DEFAULT_MAX_KIB = 1024
OSC52_MIN_MAX_KIB = 16
OSC52_MAX_MAX_KIB = 16384

TARGET_CLIPBOARD = "clipboard"
TARGET_PRIMARY = "primary"

_MARKER = b"\x1b]52;"
_BEL = 0x07
_ESC = 0x1B
_CAN = 0x18
_SUB = 0x1A
_ST_FINAL = ord("\\")
_MAX_SELECTION_LEN = 16
_SELECTION_CHARS = frozenset(b"cpqs01234567")
# The bytes that end or abort the payload: BEL, ESC (start of ST), CAN, SUB.
_PAYLOAD_STOP_RE = re.compile(rb"[\x07\x1b\x18\x1a]")
# Other C0 controls inside an OSC string are ignored by VT parsers, so a
# wrapped ``base64`` (newlines every 76 columns) still decodes.
_IGNORED_CONTROLS = bytes(range(0x20)) + b"\x7f"
_BASE64_RE = re.compile(rb"[A-Za-z0-9+/]*={0,2}")

_IDLE = 0
_SELECTION = 1
_PAYLOAD = 2
_PAYLOAD_ESC = 3


def normalize_osc52_policy(value) -> str:
    text = str(value or "").strip().lower()
    return text if text in OSC52_POLICIES else OSC52_DEFAULT_POLICY


def normalize_osc52_max_kib(value) -> int:
    try:
        kib = int(value)
    except (TypeError, ValueError):
        return OSC52_DEFAULT_MAX_KIB
    return max(OSC52_MIN_MAX_KIB, min(kib, OSC52_MAX_MAX_KIB))


def selection_targets(selection: str) -> frozenset[str]:
    """Map the OSC 52 selection parameter to clipboards we write.

    Empty means ``s 0`` in xterm; like most terminals we treat it, ``c`` and
    ``s`` as the clipboard. Cut buffers (digits) and ``q`` are ignored.
    """
    if not selection:
        return frozenset({TARGET_CLIPBOARD})
    targets = set()
    if "c" in selection or "s" in selection:
        targets.add(TARGET_CLIPBOARD)
    if "p" in selection:
        targets.add(TARGET_PRIMARY)
    return frozenset(targets)


@dataclass(frozen=True)
class ClipboardWrite:
    """Text a remote program asked to put on the local clipboard."""

    targets: frozenset[str]
    text: str
    # Sequences joined into this write (see the module docstring).
    parts: int = 1


class Osc52Scanner:
    """Find OSC 52 clipboard writes in a terminal output stream.

    Stateful: a sequence split across ``feed()`` calls is still found.
    """

    def __init__(self, max_bytes: int = OSC52_DEFAULT_MAX_KIB * 1024) -> None:
        self.max_bytes = max_bytes
        self._state = _IDLE
        self._tail = b""
        self._selection = bytearray()
        self._payload = bytearray()
        self._payload_too_long = False
        # Previous sequence ended exactly at the current stream position.
        self._adjacent = False
        self._run_targets: Optional[frozenset[str]] = None
        self._run_parts: list[bytes] = []
        self._run_b64_len = 0
        self._run_dropped = False

    def reset(self) -> None:
        self.__init__(self.max_bytes)

    def feed(self, data: Union[bytes, bytearray, memoryview, str]) -> list[ClipboardWrite]:
        """Scan one chunk of output; return the clipboard writes it completed."""
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        if not data:
            return []
        buf = self._tail + bytes(data)
        self._tail = b""
        writes: list[ClipboardWrite] = []
        pos = 0
        end = len(buf)
        while pos < end:
            if self._state == _IDLE:
                if self._adjacent:
                    head = buf[pos:pos + len(_MARKER)]
                    if len(head) < len(_MARKER) and _MARKER.startswith(head):
                        # Might be the next chunk of this run; decide later.
                        self._tail = head
                        return writes
                    if head != _MARKER:
                        self._end_run()
                idx = buf.find(_MARKER, pos)
                if idx < 0:
                    self._tail = _partial_marker_suffix(buf, pos)
                    break
                if idx != pos:
                    self._end_run()
                pos = idx + len(_MARKER)
                self._adjacent = False
                self._state = _SELECTION
                self._selection.clear()
            elif self._state == _SELECTION:
                byte = buf[pos]
                if byte == ord(";"):
                    pos += 1
                    self._state = _PAYLOAD
                    self._payload.clear()
                    self._payload_too_long = False
                elif (
                    byte in _SELECTION_CHARS
                    and len(self._selection) < _MAX_SELECTION_LEN
                ):
                    self._selection.append(byte)
                    pos += 1
                else:
                    # Not a clipboard sequence after all; rescan from here.
                    self._abort()
            elif self._state == _PAYLOAD:
                match = _PAYLOAD_STOP_RE.search(buf, pos)
                stop = match.start() if match else end
                self._append_payload(buf[pos:stop])
                if match is None:
                    pos = end
                    break
                code = buf[stop]
                pos = stop + 1
                if code == _BEL:
                    self._complete(writes)
                elif code == _ESC:
                    self._state = _PAYLOAD_ESC
                else:  # CAN / SUB cancel the sequence
                    self._abort()
            else:  # _PAYLOAD_ESC
                if buf[pos] == _ST_FINAL:
                    pos += 1
                    self._complete(writes)
                else:
                    # That ESC starts something else; hand it back to IDLE.
                    self._abort()
                    buf = b"\x1b" + buf[pos:]
                    pos = 0
                    end = len(buf)
        return writes

    # -- internals ---------------------------------------------------------

    def _append_payload(self, chunk: bytes) -> None:
        if self._payload_too_long or not chunk:
            return
        chunk = chunk.translate(None, _IGNORED_CONTROLS)
        if self._run_b64_len + len(self._payload) + len(chunk) > self.max_bytes:
            self._payload_too_long = True
            self._payload.clear()
            return
        self._payload.extend(chunk)

    def _abort(self) -> None:
        self._state = _IDLE
        self._selection.clear()
        self._payload.clear()
        self._payload_too_long = False
        self._end_run()

    def _end_run(self) -> None:
        self._adjacent = False
        self._run_targets = None
        self._run_parts = []
        self._run_b64_len = 0
        self._run_dropped = False

    def _complete(self, writes: list[ClipboardWrite]) -> None:
        self._state = _IDLE
        targets = selection_targets(self._selection.decode("ascii"))
        payload = bytes(self._payload)
        too_long = self._payload_too_long
        self._selection.clear()
        self._payload.clear()
        self._payload_too_long = False

        if self._run_targets is not None and self._run_targets != targets:
            self._end_run()
        if payload == b"?" or not targets:
            # A read, or only selections we do not write: never part of a run.
            self._end_run()
            return
        self._run_targets = targets
        self._adjacent = True
        if self._run_dropped:
            return
        if too_long:
            self._run_dropped = True
            self._run_parts = []
            return
        if not payload:
            return
        if _BASE64_RE.fullmatch(payload) is None:
            self._run_dropped = True
            self._run_parts = []
            return
        try:
            decoded = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError):
            self._run_dropped = True
            self._run_parts = []
            return
        self._run_b64_len += len(payload)
        self._run_parts.append(decoded)
        data = b"".join(self._run_parts)
        if not data:
            return
        writes.append(
            ClipboardWrite(
                targets=targets,
                text=data.decode("utf-8", "replace"),
                parts=len(self._run_parts),
            )
        )


def _partial_marker_suffix(buf: bytes, start: int) -> bytes:
    """Longest suffix of ``buf[start:]`` that could begin the marker."""
    for size in range(min(len(_MARKER) - 1, len(buf) - start), 0, -1):
        if buf.endswith(_MARKER[:size]):
            return buf[-size:]
    return b""
