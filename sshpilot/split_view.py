"""Split terminal view — SplitPane and SplitViewTab widgets."""
from __future__ import annotations

import logging
from typing import List, Optional

from gi.repository import Gtk, Gdk, GObject, GLib, Adw
from gettext import gettext as _

logger = logging.getLogger(__name__)

# Installed once per process; scoped to box.add-pane-strip so it only
# affects the strip widget and nothing else in the app.
_drop_zone_css_installed = False

def _ensure_drop_zone_css() -> None:
    global _drop_zone_css_installed
    if _drop_zone_css_installed:
        return
    provider = Gtk.CssProvider()
    # Targets plain box.add-pane-strip so there are no Adwaita sub-node
    # overrides to fight.  Two background-color declarations: the rgba()
    # fallback fires on systems where @accent_bg_color is unavailable.
    provider.load_from_data(b"""
box.toolbar.add-pane-strip {
    border-top: 2px solid alpha(@window_fg_color, 0.25);
    padding: 8px 12px;
}
box.add-pane-strip.drag-over {
    background-color: rgba(42, 161, 152, 0.25);
}
box.add-pane-scroll-spacer.drag-over {
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
    """Create H/V layout toggle buttons (same icons/tooltips as the header bar)."""
    updating = [False]

    h_btn = Gtk.ToggleButton()
    h_btn.set_icon_name("double-ended-arrows-horizontal-symbolic")
    h_btn.set_tooltip_text(_("Side by Side"))
    h_btn.add_css_class("pill" if as_pill else "flat")

    v_btn = Gtk.ToggleButton()
    v_btn.set_icon_name("double-ended-arrows-vertical-symbolic")
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

/* Pane borders: margin creates a gap; non-inset shadow fills it visibly.
   @window_fg_color is black in light mode, white in dark mode.
   margin > spread ensures corners are never clipped by the parent. */
box.split-pane {
    margin: 1px;
    box-shadow: 0 0 0 2px alpha(@window_fg_color, 0.7);
}
box.split-pane.split-pane-active {
    box-shadow: 0 0 0 2px #f5c400;
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

    Each pane contains its own mini Adw.TabBar + Adw.TabView so multiple
    terminals can be stacked as sub-tabs within one pane.  When empty, a
    placeholder is shown with a "Pick existing tab" pill button.
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
        self._inner_tab_view.connect('close-page', self._on_inner_close)
        self._inner_tab_view.connect('notify::selected-page', self._on_inner_tab_selected)

        self._inner_tab_bar = Adw.TabBar()
        self._inner_tab_bar.set_view(self._inner_tab_view)
        self._inner_tab_bar.set_autohide(False)

        # When the pane is populated the per-tab × buttons (Adwaita default,
        # visible on hover/selected) are the close mechanism.  No extra
        # "Close Pane" end-action button is added here; the placeholder has
        # its own close button for when the pane is empty.

        self._tab_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._tab_container.set_hexpand(True)
        self._tab_container.set_vexpand(True)
        self._tab_container.append(self._inner_tab_bar)
        self._tab_container.append(self._inner_tab_view)

        # ── placeholder ──────────────────────────────────────────────────
        self._placeholder = self._build_placeholder()
        self.append(self._placeholder)

        # ── inner tab bar: double-click to rename ────────────────────────
        rename_gesture = Gtk.GestureClick()
        rename_gesture.set_button(1)
        rename_gesture.connect("pressed", self._on_inner_tab_bar_pressed)
        self._inner_tab_bar.add_controller(rename_gesture)

        # ── drop target ──────────────────────────────────────────────────
        self._setup_drop_target()

        # Register with parent tab
        split_view_tab.register_pane(self)

    def _on_pane_focus_enter(self, _controller) -> None:
        self.add_css_class('split-pane-active')
        self._split_view_tab._note_active_pane(self)

    def _on_inner_tab_selected(self, *_args) -> None:
        self._split_view_tab._note_active_pane(self)

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

        pick_btn = Gtk.Button(label=_("Pick existing tab"))
        pick_btn.add_css_class("suggested-action")
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
        """Embed a TerminalWidget as a sub-tab in this pane."""
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

    def add_connection(self, connection) -> None:
        """Create a new terminal for connection and add it to this pane."""
        terminal = self._window.terminal_manager.create_terminal_for_pane(connection)
        self.add_terminal(terminal, getattr(connection, 'nickname', None))

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
        dt = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
        dt.connect("drop", self._on_drop)
        dt.connect("enter", lambda _t, _x, _y: Gdk.DragAction.MOVE)
        self.add_controller(dt)

    def _on_drop(self, _target, value, _x: float, _y: float) -> bool:
        try:
            if hasattr(value, 'get_value'):
                value = value.get_value()
            if not isinstance(value, dict) or value.get("type") != "connection":
                return False

            nicknames = value.get("connection_nicknames") or []
            if not nicknames and value.get("connection_nickname"):
                nicknames = [value["connection_nickname"]]
            if not nicknames:
                return False

            for nick in nicknames:
                conn = self._window.connection_manager.find_connection_by_nickname(nick)
                if conn is not None:
                    self.add_connection(conn)
            return True
        except Exception as exc:
            logger.error("SplitPane drop failed: %s", exc)
            return False

    # ── inner tab rename ─────────────────────────────────────────────────────

    def _on_inner_tab_bar_pressed(self, gesture, n_press, x, y) -> None:
        if n_press != 2:
            return
        page = self._inner_tab_view.get_selected_page()
        if page is not None:
            self._show_inner_tab_rename_popover(page, x, y)

    def _show_inner_tab_rename_popover(self, page, x: float, y: float) -> None:
        entry = Gtk.Entry()
        entry.set_text(page.get_title())
        entry.set_width_chars(20)

        popover = Gtk.Popover()
        popover.set_child(entry)
        popover.set_parent(self._inner_tab_bar)
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
    - A toolbar strip below the panes with layout, scroll, and add controls.
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
        self._fill_viewport = True
        self._viewport_sync_scheduled = False
        self._pending_scroll_pane: Optional[SplitPane] = None
        self._scroll_spacer: Optional[Gtk.Box] = None
        self._last_active_pane: Optional[SplitPane] = None
        self.set_hexpand(True)
        self.set_vexpand(True)

        self._content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._content_area.set_hexpand(True)
        self._content_area.set_vexpand(False)

        self._pane_scroll = Gtk.ScrolledWindow()
        self._pane_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

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

        for widget in (self, self._scroll_overlay, self._pane_scroll):
            widget.connect('notify::height', self._schedule_viewport_sync)
        self.connect('map', self._schedule_viewport_sync)
        try:
            window.connect('notify::default-height', self._schedule_viewport_sync)
            window.connect('notify::height', self._schedule_viewport_sync)
        except Exception:
            pass

        # Toolbar strip below the panes
        self._add_pane_btn: Optional[Gtk.Button] = None
        self._add_pane_strip: Optional[Gtk.Box] = None
        self._layout_h_btn: Optional[Gtk.ToggleButton] = None
        self._layout_v_btn: Optional[Gtk.ToggleButton] = None
        self._layout_toggle_updating: list = [False]
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

    def _scroll_to_pane(self, pane: SplitPane) -> bool:
        """Scroll the viewport so pane's row is visible (called after layout)."""
        if pane not in self._panes:
            return False

        row_idx: Optional[int] = None
        for i, row_box in enumerate(self._row_boxes):
            if self._widget_in_row_box(pane, row_box):
                row_idx = i
                break
        if row_idx is None:
            return False

        row_box = self._row_boxes[row_idx]
        alloc = row_box.get_allocation()
        if alloc.height <= 0:
            GLib.idle_add(self._scroll_to_pane, pane)
            return False

        try:
            adj = self._pane_scroll.get_vadjustment()
            row_y = alloc.y
            row_h = self._row_heights[row_idx] if row_idx < len(self._row_heights) else alloc.height
            page = adj.get_page_size()
            current = adj.get_value()
            # Row fully visible — nothing to do.
            if row_y >= current and row_y + row_h <= current + page:
                return False
            # Prefer aligning the row top with the viewport top; clamp to range.
            target = max(adj.get_lower(), row_y)
            max_y = max(adj.get_lower(), adj.get_upper() - page)
            adj.set_value(min(target, max_y))
        except Exception:
            pass
        return False

    # ── toolbar strip ────────────────────────────────────────────────────────

    def _build_add_pane_strip(self) -> Gtk.Widget:
        _ensure_drop_zone_css()

        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        strip.add_css_class("toolbar")
        strip.add_css_class("add-pane-strip")
        strip.set_hexpand(True)

        self._layout_h_btn, self._layout_v_btn, self._layout_toggle_updating = (
            create_layout_toggle_buttons(
                lambda: self.set_layout_mode(self.HORIZONTAL),
                lambda: self.set_layout_mode(self.VERTICAL),
                as_pill=True,
            )
        )
        strip.append(self._layout_h_btn)
        strip.append(self._layout_v_btn)

        from sshpilot import icon_utils  # noqa: PLC0415

        scroll_top_btn = Gtk.Button()
        icon_utils.set_button_icon(scroll_top_btn, 'top-large-symbolic')
        scroll_top_btn.set_tooltip_text(_("Scroll to top"))
        scroll_top_btn.add_css_class("pill")
        scroll_top_btn.connect("clicked", lambda _b: self.scroll_panes_to_top())
        strip.append(scroll_top_btn)

        scroll_bottom_btn = Gtk.Button()
        icon_utils.set_button_icon(scroll_bottom_btn, 'bottom-large-symbolic')
        scroll_bottom_btn.set_tooltip_text(_("Scroll to bottom"))
        scroll_bottom_btn.add_css_class("pill")
        scroll_bottom_btn.connect("clicked", lambda _b: self.scroll_panes_to_bottom())
        strip.append(scroll_bottom_btn)

        add_btn = Gtk.Button(label=_("Add Terminal"))
        add_btn.add_css_class("suggested-action")
        add_btn.add_css_class("pill")
        add_btn.set_halign(Gtk.Align.END)
        add_btn.set_hexpand(True)
        add_btn.connect("clicked", lambda _b: self.add_pane())
        strip.append(add_btn)

        self._add_pane_btn = add_btn
        self._add_pane_strip = strip
        self._sync_layout_toggle_buttons()

        dt = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
        dt.connect("enter", lambda _t, _x, _y: Gdk.DragAction.MOVE)
        dt.connect("drop", self._on_add_pane_drop)
        strip.add_controller(dt)

        return strip

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
            if hasattr(value, 'get_value'):
                value = value.get_value()
            if not isinstance(value, dict) or value.get("type") != "connection":
                return False
            nicknames = value.get("connection_nicknames") or []
            if not nicknames and value.get("connection_nickname"):
                nicknames = [value["connection_nickname"]]
            for nick in nicknames:
                conn = self.window.connection_manager.find_connection_by_nickname(nick)
                if conn is not None:
                    self.add_pane().add_connection(conn)
            return bool(nicknames)
        except Exception as exc:
            logger.error("add-pane drop failed: %s", exc)
            return False

    def _append_scroll_spacer(self) -> None:
        """Append the extra scroll area below rows; accepts connection drops."""
        _ensure_drop_zone_css()
        spacer = Gtk.Box()
        spacer.add_css_class("add-pane-scroll-spacer")
        spacer.set_hexpand(True)
        spacer.set_size_request(-1, self.SCROLL_SPACER_HEIGHT)

        dt = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
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
        self._pending_scroll_pane = pane
        self._rebuild_layout()
        return pane

    def remove_pane(self, pane: SplitPane) -> None:
        if pane in self._panes:
            self._panes.remove(pane)
        # _rebuild_layout uses _release_paned to safely detach panes from
        # their Paned containers; no separate unparent() needed here.
        self._rebuild_layout()
        self._update_tab_title()
        # Close the tab if no panes remain
        if not self._panes and self._tab_page is not None:
            self.window._suppress_close_confirmation = True
            try:
                self.window.tab_view.close_page(self._tab_page)
            finally:
                self.window._suppress_close_confirmation = False

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
            return False  # for GLib.idle_add

        def on_map(paned, *_args) -> None:
            GLib.idle_add(_apply_ratio, paned)

        def on_dimension_changed(paned, _param) -> None:
            if not paned._in_ratio_update:
                _apply_ratio(paned)

        def on_position_notify(paned, _param) -> None:
            if paned._in_ratio_update:
                return
            total = paned.get_allocated_height() if is_vertical else paned.get_allocated_width()
            if total > 0:
                paned._split_ratio = paned.get_position() / total

        p.connect("map", on_map)
        dim_signal = "notify::height" if is_vertical else "notify::width"
        p.connect(dim_signal, on_dimension_changed)
        p.connect("notify::position", on_position_notify)

        return p

    # ── layout rebuild ────────────────────────────────────────────────────────

    def _rebuild_layout(self) -> None:
        """Detach all panes and rebuild a fully resizable pane tree."""
        self._scroll_spacer = None

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
                    h_paned = self._make_proportional_paned(Gtk.Orientation.HORIZONTAL)
                    h_paned.set_start_child(pair[0])
                    h_paned.set_end_child(pair[1])
                    row_widgets.append(h_paned)

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

        pending = self._pending_scroll_pane
        if pending is not None:
            self._pending_scroll_pane = None
            GLib.idle_add(self._scroll_to_pane, pending)
            return False

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
        window = self.window
        try:
            terminal.disconnect()
        except Exception:
            pass
        try:
            connection = window.terminal_to_connection.get(terminal)
            if connection:
                terms = window.connection_to_terminals.get(connection, [])
                if terminal in terms:
                    terms.remove(terminal)
                    if not terms:
                        del window.connection_to_terminals[connection]
                if window.active_terminals.get(connection) is terminal:
                    remaining = window.connection_to_terminals.get(connection)
                    if remaining:
                        window.active_terminals[connection] = remaining[-1]
                    else:
                        window.active_terminals.pop(connection, None)
            window.terminal_to_connection.pop(terminal, None)
        except Exception as exc:
            logger.debug("Error cleaning up split-pane terminal dicts: %s", exc)

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
            self._tab_page.set_title(_("Split View ({n} terminals)").format(n=n))
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

    _RESIZE_STEP = 50  # pixels per Ctrl+Alt+Shift+HJKL keypress

    def _on_key_pressed(self, _ctrl, keyval, _keycode, state) -> bool:
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

        CTRL_ALT    = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.ALT_MASK
        CTRL_ALT_SH = CTRL_ALT | Gdk.ModifierType.SHIFT_MASK
        CTRL_SH     = Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK

        # Ctrl+Alt+H/J/K/L — focus navigation (vim-style, no GNOME/macOS conflicts)
        # Ctrl+Alt+Shift+H/J/K/L — resize
        HJKL_NAV = {
            Gdk.KEY_h: 'left',  Gdk.KEY_H: 'left',
            Gdk.KEY_j: 'down',  Gdk.KEY_J: 'down',
            Gdk.KEY_k: 'up',    Gdk.KEY_K: 'up',
            Gdk.KEY_l: 'right', Gdk.KEY_L: 'right',
        }

        if keyval in HJKL_NAV:
            d = HJKL_NAV[keyval]
            if mods == CTRL_ALT:
                self._navigate_pane(d)
                return True
            if mods == CTRL_ALT_SH:
                self._resize_active_pane(d)
                return True

        # Accept both the base key and its Shift-transformed variant since GTK4
        # reports the effective (shifted) keyval: \ → |, - → _
        if keyval in (Gdk.KEY_backslash, Gdk.KEY_bar) and mods == CTRL_SH:
            self.set_layout_mode(self.HORIZONTAL)
            return True
        if keyval in (Gdk.KEY_minus, Gdk.KEY_underscore) and mods == CTRL_SH:
            self.set_layout_mode(self.VERTICAL)
            return True
        if keyval in (Gdk.KEY_W, Gdk.KEY_w) and mods == CTRL_SH:
            pane = self._get_focused_pane()
            if pane:
                pane.close_pane()
            return True
        # Ctrl+Shift+N — add pane (Ctrl+Shift+T is taken by local-terminal action)
        if keyval in (Gdk.KEY_N, Gdk.KEY_n) and mods == CTRL_SH:
            self.add_pane()
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

    def _pane_grid_pos(self, idx: int) -> tuple:
        if self._layout_mode == self.VERTICAL:
            return (idx, 0)
        return (idx // 2, idx % 2)

    def _navigate_pane(self, direction: str) -> None:
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
