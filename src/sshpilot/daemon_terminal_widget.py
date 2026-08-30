"""Experimental VTE-as-emulator widget for daemon terminal sessions.

Production SSH tabs use ``TerminalWidget.start_daemon_session`` with the same
``DaemonTerminalSessionController``. This widget remains a compact development
harness and shares that controller rather than a second open/attach path.
"""

from __future__ import annotations

import logging
from .runtime_identity import new_terminal_id
from gi.repository import Gtk

from .daemon_interaction_dialogs import DaemonInteractionDialogs
from .terminal_backends import GridTrackingVteTerminal
from .terminal_display_pause import (
    DeferredDisplayFeed,
    SelectionFeedPauseController,
)
from .terminal_input import (
    MouseTrackingState,
    commit_payload_to_bytes,
    sgr_reports_to_legacy,
)
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
        self._terminal = GridTrackingVteTerminal()
        self._mouse_tracking = MouseTrackingState()
        self._display_feed_pause = DeferredDisplayFeed()
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
        self._size_handler = self._terminal.connect(
            "grid-size-changed",
            self._on_size_changed,
        )
        self._install_selection_feed_pause()

    def _install_selection_feed_pause(self) -> None:
        """Buffer daemon output while the pointer is drag-selecting.

        See SelectionFeedPauseController: an observer, never a gesture, so
        VTE keeps both its drag-select and its mouse reports.
        """
        self._selection_feed_observer = SelectionFeedPauseController(
            self._display_feed_pause,
            mouse_tracking_active=lambda: self._mouse_tracking.active,
            flush=self._resume_selection_display_feed,
        )
        self._selection_feed_observer.install(self._terminal)

    def _resume_selection_display_feed(self) -> None:
        deferred = self._display_feed_pause.end()
        if deferred:
            self._paint_display(deferred)

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
        accepted = self._display_feed_pause.accept(data)
        if accepted is None:
            return
        self._paint_display(accepted)

    def _paint_display(self, data: bytes) -> None:
        self._mouse_tracking.feed(data)
        self._terminal.feed(data)
        # VTE drops its legacy ESC[M reports when it owns no PTY, so drive it
        # in SGR instead and translate back on commit (GH #1212).
        local_modes = self._mouse_tracking.take_local_mode_feed()
        if local_modes:
            self._terminal.feed(local_modes)

    @property
    def received_bytes(self) -> int:
        return self._received_bytes

    @property
    def last_sequence(self) -> int:
        return self._controller.tab_state.expected_sequence

    def _on_commit(self, _terminal, text, size) -> None:
        if self._closed:
            return
        data = commit_payload_to_bytes(text, size)
        if self._mouse_tracking.translating_legacy:
            data = sgr_reports_to_legacy(data)
            if not data:
                return
        self._controller.send_input(data)

    def _on_size_changed(self, _terminal, *_details) -> None:
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
        if self._size_handler is not None:
            self._terminal.disconnect(self._size_handler)
            self._size_handler = None
        observer = getattr(self, "_selection_feed_observer", None)
        if observer is not None:
            observer.uninstall()
            self._selection_feed_observer = None
        self._display_feed_pause.reset()
        self._terminal.disable_grid_tracking()
        self._interaction_dialogs.close()
        # Detach by default so experimental teardown matches production policy.
        self._controller.detach()
