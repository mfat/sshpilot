"""Split terminal view — SplitPane and SplitViewTab widgets."""
from __future__ import annotations

import logging
import math
from typing import List, Optional

from gi.repository import Gtk, Gdk, Gio, GLib, Adw, Pango
from gettext import gettext as _, ngettext

from .i18n import N_
from .dnd_payload import (
    claim_in_app_drops_ahead_of_children,
    decode_dnd_payload,
    new_internal_drop_target,
)
from .shortcut_utils import (
    accel_matches_latin_fallback,
    latin_fallback_keyvals,
)

logger = logging.getLogger(__name__)


def _connections_from_drop_payload(value) -> List[str]:
    """Normalize a sidebar drop payload into an ordered list of nicknames.

    Accepts the pasteboard-safe JSON string from sidebar connection drags
    (or a bare payload dict in unit tests). Expects ``type == "connection"``
    and either ``connection_nicknames`` (multi-select) or a single
    ``connection_nickname`` fallback.

    Returns ``[]`` for anything that isn't a connection payload, so callers
    can treat an empty list as "not a drop we handle". Pure — no widget
    access — so it is unit-testable.
    """
    value = decode_dnd_payload(value)
    if not isinstance(value, dict) or value.get("type") != "connection":
        return []
    nicknames = value.get("connection_nicknames") or []
    if not nicknames and value.get("connection_nickname"):
        nicknames = [value["connection_nickname"]]
    return list(nicknames)


def _is_local_terminal_drop(value) -> bool:
    """True when *value* is the pinned Local Terminal sidebar drag payload."""
    value = decode_dnd_payload(value)
    return isinstance(value, dict) and value.get("type") == "local_terminal"


# Installed once per process; scoped to the .add-pane-strip / scroll-spacer
# style classes so it only affects those widgets and nothing else in the app.
# Class-only selectors so the rule applies whether the strip is a Gtk.Box or a
# Gtk.ActionBar (the ActionBar provides its own border/padding).
_drop_zone_css_installed = False

def _ensure_drop_zone_css() -> None:
    global _drop_zone_css_installed
    if _drop_zone_css_installed:
        return
    provider = Gtk.CssProvider()
    # Only the drag-over highlight is styled here; the strip's frame/padding now
    # comes from Gtk.ActionBar's native styling.
    provider.load_from_data(b"""
.add-pane-strip.drag-over {
    background-color: rgba(42, 161, 152, 0.25);
}
.add-pane-scroll-spacer.drag-over {
    background-color: rgba(42, 161, 152, 0.25);
}
""")
    # USER priority (800) beats Adwaita theme (200) and application CSS (600)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_USER,
    )
    _drop_zone_css_installed = True


def create_layout_toggle_buttons(
    on_horizontal,
    on_vertical,
    *,
    as_pill: bool = False,
) -> tuple[Gtk.ToggleButton, Gtk.ToggleButton, list]:
    """Create H/V layout toggle buttons for the tab bar."""
    updating = [False]

    h_btn = Gtk.ToggleButton()
    h_btn.set_icon_name("terminal-split-horizontal-symbolic")
    h_btn.set_tooltip_text(_("Side by Side"))
    h_btn.add_css_class("pill" if as_pill else "flat")

    v_btn = Gtk.ToggleButton()
    v_btn.set_icon_name("terminal-split-vertical-symbolic")
    v_btn.set_tooltip_text(_("Top / Bottom"))
    v_btn.add_css_class("pill" if as_pill else "flat")

    def _on_h_toggled(btn: Gtk.ToggleButton) -> None:
        if updating[0] or not btn.get_active():
            return
        updating[0] = True
        v_btn.set_active(False)
        updating[0] = False
        on_horizontal()

    def _on_v_toggled(btn: Gtk.ToggleButton) -> None:
        if updating[0] or not btn.get_active():
            return
        updating[0] = True
        h_btn.set_active(False)
        updating[0] = False
        on_vertical()

    h_btn.connect("toggled", _on_h_toggled)
    v_btn.connect("toggled", _on_v_toggled)
    return h_btn, v_btn, updating


_row_handle_css_installed = False

def _ensure_row_handle_css() -> None:
    global _row_handle_css_installed
    if _row_handle_css_installed:
        return
    provider = Gtk.CssProvider()
    provider.load_from_data(b"""
drawingarea.row-resize-handle {
    background-color: transparent;
    min-height: 6px;
}
drawingarea.row-resize-handle:hover {
    background-color: alpha(@accent_bg_color, 0.15);
}
box.row-drag-ghost {
    background-color: @accent_bg_color;
    min-height: 2px;
}

/* Pane card: a rounded surface holding the header and the terminal.
   The ring is a real border over the pane's own background, not an outer
   box-shadow: a shadow leaves the anti-aliased clip edge see-through, so
   the window showed through as light specks at the corners. The border is
   2px in both states so focusing a pane never shifts its content. */
box.split-pane {
    margin: 4px;
    border-radius: 10px;
    border: 2px solid mix(@headerbar_bg_color, @window_fg_color, 0.15);
    background-color: @headerbar_bg_color;
}
box.split-pane.split-pane-active {
    border-color: #f5c400;
}

/* The terminal already frames itself as a margined, rounded card; inside
   a pane that card would be a second frame, so it fills the pane flat. */
box.split-pane .terminal-card {
    margin: 0;
    border-radius: 0;
    box-shadow: none;
}

/* Pane header: one terminal per pane, so a title row, not a tab bar */
box.split-pane-header {
    background-color: @headerbar_bg_color;
    box-shadow: inset 0 -1px @headerbar_shade_color;
    padding: 5px 6px 5px 12px;
    min-height: 24px;
    color: alpha(@headerbar_fg_color, 0.6);
}
box.split-pane.split-pane-active box.split-pane-header {
    color: @headerbar_fg_color;
}
box.split-pane-header label.split-pane-title {
    font-size: 13px;
    font-weight: 500;
}
box.split-pane-header button.split-pane-close {
    min-width: 24px;
    min-height: 24px;
    padding: 0;
}
/* Unsplit / full screen: hidden until the pointer is over the pane (or a
   button has keyboard focus); full screen stays shown while zoomed. */
box.split-pane-header button.split-pane-hover-action {
    opacity: 0;
    transition: opacity 120ms ease-out;
}
box.split-pane:hover box.split-pane-header button.split-pane-hover-action,
box.split-pane.split-pane-fullscreen box.split-pane-header button.split-pane-hover-action,
box.split-pane-header button.split-pane-hover-action:focus-visible {
    opacity: 1;
}

/* A full screen pane fills the window edge to edge. */
box.split-pane.split-pane-fullscreen {
    margin: 0;
    border-radius: 0;
    border-width: 0;
}

/* Group chip at the trailing edge of the header */
box.split-pane-group {
    padding: 2px 8px 2px 7px;
    border-radius: 999px;
    background-color: alpha(currentColor, 0.1);
    font-size: 11.5px;
    font-weight: 500;
}
""")
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_USER,
    )
    _row_handle_css_installed = True


class RowResizeHandle(Gtk.DrawingArea):
    """6-px drag handle between rows; dragging adjusts the row above's height."""

    def __init__(self, get_row_idx, split_view_tab: "SplitViewTab") -> None:
        super().__init__()
        self.set_size_request(-1, 6)
        self.set_hexpand(True)
        self.set_cursor(Gdk.Cursor.new_from_name("ns-resize"))
        _ensure_row_handle_css()
        self.add_css_class("row-resize-handle")
        self._get_row_idx = get_row_idx
        self._tab = split_view_tab
        self._last_dy = 0.0
        self._drag_moved = False

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._on_drag_begin)
        drag.connect("drag-update", self._on_drag_update)
        drag.connect("drag-end", self._on_drag_end)
        self.add_controller(drag)

    def _on_drag_begin(self, _gesture, _x, _y) -> None:
        self._last_dy = 0.0
        self._drag_moved = False

    def _on_drag_update(self, _gesture, _offset_x, offset_y) -> None:
        # Accumulate the target height but do NOT resize the widget yet —
        # resizing VTE on every mouse-move event causes content to flicker.
        # The ghost line gives live position feedback instead.
        delta = offset_y - self._last_dy
        self._last_dy = offset_y
        if delta:
            self._drag_moved = True
        idx = self._get_row_idx()
        tab = self._tab
        if 0 <= idx < len(tab._row_heights):
            tab._row_heights[idx] = max(
                tab.ABSOLUTE_MIN_ROW_HEIGHT, tab._row_heights[idx] + int(delta)
            )
            tab._update_drag_ghost(idx)

    def _on_drag_end(self, _gesture, _offset_x, _offset_y) -> None:
        if self._drag_moved:
            idx = self._get_row_idx()
            if 0 <= idx < len(self._tab._row_heights):
                self._tab._mark_row_manual(idx)
        self._tab._flush_row_resize()


