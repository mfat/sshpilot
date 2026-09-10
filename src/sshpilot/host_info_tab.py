"""WebKit Host Info tab — renders daemon gather results as an HTML page.

Presentation only.  Probes go through
:class:`~sshpilot.gtk.host_info_controller.HostInfoController`; the page is
built by :mod:`sshpilot.host_info_shell` and updated with payloads from
:mod:`sshpilot.host_info_payload`.
"""

from __future__ import annotations

import json
import logging
from gettext import gettext as _
from typing import Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .api.connection_identity import connection_id_for
from .api.models.common import SessionId
from .api.models.host_info import HostInfoProbe, LiveSample
from .gtk.host_info_controller import HostInfoController, HostInfoProbeBusy
from .host_info_payload import live_payload, snapshot_payload, status_payload
from .host_info_shell import build_host_info_html
from .web_tab import webkit_available

logger = logging.getLogger(__name__)

_LIVE_INTERVAL_MS = 2000
_BACKGROUND_INTERVAL_MS = 10000


class HostInfoTab(Gtk.Box):
    """Notebook tab that shows one connection's host information in WebKit."""

    def __init__(self, window, connection) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        if not webkit_available():
            raise RuntimeError("WebKit 6 is required for HostInfoTab")

        from gi.repository import WebKit

        self._window = window
        self._connection = connection
        self._closed = False
        self._controller: Optional[HostInfoController] = None
        self._interaction_dialogs = None
        self._snapshot = None
        self._previous_live: Optional[LiveSample] = None
        self._previous_live_time = 0.0
        self._live_timer_id = 0
        self._live_interval_ms = _LIVE_INTERVAL_MS
        self._active_handler_id = 0
        self._js_ready = False
        # Keep snapshot and live separately so a LIVE sample cannot overwrite a
        # pending FULL gather before the page is ready to receive either.
        self._pending_host_info: Optional[str] = None
        self._pending_live: Optional[str] = None

        self.set_hexpand(True)
        self.set_vexpand(True)

        self._ucm = WebKit.UserContentManager()
        try:
            self._ucm.register_script_message_handler("hostInfo", None)
        except TypeError:
            self._ucm.register_script_message_handler("hostInfo")
        self._ucm.connect("script-message-received::hostInfo", self._on_script_message)

        self._webview = WebKit.WebView(user_content_manager=self._ucm)
        try:
            settings = self._webview.get_settings()
            settings.set_enable_javascript(True)
            # Surface page console noise while diagnosing the spinner hang.
            settings.set_enable_write_console_messages_to_stdout(True)
        except Exception:
            logger.debug("Host info WebView settings unavailable", exc_info=True)
        self._webview.connect("context-menu", lambda *_a: True)
        self._webview.connect("load-changed", self._on_load_changed)
        self._webview.set_hexpand(True)
        self._webview.set_vexpand(True)
        self.append(self._webview)

        self.connect("destroy", self._on_destroy)
        self._watch_window_focus()
        self._webview.load_html(build_host_info_html(), "http://localhost/")
        self._push_status(_("Gathering host information…"), busy=True)
        self._start_probe()

    # -- identity -------------------------------------------------------

    @property
    def connection(self):
        return self._connection

    def _title(self) -> str:
        nickname = getattr(self._connection, "nickname", "") or _("Host Info")
        return _("%s — Host Info") % nickname

    def _subtitle(self) -> str:
        nickname = getattr(self._connection, "nickname", "") or ""
        username = getattr(self._connection, "username", "") or ""
        host = getattr(self._connection, "host", "") or ""
        if username and host:
            return _("%(nickname)s — %(user)s@%(host)s") % {
                "nickname": nickname,
                "user": username,
                "host": host,
            }
        if host:
            return _("%(nickname)s — %(host)s") % {
                "nickname": nickname,
                "host": host,
            }
        return nickname

    # -- WebKit bridge --------------------------------------------------

    def _on_load_changed(self, _view, event) -> None:
        # Fallback when the script-message "ready" signal is delayed or missed:
        # once the document has finished loading, applyHostInfo/applyLive exist.
        try:
            from gi.repository import WebKit

            event_name = getattr(event, "value_nick", str(event))
            if event != WebKit.LoadEvent.FINISHED:
                logger.debug(
                    "Host info WebView load-changed event=%s js_ready=%s",
                    event_name,
                    self._js_ready,
                )
                return
        except Exception:
            logger.debug("Host info load-changed handling failed", exc_info=True)
            return
        logger.debug("Host info WebView load FINISHED; marking JS ready")
        self._mark_js_ready(source="load-finished")

    def _on_script_message(self, _ucm, value) -> None:
        try:
            if hasattr(value, "to_json"):
                raw = value.to_json(0)
            else:
                raw = value.get_js_value().to_json(0)
            logger.debug(
                "Host info script message raw type=%s preview=%r",
                type(raw).__name__,
                (raw[:200] if isinstance(raw, str) else raw),
            )
            message = json.loads(raw)
            # postMessage(JSON.stringify(...)) arrives as a JSON string value.
            if isinstance(message, str):
                message = json.loads(message)
        except Exception:
            logger.debug("Host info script message decode failed", exc_info=True)
            return
        if not isinstance(message, dict):
            logger.debug(
                "Host info script message not a dict: %s", type(message).__name__
            )
            return
        kind = message.get("type")
        logger.debug("Host info script message type=%s keys=%s", kind, sorted(message))
        if kind == "ready":
            self._mark_js_ready(source="script-ready")
        elif kind == "refresh":
            self._on_refresh()
        elif kind == "js-error":
            logger.warning(
                "Host info page JS error: %s",
                message.get("message") or message,
            )
        elif kind == "applied":
            logger.debug(
                "Host info page applied fn=%s status=%s",
                message.get("fn"),
                message.get("status"),
            )

    def _mark_js_ready(self, *, source: str = "unknown") -> None:
        if self._closed:
            return
        was_ready = self._js_ready
        self._js_ready = True
        pending_host = self._pending_host_info is not None
        pending_live = self._pending_live is not None
        logger.debug(
            "Host info JS ready source=%s was_ready=%s pending_host=%s pending_live=%s",
            source,
            was_ready,
            pending_host,
            pending_live,
        )
        if was_ready and not pending_host and not pending_live:
            return
        host_info, self._pending_host_info = self._pending_host_info, None
        live, self._pending_live = self._pending_live, None
        # Snapshot first: applyLive is a no-op until applyHostInfo has run.
        if host_info is not None:
            self._evaluate(host_info, label="flush-host-info")
        if live is not None:
            self._evaluate(live, label="flush-live")

    def _evaluate(self, script: str, *, label: str = "script") -> None:
        if self._closed or self._webview is None:
            return
        chars = len(script)
        utf8_bytes = len(script.encode("utf-8"))
        logger.debug(
            "Host info evaluate_javascript label=%s chars=%d utf8_bytes=%d head=%r",
            label,
            chars,
            utf8_bytes,
            script[:120],
        )

        def on_js_finished(webview, result, _user_data=None):
            try:
                value = webview.evaluate_javascript_finish(result)
                preview = None
                if value is not None and hasattr(value, "to_json"):
                    try:
                        preview = value.to_json(0)
                    except Exception:
                        preview = type(value).__name__
                logger.debug(
                    "Host info evaluate_javascript finished ok label=%s result=%r",
                    label,
                    preview,
                )
            except Exception as exc:
                logger.warning(
                    "Host info evaluate_javascript failed label=%s: %s",
                    label,
                    exc,
                )

        try:
            # WebKit length is bytes, or -1 for a NUL-terminated C string.
            # PyGObject passes NUL-terminated UTF-8; -1 avoids truncating
            # non-ASCII payloads when len(script) < utf-8 byte size.
            self._webview.evaluate_javascript(
                script, -1, None, None, None, on_js_finished, None
            )
        except Exception:
            logger.warning(
                "Host info evaluate_javascript submit failed label=%s",
                label,
                exc_info=True,
            )

    def _run_javascript(self, script: str, *, kind: str) -> None:
        if self._closed or self._webview is None:
            return
        if not self._js_ready:
            logger.debug(
                "Host info queueing JS kind=%s chars=%d (js not ready yet)",
                kind,
                len(script),
            )
            if kind == "host_info":
                self._pending_host_info = script
            else:
                self._pending_live = script
            return
        self._evaluate(script, label=kind)

    def _push_json(self, function_name: str, payload: dict) -> None:
        encoded = json.dumps(payload, ensure_ascii=False)
        # JSON text is a JS expression; wrap so a string payload is also fine.
        script = f"window.{function_name}({encoded});"
        kind = "live" if function_name == "applyLive" else "host_info"
        logger.debug(
            "Host info push fn=%s status=%s payload_chars=%d js_ready=%s",
            function_name,
            payload.get("status"),
            len(encoded),
            self._js_ready,
        )
        self._run_javascript(script, kind=kind)

    def _push_status(self, message: str, *, busy: bool = False) -> None:
        self._push_json(
            "applyHostInfo",
            status_payload(
                title=self._title(),
                subtitle=self._subtitle(),
                message=message,
                busy=busy,
            ),
        )

    # -- daemon probes --------------------------------------------------

    def _ensure_controller(self) -> Optional[HostInfoController]:
        if self._controller is not None:
            return self._controller
        client = getattr(self._window, "client", None)
        if client is None:
            return None
        self._controller = HostInfoController(client)
        if self._interaction_dialogs is None:
            self._attach_interaction_presenter(client)
        return self._controller

    def _attach_interaction_presenter(self, client) -> None:
        bridge = getattr(self._window, "client_bridge", None)
        if bridge is None:
            return
        try:
            from .daemon_interaction_dialogs import DaemonInteractionDialogs

            self._interaction_dialogs = DaemonInteractionDialogs(
                client, bridge, self._window
            )
        except Exception:
            logger.debug("Host info interaction presenter unavailable", exc_info=True)

    def _bind_interactions(self, operation_id) -> bool:
        if self._closed or self._interaction_dialogs is None:
            return False
        try:
            self._interaction_dialogs.set_session(SessionId(str(operation_id)))
        except Exception:
            logger.debug(
                "Host info interaction presenter bind failed", exc_info=True
            )
        return False

    def _submit(self, probe: HostInfoProbe, on_result, on_error) -> bool:
        controller = self._ensure_controller()
        if controller is None:
            return False
        try:
            connection_id = connection_id_for(self._connection)
        except Exception:
            logger.debug("Host info connection identity unavailable", exc_info=True)
            return False
        on_started = None
        if probe is HostInfoProbe.FULL:
            on_started = lambda operation_id: GLib.idle_add(  # noqa: E731
                self._bind_interactions, operation_id
            )
        controller.start(
            connection_id,
            probe,
            lambda summary: GLib.idle_add(on_result, summary),
            lambda error: GLib.idle_add(on_error, error),
            on_started=on_started,
        )
        return True

    def _start_probe(self) -> None:
        try:
            started = self._submit(
                HostInfoProbe.FULL, self._on_snapshot, self._on_error
            )
        except HostInfoProbeBusy:
            return
        if not started:
            self._push_status(_("Daemon connection unavailable."))

    def _on_refresh(self) -> None:
        self._stop_live_timer()
        self._push_status(_("Gathering host information…"), busy=True)
        self._start_probe()

    def _on_snapshot(self, summary) -> bool:
        if self._closed:
            return False
        if summary.failure is not None:
            logger.warning(
                "Host info FULL gather failed: %s", summary.failure.message
            )
            self._push_status(
                _("Could not gather host information.\n\n%s") % summary.failure.message
            )
            return False
        if summary.snapshot is None:
            logger.warning("Host info FULL gather returned no snapshot")
            self._push_status(_("The host returned no system information."))
            return False
        logger.debug(
            "Host info FULL gather ok hostname=%r js_ready=%s interfaces=%d",
            summary.snapshot.hostname,
            self._js_ready,
            len(summary.snapshot.interfaces),
        )
        self._snapshot = summary.snapshot
        self._previous_live = LiveSample(
            counters=summary.counters,
            cpu_times=summary.snapshot.cpu_times,
            memory=summary.snapshot.memory,
            load_average=summary.snapshot.load_average,
        )
        self._previous_live_time = GLib.get_monotonic_time() / 1_000_000
        self._push_json(
            "applyHostInfo",
            snapshot_payload(
                summary.snapshot,
                title=self._title(),
                subtitle=self._subtitle(),
            ),
        )
        self._start_live_timer()
        return False

    def _on_error(self, error: BaseException) -> bool:
        if self._closed:
            return False
        logger.warning("Host info gather failed: %s", error)
        self._push_status(_("Could not gather host information.\n\n%s") % str(error))
        return False

    # -- live sampling --------------------------------------------------

    def _watch_window_focus(self) -> None:
        try:
            self._active_handler_id = self._window.connect(
                "notify::is-active", self._on_window_active
            )
        except Exception:
            logger.debug("Host info focus tracking unavailable", exc_info=True)

    def _on_window_active(self, *_args) -> None:
        if self._closed:
            return
        interval = self._preferred_interval()
        if interval == self._live_interval_ms:
            return
        self._live_interval_ms = interval
        if self._live_timer_id:
            self._stop_live_timer()
            self._start_live_timer()

    def _preferred_interval(self) -> int:
        try:
            active = bool(self._window.is_active())
        except Exception:
            active = True
        return _LIVE_INTERVAL_MS if active else _BACKGROUND_INTERVAL_MS

    def _start_live_timer(self) -> None:
        if self._live_timer_id:
            return
        self._live_interval_ms = self._preferred_interval()
        self._live_timer_id = GLib.timeout_add(self._live_interval_ms, self._tick_live)

    def _stop_live_timer(self) -> None:
        if self._live_timer_id:
            GLib.source_remove(self._live_timer_id)
            self._live_timer_id = 0

    def _tick_live(self) -> bool:
        if self._closed:
            self._live_timer_id = 0
            return False
        try:
            started = self._submit(
                HostInfoProbe.LIVE, self._on_live, self._on_live_error
            )
        except HostInfoProbeBusy:
            return True
        if not started:
            self._live_timer_id = 0
            return False
        return True

    def _on_live(self, summary) -> bool:
        if self._closed or summary.failure is not None or summary.live is None:
            return False
        now = GLib.get_monotonic_time() / 1_000_000
        elapsed = now - self._previous_live_time
        sample = summary.live
        if self._previous_live is not None:
            frequency = None
            if self._snapshot is not None:
                frequency = self._snapshot.cpu.frequency_mhz
            self._push_json(
                "applyLive",
                live_payload(
                    self._previous_live,
                    sample,
                    elapsed,
                    frequency_mhz=frequency,
                ),
            )
        self._previous_live = sample
        self._previous_live_time = now
        return False

    def _on_live_error(self, error: BaseException) -> bool:
        logger.debug("Host info live sample failed: %s", error)
        return False

    # -- teardown -------------------------------------------------------

    def cleanup(self) -> None:
        """Stop timers and the controller; safe to call more than once."""

        if self._closed:
            return
        self._closed = True
        self._stop_live_timer()
        if self._active_handler_id and self._window is not None:
            try:
                self._window.disconnect(self._active_handler_id)
            except Exception:
                pass
            self._active_handler_id = 0
        if self._interaction_dialogs is not None:
            try:
                closer = getattr(self._interaction_dialogs, "close", None)
                if callable(closer):
                    closer()
            except Exception:
                logger.debug("Host info interaction presenter close failed", exc_info=True)
            self._interaction_dialogs = None
        if self._controller is not None:
            try:
                self._controller.close()
            except Exception:
                logger.debug("Host info controller close failed", exc_info=True)
            self._controller = None

    def _on_destroy(self, *_args) -> None:
        self.cleanup()


