"""Split terminal view — SplitPane and SplitViewTab widgets."""
from __future__ import annotations

import logging
from typing import List, Optional

from gi.repository import Gtk, Gdk, GObject, GLib, Adw
from gettext import gettext as _

logger = logging.getLogger(__name__)

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
        self.set_size_request(-1, 200)

        # ── inner tab view (mini Adw.TabBar + Adw.TabView) ───────────────
        self._inner_tab_view = Adw.TabView()
        self._inner_tab_view.set_hexpand(True)
        self._inner_tab_view.set_vexpand(True)
        self._inner_tab_view.connect("close-page", self._on_inner_close)

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
        remaining = self.get_terminal_count()
        if remaining == 0:
            self.close_pane()
        elif remaining <= 1:
            # Defensive fallback if a single page remains but _has_terminals
            # was not in sync for any reason.
            self._restore_placeholder()
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
    - An "Add Terminal" pill button strip below the panes.

    The H/V layout toggle lives in a global autohiding overlay managed by
    the main window (window.py), not inside this widget.
    """

    __gtype_name__ = "SshPilotSplitViewTab"

    HORIZONTAL = 'horizontal'
    VERTICAL = 'vertical'
    DEFAULT_PANE_HEIGHT = 200

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self._panes: List[SplitPane] = []
        self._layout_mode = self.HORIZONTAL
        self._tab_page = None
        self.set_hexpand(True)
        self.set_vexpand(True)

        # Content area holds the Paned tree.  vexpand=True is required so the
        # ScrolledWindow stretches the tree to fill the viewport when panes are
        # few enough to fit; the scrollbar appears only when the aggregate
        # natural height (n_rows × DEFAULT_PANE_HEIGHT) exceeds the viewport.
        self._content_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._content_area.set_hexpand(True)
        self._content_area.set_vexpand(True)

        self._pane_scroll = Gtk.ScrolledWindow()
        self._pane_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._pane_scroll.set_hexpand(True)
        self._pane_scroll.set_vexpand(True)
        self._pane_scroll.set_child(self._content_area)
        self.append(self._pane_scroll)

        # "Add Terminal" strip below the panes
        self._add_strip = self._build_add_pane_strip()
        self.append(self._add_strip)

        # Start with 2 empty panes (minimum requirement)
        self.add_pane()
        self.add_pane()

    # ── layout mode (public API for window.py overlay toggle) ────────────────

    def get_layout_mode(self) -> str:
        return self._layout_mode

    def set_layout_mode(self, mode: str) -> None:
        if mode not in (self.HORIZONTAL, self.VERTICAL):
            return
        if mode != self._layout_mode:
            self._layout_mode = mode
            self._rebuild_layout()

    # ── "Add Terminal" strip ─────────────────────────────────────────────────

    def _build_add_pane_strip(self) -> Gtk.Widget:
        # Strip spans the full width so the whole area acts as a drop target.
        # The pill button is centred within the strip via halign on the button.
        strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        strip.set_hexpand(True)
        strip.set_margin_top(8)
        strip.set_margin_bottom(8)

        btn = Gtk.Button(label=_("Add Terminal"))
        btn.add_css_class("suggested-action")
        btn.add_css_class("pill")
        btn.set_halign(Gtk.Align.CENTER)
        btn.set_hexpand(True)
        btn.connect("clicked", lambda _b: self.add_pane())
        strip.append(btn)

        # Drop target on the strip: create a new pane and add the connection
        dt = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
        dt.connect("drop", self._on_add_strip_drop)
        dt.connect("enter", lambda _t, _x, _y: Gdk.DragAction.MOVE)
        strip.add_controller(dt)

        return strip

    def _on_add_strip_drop(self, _target, value, _x, _y) -> bool:
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
            logger.error("add-strip drop failed: %s", exc)
            return False

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

    def get_pane_count(self) -> int:
        return len(self._panes)

    # ── layout rebuild ────────────────────────────────────────────────────────

    def _rebuild_layout(self) -> None:
        """Detach all panes and rebuild a fully resizable pane tree."""
        def _release_paned(widget: Gtk.Widget) -> None:
            """Recursively null Paned children so panes can be safely re-parented."""
            if not isinstance(widget, Gtk.Paned):
                return
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

        n = len(self._panes)
        if n == 0:
            return

        if self._layout_mode == self.VERTICAL:
            root = self._chain_panes(self._panes, Gtk.Orientation.VERTICAL)
            self._content_area.append(root)
        else:
            # HORIZONTAL: pane pairs become rows. Rows are chained vertically
            # with Gtk.Paned so row heights can be manually resized.
            row_widgets: List[Gtk.Widget] = []
            for i in range(0, n, 2):
                pair = self._panes[i:i + 2]
                if len(pair) == 1:
                    row_widgets.append(pair[0])
                else:
                    h_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
                    h_paned.set_hexpand(True)
                    h_paned.set_vexpand(True)
                    h_paned.set_wide_handle(True)
                    h_paned.set_resize_start_child(True)
                    h_paned.set_resize_end_child(True)
                    h_paned.set_shrink_start_child(False)
                    h_paned.set_shrink_end_child(False)
                    h_paned.set_start_child(pair[0])
                    h_paned.set_end_child(pair[1])
                    row_widgets.append(h_paned)
            root = self._chain_panes(row_widgets, Gtk.Orientation.VERTICAL)
            self._content_area.append(root)

        self._normalize_pane_heights()

    def _chain_panes(
        self,
        widgets: List[Gtk.Widget],
        orientation: Gtk.Orientation,
    ) -> Gtk.Widget:
        """Build a nested Gtk.Paned chain so every boundary is draggable."""
        if not widgets:
            return Gtk.Box()
        if len(widgets) == 1:
            return widgets[0]

        root = Gtk.Paned(orientation=orientation)
        root.set_hexpand(True)
        root.set_vexpand(True)
        root.set_wide_handle(True)
        root.set_resize_start_child(True)
        root.set_resize_end_child(True)
        # Critical for nested vertical paned chains: don't let either side
        # collapse to zero, otherwise lower panes disappear from view.
        root.set_shrink_start_child(False)
        root.set_shrink_end_child(False)
        root.set_start_child(widgets[0])
        root.set_end_child(self._chain_panes(widgets[1:], orientation))
        return root

    def _normalize_pane_heights(self) -> None:
        """Enforce minimum pane height while allowing growth to fill the viewport."""
        for pane in self._panes:
            try:
                pane.set_vexpand(True)
                pane.set_size_request(-1, self.DEFAULT_PANE_HEIGHT)
            except Exception:
                pass

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