class SplitPane(Gtk.Box):
    """
    A single pane in the split view grid.

    Each pane holds exactly one terminal under a compact title header
    (icon, title, group chip, close). An Adw.TabView is kept as the
    single-page container. When empty, a placeholder is shown with a
    "Pick existing tab" pill button.
    """

    __gtype_name__ = "SshPilotSplitPane"

    def __init__(self, split_view_tab: "SplitViewTab", window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._split_view_tab = split_view_tab
        self._window = window
        self._has_terminals = False
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_size_request(-1, max(
            split_view_tab.ABSOLUTE_MIN_ROW_HEIGHT,
            split_view_tab._get_default_row_height(),
        ))
        _ensure_row_handle_css()
        self.add_css_class("split-pane")
        # Clip header + terminal to the pane's rounded corners.
        self.set_overflow(Gtk.Overflow.HIDDEN)

        focus_ctrl = Gtk.EventControllerFocus()
        focus_ctrl.connect('enter', self._on_pane_focus_enter)
        focus_ctrl.connect('leave', lambda _c: self.remove_css_class('split-pane-active'))
        self.add_controller(focus_ctrl)

        click_ctrl = Gtk.GestureClick()
        click_ctrl.set_button(0)
        click_ctrl.connect('pressed', lambda *_: self._split_view_tab._note_active_pane(self))
        self.add_controller(click_ctrl)

        # ── inner tab view (mini Adw.TabBar + Adw.TabView) ───────────────
        self._inner_tab_view = Adw.TabView()
        self._inner_tab_view.set_hexpand(True)
        self._inner_tab_view.set_vexpand(True)
        # Same duplicate-handler hazard as the outer Adw.TabView (see
        # window.py setup_content_area): this inner view has no keyboard
        # navigation of its own wired up (sub-tabs are switched by clicking
        # the mini tab bar), so its built-in accelerators are an unclaimed
        # second consumer of keys like Ctrl+PageUp/Down that must instead
        # reach the focused VTE terminal. Disable them so this pane never
        # shadows either SSH Pilot's tab actions or terminal pass-through.
        try:
            self._inner_tab_view.set_shortcuts(Adw.TabViewShortcuts.NONE)
        except Exception:
            logger.debug("Could not adjust inner Adw.TabView shortcuts", exc_info=True)
        self._inner_tab_view.connect('close-page', self._on_inner_close)
        self._inner_tab_view.connect('notify::selected-page', self._on_inner_tab_selected)

        # A pane holds exactly one terminal, so the header is a plain title
        # row (icon · title · group chip · close) instead of an Adw.TabBar.
        # The Adw.TabView stays as the single-page container so close-page
        # cleanup keeps working unchanged.
        self._title_page = None
        self._title_handler = None
        self._header = self._build_header()

        # The header's close button closes the pane; the placeholder has its
        # own close button for when the pane is empty.

        self._tab_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._tab_container.set_hexpand(True)
        self._tab_container.set_vexpand(True)
        self._tab_container.append(self._header)
        self._tab_container.append(self._inner_tab_view)

        # ── placeholder ──────────────────────────────────────────────────
        self._placeholder = self._build_placeholder()
        self.append(self._placeholder)

        # ── header: double-click to rename ───────────────────────────────
        rename_gesture = Gtk.GestureClick()
        rename_gesture.set_button(1)
        rename_gesture.connect("pressed", self._on_header_pressed)
        self._header.add_controller(rename_gesture)

        # ── header: right-click menu ─────────────────────────────────────
        self._header_menu: Optional[Gtk.PopoverMenu] = None
        self._header_menu_xy = (0.0, 0.0)
        self._install_header_menu()

        # ── drop target ──────────────────────────────────────────────────
        self._setup_drop_target()

        # Register with parent tab
        split_view_tab.register_pane(self)

    def _on_pane_focus_enter(self, _controller) -> None:
        self.add_css_class('split-pane-active')
        self._split_view_tab._note_active_pane(self)

    def _on_inner_tab_selected(self, *_args) -> None:
        self._split_view_tab._note_active_pane(self)
        self._bind_title()
        self._update_group_chip()

    # ── header ───────────────────────────────────────────────────────────────

    def _build_header(self) -> Gtk.Widget:
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.add_css_class("split-pane-header")
        header.set_hexpand(True)

        icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        icon.set_pixel_size(14)
        header.append(icon)

        self._title_label = Gtk.Label(label=_("Terminal"))
        self._title_label.add_css_class("split-pane-title")
        self._title_label.set_xalign(0)
        self._title_label.set_hexpand(True)
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        header.append(self._title_label)

        # Unsplit and fullscreen are revealed on hover (see the CSS) and sit
        # ahead of the group chip, which keeps its place next to close.
        unsplit_btn = self._make_header_button(
            "adw-external-link-symbolic", _("Move to Own Tab"),
            lambda _b: self._split_view_tab.move_pane_to_own_tab(self),
        )
        unsplit_btn.add_css_class("split-pane-hover-action")
        header.append(unsplit_btn)

        self._fullscreen_btn = self._make_header_button(
            "view-fullscreen-symbolic", _("Fullscreen"),
            lambda _b: self._split_view_tab.toggle_pane_fullscreen(self),
        )
        self._fullscreen_btn.add_css_class("split-pane-hover-action")
        header.append(self._fullscreen_btn)

        header.append(self._build_group_chip())

        header.append(self._make_header_button(
            "window-close-symbolic", _("Close Pane"),
            lambda _b: self.close_pane(),
        ))
        return header

    def _make_header_button(self, icon_name: str, tooltip: str, on_clicked) -> Gtk.Button:
        from sshpilot import icon_utils  # noqa: PLC0415
        button = Gtk.Button()
        icon_utils.set_button_icon(button, icon_name)
        button.add_css_class("flat")
        button.add_css_class("split-pane-close")
        button.set_tooltip_text(tooltip)
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", on_clicked)
        return button

    # ── header right-click menu ──────────────────────────────────────────────

    # Terminal-tab menu items that apply to a pane unchanged; they run the
    # window's own tab-menu handlers on this pane's terminal.
    _DELEGATED_TAB_ACTIONS = (
        ('tabmenu-reconnect', N_('Reconnect')),
        ('tabmenu-manage-files', N_('Manage Files')),
        ('tabmenu-show-files-panel', N_('Show File Pane')),
        ('tabmenu-hide-files-panel', N_('Hide File Pane')),
        ('tabmenu-open-system-terminal', N_('Open in System Terminal')),
    )

    def _install_header_menu(self) -> None:
        group = Gio.SimpleActionGroup()

        def _add(name, callback):
            action = Gio.SimpleAction.new(name, None)
            action.connect('activate', lambda *_a: callback())
            group.add_action(action)

        _add('duplicate', self._duplicate_into_new_pane)
        _add('rename', self._rename_from_menu)
        for name, _label in self._DELEGATED_TAB_ACTIONS:
            _add(name, lambda n=name: self._run_tab_action(n))
        _add('unsplit', lambda: self._split_view_tab.move_pane_to_own_tab(self))
        _add('fullscreen', lambda: self._split_view_tab.toggle_pane_fullscreen(self))
        _add('exit-fullscreen', lambda: self._split_view_tab.toggle_pane_fullscreen(self))
        _add('close', self.close_pane)
        self.insert_action_group('pane', group)
        self._header_actions = group

        def _item(label, name):
            item = Gio.MenuItem.new(label, 'pane.' + name)
            # Same convention as the tab menu: inapplicable items vanish.
            item.set_attribute_value('hidden-when', GLib.Variant('s', 'action-disabled'))
            return item

        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append_item(_item(_('Duplicate'), 'duplicate'))
        sec.append_item(_item(_('Rename…'), 'rename'))
        menu.append_section(None, sec)
        sec = Gio.Menu()
        for name, label in self._DELEGATED_TAB_ACTIONS:
            sec.append_item(_item(_(label), name))
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append_item(_item(_('Move to Own Tab'), 'unsplit'))
        sec.append_item(_item(_('Fullscreen'), 'fullscreen'))
        sec.append_item(_item(_('Exit Fullscreen'), 'exit-fullscreen'))
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append_item(_item(_('Close Pane'), 'close'))
        menu.append_section(None, sec)
        self._header_menu_model = menu

        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)
        gesture.connect('pressed', self._on_header_secondary_pressed)
        self._header.add_controller(gesture)

    def _current_terminal(self):
        terminals = self.get_terminals()
        return terminals[0] if terminals else None

    def _on_header_secondary_pressed(self, gesture, _n_press, x, y) -> None:
        terminal = self._current_terminal()
        if terminal is None:
            return
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self._split_view_tab._focus_pane(self)
        self._header_menu_xy = (x, y)

        try:
            enabled = self._window._enabled_tab_actions(terminal)
        except Exception:
            logger.debug("Could not gate the pane menu", exc_info=True)
            enabled = set()
        fullscreen = self._split_view_tab._fullscreen_pane is self
        states = {name: name in enabled for name, _label in self._DELEGATED_TAB_ACTIONS}
        states.update({
            'duplicate': 'tabmenu-duplicate' in enabled,
            'rename': True,
            'unsplit': True,
            'fullscreen': not fullscreen,
            'exit-fullscreen': fullscreen,
            'close': True,
        })
        for name, state in states.items():
            self._header_actions.lookup_action(name).set_enabled(state)

        if self._header_menu is None:
            self._header_menu = Gtk.PopoverMenu.new_from_model(self._header_menu_model)
            self._header_menu.set_parent(self._header)
            self._header_menu.set_has_arrow(False)
            self._header_menu.set_halign(Gtk.Align.START)
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        self._header_menu.set_pointing_to(rect)
        self._header_menu.popup()

    def _run_tab_action(self, name: str) -> None:
        terminal = self._current_terminal()
        if terminal is not None:
            self._window.run_tab_menu_action_for(terminal, name)

    def _duplicate_into_new_pane(self) -> None:
        """Duplicate opens the same host (or a local shell) in a new pane."""
        terminal = self._current_terminal()
        if terminal is None:
            return
        try:
            is_local = terminal._is_local_terminal()
        except Exception:
            is_local = False
        if is_local:
            self._split_view_tab.add_pane().add_local_terminal()
            return
        conn = self._window.terminal_to_connection.get(terminal) or getattr(
            terminal, 'connection', None
        )
        if conn is not None:
            self._split_view_tab.add_pane().add_connection(conn)

    def _rename_from_menu(self) -> None:
        page = self._inner_tab_view.get_selected_page() if self._has_terminals else None
        if page is not None:
            x, y = self._header_menu_xy
            self._show_inner_tab_rename_popover(page, x, y)

    def set_fullscreen_state(self, fullscreen: bool) -> None:
        """Flip the header's full screen button between enter and exit."""
        from sshpilot import icon_utils  # noqa: PLC0415
        icon_utils.set_button_icon(
            self._fullscreen_btn,
            "view-restore-symbolic" if fullscreen else "view-fullscreen-symbolic",
        )
        self._fullscreen_btn.set_tooltip_text(
            _("Exit Fullscreen") if fullscreen else _("Fullscreen")
        )
        if fullscreen:
            self.add_css_class("split-pane-fullscreen")
        else:
            self.remove_css_class("split-pane-fullscreen")

    def _bind_title(self) -> None:
        """Mirror the single page's title into the header label."""
        page = self._inner_tab_view.get_selected_page() if self._has_terminals else None
        if page is self._title_page:
            return
        if self._title_page is not None and self._title_handler is not None:
            try:
                self._title_page.disconnect(self._title_handler)
            except Exception:
                pass
        self._title_page = page
        self._title_handler = None
        if page is None:
            return
        self._title_label.set_label(page.get_title() or _("Terminal"))
        self._title_handler = page.connect(
            'notify::title',
            lambda p, _ps: self._title_label.set_label(p.get_title() or _("Terminal")),
        )

    def _build_group_chip(self) -> Gtk.Widget:
        chip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        chip.add_css_class("split-pane-group")
        chip.set_valign(Gtk.Align.CENTER)
        self._group_rgba: Optional[Gdk.RGBA] = None
        dot = Gtk.DrawingArea()
        dot.set_content_width(6)
        dot.set_content_height(6)
        dot.set_valign(Gtk.Align.CENTER)
        dot.set_draw_func(self._draw_group_dot)
        self._group_dot = dot
        self._group_label = Gtk.Label()
        chip.append(dot)
        chip.append(self._group_label)
        chip.set_visible(False)
        self._group_chip = chip
        return chip

    def _draw_group_dot(self, _area, cr, width, height) -> None:
        rgba = self._group_rgba
        if rgba is None:
            return
        cr.set_source_rgba(rgba.red, rgba.green, rgba.blue, rgba.alpha)
        cr.arc(width / 2, height / 2, min(width, height) / 2, 0, 2 * math.pi)
        cr.fill()

    def _group_for_terminal(self, terminal):
        """Return (name, color) of the terminal's connection group, or (None, None)."""
        conn = getattr(terminal, 'connection', None)
        gm = getattr(self._window, 'group_manager', None)
        if conn is None or gm is None:
            return None, None
        try:
            gid = gm.get_connection_group(conn)
            group = gm.groups.get(gid) if gid else None
        except Exception:
            return None, None
        if not group:
            return None, None
        return group.get('name'), group.get('color')

    def _update_group_chip(self) -> None:
        if getattr(self, '_group_chip', None) is None:
            return
        page = self._inner_tab_view.get_selected_page() if self._has_terminals else None
        terminal = page.get_child() if page is not None else None
        name, color = self._group_for_terminal(terminal) if terminal else (None, None)
        if not name:
            self._group_chip.set_visible(False)
            return
        self._group_label.set_label(name)
        rgba = Gdk.RGBA()
        self._group_rgba = rgba if color and rgba.parse(color) else None
        self._group_dot.set_visible(self._group_rgba is not None)
        self._group_dot.queue_draw()
        self._group_chip.set_visible(True)

    # ── placeholder ──────────────────────────────────────────────────────────

    def _build_placeholder(self) -> Gtk.Widget:
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        # Close-pane header (always visible even when empty)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.set_hexpand(True)
        close_btn = Gtk.Button()
        close_btn.set_icon_name("window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.set_tooltip_text(_("Close Pane"))
        close_btn.set_halign(Gtk.Align.END)
        close_btn.set_hexpand(True)
        close_btn.connect("clicked", lambda _b: self.close_pane())
        header.append(close_btn)
        outer.append(header)

        # Centered content — vexpand=True so valign=CENTER has space to work in
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        inner.set_halign(Gtk.Align.CENTER)
        inner.set_valign(Gtk.Align.CENTER)
        inner.set_vexpand(True)

        icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("dim-label")
        inner.append(icon)

        lbl = Gtk.Label(label=_("Drop a connection here"))
        lbl.add_css_class("dim-label")
        lbl.add_css_class("title-3")
        inner.append(lbl)

        sub = Gtk.Label(label=_("or"))
        sub.add_css_class("dim-label")
        inner.append(sub)

        select_host_btn = Gtk.Button(label=_("Select host"))
        select_host_btn.add_css_class("suggested-action")
        select_host_btn.add_css_class("pill")
        select_host_btn.set_halign(Gtk.Align.CENTER)
        select_host_btn.connect("clicked", self._on_select_host_clicked)
        inner.append(select_host_btn)

        local_btn = Gtk.Button(label=_("Local terminal"))
        local_btn.add_css_class("pill")
        local_btn.set_halign(Gtk.Align.CENTER)
        local_btn.set_tooltip_text(_("Open a local shell in this pane"))
        local_btn.connect("clicked", lambda _b: self.add_local_terminal())
        inner.append(local_btn)

        pick_btn = Gtk.Button(label=_("Pick existing tab"))
        pick_btn.add_css_class("pill")
        pick_btn.set_halign(Gtk.Align.CENTER)
        pick_btn.connect("clicked", self._on_pick_existing_tab_clicked)
        self._pick_button = pick_btn

        has_tabs = self._has_open_terminal_tabs()
        pick_btn.set_sensitive(has_tabs)
        if not has_tabs:
            pick_btn.set_tooltip_text(_("No open terminal tabs to pick from"))

        inner.append(pick_btn)
        outer.append(inner)
        return outer

    def _has_open_terminal_tabs(self) -> bool:
        from .terminal import TerminalWidget  # noqa: PLC0415
        try:
            n = self._window.tab_view.get_n_pages()
            for i in range(n):
                page = self._window.tab_view.get_nth_page(i)
                if page and isinstance(page.get_child(), TerminalWidget):
                    return True
        except Exception:
            pass
        return False

    def _restore_placeholder(self) -> bool:
        """Swap back from tab container to placeholder.  Called via idle_add."""
        if not self._has_terminals:
            return False  # already restored; guard against double idle_add
        try:
            self.remove(self._tab_container)
        except Exception:
            pass
        self._has_terminals = False
        self._placeholder = self._build_placeholder()
        self._placeholder.set_hexpand(True)
        self._placeholder.set_vexpand(True)
        self.append(self._placeholder)
        self._split_view_tab._update_tab_title()
        return False  # one-shot idle

    # ── terminal management ──────────────────────────────────────────────────

    def add_terminal(self, terminal, title: Optional[str] = None) -> None:
        """Embed a TerminalWidget in this pane (one terminal per pane).

        If this pane is already occupied the terminal goes into a new pane."""
        if self.get_terminal_count() >= 1:
            self._split_view_tab.add_pane().add_terminal(terminal, title)
            return
        # If this terminal came from the main tab view it carries the window's
        # convert-to-split DropTarget. Inside a pane that target just swallows
        # connection drops over the terminal area (its handler finds no main-tab
        # page), so only the tab bar would accept drops. Detach it so the pane's
        # own drop target handles drops anywhere in the pane.
        _convert_dt = getattr(terminal, '_convert_to_split_dt', None)
        if _convert_dt is not None:
            try:
                terminal.remove_controller(_convert_dt)
            except Exception:
                pass
            terminal._convert_to_split_dt = None

        # Ensure the terminal is detached from any existing parent first so
        # GTK4 allows reparenting into the inner TabView.
        try:
            parent = terminal.get_parent()
            if parent is not None:
                terminal.unparent()
        except Exception:
            pass

        if not self._has_terminals:
            try:
                self.remove(self._placeholder)
            except Exception:
                pass
            self.append(self._tab_container)
            self._has_terminals = True

        if title is None:
            conn = getattr(terminal, 'connection', None)
            title = getattr(conn, 'nickname', None) or _("Terminal")

        from sshpilot import icon_utils  # noqa: PLC0415
        page = self._inner_tab_view.append(terminal)
        page.set_title(title)
        try:
            page.set_icon(icon_utils.new_gicon_from_icon_name('utilities-terminal-symbolic'))
        except Exception:
            pass
        self._inner_tab_view.set_selected_page(page)
        self._split_view_tab._update_tab_title()
        self._split_view_tab._note_active_pane(self)
        self._bind_title()
        self._update_group_chip()

    def add_connection(self, connection) -> None:
        """Create a new terminal for connection and add it to this pane."""
        terminal = self._window.terminal_manager.create_terminal_for_pane(connection)
        self.add_terminal(terminal, getattr(connection, 'nickname', None))

    def add_local_terminal(self) -> None:
        """Create a local-shell terminal and add it to this pane."""
        terminal = self._window.terminal_manager.create_local_terminal_for_pane(
            _("Terminal")
        )
        self.add_terminal(terminal, _("Terminal"))

    def get_terminals(self) -> list:
        result = []
        if not self._has_terminals:
            return result
        try:
            n = self._inner_tab_view.get_n_pages()
            for i in range(n):
                page = self._inner_tab_view.get_nth_page(i)
                if page:
                    child = page.get_child()
                    if child is not None:
                        result.append(child)
        except Exception:
            pass
        return result

    def get_terminal_count(self) -> int:
        if not self._has_terminals:
            return 0
        try:
            return self._inner_tab_view.get_n_pages()
        except Exception:
            return 0

    def _on_inner_close(self, tab_view, page) -> bool:
        terminal = page.get_child() if page else None
        if terminal is not None:
            if terminal in self._window.terminal_to_connection:
                self._split_view_tab._cleanup_terminal(terminal)

        # Let Adw.TabView finish closing first, then decide whether this pane
        # should disappear entirely (last inner tab closed) or keep showing a
        # placeholder. This avoids stale empty panes and tab/pane mismatch.
        GLib.idle_add(self._after_inner_close)

        return False  # Allow AdwTabView to proceed with the close

    def _after_inner_close(self) -> bool:
        if self.get_terminal_count() == 0:
            self.close_pane()
        return False

    # ── pane close ───────────────────────────────────────────────────────────

    def close_pane(self) -> None:
        for terminal in self.get_terminals():
            if terminal in self._window.terminal_to_connection:
                self._split_view_tab._cleanup_terminal(terminal)
        self._split_view_tab.remove_pane(self)

    # ── drag-and-drop ────────────────────────────────────────────────────────

    def _setup_drop_target(self) -> None:
        dt = new_internal_drop_target()
        dt.connect("drop", self._on_drop)
        dt.connect("enter", lambda _t, _x, _y: Gdk.DragAction.MOVE)
        # The pane holds the terminals, so it must get ahead of PyXterm's
        # web view as well.
        claim_in_app_drops_ahead_of_children(dt)
        self.add_controller(dt)

    def _on_drop(self, _target, value, _x: float, _y: float) -> bool:
        try:
            if _is_local_terminal_drop(value):
                if self.get_terminal_count() == 0:
                    self.add_local_terminal()
                else:
                    self._split_view_tab.add_pane().add_local_terminal()
                return True

            nicknames = _connections_from_drop_payload(value)
            if not nicknames:
                return False

            for nick in nicknames:
                conn = self._window.connection_manager.find_connection_by_nickname(nick)
                if conn is None:
                    continue
                # Never stack multiple tabs inside one pane. Fill THIS pane only
                # while it's still an empty placeholder; once it holds a terminal,
                # each dropped connection opens in a new pane instead.
                if self.get_terminal_count() == 0:
                    self.add_connection(conn)
                else:
                    self._split_view_tab.add_pane().add_connection(conn)
            return True
        except Exception as exc:
            logger.error("SplitPane drop failed: %s", exc)
            return False

    # ── inner tab rename ─────────────────────────────────────────────────────

    def _on_header_pressed(self, gesture, n_press, x, y) -> None:
        if n_press == 1:
            # The header holds nothing focusable, so a click there would leave
            # focus (and the active ring) on another pane; hand it to the
            # terminal as a click inside the pane does.
            self._split_view_tab._focus_pane(self)
            return
        if n_press != 2:
            return
        page = self._inner_tab_view.get_selected_page() if self._has_terminals else None
        if page is not None:
            self._show_inner_tab_rename_popover(page, x, y)

    def _show_inner_tab_rename_popover(self, page, x: float, y: float) -> None:
        entry = Gtk.Entry()
        entry.set_text(page.get_title())
        entry.set_width_chars(20)

        popover = Gtk.Popover()
        popover.set_child(entry)
        popover.set_parent(self._header)
        popover.set_has_arrow(False)

        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
        popover.set_pointing_to(rect)
        popover._committed = False

        def commit(_entry):
            if not popover._committed:
                popover._committed = True
                text = entry.get_text().strip()
                if text:
                    page.set_title(text)
            popover.popdown()

        def on_closed(p):
            if not p._committed:
                text = entry.get_text().strip()
                if text:
                    page.set_title(text)

        entry.connect("activate", commit)
        popover.connect("closed", on_closed)
        popover.popup()
        GLib.idle_add(lambda: (entry.grab_focus(), entry.select_region(0, -1), False)[-1])

    # ── "Select host" button (reuses the command-snippets host picker) ─────────

    def _on_select_host_clicked(self, button: Gtk.Button) -> None:
        from .host_picker import show_host_picker  # noqa: PLC0415
        show_host_picker(
            self._window,
            button,
            lambda conn: self.add_connection(conn),
            include_local_terminal=True,
            on_select_local=self.add_local_terminal,
        )

    # ── "Pick existing tab" button ────────────────────────────────────────────

    def _on_pick_existing_tab_clicked(self, button: Gtk.Button) -> None:
        from .terminal import TerminalWidget  # noqa: PLC0415
        window = self._window

        tab_entries = []
        try:
            n = window.tab_view.get_n_pages()
            for i in range(n):
                page = window.tab_view.get_nth_page(i)
                if page and isinstance(page.get_child(), TerminalWidget):
                    tab_entries.append(
                        (page, page.get_child(), page.get_title() or _("Terminal"))
                    )
        except Exception as exc:
            logger.error("Failed to enumerate terminal tabs: %s", exc)
            return

        if not tab_entries:
            return

        popover = Gtk.Popover()
        popover.set_parent(button)
        popover.set_autohide(True)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        list_box.add_css_class("navigation-sidebar")

        for page, terminal, title in tab_entries:
            row = Gtk.ListBoxRow()
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            row_box.set_margin_top(6)
            row_box.set_margin_bottom(6)
            row_box.set_margin_start(12)
            row_box.set_margin_end(12)
            img = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
            row_box.append(img)
            lbl = Gtk.Label(label=title)
            lbl.set_xalign(0)
            lbl.set_max_width_chars(40)
            lbl.set_hexpand(True)
            row_box.append(lbl)
            row.set_child(row_box)
            row._embed_page = page
            row._embed_terminal = terminal
            row._embed_title = title
            list_box.append(row)

        def _on_row_activated(_lb, row):
            popover.popdown()
            # Defer so the popover can close first, then do the reparent
            GLib.idle_add(
                self._embed_existing_tab,
                row._embed_page,
                row._embed_terminal,
                row._embed_title,
            )

        list_box.connect("row-activated", _on_row_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # size_request forces the popover wide enough to show full titles
        scroll.set_size_request(320, -1)
        scroll.set_max_content_height(300)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(list_box)
        popover.set_child(scroll)
        popover.popup()

    def _embed_existing_tab(
        self, page, terminal, title: Optional[str] = None
    ) -> bool:
        """Move a terminal from the main tab_view into this pane (live session)."""
        window = self._window
        window._suppress_close_confirmation = True
        window._moving_tab_to_pane = True
        try:
            window.tab_view.close_page(page)
        finally:
            window._suppress_close_confirmation = False
            window._moving_tab_to_pane = False
        # Defer the actual reparent so the tab_view has time to finish its
        # internal close sequence before we try to reparent the widget.
        GLib.idle_add(self._finish_embed, terminal, title)
        return False  # one-shot idle (this function was called via idle_add)

    def _finish_embed(self, terminal, title: Optional[str]) -> bool:
        self.add_terminal(terminal, title)
        return False


# ════════════════════════════════════════════════════════════════════════════


class SplitViewTab(Gtk.Box):
    """
    Top-level widget for a split-view tab page.

    Contains:
    - A Gtk.Box (content area) holding a dynamically rebuilt nested Gtk.Paned
      structure so every pane boundary is drag-resizable.
    - An action bar below the panes with scroll and add controls.
    """

    __gtype_name__ = "SshPilotSplitViewTab"

    HORIZONTAL = 'horizontal'
    VERTICAL = 'vertical'
    MIN_PANE_HEIGHT_RATIO = 0.5   # default row height as fraction of viewport
    ABSOLUTE_MIN_ROW_HEIGHT = 100  # hard floor when manually shrinking a row
    MIN_PANE_WIDTH = 200          # minimum width per pane in side-by-side splits (px)
    SCROLL_SPACER_HEIGHT = 600  # extra scroll room below the last row
    ROW_HANDLE_HEIGHT = 6

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self._panes: List[SplitPane] = []
        self._layout_mode = self.HORIZONTAL
        self._tab_page = None
        self._row_heights: List[int] = []
        self._row_height_ratios: List[float] = []
        self._manual_row_indices: set[int] = set()
        self._row_boxes: List[Gtk.Box] = []
        # Horizontal paneds currently in the layout, ordered by pane-pair index.
        # Tracked so width ratios can be preserved across rebuilds and
        # deterministically re-applied (the width analogue of _row_heights).
        self._h_paneds: List[Gtk.Paned] = []
        self._fill_viewport = True
        self._viewport_sync_scheduled = False
        self._scroll_spacer: Optional[Gtk.Box] = None
        self._last_active_pane: Optional[SplitPane] = None
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._content_area.set_hexpand(True)
        self._content_area.set_vexpand(False)

        self._pane_scroll = Gtk.ScrolledWindow()
        self._pane_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        # A floating scrollbar would sit on top of the right-edge pane's own
        # terminal scrollbar and swallow its clicks (#1251); give it a column.
        self._pane_scroll.set_overlay_scrolling(False)

        self._pane_scroll.set_hexpand(True)
        self._pane_scroll.set_vexpand(True)
        self._pane_scroll.set_child(self._content_area)

        # Ghost line shown during row-resize drag to give live position feedback.
        _ensure_row_handle_css()
        self._drag_ghost = Gtk.Box()
        self._drag_ghost.add_css_class("row-drag-ghost")
        self._drag_ghost.set_size_request(-1, 2)
        self._drag_ghost.set_hexpand(True)
        self._drag_ghost.set_valign(Gtk.Align.START)
        self._drag_ghost.set_visible(False)

        self._scroll_overlay = Gtk.Overlay()
        self._scroll_overlay.set_hexpand(True)
        self._scroll_overlay.set_vexpand(True)
        self._scroll_overlay.set_child(self._pane_scroll)
        self._scroll_overlay.add_overlay(self._drag_ghost)
        self._scroll_overlay.set_clip_overlay(self._drag_ghost, True)
        self.append(self._scroll_overlay)

        # Holds the one pane shown in pane full screen (see
        # _begin_pane_fullscreen); hidden otherwise.
        self._fullscreen_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._fullscreen_box.set_hexpand(True)
        self._fullscreen_box.set_vexpand(True)
        self._fullscreen_box.set_visible(False)
        self.append(self._fullscreen_box)
        self._fullscreen_pane: Optional[SplitPane] = None
        self._started_window_fullscreen = False
        self._fullscreened_handler: Optional[int] = None

        # Refit the panes whenever the scroll viewport's height changes. GTK4
        # widgets have no usable size-change signal ('notify::height' is not a
        # real property and Gtk.Box ignores a do_size_allocate override), but the
        # scrolled window's vertical adjustment emits 'notify::page-size' whenever
        # its visible height changes — on window resize AND when the bottom action
        # bar reveals and steals space. page-size tracks the viewport only (not
        # the content height we set during the sync), so there's no feedback loop.
        self._pane_scroll.get_vadjustment().connect(
            'notify::page-size', self._schedule_viewport_sync
        )
        self.connect('map', self._schedule_viewport_sync)
        # Reveal the action bar ~1s after the tab is first shown, then leave it.
        self.connect('map', self._on_first_map_reveal_strip)
        try:
            window.connect('notify::default-height', self._schedule_viewport_sync)
        except Exception:
            pass

        # Action bar strip below the panes (revealed shortly after the tab opens)
        self._add_pane_btn: Optional[Gtk.Button] = None
        self._add_pane_strip: Optional[Gtk.ActionBar] = None
        self._layout_h_btn: Optional[Gtk.ToggleButton] = None
        self._layout_v_btn: Optional[Gtk.ToggleButton] = None
        self._layout_toggle_updating: list = [False]
        self._add_strip_reveal_scheduled = False
        self._add_strip = self._build_add_pane_strip()
        self.append(self._add_strip)

        # Start with 2 empty panes (minimum requirement)
        self.add_pane()
        self.add_pane()

        # Monitor drag motion over the entire tab so the strip reacts as soon
        # as a connection is dragged anywhere over the terminal area.
        drag_motion = Gtk.DropControllerMotion()
        drag_motion.connect("enter", self._on_drag_enter_tab)
        drag_motion.connect("leave", self._on_drag_leave_tab)
        self.add_controller(drag_motion)

        # Customizable split-view shortcuts (action name → callback). The
        # accelerators live in the shortcut registry/config (see main.py
        # register_custom_shortcut); _on_key_pressed matches the live event
        # against their effective accelerators. Built once; the callbacks bind
        # to this instance with constant arguments.
        self._split_actions = {
            'split-focus-left':  lambda: self._navigate_pane('left'),
            'split-focus-down':  lambda: self._navigate_pane('down'),
            'split-focus-up':    lambda: self._navigate_pane('up'),
            'split-focus-right': lambda: self._navigate_pane('right'),
            'split-resize-left':  lambda: self._resize_active_pane('left'),
            'split-resize-down':  lambda: self._resize_active_pane('down'),
            'split-resize-up':    lambda: self._resize_active_pane('up'),
            'split-resize-right': lambda: self._resize_active_pane('right'),
            'split-layout-horizontal': lambda: self.set_layout_mode(self.HORIZONTAL),
            'split-layout-vertical':   lambda: self.set_layout_mode(self.VERTICAL),
            'split-add-pane': lambda: self.add_pane(),
        }

        # CAPTURE-phase key controller intercepts shortcuts before VTE eats them
        key_ctrl = Gtk.EventControllerKey()
        key_ctrl.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_ctrl.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_ctrl)

    # ── layout mode (public API for window.py overlay toggle) ────────────────

    def get_layout_mode(self) -> str:
        return self._layout_mode

    def set_layout_mode(self, mode: str) -> None:
        if mode not in (self.HORIZONTAL, self.VERTICAL):
            return
        if mode != self._layout_mode:
            self._layout_mode = mode
            self._rebuild_layout()
        self._sync_layout_toggle_buttons()
        try:
            if hasattr(self.window, '_update_layout_toggle_state'):
                self.window._update_layout_toggle_state()
        except Exception:
            pass

    def _sync_layout_toggle_buttons(self) -> None:
        if self._layout_h_btn is None or self._layout_v_btn is None:
            return
        self._layout_toggle_updating[0] = True
        try:
            self._layout_h_btn.set_active(self._layout_mode == self.HORIZONTAL)
            self._layout_v_btn.set_active(self._layout_mode == self.VERTICAL)
        finally:
            self._layout_toggle_updating[0] = False

    def scroll_panes_to_top(self) -> None:
        try:
            adj = self._pane_scroll.get_vadjustment()
            adj.set_value(adj.get_lower())
        except Exception:
            pass

    def scroll_panes_to_bottom(self) -> None:
        try:
            adj = self._pane_scroll.get_vadjustment()
            upper = adj.get_upper()
            page = adj.get_page_size()
            adj.set_value(max(adj.get_lower(), upper - page))
        except Exception:
            pass

    def fill_panes_to_viewport(self) -> None:
        """Reset rows to auto-fill the scroll viewport (no leftover empty space).

        Re-enables fill mode and clears manual overrides so the rows are sized
        to exactly fill the viewport — for a single row (e.g. two side-by-side
        panes) that means the full height rather than the 50% default. Also
        resets any horizontal pane-width splits back to 50/50."""
        viewport = self._get_scroll_viewport_height()
        if viewport <= 0:
            GLib.idle_add(lambda: self.fill_panes_to_viewport() or False)
            return
        self._manual_row_indices.clear()
        self._row_height_ratios.clear()
        self._fill_viewport = True
        for row_box in self._row_boxes:
            child = row_box.get_first_child()
            if (child is not None
                    and isinstance(child, Gtk.Paned)
                    and child.get_orientation() == Gtk.Orientation.HORIZONTAL
                    and hasattr(child, '_split_ratio')):
                child._split_ratio = 0.5
                total = child.get_allocated_width()
                if total > 0:
                    child._in_ratio_update = True
                    child.set_position(total // 2)
                    child._in_ratio_update = False
        self._sync_row_heights_to_viewport()
        self.scroll_panes_to_top()

    def _on_default_clicked(self) -> None:
        """Handle the "Default" button.

        With two panes (a single side-by-side row, or two stacked rows) reset to
        fill the viewport so no empty space is left below. With more than two
        panes keep the previous behaviour (50% rows that scroll)."""
        if len(self._panes) <= 2:
            self.fill_panes_to_viewport()
        else:
            self.reset_all_row_heights(0.5)

    def reset_all_row_heights(self, ratio: float) -> None:
        """Reset every row to `ratio` × viewport height, clearing all manual overrides.
        Also resets any horizontal pane-width splits back to 50/50."""
        viewport = self._get_scroll_viewport_height()
        if viewport <= 0:
            GLib.idle_add(lambda: self.reset_all_row_heights(ratio) or False)
            return
        self._manual_row_indices.clear()
        self._row_height_ratios.clear()
        self._fill_viewport = False
        height = max(self.ABSOLUTE_MIN_ROW_HEIGHT, int(viewport * ratio))
        num_rows = len(self._row_boxes)
        self._row_heights = [height] * num_rows
        for i, row_box in enumerate(self._row_boxes):
            row_box.set_size_request(-1, height)
            self._manual_row_indices.add(i)
            self._row_height_ratios.append(ratio)
            child = row_box.get_first_child()
            if (child is not None
                    and isinstance(child, Gtk.Paned)
                    and child.get_orientation() == Gtk.Orientation.HORIZONTAL
                    and hasattr(child, '_split_ratio')):
                child._split_ratio = 0.5
                total = child.get_allocated_width()
                if total > 0:
                    child._in_ratio_update = True
                    child.set_position(total // 2)
                    child._in_ratio_update = False
        self._normalize_pane_heights()
        self.scroll_panes_to_top()

    # ── toolbar strip ────────────────────────────────────────────────────────

    def _build_add_pane_strip(self) -> Gtk.Widget:
        _ensure_drop_zone_css()

        # Gtk.ActionBar gives native bottom-bar styling and a built-in revealer
        # (set_revealed) animation. It starts hidden and is revealed shortly
        # after the tab is shown (see _on_first_map_reveal_strip).
        strip = Gtk.ActionBar()
        strip.add_css_class("add-pane-strip")
        strip.set_hexpand(True)
        strip.set_revealed(False)

        from sshpilot import icon_utils  # noqa: PLC0415

        scroll_top_btn = Gtk.Button()
        icon_utils.set_button_icon(scroll_top_btn, 'top-large-symbolic')
        scroll_top_btn.set_tooltip_text(_("Scroll to top"))
        scroll_top_btn.add_css_class("pill")
        scroll_top_btn.connect("clicked", lambda _b: self.scroll_panes_to_top())
        strip.pack_start(scroll_top_btn)

        scroll_bottom_btn = Gtk.Button()
        icon_utils.set_button_icon(scroll_bottom_btn, 'bottom-large-symbolic')
        scroll_bottom_btn.set_tooltip_text(_("Scroll to bottom"))
        scroll_bottom_btn.add_css_class("pill")
        scroll_bottom_btn.connect("clicked", lambda _b: self.scroll_panes_to_bottom())
        strip.pack_start(scroll_bottom_btn)

        large_btn = Gtk.Button(label=_("Default"))
        large_btn.add_css_class("pill")
        large_btn.set_tooltip_text(_("Reset panes to their default size"))
        large_btn.connect("clicked", lambda _b: self._on_default_clicked())
        strip.pack_start(large_btn)

        compact_btn = Gtk.Button(label=_("Compact"))
        compact_btn.add_css_class("pill")
        compact_btn.set_tooltip_text(_("Reset panes to a smaller size"))
        compact_btn.connect("clicked", lambda _b: self.reset_all_row_heights(0.3))
        strip.pack_start(compact_btn)

        self._layout_h_btn, self._layout_v_btn, self._layout_toggle_updating = (
            create_layout_toggle_buttons(
                lambda: self.set_layout_mode(self.HORIZONTAL),
                lambda: self.set_layout_mode(self.VERTICAL),
                as_pill=True,
            )
        )
        strip.pack_start(self._layout_h_btn)
        strip.pack_start(self._layout_v_btn)

        add_btn = Gtk.Button(label=_("+ Add Terminal"))
        add_btn.add_css_class("suggested-action")
        add_btn.add_css_class("pill")
        add_btn.connect("clicked", lambda _b: self.add_pane())
        # pack_end places it at the trailing edge — no manual halign/hexpand hack.
        strip.pack_end(add_btn)

        self._add_pane_btn = add_btn
        self._add_pane_strip = strip
        self._sync_layout_toggle_buttons()

        dt = new_internal_drop_target()
        dt.connect("enter", lambda _t, _x, _y: Gdk.DragAction.MOVE)
        dt.connect("drop", self._on_add_pane_drop)
        strip.add_controller(dt)

        return strip

    def _on_first_map_reveal_strip(self, *_args) -> None:
        """On the tab's first map, schedule the action bar's animated reveal a
        second later. Guarded so repeated map/unmap cycles don't re-trigger it."""
        if self._add_strip_reveal_scheduled:
            return
        self._add_strip_reveal_scheduled = True
        GLib.timeout_add(1000, self._reveal_add_strip)

    def _reveal_add_strip(self) -> bool:
        if self._add_pane_strip is not None:
            try:
                self._add_pane_strip.set_revealed(True)
            except Exception:
                pass
        return False  # one-shot

    # ── drag-over strip state ─────────────────────────────────────────────────

    def _on_drag_enter_tab(self, _controller, _x, _y) -> None:
        self._set_add_pane_drop_highlight(True)

    def _on_drag_leave_tab(self, _controller) -> None:
        self._set_add_pane_drop_highlight(False)

    def _set_add_pane_drop_highlight(self, active: bool) -> None:
        if self._add_pane_strip:
            if active:
                self._add_pane_strip.add_css_class("drag-over")
            else:
                self._add_pane_strip.remove_css_class("drag-over")
        if self._scroll_spacer:
            if active:
                self._scroll_spacer.add_css_class("drag-over")
            else:
                self._scroll_spacer.remove_css_class("drag-over")
        if self._add_pane_btn:
            self._add_pane_btn.set_label(
                _("Drop here to add") if active else _("Add Terminal")
            )

    def _on_add_pane_drop(self, _target, value, _x, _y) -> bool:
        try:
            if _is_local_terminal_drop(value):
                self.add_pane().add_local_terminal()
                return True
            nicknames = _connections_from_drop_payload(value)
            if not nicknames:
                return False
            for nick in nicknames:
                conn = self.window.connection_manager.find_connection_by_nickname(nick)
                if conn is not None:
                    self.add_pane().add_connection(conn)
            return True
        except Exception as exc:
            logger.error("add-pane drop failed: %s", exc)
            return False

    def _append_scroll_spacer(self) -> None:
        """Append the extra scroll area below rows; accepts connection drops."""
        _ensure_drop_zone_css()
        spacer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        spacer.add_css_class("add-pane-scroll-spacer")
        spacer.set_hexpand(True)
        spacer.set_size_request(-1, self.SCROLL_SPACER_HEIGHT)

        # Dim centered hint so the reserved scroll room reads as intentional
        # empty space rather than a rendering gap. Native Adwaita classes only
        # (no custom visuals); the existing .drag-over tint takes over as the
        # strong signal while dragging.
        hint = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hint.set_halign(Gtk.Align.CENTER)
        hint.set_valign(Gtk.Align.CENTER)
        hint.set_hexpand(True)
        hint_label = Gtk.Label(
            label=_(
                "Empty space. Drop connections here,"
                " or press the Add Terminal button below."
            )
        )
        hint_label.set_halign(Gtk.Align.CENTER)
        hint_label.set_justify(Gtk.Justification.CENTER)
        hint_label.add_css_class("dim-label")
        hint_label.add_css_class("monospace")
        hint.append(hint_label)
        top_filler = Gtk.Box()
        top_filler.set_vexpand(True)
        bottom_filler = Gtk.Box()
        bottom_filler.set_vexpand(True)
        spacer.append(top_filler)
        spacer.append(hint)
        spacer.append(bottom_filler)

        dt = new_internal_drop_target()
        dt.connect("enter", lambda _t, _x, _y: self._on_scroll_spacer_drag_enter())
        dt.connect("leave", lambda _t: self._on_scroll_spacer_drag_leave())
        dt.connect("drop", self._on_add_pane_drop)
        spacer.add_controller(dt)

        self._scroll_spacer = spacer
        self._content_area.append(spacer)

    def _on_scroll_spacer_drag_enter(self) -> Gdk.DragAction:
        self._set_add_pane_drop_highlight(True)
        return Gdk.DragAction.MOVE

    def _on_scroll_spacer_drag_leave(self) -> None:
        self._set_add_pane_drop_highlight(False)

    # ── pane management ──────────────────────────────────────────────────────

    def add_pane(self) -> SplitPane:
        """Add a new empty pane and rebuild the layout. Returns the new pane."""
        pane = SplitPane(self, self.window)  # SplitPane.__init__ calls register_pane
        self._rebuild_layout()
        return pane

    def remove_pane(self, pane: SplitPane) -> None:
        if pane in self._panes:
            self._panes.remove(pane)
        # _rebuild_layout uses _release_paned to safely detach panes from
        # their Paned containers; no separate unparent() needed here.
        self._rebuild_layout()
        self._update_tab_title()
        if (len(self._panes) == 1
                and self._panes[0].get_terminal_count() == 1
                and not getattr(self, '_collapse_pending', False)):
            # Pane removal can run from an inner TabView close callback. Wait
            # until that close has settled before reparenting the survivor.
            self._collapse_pending = True
            GLib.idle_add(self._collapse_single_terminal_pane)
        # Close the tab if no panes remain
        if not self._panes and self._tab_page is not None:
            self.window._suppress_close_confirmation = True
            try:
                self.window.tab_view.close_page(self._tab_page)
            finally:
                self.window._suppress_close_confirmation = False

    def _collapse_single_terminal_pane(self) -> bool:
        """Replace a one-terminal split tab with a normal terminal tab."""
        self._collapse_pending = False
        if len(self._panes) != 1 or self._tab_page is None:
            return False
        pane = self._panes[0]
        terminals = pane.get_terminals()
        if len(terminals) != 1:
            return False

        terminal = terminals[0]
        tab_view = self.window.tab_view
        try:
            position = tab_view.get_page_position(self._tab_page)
        except Exception:
            position = tab_view.get_n_pages()

        # Detach only the widget. Closing the inner page would run its normal
        # terminal cleanup path and disconnect the session we are preserving.
        title = self._detach_pane_terminal(pane, terminal)
        if title is None:
            return False

        self.window._suppress_close_confirmation = True
        try:
            tab_view.close_page(self._tab_page)
        finally:
            self.window._suppress_close_confirmation = False

        self._insert_main_tab(terminal, title, position)
        return False

    def _detach_pane_terminal(self, pane: SplitPane, terminal) -> Optional[str]:
        """Unparent a pane's terminal without closing its session.

        Returns the terminal's title, or None if it could not be detached.
        """
        inner_page = pane._inner_tab_view.get_page(terminal)
        title = (
            inner_page.get_title()
            if inner_page is not None
            else getattr(getattr(terminal, 'connection', None), 'nickname', None)
        ) or _("Terminal")
        try:
            terminal.unparent()
        except Exception:
            logger.debug("Could not detach split terminal", exc_info=True)
            return None
        return title

    def _insert_main_tab(self, terminal, title: str, position: int) -> None:
        tab_view = self.window.tab_view
        try:
            page = tab_view.insert(terminal, position)
        except Exception:
            page = tab_view.append(terminal)
        page.set_title(title)
        try:
            from sshpilot import icon_utils  # noqa: PLC0415
            page.set_icon(
                icon_utils.new_gicon_from_icon_name('utilities-terminal-symbolic')
            )
        except Exception:
            pass
        tab_view.set_selected_page(page)
        self.window._update_tab_button_visibility()

    def move_pane_to_own_tab(self, pane: SplitPane) -> None:
        """Unsplit: move the pane's terminal into a regular tab of its own.

        The new tab opens right after this split tab. The emptied pane is
        removed, which may in turn collapse or close this split tab.
        """
        terminals = pane.get_terminals()
        if not terminals or self._tab_page is None:
            return
        if self._fullscreen_pane is not None:
            self._end_pane_fullscreen()
        terminal = terminals[0]
        tab_view = self.window.tab_view
        try:
            position = tab_view.get_page_position(self._tab_page) + 1
        except Exception:
            position = tab_view.get_n_pages()
        title = self._detach_pane_terminal(pane, terminal)
        if title is None:
            return
        # The pane's own close path would clean up the terminal; it no longer
        # holds it, so it just leaves the layout.
        pane._has_terminals = False
        self._insert_main_tab(terminal, title, position)
        self.remove_pane(pane)

    # ── pane full screen ─────────────────────────────────────────────────────

    def toggle_pane_fullscreen(self, pane: SplitPane) -> None:
        if self._fullscreen_pane is pane:
            self._end_pane_fullscreen()
        else:
            self._begin_pane_fullscreen(pane)

    def _begin_pane_fullscreen(self, pane: SplitPane) -> None:
        """Show one pane alone, filling the window in full screen.

        The pane leaves the grid for a dedicated container; leaving full
        screen rebuilds the grid, which puts it back in place.
        """
        if self._fullscreen_pane is not None:
            self._end_pane_fullscreen()
        if pane not in self._panes:
            return
        self._release_pane(pane)
        self._fullscreen_box.append(pane)
        self._scroll_overlay.set_visible(False)
        self._add_strip.set_visible(False)
        self._fullscreen_box.set_visible(True)
        self._fullscreen_pane = pane
        pane.set_fullscreen_state(True)
        self._focus_pane(pane)

        controller = getattr(self.window, 'fullscreen_controller', None)
        self._started_window_fullscreen = False
        if controller is not None and not controller.active:
            controller.enter()
            self._started_window_fullscreen = True
        if self._fullscreened_handler is None:
            self._fullscreened_handler = self.window.connect(
                'notify::fullscreened', self._on_window_fullscreened_changed
            )

    def _end_pane_fullscreen(self, *, leave_window_fullscreen: bool = True) -> None:
        pane = self._fullscreen_pane
        if pane is None:
            return
        self._fullscreen_pane = None
        if self._fullscreened_handler is not None:
            self.window.disconnect(self._fullscreened_handler)
            self._fullscreened_handler = None
        try:
            self._fullscreen_box.remove(pane)
        except Exception:
            pass
        self._fullscreen_box.set_visible(False)
        self._scroll_overlay.set_visible(True)
        self._add_strip.set_visible(True)
        pane.set_fullscreen_state(False)
        if pane in self._panes:
            self._rebuild_layout()
            self._focus_pane(pane)

        controller = getattr(self.window, 'fullscreen_controller', None)
        if (leave_window_fullscreen and self._started_window_fullscreen
                and controller is not None and controller.active):
            controller.exit()
        self._started_window_fullscreen = False

    def _on_window_fullscreened_changed(self, window, _pspec) -> None:
        # Escape, F11 or the WM took the window out of full screen: the pane
        # goes back into the grid too.
        if not window.is_fullscreen():
            self._end_pane_fullscreen(leave_window_fullscreen=False)

    def _release_pane(self, pane: SplitPane) -> None:
        parent = pane.get_parent()
        if isinstance(parent, Gtk.Paned):
            if parent.get_start_child() is pane:
                parent.set_start_child(None)
            elif parent.get_end_child() is pane:
                parent.set_end_child(None)
        elif parent is not None:
            parent.remove(pane)

    def register_pane(self, pane: SplitPane) -> None:
        if pane not in self._panes:
            self._panes.append(pane)

    def _note_active_pane(self, pane: SplitPane) -> None:
        """Remember which pane the user last interacted with."""
        if pane in self._panes:
            self._last_active_pane = pane

    def get_pane_count(self) -> int:
        return len(self._panes)

    # ── proportional Paned factory ────────────────────────────────────────────

    def _make_proportional_paned(
        self,
        orientation: Gtk.Orientation,
        ratio: float = 0.5,
    ) -> Gtk.Paned:
        """Return a Gtk.Paned whose divider tracks a ratio, not a pixel position.

        When the widget is resized the divider is repositioned at
        `ratio × new_total_size` so the split stays proportional.  When the
        user drags the divider the new ratio is saved automatically.
        `shrink=False` on both children ensures neither pane collapses below
        its minimum size (set via set_size_request on the SplitPane).
        """
        p = Gtk.Paned(orientation=orientation)
        p.set_hexpand(True)
        p.set_vexpand(True)
        p.set_wide_handle(True)
        p.set_resize_start_child(True)
        p.set_resize_end_child(True)
        p.set_shrink_start_child(False)
        p.set_shrink_end_child(False)

        is_vertical = (orientation == Gtk.Orientation.VERTICAL)
        p._split_ratio = ratio
        p._in_ratio_update = False
        # Until the intended ratio has been applied at a real allocation, GTK's
        # own default-position notifications must NOT overwrite _split_ratio —
        # otherwise a ratio carried over from a previous layout is clobbered back
        # to the natural-size split before _apply_ratio gets to enforce it.
        p._ratio_settled = False

        def _apply_ratio(paned: Gtk.Paned) -> None:
            total = paned.get_allocated_height() if is_vertical else paned.get_allocated_width()
            if total <= 0:
                return
            pos = int(paned._split_ratio * total)
            min_ext = (self._min_extent_for_total(total) if is_vertical
                       else self.MIN_PANE_WIDTH)
            pos = max(min_ext, min(total - min_ext, pos))
            paned._in_ratio_update = True
            paned.set_position(pos)
            paned._in_ratio_update = False
            paned._ratio_settled = True
            return False  # for GLib.idle_add

        def on_map(paned, *_args) -> None:
            GLib.idle_add(_apply_ratio, paned)

        def on_dimension_changed(paned, _param) -> None:
            if not paned._in_ratio_update:
                _apply_ratio(paned)

        def on_position_notify(paned, _param) -> None:
            # Ignore programmatic moves and GTK's pre-settlement default
            # positions; only record the ratio from genuine user drags.
            if paned._in_ratio_update or not paned._ratio_settled:
                return
            total = paned.get_allocated_height() if is_vertical else paned.get_allocated_width()
            if total > 0:
                paned._split_ratio = paned.get_position() / total

        p.connect("map", on_map)
        dim_signal = "notify::height" if is_vertical else "notify::width"
        p.connect(dim_signal, on_dimension_changed)
        p.connect("notify::position", on_position_notify)

        # Allow callers (e.g. _rebuild_layout) to force the ratio back onto the
        # divider once the paned has a real allocation, without relying on the
        # map / notify::width signals having fired.
        p._force_ratio = lambda _p=p: _apply_ratio(_p)

        return p

    # ── layout rebuild ────────────────────────────────────────────────────────

    def _rebuild_layout(self) -> None:
        """Detach all panes and rebuild a fully resizable pane tree."""
        # Any change to the grid (adding, closing or moving a pane) ends pane
        # full screen first, so the zoomed pane is never pulled out from
        # under its container.
        if self._fullscreen_pane is not None:
            self._end_pane_fullscreen()
        self._scroll_spacer = None

        # Preserve the horizontal split ratio of each pane pair across the
        # rebuild (the width analogue of old_heights below). The old paned
        # objects still carry their _split_ratio even after teardown.
        old_h_ratios = {
            i: getattr(p, "_split_ratio", 0.5)
            for i, p in enumerate(self._h_paneds)
        }
        self._h_paneds = []

        def _release_paned(widget: Gtk.Widget) -> None:
            """Recursively null Paned children so panes can be safely re-parented.

            Also traverses Gtk.Box children so that h_paneds nested inside row_boxes
            (used by the HORIZONTAL scrollable layout) are cleaned up correctly.
            `set_start/end_child(None)` is used instead of unparent() because
            unparent() on a Paned's end_child can silently fail in GTK4.
            """
            if isinstance(widget, Gtk.Paned):
                start = widget.get_start_child()
                end = widget.get_end_child()
                if start is not None:
                    _release_paned(start)
                    try:
                        widget.set_start_child(None)
                    except Exception:
                        pass
                if end is not None:
                    _release_paned(end)
                    try:
                        widget.set_end_child(None)
                    except Exception:
                        pass
            elif isinstance(widget, Gtk.Box):
                child = widget.get_first_child()
                while child is not None:
                    nxt = child.get_next_sibling()
                    _release_paned(child)
                    child = nxt

        # Release all Paned children via set_start/end_child(None) before
        # removing the Paned wrappers.  Using widget.unparent() on a Paned's
        # end_child can silently fail in GTK4, leaving the pane stranded inside
        # a detached Paned and preventing correct re-parenting on rebuild.
        child = self._content_area.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            _release_paned(child)
            try:
                self._content_area.remove(child)
            except Exception:
                pass
            child = nxt

        # Catch any panes still attached as direct Box children (single-pane
        # or vertical mode layouts where panes are appended directly).
        for pane in self._panes:
            try:
                if pane.get_parent() is not None:
                    pane.unparent()
            except Exception:
                pass

        # Row heights fill the scroll viewport at startup; the spacer below
        # adds extra scroll room once rows are resized taller.
        self._content_area.set_vexpand(False)

        n = len(self._panes)
        if n == 0:
            return

        if self._layout_mode == self.VERTICAL:
            # VERTICAL: each pane is its own row, stacked with resize handles.
            old_heights = list(self._row_heights)
            self._row_heights = []
            self._row_boxes = []
            for row_idx, pane in enumerate(self._panes):
                h = self._row_height_for_rebuild(old_heights, row_idx)
                self._row_heights.append(h)
                row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                row_box.set_hexpand(True)
                row_box.set_vexpand(False)
                row_box.set_size_request(-1, h)
                row_box.append(pane)
                self._row_boxes.append(row_box)
                self._content_area.append(row_box)
                handle = RowResizeHandle(lambda idx=row_idx: idx, self)
                self._content_area.append(handle)
            self._append_scroll_spacer()
        else:
            # HORIZONTAL: pane pairs become rows placed directly in the Box.
            # Custom RowResizeHandle widgets between rows allow each row to be
            # resized independently and grow beyond the viewport (enabling scroll).
            row_widgets: List[Gtk.Widget] = []
            for i in range(0, n, 2):
                pair = self._panes[i:i + 2]
                if len(pair) == 1:
                    row_widgets.append(pair[0])
                else:
                    pair_idx = i // 2
                    h_paned = self._make_proportional_paned(
                        Gtk.Orientation.HORIZONTAL,
                        ratio=old_h_ratios.get(pair_idx, 0.5),
                    )
                    h_paned.set_start_child(pair[0])
                    h_paned.set_end_child(pair[1])
                    row_widgets.append(h_paned)
                    self._h_paneds.append(h_paned)

            old_heights = list(self._row_heights)
            self._row_heights = []
            self._row_boxes = []
            for row_idx, row_widget in enumerate(row_widgets):
                h = self._row_height_for_rebuild(old_heights, row_idx)
                self._row_heights.append(h)
                row_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
                row_box.set_hexpand(True)
                row_box.set_vexpand(False)
                row_box.set_size_request(-1, h)
                row_box.append(row_widget)
                self._row_boxes.append(row_box)
                self._content_area.append(row_box)
                # Handle after every row (including the last).
                handle = RowResizeHandle(lambda idx=row_idx: idx, self)
                self._content_area.append(handle)

            self._append_scroll_spacer()

        num_rows = len(self._row_boxes)
        self._manual_row_indices = {
            i for i in self._manual_row_indices if i < num_rows
        }
        self._row_height_ratios = self._row_height_ratios[:num_rows]

        self._normalize_pane_heights()
        self._schedule_viewport_sync()
        # Force each horizontal divider back onto its ratio once the rebuilt
        # paneds have a real allocation. Without this a recreated paned whose
        # width did not change (e.g. the top row when a 3rd pane is added)
        # never re-applies its ratio via map/notify and falls back to GTK's
        # natural-size split, making the two panes unequal.
        GLib.idle_add(self._apply_horizontal_ratios)

    def _apply_horizontal_ratios(self) -> bool:
        """Re-assert each horizontal paned's split ratio after a rebuild.

        Returns True (so GLib.idle_add re-runs it) while any paned still has no
        allocation yet; False once every divider has been positioned.
        """
        pending = False
        for paned in self._h_paneds:
            if paned.get_allocated_width() > 0:
                paned._force_ratio()
            else:
                pending = True
        return pending

    def _chain_panes(
        self,
        widgets: List[Gtk.Widget],
        orientation: Gtk.Orientation,
    ) -> Gtk.Widget:
        """Build a nested proportional-Paned chain so every boundary is draggable."""
        if not widgets:
            return Gtk.Box()
        if len(widgets) == 1:
            return widgets[0]

        root = self._make_proportional_paned(orientation)
        root.set_start_child(widgets[0])
        root.set_end_child(self._chain_panes(widgets[1:], orientation))
        return root

    def _normalize_pane_heights(self) -> None:
        """Set per-pane minimum height to match its row (manual rows may be < 50%)."""
        default_min = self._get_default_row_height()
        for pane in self._panes:
            row_min = default_min
            for i, row_box in enumerate(self._row_boxes):
                if self._widget_in_row_box(pane, row_box):
                    if i < len(self._row_heights):
                        row_min = self._row_heights[i]
                    break
            try:
                pane.set_vexpand(True)
                pane.set_size_request(-1, row_min)
            except Exception:
                pass

    def _widget_in_row_box(self, widget: Gtk.Widget, row_box: Gtk.Box) -> bool:
        while widget is not None:
            if widget is row_box:
                return True
            widget = widget.get_parent()
        return False

    def _row_height_for_rebuild(self, old_heights: List[int], row_idx: int) -> int:
        """Return the row height to use during layout rebuild."""
        if row_idx in self._manual_row_indices and row_idx < len(old_heights):
            return old_heights[row_idx]
        if self._fill_viewport:
            return self._get_default_row_height()
        if row_idx < len(old_heights):
            return old_heights[row_idx]
        return self._get_default_row_height()

    def _get_scroll_viewport_height(self) -> int:
        try:
            for widget in (self._pane_scroll, self._scroll_overlay, self):
                height = widget.get_height()
                if height <= 0:
                    height = widget.get_allocated_height()
                if height > 0:
                    if widget is self and self._add_strip is not None:
                        strip_h = self._add_strip.get_height()
                        if strip_h <= 0:
                            strip_h = self._add_strip.get_allocated_height()
                        height = max(0, height - strip_h)
                    return max(0, int(height))
        except Exception:
            pass
        return 0

    def _min_extent_for_total(self, total: int) -> int:
        """Size along an axis as a fraction of the available total."""
        if total <= 0:
            return 1
        return max(1, int(total * self.MIN_PANE_HEIGHT_RATIO))

    def _get_default_row_height(self) -> int:
        """Default row height (50% of scroll viewport) for non-manual rows."""
        return self._min_extent_for_total(self._get_scroll_viewport_height())

    def _minimum_rows_content_height(self, num_rows: int) -> int:
        if num_rows <= 0:
            return 0
        total = 0
        default_h = self._get_default_row_height()
        for i in range(num_rows):
            if i in self._manual_row_indices and i < len(self._row_heights):
                total += self._row_heights[i]
            else:
                total += default_h
        return total + num_rows * self.ROW_HANDLE_HEIGHT

    def _save_scroll_position(self) -> float:
        try:
            return self._pane_scroll.get_vadjustment().get_value()
        except Exception:
            return 0.0

    def _restore_scroll_position(self, scroll_y: float) -> bool:
        try:
            adj = self._pane_scroll.get_vadjustment()
            upper = adj.get_upper()
            page = adj.get_page_size()
            max_y = max(adj.get_lower(), upper - page)
            adj.set_value(max(adj.get_lower(), min(scroll_y, max_y)))
        except Exception:
            pass
        return False

    def _mark_row_manual(self, row_idx: int) -> None:
        """Remember a user-chosen row height (may be below the 50% default)."""
        if row_idx < 0 or row_idx >= len(self._row_heights):
            return
        viewport = self._get_scroll_viewport_height()
        self._manual_row_indices.add(row_idx)
        while len(self._row_height_ratios) <= row_idx:
            self._row_height_ratios.append(self.MIN_PANE_HEIGHT_RATIO)
        if viewport > 0:
            self._row_height_ratios[row_idx] = (
                self._row_heights[row_idx] / viewport
            )
        self._fill_viewport = False

    def _schedule_viewport_sync(self, *_args) -> None:
        if self._viewport_sync_scheduled:
            return
        self._viewport_sync_scheduled = True
        GLib.idle_add(self._run_viewport_sync)

    def _run_viewport_sync(self) -> bool:
        self._viewport_sync_scheduled = False
        viewport = self._get_scroll_viewport_height()
        if viewport <= 0:
            GLib.idle_add(self._run_viewport_sync)
            return False
        before = viewport
        self._sync_row_heights_to_viewport()
        after = self._get_scroll_viewport_height()
        if after != before:
            self._schedule_viewport_sync()
        return False

    def _compute_fill_row_height(self, num_rows: int) -> Optional[int]:
        """Height for each auto row so rows fill the scroll viewport."""
        if num_rows <= 0:
            return None
        viewport = self._get_scroll_viewport_height()
        if viewport <= 0:
            return None
        manual_total = sum(
            self._row_heights[i]
            for i in self._manual_row_indices
            if i < len(self._row_heights)
        )
        auto_rows = num_rows - len(self._manual_row_indices)
        if auto_rows <= 0:
            return None
        available = (
            viewport - manual_total - (num_rows * self.ROW_HANDLE_HEIGHT)
        )
        if available <= 0:
            return self._get_default_row_height()
        return max(self._get_default_row_height(), available // auto_rows)

    def _compute_target_row_heights(self) -> List[int]:
        num_rows = len(self._row_boxes)
        if num_rows <= 0:
            return []

        viewport = self._get_scroll_viewport_height()
        default_h = self._get_default_row_height()
        exceeds = self._minimum_rows_content_height(num_rows) > viewport

        if exceeds:
            self._fill_viewport = False
        elif not self._manual_row_indices:
            # Content fits and nothing is manually sized → restore auto-fill so a
            # layout that can fill the viewport (e.g. two side-by-side panes after
            # a VERTICAL→HORIZONTAL switch) does so by default. Without this the
            # flag is sticky-off after any over-tall (exceeding) layout.
            self._fill_viewport = True

        auto_fill_h: Optional[int] = None
        if self._fill_viewport and not exceeds:
            auto_fill_h = self._compute_fill_row_height(num_rows)

        heights: List[int] = []
        for i in range(num_rows):
            if i in self._manual_row_indices:
                ratio = (self._row_height_ratios[i]
                         if i < len(self._row_height_ratios)
                         else self.MIN_PANE_HEIGHT_RATIO)
                h = max(int(ratio * viewport), self.ABSOLUTE_MIN_ROW_HEIGHT)
            elif auto_fill_h is not None:
                h = auto_fill_h
            elif exceeds:
                h = default_h
            else:
                h = default_h
            heights.append(h)
        return heights

    def _sync_row_heights_to_viewport(self, *_args) -> bool:
        """Recompute row heights; manual rows keep user ratios, others use 50%/fill."""
        if not self._row_boxes:
            return False

        viewport = self._get_scroll_viewport_height()
        if viewport <= 0:
            return False

        scroll_y = self._save_scroll_position()
        heights = self._compute_target_row_heights()
        if not heights:
            return False

        exceeds = self._minimum_rows_content_height(len(self._row_boxes)) > viewport
        self._row_heights = heights
        for i, row_box in enumerate(self._row_boxes):
            row_box.set_size_request(-1, heights[i])
        self._normalize_pane_heights()

        if exceeds:
            return self._restore_scroll_position(scroll_y)
        return False

    def _flush_row_resize(self) -> bool:
        """Apply accumulated row-height changes in one GTK layout pass."""
        scroll_y = self._save_scroll_position()
        self._drag_ghost.set_visible(False)
        for i, row_box in enumerate(self._row_boxes):
            row_box.set_size_request(-1, self._row_heights[i])
        self._normalize_pane_heights()
        GLib.idle_add(self._restore_scroll_position, scroll_y)
        return False

    def _update_drag_ghost(self, row_idx: int) -> None:
        """Move the ghost line to the projected bottom edge of row_idx."""
        if row_idx >= len(self._row_boxes):
            return
        row_box = self._row_boxes[row_idx]
        alloc = row_box.get_allocation()
        scroll_y = self._pane_scroll.get_vadjustment().get_value()
        ghost_y = alloc.y + self._row_heights[row_idx] - scroll_y
        self._drag_ghost.set_margin_top(max(0, int(ghost_y)))
        self._drag_ghost.set_visible(True)

    # ── pre-population ────────────────────────────────────────────────────────

    def populate(self, connections: list) -> None:
        """
        Fill panes from a list of Connection objects.

        The two initial empty panes absorb the first two connections;
        extra connections each get a new pane.
        """
        if not connections:
            return
        for i, conn in enumerate(connections):
            if i < len(self._panes):
                self._panes[i].add_connection(conn)
            else:
                self.add_pane().add_connection(conn)

    # ── terminal lifecycle ────────────────────────────────────────────────────

    def _cleanup_terminal(self, terminal) -> None:
        """Disconnect terminal and remove it from window tracking dicts."""
        # Shared with the file-manager terminal panel: disposes an attached
        # files panel, disconnects, and cleans the tracking dicts.
        self.window.cleanup_embedded_terminal(terminal)

    def cleanup_all(self) -> None:
        """Disconnect all embedded terminals (called when the tab is being closed)."""
        for pane in list(self._panes):
            for terminal in pane.get_terminals():
                if terminal in self.window.terminal_to_connection:
                    self._cleanup_terminal(terminal)

    # ── tab title ────────────────────────────────────────────────────────────

    def _update_tab_title(self) -> None:
        if self._tab_page is None:
            return
        n = sum(p.get_terminal_count() for p in self._panes)
        if n > 0:
            self._tab_page.set_title(
                ngettext(
                    "Split View ({n} terminal)",
                    "Split View ({n} terminals)",
                    n,
                ).format(n=n)
            )
        else:
            self._tab_page.set_title(_("Split View"))

    def get_all_terminals(self) -> list:
        """Return every TerminalWidget embedded in this split tab."""
        result = []
        for pane in self._panes:
            result.extend(pane.get_terminals())
        return result

    def get_focused_terminal(self):
        """Return the terminal in the focused pane's selected inner tab, if any."""
        pane = self._get_focused_pane()
        if pane is None:
            pane = self._last_active_pane
        if pane is None:
            for candidate in self._panes:
                if candidate.get_terminal_count() > 0:
                    pane = candidate
                    break
        if pane is None:
            return None

        page = pane._inner_tab_view.get_selected_page()
        if page is None:
            terminals = pane.get_terminals()
            return terminals[0] if terminals else None
        return page.get_child()

    # ── keyboard navigation ───────────────────────────────────────────────────

    _RESIZE_STEP = 50  # pixels per resize-pane keypress

    def _on_key_pressed(self, _ctrl, keyval, keycode, state) -> bool:
        # Guard: only act when the focused widget is inside THIS SplitViewTab.
        # Adw.TabView keeps all tab-page children realized, so GTK4 may invoke
        # our CAPTURE handler even when a sibling widget (e.g. the connection
        # list) has focus. Walk the parent chain to confirm.
        root = self.get_root()
        focused = root.get_focus() if root else None
        w = focused
        while w is not None:
            if w is self:
                break
            w = w.get_parent()
        else:
            return False

        mods = state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | Gdk.ModifierType.SHIFT_MASK
            | Gdk.ModifierType.META_MASK
        )

        # Cheap early-out: every customizable split shortcut uses Control or Alt,
        # so plain typing skips the trigger matching below entirely.
        if not (mods & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK)):
            return False

        # Config-driven shortcuts: match the live key event against each action's
        # effective accelerators using GTK's own trigger machinery (the same one
        # ShortcutController uses), so Shift/symbol/case normalization — and any
        # user rebinding — Just Work without manual keyval tables. These actions
        # are disabled by default (no accelerator); users assign their own in the
        # shortcut editor (see main.py register_custom_shortcut).
        event = _ctrl.get_current_event()
        app = self.window.get_application() if self.window is not None else None
        if app is not None and not getattr(app, 'accelerators_enabled', True):
            # Terminal Shortcut Pass-through is active: this CAPTURE-phase
            # handler must not claim any key either, or it would defeat the
            # "disable all keyboard shortcuts" contract for split-view users
            # who have assigned one of these actions.
            return False
        if event is not None and app is not None and hasattr(app, 'get_effective_shortcuts'):
            assigned = [
                (accel, callback)
                for name, callback in self._split_actions.items()
                for accel in (app.get_effective_shortcuts(name) or [])
            ]
            for accel, callback in assigned:
                trigger = Gtk.ShortcutTrigger.parse_string(accel)
                if trigger is not None and trigger.trigger(event, False) == Gdk.KeyMatch.EXACT:
                    callback()
                    return True

            # Nothing matched, which under a non-Latin layout may only mean GTK
            # could not see past the active group (GH #1249). Ask the keymap
            # what this physical key is in the layout's Latin group.
            candidates = latin_fallback_keyvals(
                self.get_display(), keyval, keycode)
            if not candidates:
                return False
            for accel, callback in assigned:
                if accel_matches_latin_fallback(event, accel, candidates, state):
                    callback()
                    return True

        return False

    def _get_focused_pane(self) -> Optional[SplitPane]:
        root = self.get_root()
        focused = root.get_focus() if root else None
        widget = focused
        while widget is not None:
            if isinstance(widget, SplitPane):
                return widget
            widget = widget.get_parent()
        return None

    def close_focused_pane(self) -> None:
        """Close the focused pane (used by the context-aware Close Tab action).
        Falls back to the last-active / first pane. Closing the last pane closes
        the whole split-view tab via remove_pane()."""
        pane = (self._get_focused_pane()
                or self._last_active_pane
                or (self._panes[0] if self._panes else None))
        if pane is not None:
            pane.close_pane()

    def _pane_grid_pos(self, idx: int) -> tuple:
        if self._layout_mode == self.VERTICAL:
            return (idx, 0)
        return (idx // 2, idx % 2)

    def _navigate_pane(self, direction: str) -> None:
        if self._fullscreen_pane is not None:
            return  # the other panes are not on screen
        current = self._get_focused_pane()
        if current is None:
            if self._panes:
                self._focus_pane(self._panes[0])
            return
        if current not in self._panes:
            return

        idx = self._panes.index(current)
        row, col = self._pane_grid_pos(idx)
        pos_map = {self._pane_grid_pos(i): p for i, p in enumerate(self._panes)}

        dr, dc = {'up': (-1, 0), 'down': (1, 0),
                  'left': (0, -1), 'right': (0, 1)}[direction]
        target = pos_map.get((row + dr, col + dc))
        if target:
            self._focus_pane(target)

    def _focus_pane(self, pane: SplitPane) -> None:
        self._note_active_pane(pane)
        page = pane._inner_tab_view.get_selected_page()
        if page is None:
            pane.grab_focus()
            return
        child = page.get_child()
        if hasattr(child, 'backend') and child.backend:
            child.backend.grab_focus()
        else:
            child.grab_focus()

    def _resize_active_pane(self, direction: str) -> None:
        pane = self._get_focused_pane()
        if pane is None:
            return

        # In HORIZONTAL layout, vertical row resizing is done via _row_boxes /
        # _row_heights (no Paned involved), so handle it separately.
        if direction in ('up', 'down') and self._layout_mode == self.HORIZONTAL:
            scroll_y = self._save_scroll_position()
            for idx, row_box in enumerate(self._row_boxes):
                widget: Optional[Gtk.Widget] = pane
                while widget is not None:
                    if widget is row_box:
                        delta = self._RESIZE_STEP if direction == 'down' else -self._RESIZE_STEP
                        new_h = max(
                            self.ABSOLUTE_MIN_ROW_HEIGHT,
                            self._row_heights[idx] + delta,
                        )
                        self._row_heights[idx] = new_h
                        row_box.set_size_request(-1, new_h)
                        self._mark_row_manual(idx)
                        self._normalize_pane_heights()
                        GLib.idle_add(self._restore_scroll_position, scroll_y)
                        return
                    widget = widget.get_parent()
            return

        need_h = direction in ('left', 'right')
        need_orient = (Gtk.Orientation.HORIZONTAL if need_h
                       else Gtk.Orientation.VERTICAL)

        widget = pane
        paned = None
        is_start = True
        while widget is not None:
            parent = widget.get_parent()
            if (isinstance(parent, Gtk.Paned)
                    and parent.get_orientation() == need_orient):
                paned = parent
                is_start = (parent.get_start_child() is widget)
                break
            widget = parent

        if paned is None:
            return

        if direction in ('right', 'down'):
            delta = self._RESIZE_STEP if is_start else -self._RESIZE_STEP
        else:
            delta = -self._RESIZE_STEP if is_start else self._RESIZE_STEP

        is_h_paned = paned.get_orientation() == Gtk.Orientation.HORIZONTAL
        total = (paned.get_allocated_width() if is_h_paned
                 else paned.get_allocated_height())
        min_ext = (self.MIN_PANE_WIDTH if is_h_paned
                   else self._min_extent_for_total(total))
        new_pos = max(min_ext, min(total - min_ext, paned.get_position() + delta))
        paned.set_position(new_pos)