def open_host_info_tab(window, connection) -> bool:
    """Open (or focus) a WebKit Host Info tab for ``connection``.

    Returns True when a tab was shown.  Callers should fall back to the GTK
    dialog when this returns False (WebKit missing or tab creation failed).
    """

    if window is None or connection is None or not webkit_available():
        return False

    tab_view = getattr(window, "tab_view", None)
    if tab_view is None:
        return False

    # Re-focus an existing tab for the same connection.
    try:
        n_pages = tab_view.get_n_pages()
        for index in range(n_pages):
            page = tab_view.get_nth_page(index)
            child = page.get_child() if hasattr(page, "get_child") else None
            if isinstance(child, HostInfoTab) and child.connection is connection:
                tab_view.set_selected_page(page)
                return True
    except Exception:
        logger.debug("Host info tab lookup failed", exc_info=True)

    try:
        if hasattr(window, "show_tab_view"):
            window.show_tab_view()
        tab = HostInfoTab(window, connection)
        page = tab_view.append(tab)
        nickname = getattr(connection, "nickname", "") or _("Host")
        page.set_title(_("%s — Info") % nickname)
        try:
            from . import icon_utils

            page.set_icon(
                icon_utils.new_gicon_from_icon_name("info-outline-symbolic")
            )
        except Exception:
            pass
        tab_view.set_selected_page(page)
        return True
    except Exception:
        logger.exception("Failed to open Host Info tab")
        return False
