"""In-process PTY bridge for the embedded xterm.js terminal backend.

This is the WebKit-agnostic engine behind :class:`PyXtermBridgeBackend`: it owns a
real PTY (via :class:`Vte.Pty`), spawns the child (ssh/shell) on it *in the main
process* — no Flask server, no localhost socket — and streams the child's output
back through a plain callback. The backend turns those callbacks into
``evaluate_javascript("term.write(…)")``; keeping WebKit out of this module lets it
be unit-tested headlessly (see ``tests/test_xterm_pty_bridge.py``).

Design notes:
- Output is read on the GLib main loop from the PTY master fd, decoded with an
  *incremental* UTF-8 decoder (so a multibyte char split across two reads is not
  corrupted — the old Flask server used ``decode(errors="ignore")`` which mangled
  them), and **coalesced**: chunks are buffered and flushed on a short timer so a
  burst (``cat bigfile``/``yes``) becomes a few large ``on_output`` calls instead of
  thousands of tiny ones.
- Input (``write``) and resize go straight to the PTY (``os.write`` /
  ``Vte.Pty.set_size``); there is no round-trip through JavaScript.
- Child exit is reported via ``GLib.child_watch_add`` on the real child pid.
"""
from __future__ import annotations

import codecs
import logging
import os
from typing import Callable, Optional, Sequence

import gi

gi.require_version("Vte", "3.91")
from gi.repository import GLib, Vte  # noqa: E402

logger = logging.getLogger(__name__)

# Prefer the non-deprecated unix fd source; fall back on older PyGObject.
try:
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import GLibUnix  # type: ignore

    def _fd_add(fd, condition, callback):
        return GLibUnix.fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition, callback)
except Exception:  # pragma: no cover - very old GLib
    def _fd_add(fd, condition, callback):
        return GLib.unix_fd_add_full(GLib.PRIORITY_DEFAULT, fd, condition, callback)


