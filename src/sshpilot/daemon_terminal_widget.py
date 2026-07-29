"""Experimental VTE-as-emulator widget for daemon terminal sessions."""

from __future__ import annotations

import logging

from gi.repository import Gtk, Vte

from .api.capabilities import Capability
from .api.models.sessions import (
    AttachSessionRequest,
    CloseSessionRequest,
    OpenSessionRequest,
)
from .api.models.terminal import (
    ResizeTerminalRequest,
    TerminalDimensions,
    TerminalInput,
)
from .daemon_interaction_dialogs import DaemonInteractionDialogs

logger = logging.getLogger(__name__)


class DaemonTerminalWidget(Gtk.Box):
    """Development-only terminal renderer; VTE owns no child process here."""

    def __init__(self, client, bridge, connection_id) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        required = {
            Capability.SESSIONS_READ,
            Capability.SESSIONS_WRITE,
            Capability.SESSIONS_EVENTS,
            Capability.TERMINAL_OUTPUT,
            Capability.TERMINAL_INPUT,
            Capability.TERMINAL_RESIZE,
            Capability.TERMINAL_REPLAY,
            Capability.INTERACTIONS_READ,
            Capability.INTERACTIONS_RESPOND,
            Capability.INTERACTIONS_EVENTS,
            Capability.INTERACTIONS_HOST_KEY,
            Capability.INTERACTIONS_PASSWORD,
            Capability.INTERACTIONS_PASSPHRASE,
        }
        supported = client.get_capabilities().supported
        if not required <= supported:
            raise RuntimeError(
                "Experimental daemon terminal capabilities are unavailable"
            )
        self._client = client
        self._bridge = bridge
        self._connection_id = connection_id
        self._session = None
        self._attachment = None
        self._stream = None
        self._closed = False
        self._received_bytes = 0
        self._last_sequence = 0
        self._terminal = Vte.Terminal()
        self.append(self._terminal)
        self._terminal.connect("commit", self._on_commit)
        self._terminal.connect("char-size-changed", self._on_size_changed)
        self._interaction_dialogs = DaemonInteractionDialogs(
            client,
            bridge,
            self,
        )

    @property
    def terminal(self):
        return self._terminal

    def start(self) -> None:
        dimensions = self._dimensions()
        self._bridge.submit(
            lambda: self._client.open_session(
                OpenSessionRequest(
                    connection_id=self._connection_id,
                    dimensions=dimensions,
                )
            ),
            on_success=self._on_opened,
            on_error=self._on_error,
        )

    def _on_opened(self, summary) -> None:
        if self._closed:
            return
        self._session = summary
        self._interaction_dialogs.set_session(summary.id)
        self._stream = self._bridge.bind_terminal(
            self._client,
            summary.id,
            on_output=self._on_output,
            on_continuity_lost=self._on_continuity_lost,
            on_error=self._on_error,
        )
        self._bridge.submit(
            lambda: self._client.attach_session(
                AttachSessionRequest(
                    session_id=summary.id,
                    request_input=True,
                    want_terminal_output=True,
                    from_sequence=0,
                )
            ),
            on_success=self._on_attached,
            on_error=self._on_error,
        )

    def _on_attached(self, result) -> None:
        if not self._closed:
            self._attachment = result.attachment

    def _on_output(self, output) -> None:
        if self._closed:
            return
        self._received_bytes += len(output.data)
        self._last_sequence = output.next_sequence
        self._terminal.feed(output.data)

    @property
    def received_bytes(self) -> int:
        """Development diagnostic; terminal contents remain private."""

        return self._received_bytes

    @property
    def last_sequence(self) -> int:
        return self._last_sequence

    def _on_commit(self, _terminal, text, _size) -> None:
        attachment = self._attachment
        session = self._session
        if self._closed or attachment is None or session is None:
            return
        data = text.encode("utf-8")
        self._bridge.submit(
            lambda: self._client.send_terminal_input(
                TerminalInput(
                    session_id=session.id,
                    attachment_id=attachment.id,
                    data=data,
                )
            ),
            on_success=lambda _value: None,
            on_error=self._on_error,
        )

    def _on_size_changed(self, _terminal, _width, _height) -> None:
        attachment = self._attachment
        session = self._session
        if self._closed or attachment is None or session is None:
            return
        dimensions = self._dimensions()
        self._bridge.submit(
            lambda: self._client.resize_terminal(
                ResizeTerminalRequest(
                    session_id=session.id,
                    attachment_id=attachment.id,
                    dimensions=dimensions,
                )
            ),
            on_success=lambda _value: None,
            on_error=self._on_error,
        )

    def _dimensions(self) -> TerminalDimensions:
        rows = max(1, min(1000, int(self._terminal.get_row_count() or 24)))
        columns = max(
            1,
            min(1000, int(self._terminal.get_column_count() or 80)),
        )
        return TerminalDimensions(rows=rows, columns=columns)

    def _on_continuity_lost(
        self,
        _session_id,
        _expected,
        _available,
    ) -> None:
        self._terminal.feed(
            b"\r\n[sshPilot terminal history continuity was lost]\r\n"
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
        if self._stream is not None:
            self._stream.close()
            self._stream = None
        if self._session is not None:
            session_id = self._session.id
            try:
                self._bridge.submit(
                    lambda: self._client.close_session(
                        CloseSessionRequest(session_id=session_id)
                    ),
                    on_success=lambda _value: None,
                    on_error=self._on_error,
                )
            except RuntimeError:
                pass
