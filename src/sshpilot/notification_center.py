"""The notification center dialog opened from the status bar's bell.

An ``Adw.Dialog`` (libadwaita 1.5) with two tabs, Activity and News, each a
list of :class:`sshpilot.notification_store.NotificationEntry` grouped by day.
Every entry expands to its details and offers Copy, Dismiss and at most one
action button. The dialog follows the store live while it is open.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Dict, Optional, Set

from gi.repository import Adw, Gdk, GLib, Gtk

from .i18n import gettext as _
from .notification_store import TAB_ACTIVITY, TAB_NEWS, NotificationEntry, NotificationStore

logger = logging.getLogger(__name__)


class _View:
    """Everything one row needs, resolved from an entry in the UI language."""

    def __init__(self, title: str, details: str, severity: str,
                 action_label: Optional[str] = None, action_name: Optional[str] = None,
                 action_target: Optional[str] = None, link_url: Optional[str] = None,
                 link_label: Optional[str] = None) -> None:
        self.title = title
        self.details = details
        self.severity = severity
        self.action_label = action_label
        self.action_name = action_name
        self.action_target = action_target
        self.link_url = link_url
        self.link_label = link_label


def entry_view(entry: NotificationEntry) -> _View:
    if entry.tab == TAB_NEWS:
        from .news_feed import pick_text

        return _View(
            title=pick_text(entry.title_i18n),
            details=pick_text(entry.body_i18n),
            severity=entry.severity,
            link_url=entry.link_url,
            link_label=pick_text(entry.link_label_i18n) or _("Open Link"),
        )
    from .gtk.notification_messages import format_notification

    rendered = format_notification(entry.code or "", entry.params) if entry.code else None
    if rendered is None:
        return _View(entry.fallback_title, entry.fallback_details, entry.severity)
    return _View(
        rendered.title,
        rendered.details,
        rendered.severity.value,
        rendered.action_label,
        rendered.action_name,
        rendered.action_target,
    )


def _severity_icon(severity: str) -> str:
    from .gtk.notification_messages import SEVERITY_ICONS, NotificationSeverity

    try:
        return SEVERITY_ICONS[NotificationSeverity(severity)]
    except (ValueError, KeyError):
        return "dialog-information-symbolic"


def _severity_class(severity: str) -> str:
    from .gtk.notification_messages import SEVERITY_CSS_CLASSES, NotificationSeverity

    try:
        return SEVERITY_CSS_CLASSES[NotificationSeverity(severity)]
    except (ValueError, KeyError):
        return "dim-label"


def _day_heading(when: datetime, now: datetime) -> str:
    day = when.date()
    if day == now.date():
        return _("Today")
    if day == (now - timedelta(days=1)).date():
        return _("Yesterday")
    return _("Earlier")


def _time_text(when: datetime, now: datetime) -> str:
    stamp = GLib.DateTime.new_from_unix_local(int(when.timestamp()))
    if stamp is None:
        return ""
    if when.date() >= (now - timedelta(days=1)).date():
        return stamp.format("%H:%M") or ""
    return stamp.format("%x") or ""


class NotificationCenterDialog(Adw.Dialog):
    """Activity and News, newest first, grouped by day."""

    def __init__(self, store: NotificationStore, *, initial_tab: Optional[str] = None) -> None:
        super().__init__()
        self._store = store
        self.set_title(_("Notifications"))
        self.set_content_width(520)
        self.set_content_height(560)
        # Entries that were unread when the dialog opened stay marked for its
        # lifetime, even though opening a tab marks them read in the store.
        self._fresh: Set[str] = {
            e.id for tab in (TAB_ACTIVITY, TAB_NEWS) for e in store.entries(tab) if not e.read
        }
        self._lists: Dict[str, Gtk.Box] = {}
        self._clear_buttons: Dict[str, Gtk.Button] = {}
        self._pages: Dict[str, Adw.ViewStackPage] = {}

        self.stack = Adw.ViewStack()
        for tab, title, icon in (
            (TAB_ACTIVITY, _("Activity"), "preferences-system-notifications-symbolic"),
            (TAB_NEWS, _("News"), "web-browser-symbolic"),
        ):
            self._pages[tab] = self.stack.add_titled_with_icon(
                self._build_tab(tab), tab, title, icon
            )

        switcher = Adw.ViewSwitcher()
        switcher.set_stack(self.stack)
        switcher.set_policy(Adw.ViewSwitcherPolicy.WIDE)

        header = Adw.HeaderBar()
        header.set_title_widget(switcher)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.stack)
        self.set_child(toolbar)

        if initial_tab in (TAB_ACTIVITY, TAB_NEWS):
            self.stack.set_visible_child_name(initial_tab)
        elif store.unread_activity == 0 and store.unread_news > 0:
            self.stack.set_visible_child_name(TAB_NEWS)

        self._changed_handler = store.connect("changed", lambda *_a: self._rebuild())
        self.stack.connect("notify::visible-child-name", lambda *_a: self._mark_visible_read())
        self.connect("closed", self._on_closed)
        self._rebuild()
        self._mark_visible_read()

    # -- structure ---------------------------------------------------------

    def _build_tab(self, tab: str) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.set_margin_top(6)
        bar.set_margin_start(12)
        bar.set_margin_end(12)
        bar.set_halign(Gtk.Align.END)
        clear = Gtk.Button(label=_("Clear All"))
        clear.add_css_class("flat")
        clear.connect("clicked", lambda *_a, t=tab: self._store.clear(t))
        bar.append(clear)
        self._clear_buttons[tab] = clear
        outer.append(bar)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.set_margin_top(6)
        content.set_margin_bottom(18)
        content.set_margin_start(12)
        content.set_margin_end(12)
        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(content)
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_child(clamp)
        outer.append(scrolled)
        self._lists[tab] = content
        return outer

    def _empty_page(self, tab: str) -> Gtk.Widget:
        page = Adw.StatusPage()
        page.add_css_class("compact")
        if tab == TAB_NEWS:
            page.set_icon_name("web-browser-symbolic")
            page.set_title(_("No News"))
            page.set_description(_("Announcements about SSH Pilot appear here"))
        else:
            page.set_icon_name("preferences-system-notifications-symbolic")
            page.set_title(_("No Notifications"))
        page.set_vexpand(True)
        return page

    def _rebuild(self) -> None:
        now = datetime.now().astimezone()
        for tab, box in self._lists.items():
            child = box.get_first_child()
            while child is not None:
                following = child.get_next_sibling()
                box.remove(child)
                child = following
            entries = self._store.entries(tab)
            self._clear_buttons[tab].set_sensitive(bool(entries))
            if not entries:
                box.append(self._empty_page(tab))
            groups: Dict[str, Adw.PreferencesGroup] = {}
            for entry in entries:
                when = entry.created_datetime().astimezone()
                if entry.tab == TAB_NEWS:
                    # A news date is a calendar day; converting its UTC
                    # midnight would move it to the previous day west of UTC.
                    day = entry.created_datetime().date()
                    when = datetime(day.year, day.month, day.day, 12).astimezone()
                heading = _day_heading(when, now)
                group = groups.get(heading)
                if group is None:
                    group = Adw.PreferencesGroup(title=heading)
                    groups[heading] = group
                    box.append(group)
                group.add(self._build_row(entry, when, now))
        self._pages[TAB_ACTIVITY].set_badge_number(self._store.unread_activity)
        self._pages[TAB_NEWS].set_badge_number(self._store.unread_news)

    def _build_row(self, entry: NotificationEntry, when: datetime, now: datetime) -> Gtk.Widget:
        view = entry_view(entry)
        row = Adw.ExpanderRow()
        if hasattr(row, "set_use_markup"):
            row.set_use_markup(False)
        row.set_title(view.title)
        if entry.tab == TAB_NEWS:
            # News carries a date, not a time of day.
            published = entry.created_datetime()
            stamp = GLib.DateTime.new_local(
                published.year, published.month, published.day, 0, 0, 0
            )
            row.set_subtitle((stamp.format("%x") if stamp else "") or "")
        else:
            row.set_subtitle(_time_text(when, now))
        if hasattr(row, "set_title_lines"):
            row.set_title_lines(2)

        icon = Gtk.Image.new_from_icon_name(_severity_icon(view.severity))
        icon.add_css_class(_severity_class(view.severity))
        row.add_prefix(icon)
        if entry.id in self._fresh:
            dot = Gtk.Label(label="●")
            dot.add_css_class("accent")
            dot.set_tooltip_text(_("New"))
            row.add_prefix(dot)

        dismiss = Gtk.Button.new_from_icon_name("window-close-symbolic")
        dismiss.add_css_class("flat")
        dismiss.set_valign(Gtk.Align.CENTER)
        dismiss.set_tooltip_text(_("Dismiss"))
        dismiss.update_property([Gtk.AccessibleProperty.LABEL], [_("Dismiss")])
        dismiss.connect("clicked", lambda *_a, i=entry.id: self._store.dismiss(i))
        if hasattr(row, "add_suffix"):  # libadwaita >= 1.4
            row.add_suffix(dismiss)
        else:  # pragma: no cover
            row.add_action(dismiss)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.set_margin_top(10)
        body.set_margin_bottom(10)
        body.set_margin_start(12)
        body.set_margin_end(12)
        if view.details:
            details = Gtk.Label(label=view.details)
            details.set_wrap(True)
            details.set_xalign(0.0)
            details.set_selectable(True)
            body.append(details)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        buttons.set_halign(Gtk.Align.END)
        copy = Gtk.Button(label=_("Copy"))
        copy.connect("clicked", lambda *_a, v=view: self._copy(v))
        buttons.append(copy)
        if view.link_url:
            link = Gtk.Button(label=view.link_label or _("Open Link"))
            link.add_css_class("suggested-action")
            link.connect("clicked", lambda *_a, u=view.link_url: self._open_link(u))
            buttons.append(link)
        elif view.action_name and view.action_label:
            action = Gtk.Button(label=view.action_label)
            action.add_css_class("suggested-action")
            action.connect(
                "clicked",
                lambda *_a, n=view.action_name, t=view.action_target: self._run_action(n, t),
            )
            buttons.append(action)
        body.append(buttons)
        row.add_row(body)
        return row

    # -- behaviour ---------------------------------------------------------

    def _mark_visible_read(self) -> None:
        tab = self.stack.get_visible_child_name()
        if tab in (TAB_ACTIVITY, TAB_NEWS):
            self._store.mark_read(tab)

    def _copy(self, view: _View) -> None:
        text = view.title if not view.details else f"{view.title}\n\n{view.details}"
        if view.link_url:
            text = f"{text}\n{view.link_url}"
        display = Gdk.Display.get_default()
        if display is not None:
            display.get_clipboard().set(text)

    def _open_link(self, url: str) -> None:
        parent = self.get_root() if isinstance(self.get_root(), Gtk.Window) else None
        try:
            launcher = Gtk.UriLauncher.new(url)
            launcher.launch(parent, None, None, None)
        except Exception:
            logger.warning("Could not open the news link", exc_info=True)

    def _run_action(self, name: str, target: Optional[str]) -> None:
        root = self.get_root()
        variant = GLib.Variant.new_string(target) if target is not None else None
        self.close()
        try:
            if root is not None:
                # Through the class: MainWindow shadows ``activate_action``
                # with a SimpleAction attribute of the same name.
                Gtk.Widget.activate_action(root, name, variant)
        except Exception:
            logger.warning("Notification action %s failed", name, exc_info=True)

    def _on_closed(self, *_args) -> None:
        if self._changed_handler:
            try:
                self._store.disconnect(self._changed_handler)
            except Exception:
                pass
            self._changed_handler = 0
