"""Experimental VTE-as-emulator widget for daemon terminal sessions.

Production SSH tabs use ``TerminalWidget.start_daemon_session`` with the same
``DaemonTerminalSessionController``. This widget remains a compact development
harness and shares that controller rather than a second open/attach path.
"""

from __future__ import annotations

import logging
from .runtime_identity import new_terminal_id
from gi.repository import GLib, Gtk, Vte

from .daemon_interaction_dialogs import DaemonInteractionDialogs
from .terminal_session_controller import (
    DaemonTerminalSessionController,
    daemon_terminal_capabilities_missing,
)

logger = logging.getLogger(__name__)


class DaemonTerminalWidget(Gtk.Box):
    """VTE owns no child process; the daemon owns the PTY and OpenSSH child."""

    def __init__(self, client, bridge, connection_id) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        if daemon_terminal_capabilities_missing(client):
            raise RuntimeError(
                "Experimental daemon terminal capabilities are unavailable"
            )
        self._client = client
        self._bridge = bridge
        self._connection_id = connection_id
        self._closed = False
        self._received_bytes = 0
        self._terminal = Vte.Terminal()
        self.append(self._terminal)
        self._interaction_dialogs = DaemonInteractionDialogs(
            client,
            bridge,
            self,
        )
        self._controller = DaemonTerminalSessionController(
            client=client,
            bridge=bridge,
            connection_id=connection_id,
            view_id=f"experimental-{new_terminal_id()}",
            on_output=self._on_output,
            on_continuity_lost=self._on_continuity_lost,
            on_error=self._on_error,
        )
        self._terminal.connect("commit", self._on_commit)
        # Not "char-size-changed" (VTE docs: fires only on font/cell-metric
        # changes, e.g. zoom, never on a widget resize) and not
        # notify::column-count/row-count (not real GObject properties on
        # Vte.Terminal — that notify never fires). GTK4 also removed the
        # public size-allocate signal. Polling once per rendered frame via
        # add_tick_callback is the only mechanism GTK4 offers for observing
        # VTE's actual grid size — it only runs while mapped, and this only
        # invokes _on_size_changed on an actual change. Leaving this
        # unresolved left the remote PTY stuck at its opening size
        # regardless of how big the window grew (GH #1164).
        self._size_poll_state = {"size": None}
        self._terminal.add_tick_callback(self._on_size_tick)

    @property
    def terminal(self):
        return self._terminal

    def start(self) -> None:
        from .api.models.terminal import TerminalDimensions

        rows = max(1, min(1000, int(self._terminal.get_row_count() or 24)))
        columns = max(
            1,
            min(1000, int(self._terminal.get_column_count() or 80)),
        )
        self._controller.open(
            self._connection_id,
            TerminalDimensions(rows=rows, columns=columns),
        )

    def _on_output(self, data: bytes) -> None:
        if self._closed:
            return
        self._received_bytes += len(data)
        tab = self._controller.tab_state
        if tab.session_id is not None:
            self._interaction_dialogs.set_session(tab.session_id)
        self._terminal.feed(data)

    @property
    def received_bytes(self) -> int:
        return self._received_bytes

    @property
    def last_sequence(self) -> int:
        return self._controller.tab_state.expected_sequence

    def _on_commit(self, _terminal, text, _size) -> None:
        if self._closed:
            return
        self._controller.send_input(text.encode("utf-8"))

    def _on_size_tick(self, widget, _frame_clock) -> bool:
        try:
            size = (widget.get_row_count(), widget.get_column_count())
        except Exception:
            return GLib.SOURCE_CONTINUE
        state = self._size_poll_state
        if state["size"] is not None and size != state["size"]:
            self._on_size_changed(widget)
        state["size"] = size
        return GLib.SOURCE_CONTINUE

    def _on_size_changed(self, _terminal, _pspec=None) -> None:
        if self._closed:
            return
        from .api.models.terminal import TerminalDimensions

        rows = max(1, min(1000, int(self._terminal.get_row_count() or 24)))
        columns = max(
            1,
            min(1000, int(self._terminal.get_column_count() or 80)),
        )
        self._controller.resize(TerminalDimensions(rows=rows, columns=columns))

    def _on_continuity_lost(self) -> None:
        self._terminal.feed(
            b"\r\n[Earlier terminal output is no longer available]\r\n"
        )

    @staticmethod
    def _on_error(error) -> None:
        logger.warning(
            "Experimental daemon terminal operation failed code=%s",
            getattr(getattr(error, "code", None), "value", "internal_error"),
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._interaction_dialogs.close()
        # Detach by default so experimental teardown matches production policy.
        self._controller.detach()