class XtermPtyBridge:
    """Own a PTY, spawn a child on it, and pump I/O on the GLib main loop.

    :param on_output: called with decoded, coalesced ``str`` chunks of PTY output.
    :param on_exit: optional; called with the child's wait status when it exits.
    :param flush_ms: output coalescing interval in milliseconds.
    :param read_size: bytes per ``os.read`` from the PTY master.
    """

    def __init__(
        self,
        on_output: Callable[[str], None],
        on_exit: Optional[Callable[[int], None]] = None,
        flush_ms: int = 12,
        read_size: int = 65536,
    ) -> None:
        self._on_output = on_output
        self._on_exit = on_exit
        self._flush_ms = max(1, int(flush_ms))
        self._read_size = read_size

        self._pty: Optional[Vte.Pty] = None
        self._child_pid: Optional[int] = None
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._buf: list[str] = []

        self._fd_source: Optional[int] = None
        self._flush_source: Optional[int] = None
        self._child_source: Optional[int] = None
        self._closed = False

    # ---- lifecycle -----------------------------------------------------------

    def spawn(
        self,
        argv: Sequence[str],
        env: Optional[Sequence[str]] = None,
        cwd: Optional[str] = None,
        rows: int = 24,
        cols: int = 80,
        on_spawned: Optional[Callable[[Optional[int], Optional[Exception]], None]] = None,
    ) -> None:
        """Create a PTY and spawn ``argv`` on it (async).

        ``argv``/``env`` must already be the fully-prepared command from the shared
        connection path (sshpass/askpass/identity applied); this module adds no
        auth of its own. ``on_spawned(pid, error)`` fires when the child is running
        (or failed).
        """
        self._pty = Vte.Pty.new_sync(Vte.PtyFlags.DEFAULT)
        self._pty.set_size(max(1, rows), max(1, cols))

        def _on_spawn_finished(pty, result, *_):
            try:
                res = Vte.Pty.spawn_finish(pty, result)
                pid = res[1] if isinstance(res, (tuple, list)) else res
            except Exception as exc:  # noqa: BLE001 - report to caller
                logger.debug("PTY spawn failed: %s", exc, exc_info=True)
                if on_spawned:
                    on_spawned(None, exc)
                return
            self._child_pid = pid
            self._start_watches()
            if on_spawned:
                on_spawned(pid, None)

        self._pty.spawn_async(
            cwd,
            list(argv),
            list(env) if env is not None else None,
            GLib.SpawnFlags.DEFAULT,
            None,  # child_setup — VTE puts the child in its own session for the PTY
            None,  # child_setup_data
            -1,    # timeout: no limit
            None,  # cancellable
            _on_spawn_finished,
        )

    def _start_watches(self) -> None:
        fd = self._pty.get_fd()
        self._fd_source = _fd_add(
            fd,
            GLib.IOCondition.IN | GLib.IOCondition.HUP | GLib.IOCondition.ERR,
            self._on_readable,
        )
        self._flush_source = GLib.timeout_add(self._flush_ms, self._flush)
        if self._child_pid is not None:
            self._child_source = GLib.child_watch_add(
                GLib.PRIORITY_DEFAULT, self._child_pid, self._on_child_exit
            )

    # ---- I/O -----------------------------------------------------------------

    def write(self, data) -> None:
        """Send input to the child (keystrokes, pasted text, broadcast)."""
        if self._closed or self._pty is None:
            return
        if isinstance(data, str):
            data = data.encode("utf-8", "replace")
        try:
            os.write(self._pty.get_fd(), data)
        except OSError as exc:
            logger.debug("PTY write failed: %s", exc)

    def resize(self, rows: int, cols: int) -> None:
        if self._closed or self._pty is None:
            return
        try:
            self._pty.set_size(max(1, int(rows)), max(1, int(cols)))
        except Exception as exc:  # noqa: BLE001
            logger.debug("PTY resize failed: %s", exc)

    def _on_readable(self, fd, condition):
        if condition & (GLib.IOCondition.HUP | GLib.IOCondition.ERR):
            self._flush()
            self._fd_source = None          # this source is being removed
            self._stop_flush_source()       # child is gone; stop the pump
            return GLib.SOURCE_REMOVE
        try:
            data = os.read(fd, self._read_size)
        except OSError:
            self._fd_source = None
            self._stop_flush_source()
            return GLib.SOURCE_REMOVE
        if not data:
            self._fd_source = None
            self._stop_flush_source()
            return GLib.SOURCE_REMOVE
        self._buf.append(self._decoder.decode(data))
        return GLib.SOURCE_CONTINUE

    def _stop_flush_source(self):
        """Stop the coalescing timer. Called when the child is gone so the bridge
        does not keep a live GLib source forever even if ``close()`` is never
        called (tab teardown kills the child via its pgid, not via close())."""
        if self._flush_source is not None:
            try:
                GLib.source_remove(self._flush_source)
            except Exception:  # noqa: BLE001
                pass
            self._flush_source = None

    def _flush(self):
        if self._buf:
            chunk = "".join(self._buf)
            self._buf.clear()
            try:
                self._on_output(chunk)
            except Exception:  # noqa: BLE001 - never let a sink error kill the loop
                logger.debug("on_output sink raised", exc_info=True)
        return GLib.SOURCE_CONTINUE

    # ---- exit / teardown -----------------------------------------------------

    def _on_child_exit(self, pid, status, *_):
        self._child_source = None
        self._flush()
        self._stop_flush_source()
        if self._on_exit:
            try:
                self._on_exit(status)
            except Exception:  # noqa: BLE001
                logger.debug("on_exit sink raised", exc_info=True)

    @property
    def child_pid(self) -> Optional[int]:
        return self._child_pid

    def get_pty(self) -> Optional[Vte.Pty]:
        return self._pty

    def close(self) -> None:
        """Tear down sources and the PTY. Safe to call more than once.

        Ordered and synchronous (do not defer to GC): remove the fd/flush/child
        sources first, then drop the PTY, so no callback fires against a
        half-torn-down bridge.
        """
        if self._closed:
            return
        self._closed = True
        for attr in ("_fd_source", "_flush_source", "_child_source"):
            src = getattr(self, attr)
            if src is not None:
                try:
                    GLib.source_remove(src)
                except Exception:  # noqa: BLE001 - already removed
                    pass
                setattr(self, attr, None)
        pty, self._pty = self._pty, None
        if pty is not None:
            try:
                if hasattr(pty, "close"):
                    pty.close()
            except Exception:  # noqa: BLE001
                pass
