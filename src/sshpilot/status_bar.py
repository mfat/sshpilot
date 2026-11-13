"""The main window's status bar.

A full-width strip under the sidebar and the content area, built from plain
GTK 4.0 widgets (``Gtk.Statusbar`` is deprecated). On the left it shows one
short message at a time; on the right it carries two buttons: the log viewer
(``win.view-logs``) and the notification center (``win.show-notifications``),
whose bell has an unread-count badge.

What the message area may say is curated in
:mod:`sshpilot.gtk.status_bar_messages`, which ships empty. Code anywhere in
the app shows a message with::

    from sshpilot.status_bar import post_status
    post_status("some.code", name="web-01")

Daemon events reach it through that module's ``EVENT_ROUTES``; while no route
exists, :class:`StatusBarController` does not subscribe to the daemon at all.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from gi.repository import Gio, GLib, Gtk, Pango

from .i18n import gettext as _, ngettext

logger = logging.getLogger(__name__)

STATUS_BAR_CSS_CLASS = "sshpilot-status-bar"
BADGE_CSS_CLASS = "sshpilot-status-badge"
LOGS_ACTION = "win.view-logs"
NOTIFICATIONS_ACTION = "win.show-notifications"
BADGE_MAX = 99

_CSS = """
.sshpilot-status-bar {
  padding: 2px 6px 2px 10px;
  min-height: 26px;
}
.sshpilot-status-bar .sshpilot-status-text {
  font-size: 0.9em;
}
.sshpilot-status-bar button.flat {
  min-height: 22px;
  min-width: 22px;
  padding: 2px 6px;
}
.sshpilot-status-badge {
  background-color: #e01b24;
  color: #ffffff;
  border-radius: 999px;
  font-size: 0.65em;
  font-weight: bold;
  min-width: 9px;
  min-height: 9px;
  padding: 0 3px;
}
"""

_css_installed = False


def _install_css() -> None:
    global _css_installed
    if _css_installed:
        return
    from gi.repository import Gdk

    display = Gdk.Display.get_default()
    if display is None:
        return
    provider = Gtk.CssProvider()
    if hasattr(provider, "load_from_string"):  # GTK >= 4.12
        provider.load_from_string(_CSS)
    else:  # pragma: no cover - older GTK only
        provider.load_from_data(_CSS.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )
    _css_installed = True


def badge_text(count: int) -> str:
    """What the bell badge shows for ``count`` unread entries ("" hides it)."""
    if count <= 0:
        return ""
    if count > BADGE_MAX:
        return f"{BADGE_MAX}+"
    return str(count)


class StatusBar(Gtk.Box):
    """The status bar widget. Holds no wording of its own beyond its chrome."""

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        _install_css()
        self.add_css_class(STATUS_BAR_CSS_CLASS)
        self._timeout_source = 0
        self._level_class: Optional[str] = None

        self.message_icon = Gtk.Image()
        self.message_icon.set_pixel_size(14)
        self.message_icon.set_visible(False)
        self.append(self.message_icon)

        self.message_label = Gtk.Label(accessible_role=Gtk.AccessibleRole.STATUS)
        self.message_label.add_css_class("sshpilot-status-text")
        self.message_label.set_xalign(0.0)
        self.message_label.set_hexpand(True)
        self.message_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.message_label.set_single_line_mode(True)
        self.append(self.message_label)

        self.logs_button = Gtk.Button()
        self.logs_button.set_icon_name("text-x-generic-symbolic")
        self.logs_button.add_css_class("flat")
        self.logs_button.set_tooltip_text(_("View Logs"))
        self.logs_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [_("View Logs")]
        )
        self.logs_button.set_action_name(LOGS_ACTION)
        self.logs_button.set_valign(Gtk.Align.CENTER)
        self.append(self.logs_button)

        bell = Gtk.Image.new_from_icon_name("preferences-system-notifications-symbolic")
        self.badge_label = Gtk.Label()
        self.badge_label.add_css_class(BADGE_CSS_CLASS)
        self.badge_label.set_halign(Gtk.Align.END)
        self.badge_label.set_valign(Gtk.Align.START)
        self.badge_label.set_can_target(False)
        self.badge_label.set_visible(False)
        bell_overlay = Gtk.Overlay()
        bell_overlay.set_child(bell)
        bell_overlay.add_overlay(self.badge_label)
        # Room for the badge to sit over the bell's top-right corner.
        bell.set_margin_top(3)
        bell.set_margin_end(6)

        self.notifications_button = Gtk.Button()
        self.notifications_button.set_child(bell_overlay)
        self.notifications_button.add_css_class("flat")
        self.notifications_button.set_action_name(NOTIFICATIONS_ACTION)
        self.notifications_button.set_valign(Gtk.Align.CENTER)
        self.append(self.notifications_button)
        self.set_unread_count(0)

        self.connect("unrealize", lambda *_a: self._cancel_timeout())

    # -- message area ------------------------------------------------------

    def show_message(self, text: str, *, icon_name: Optional[str] = None,
                     css_class: Optional[str] = None,
                     timeout_s: Optional[int] = None) -> None:
        """Show ``text`` (already translated), replacing any current message."""
        self._cancel_timeout()
        self.message_label.set_text(text)
        self.message_label.set_tooltip_text(text)
        if self._level_class:
            self.message_icon.remove_css_class(self._level_class)
            self._level_class = None
        if icon_name:
            self.message_icon.set_from_icon_name(icon_name)
            if css_class:
                self.message_icon.add_css_class(css_class)
                self._level_class = css_class
            self.message_icon.set_visible(True)
        else:
            self.message_icon.set_visible(False)
        announce = getattr(self.message_label, "announce", None)  # GTK >= 4.14
        priority = getattr(Gtk, "AccessibleAnnouncementPriority", None)
        if callable(announce) and priority is not None and text:
            try:
                announce(text, priority.MEDIUM)
            except Exception:
                logger.debug("Status announcement failed", exc_info=True)
        if timeout_s:
            self._timeout_source = GLib.timeout_add_seconds(int(timeout_s), self._on_timeout)

    def clear_message(self) -> None:
        self._cancel_timeout()
        self.message_label.set_text("")
        self.message_label.set_tooltip_text(None)
        self.message_icon.set_visible(False)

    def message_text(self) -> str:
        return self.message_label.get_text()

    def _on_timeout(self) -> bool:
        self._timeout_source = 0
        self.clear_message()
        return GLib.SOURCE_REMOVE

    def _cancel_timeout(self) -> None:
        if self._timeout_source:
            GLib.source_remove(self._timeout_source)
            self._timeout_source = 0

    # -- bell badge --------------------------------------------------------

    def set_unread_count(self, count: int) -> None:
        text = badge_text(int(count))
        self.badge_label.set_text(text)
        self.badge_label.set_visible(bool(text))
        if count > 0:
            label = ngettext(
                "Notifications ({count} unread)",
                "Notifications ({count} unread)",
                count,
            ).format(count=count)
        else:
            label = _("Notifications")
        self.notifications_button.set_tooltip_text(label)
        self.notifications_button.update_property(
            [Gtk.AccessibleProperty.LABEL], [label]
        )


class StatusBarController:
    """Routes curated messages to one window's :class:`StatusBar`.

    ``post`` is the in-app entry point. ``attach_client`` subscribes to daemon
    events only when :data:`EVENT_ROUTES` or :data:`NOTIFY_EVENT_ROUTES` has
    rows, and drops events from a previous daemon client the same way
    :class:`ConnectionRuntimeStatusStore` does.
    """

    def __init__(
        self,
        bar: StatusBar,
        *,
        name_lookup: Optional[Callable[[Optional[str]], str]] = None,
        dispatch: Optional[Callable[[Callable[[], None]], Any]] = None,
        notification_store: Any = None,
    ) -> None:
        self.bar = bar
        self._name_lookup = name_lookup or (lambda connection_id: connection_id or "")
        self._dispatch = dispatch or (
            lambda callback: GLib.idle_add(lambda: (callback(), GLib.SOURCE_REMOVE)[1])
        )
        self._lock = threading.Lock()
        self._client = None
        self._subscription = None
        self._generation = 0
        self._store = None
        self._store_handler = 0
        if notification_store is not None:
            self.bind_store(notification_store)

    # -- messages ----------------------------------------------------------

    def post(self, code: str, **params: Any) -> None:
        if threading.current_thread() is not threading.main_thread():
            self._dispatch(lambda: self.post(code, **params))
            return
        from .gtk.status_bar_messages import (
            LEVEL_CSS_CLASSES,
            LEVEL_ICONS,
            format_status_message,
        )

        rendered = format_status_message(code, params)
        if rendered is None:
            logger.debug("Status message %r is not in the catalogue or is disabled", code)
            return
        self.bar.show_message(
            rendered.text,
            icon_name=LEVEL_ICONS.get(rendered.level),
            css_class=LEVEL_CSS_CLASSES.get(rendered.level),
            timeout_s=rendered.timeout_s,
        )

    def show_text(self, text: str, *, level: str = "info",
                  timeout_s: Optional[int] = 5) -> None:
        """Show ready-made text (plugins, API >= 1.17). App code uses
        :meth:`post` with a catalogued code instead."""
        if threading.current_thread() is not threading.main_thread():
            self._dispatch(lambda: self.show_text(text, level=level, timeout_s=timeout_s))
            return
        from .gtk.status_bar_messages import LEVEL_CSS_CLASSES, LEVEL_ICONS, StatusLevel

        try:
            status_level = StatusLevel(level)
        except ValueError:
            status_level = StatusLevel.INFO
        text = str(text or "").strip()
        if not text:
            return
        self.bar.show_message(
            text,
            icon_name=LEVEL_ICONS.get(status_level),
            css_class=LEVEL_CSS_CLASSES.get(status_level),
            timeout_s=timeout_s,
        )

    def clear(self) -> None:
        self.bar.clear_message()

    # -- notification badge ------------------------------------------------

    def bind_store(self, store: Any) -> None:
        self._unbind_store()
        self._store = store
        self._store_handler = store.connect(
            "notify::unread-count",
            lambda *_a: self.bar.set_unread_count(store.unread_count),
        )
        self.bar.set_unread_count(store.unread_count)

    def _unbind_store(self) -> None:
        if self._store is not None and self._store_handler:
            try:
                self._store.disconnect(self._store_handler)
            except Exception:
                pass
        self._store = None
        self._store_handler = 0

    # -- daemon events -----------------------------------------------------

    @staticmethod
    def _routed_event_types():
        from .gtk.notification_messages import NOTIFY_EVENT_ROUTES
        from .gtk.status_bar_messages import EVENT_ROUTES

        return set(EVENT_ROUTES) | set(NOTIFY_EVENT_ROUTES)

    @property
    def subscribed(self) -> bool:
        return self._subscription is not None

    def attach_client(self, client: Any) -> None:
        with self._lock:
            old, self._subscription = self._subscription, None
            self._generation += 1
            generation = self._generation
            self._client = client
        if old is not None:
            old.unsubscribe()
        if client is None or not self._routed_event_types():
            return
        instance_id = getattr(client, "server_instance_id", None)
        subscription = client.subscribe_events(
            lambda event: self._accept_event(event, generation, instance_id)
        )
        with self._lock:
            if generation != self._generation:
                subscription.unsubscribe()
                return
            self._subscription = subscription

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            subscription, self._subscription = self._subscription, None
            self._client = None
        if subscription is not None:
            subscription.unsubscribe()

    def shutdown(self) -> None:
        self.close()
        self._unbind_store()

    def _accept_event(self, event: Any, generation: int, instance_id: Optional[str]) -> None:
        with self._lock:
            if generation != self._generation:
                return
            if getattr(self._client, "server_instance_id", None) != instance_id:
                return
        self._dispatch(lambda: self._route_event(event, generation))

    def _route_event(self, event: Any, generation: int) -> None:
        if generation != self._generation:
            return
        from .gtk.notification_messages import NOTIFY_EVENT_ROUTES
        from .gtk.status_bar_messages import EVENT_ROUTES

        route = EVENT_ROUTES.get(event.type)
        if route is not None:
            try:
                routed = route(event, self._name_lookup)
            except Exception:
                logger.debug("Status route for %s failed", event.type, exc_info=True)
                routed = None
            if routed is not None:
                self.post(routed[0], **routed[1])
        notify_route = NOTIFY_EVENT_ROUTES.get(event.type)
        if notify_route is not None:
            try:
                routed = notify_route(event, self._name_lookup)
            except Exception:
                logger.debug("Notification route for %s failed", event.type, exc_info=True)
                routed = None
            if routed is not None:
                from .notification_store import notify

                notify(routed[0], **routed[1])


def post_status(code: str, **params: Any) -> None:
    """Show a catalogued message in the active window's status bar.

    A no-op (logged at debug) when the code is not in
    :data:`sshpilot.gtk.status_bar_messages.STATUS_MESSAGES` or no window is up.
    Safe from any thread.
    """
    if threading.current_thread() is not threading.main_thread():
        GLib.idle_add(lambda: (post_status(code, **params), GLib.SOURCE_REMOVE)[1])
        return
    app = Gio.Application.get_default()
    poster = getattr(app, "post_status", None) if app is not None else None
    if callable(poster):
        poster(code, **params)
    else:
        logger.debug("No application to show status %r", code)
