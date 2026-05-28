"""Split terminal view — SplitPane and SplitViewTab widgets."""
from __future__ import annotations

import logging
from typing import Optional, List

from gi.repository import Gtk, Gdk, GObject, GLib, Pango
from gettext import gettext as _

logger = logging.getLogger(__name__)


class SplitPane(Gtk.Overlay):
    """
    A single cell in the split grid.

    Contains either an empty placeholder or a live TerminalWidget.
    The pane tree is a binary tree of Gtk.Paned nodes; each leaf is a
    SplitPane.  When a pane is split it replaces itself in its parent
    with a Gtk.Paned whose two children are (self, new_pane).
    """

    __gtype_name__ = "SshPilotSplitPane"

    def __init__(self, split_view_tab: "SplitViewTab", terminal=None) -> None:
        super().__init__()
        self.split_view_tab = split_view_tab
        self._terminal = None
        self.set_hexpand(True)
        self.set_vexpand(True)

        # Build placeholder (shown when empty)
        self._placeholder = self._build_placeholder()
        self.set_child(self._placeholder)

        # Hover controls overlay
        self._hover_controls = self._build_hover_controls()
        self._hover_revealer = Gtk.Revealer()
        self._hover_revealer.set_transition_type(Gtk.RevealerTransitionType.CROSSFADE)
        self._hover_revealer.set_transition_duration(150)
        self._hover_revealer.set_child(self._hover_controls)
        self._hover_revealer.set_halign(Gtk.Align.END)
        self._hover_revealer.set_valign(Gtk.Align.START)
        self._hover_revealer.set_reveal_child(False)
        self.add_overlay(self._hover_revealer)

        # Motion controller for hover reveal
        motion = Gtk.EventControllerMotion()
        motion.connect("enter", self._on_motion_enter)
        motion.connect("leave", self._on_motion_leave)
        self.add_controller(motion)

        # Drop target for connection drags from sidebar
        self._setup_drop_target()

        # Register with the parent SplitViewTab
        split_view_tab.register_pane(self)

        if terminal is not None:
            self.set_terminal(terminal)

    # ── placeholder ───────────────────────────────────────────────────────

    def _build_placeholder(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        icon = Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        icon.set_pixel_size(48)
        icon.add_css_class("dim-label")
        box.append(icon)

        label = Gtk.Label(label=_("Drop a connection here"))
        label.add_css_class("dim-label")
        label.add_css_class("title-3")
        box.append(label)

        sub = Gtk.Label(label=_("or"))
        sub.add_css_class("dim-label")
        box.append(sub)

        pick_btn = Gtk.Button(label=_("Pick existing tab"))
        pick_btn.add_css_class("suggested-action")
        pick_btn.add_css_class("pill")
        pick_btn.set_halign(Gtk.Align.CENTER)
        pick_btn.connect("clicked", self._on_pick_existing_tab_clicked)
        self._pick_button = pick_btn
        box.append(pick_btn)

        self._update_pick_button_sensitivity_on_widget(pick_btn)
        return box

    def _update_pick_button_sensitivity_on_widget(self, btn: Gtk.Button) -> None:
        """Sensitise / desensitise the 'Pick existing tab' button."""
        has_terminal_tabs = self._has_open_terminal_tabs()
        btn.set_sensitive(has_terminal_tabs)
        btn.set_tooltip_text(
            None if has_terminal_tabs else _("No open terminal tabs to pick from")
        )

    def _has_open_terminal_tabs(self) -> bool:
        from .terminal import TerminalWidget  # local import avoids circular
        window = self.split_view_tab.window
        try:
            n = window.tab_view.get_n_pages()
            for i in range(n):
                page = window.tab_view.get_nth_page(i)
                if page is None:
                    continue
                if isinstance(page.get_child(), TerminalWidget):
                    return True
        except Exception:
            pass
        return False

    # ── hover controls ────────────────────────────────────────────────────

    def _build_hover_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        box.set_margin_top(4)
        box.set_margin_end(4)

        split_h_btn = Gtk.Button()
        split_h_btn.set_icon_name("view-dual-symbolic")
        split_h_btn.add_css_class("flat")
        split_h_btn.set_tooltip_text(_("Split Side by Side"))
        split_h_btn.connect("clicked", lambda _b: self.split_horizontal())
        box.append(split_h_btn)

        split_v_btn = Gtk.Button()
        split_v_btn.set_icon_name("view-paged-symbolic")
        split_v_btn.add_css_class("flat")
        split_v_btn.set_tooltip_text(_("Split Top/Bottom"))
        split_v_btn.connect("clicked", lambda _b: self.split_vertical())
        box.append(split_v_btn)

        close_btn = Gtk.Button()
        close_btn.set_icon_name("window-close-symbolic")
        close_btn.add_css_class("flat")
        close_btn.set_tooltip_text(_("Close Pane"))
        close_btn.connect("clicked", lambda _b: self.close_pane())
        box.append(close_btn)

        return box

    def _on_motion_enter(self, _ctrl, _x, _y) -> None:
        self._hover_revealer.set_reveal_child(True)

    def _on_motion_leave(self, _ctrl) -> None:
        self._hover_revealer.set_reveal_child(False)

    # ── terminal management ───────────────────────────────────────────────

    def set_terminal(self, terminal) -> None:
        """Replace placeholder (or existing terminal) with a live TerminalWidget."""
        self._terminal = terminal
        if terminal is not None:
            self.set_child(terminal)
        else:
            self._placeholder = self._build_placeholder()
            self.set_child(self._placeholder)
        self.split_view_tab._update_tab_title()

    def get_terminal(self):
        return self._terminal

    # ── split / close ────────────────────────────────────────────────────

    def split_horizontal(self) -> "SplitPane":
        return self._do_split(Gtk.Orientation.HORIZONTAL)

    def split_vertical(self) -> "SplitPane":
        return self._do_split(Gtk.Orientation.VERTICAL)

    def _do_split(self, orientation: Gtk.Orientation) -> "SplitPane":
        new_pane = SplitPane(self.split_view_tab)
        paned = Gtk.Paned(orientation=orientation)
        paned.set_wide_handle(True)
        paned.set_hexpand(True)
        paned.set_vexpand(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        # Replace self in parent with the new Paned node
        self._replace_in_parent(paned)
        # Nest self and new pane inside the Paned
        paned.set_start_child(self)
        paned.set_end_child(new_pane)
        self.split_view_tab._update_tab_title()
        return new_pane

    def _replace_in_parent(self, new_widget: Gtk.Widget) -> None:
        """Swap self out of its parent container, inserting new_widget in its place."""
        parent = self.get_parent()
        if isinstance(parent, SplitViewTab):
            parent._set_root(new_widget)
        elif isinstance(parent, Gtk.Paned):
            if parent.get_start_child() is self:
                parent.set_start_child(new_widget)
            else:
                parent.set_end_child(new_widget)

    def close_pane(self) -> None:
        if self.split_view_tab.get_pane_count() == 1:
            # Last pane — close the whole split-view tab
            self.split_view_tab.cleanup_all()
            window = self.split_view_tab.window
            page = self.split_view_tab._tab_page
            if page is not None:
                window._suppress_close_confirmation = True
                try:
                    window.tab_view.close_page(page)
                finally:
                    window._suppress_close_confirmation = False
            return

        parent = self.get_parent()
        if not isinstance(parent, Gtk.Paned):
            return

        # Identify the sibling pane/node
        if parent.get_start_child() is self:
            sibling = parent.get_end_child()
        else:
            sibling = parent.get_start_child()

        if sibling is None:
            return

        # Clean up this pane's terminal if any
        terminal = self.get_terminal()
        if terminal is not None:
            self.split_view_tab._cleanup_terminal(terminal)
            self._terminal = None

        # Unparent sibling from the Paned before re-parenting it
        try:
            sibling.unparent()
        except Exception:
            pass

        # Replace the containing Paned with the sibling in the grandparent
        grandparent = parent.get_parent()
        if isinstance(grandparent, SplitViewTab):
            grandparent._set_root(sibling)
        elif isinstance(grandparent, Gtk.Paned):
            if grandparent.get_start_child() is parent:
                grandparent.set_start_child(sibling)
            else:
                grandparent.set_end_child(sibling)

        self.split_view_tab.unregister_pane(self)
        self.split_view_tab._update_tab_title()

    # ── drag-and-drop ─────────────────────────────────────────────────────

    def _setup_drop_target(self) -> None:
        dt = Gtk.DropTarget.new(type=GObject.TYPE_PYOBJECT, actions=Gdk.DragAction.MOVE)
        dt.connect("drop", self._on_drop)
        dt.connect("enter", lambda _t, _x, _y: Gdk.DragAction.MOVE)
        self.add_controller(dt)

    def _on_drop(self, _target, value, _x: float, _y: float) -> bool:
        try:
            if hasattr(value, 'get_value'):
                value = value.get_value()
            if not isinstance(value, dict):
                return False
            if value.get("type") != "connection":
                return False

            nicknames = self._extract_connections(value)
            if not nicknames:
                return False

            window = self.split_view_tab.window
            connections = []
            for nick in nicknames:
                conn = window.connection_manager.find_connection_by_nickname(nick)
                if conn is not None:
                    connections.append(conn)

            if not connections:
                return False

            # First connection fills this pane; extras get additional vertical splits
            terminal = window.terminal_manager.create_terminal_for_pane(connections[0])
            self.set_terminal(terminal)
            last_pane = self

            for conn in connections[1:]:
                new_pane = last_pane.split_vertical()
                term = window.terminal_manager.create_terminal_for_pane(conn)
                new_pane.set_terminal(term)
                last_pane = new_pane

            return True
        except Exception as exc:
            logger.error("SplitPane drop failed: %s", exc)
            return False

    @staticmethod
    def _extract_connections(value: dict) -> List[str]:
        nicknames = value.get("connection_nicknames")
        if isinstance(nicknames, list) and nicknames:
            return nicknames
        single = value.get("connection_nickname")
        return [single] if single else []

    # ── "Pick existing tab" button ────────────────────────────────────────

    def _on_pick_existing_tab_clicked(self, button: Gtk.Button) -> None:
        from .terminal import TerminalWidget
        window = self.split_view_tab.window

        tab_entries: List[tuple] = []
        try:
            n = window.tab_view.get_n_pages()
            for i in range(n):
                page = window.tab_view.get_nth_page(i)
                if page is None:
                    continue
                child = page.get_child()
                if isinstance(child, TerminalWidget):
                    tab_entries.append((page, child, page.get_title() or _("Terminal")))
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
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_hexpand(True)
            row_box.append(lbl)
            row.set_child(row_box)
            # Attach data to row for the activated callback
            row._embed_page = page
            row._embed_terminal = terminal
            list_box.append(row)

        def _on_row_activated(_lb, row):
            popover.popdown()
            GLib.idle_add(lambda: self._embed_existing_tab(row._embed_page, row._embed_terminal))

        list_box.connect("row-activated", _on_row_activated)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_max_content_height(300)
        scroll.set_propagate_natural_height(True)
        scroll.set_child(list_box)

        popover.set_child(scroll)
        popover.popup()

    def _embed_existing_tab(self, page, terminal) -> bool:
        """Move an existing tab's terminal into this pane (reparent, no new session)."""
        window = self.split_view_tab.window
        window._suppress_close_confirmation = True
        window._moving_tab_to_pane = True
        try:
            window.tab_view.close_page(page)
        finally:
            window._suppress_close_confirmation = False
            window._moving_tab_to_pane = False
        self.set_terminal(terminal)
        return False  # GLib.idle_add one-shot


# ═══════════════════════════════════════════════════════════════════════════════


class SplitViewTab(Gtk.Box):
    """
    Top-level widget placed as the child of an Adw.TabPage.
    Owns the root of the Gtk.Paned tree and tracks all SplitPane leaves.
    """

    __gtype_name__ = "SshPilotSplitViewTab"

    def __init__(self, window) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.window = window
        self._panes: List[SplitPane] = []
        self._tab_page = None
        self.set_hexpand(True)
        self.set_vexpand(True)

        # Create the initial (empty) pane and make it the root
        self._root: Optional[Gtk.Widget] = None
        initial = SplitPane(self)       # registers itself via register_pane
        self._root = initial
        self.append(initial)

    # ── pane tree ─────────────────────────────────────────────────────────

    def _set_root(self, widget: Gtk.Widget) -> None:
        """Replace the root widget (called by SplitPane._replace_in_parent)."""
        if self._root is not None:
            try:
                self.remove(self._root)
            except Exception:
                pass
        self._root = widget
        self.append(widget)

    def register_pane(self, pane: SplitPane) -> None:
        if pane not in self._panes:
            self._panes.append(pane)

    def unregister_pane(self, pane: SplitPane) -> None:
        if pane in self._panes:
            self._panes.remove(pane)
        # If no panes remain, close the tab
        if not self._panes and self._tab_page is not None:
            window = self.window
            window._suppress_close_confirmation = True
            try:
                window.tab_view.close_page(self._tab_page)
            finally:
                window._suppress_close_confirmation = False

    def get_pane_count(self) -> int:
        return len(self._panes)

    # ── population ───────────────────────────────────────────────────────

    def populate(self, connections: list) -> None:
        """Pre-populate from a list of Connection objects (context-menu trigger)."""
        if not connections:
            return
        window = self.window
        first_terminal = window.terminal_manager.create_terminal_for_pane(connections[0])
        self._panes[0].set_terminal(first_terminal)
        last_pane = self._panes[0]

        for conn in connections[1:]:
            new_pane = last_pane.split_vertical()
            term = window.terminal_manager.create_terminal_for_pane(conn)
            new_pane.set_terminal(term)
            last_pane = new_pane

    # ── terminal lifecycle ────────────────────────────────────────────────

    def _get_all_terminals(self) -> list:
        return [p.get_terminal() for p in self._panes if p.get_terminal() is not None]

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
            logger.debug("Error cleaning up pane terminal dicts: %s", exc)

    def cleanup_all(self) -> None:
        """Disconnect all embedded terminals and remove them from tracking dicts."""
        for pane in list(self._panes):
            terminal = pane.get_terminal()
            if terminal is not None:
                self._cleanup_terminal(terminal)
                pane._terminal = None

    # ── tab title ────────────────────────────────────────────────────────

    def _update_tab_title(self) -> None:
        if self._tab_page is None:
            return
        n_active = sum(1 for p in self._panes if p.get_terminal() is not None)
        if n_active > 0:
            self._tab_page.set_title(
                _("Split View ({n} terminals)").format(n=n_active)
            )
        else:
            self._tab_page.set_title(_("Split View"))
