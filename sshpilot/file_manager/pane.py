"""Single file-manager pane (local or remote SFTP).

Houses ``FilePane`` — the main browser widget shown in the left/right side of
``FileManagerWindow``. Talks to the OpenSSH SFTP backend for remote operations and
exposes signals back to the window for cross-pane orchestration.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
import pathlib
import posixpath
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Tuple

from gi.repository import Adw, Gdk, Gio, GLib, GObject, Gtk, Pango

from ..platform_utils import is_flatpak, is_macos
from ..text_editor import RemoteFileEditorWindow
from .pane_controls import PaneToolbar
from .portal_docs import (
    _grant_persistent_access,
    _load_first_doc_path,
    _pretty_path_for_display,
    _save_doc,
)
from .properties_dialog import PropertiesDialog
from .common import FileEntry


logger = logging.getLogger(__name__)


if TYPE_CHECKING:
    from ..file_manager_window import FileManagerWindow


from .icon_levels import (
    _DEFAULT_ICON_LEVEL,
    _GRID_ICON_SIZES,
    _LIST_ICON_SIZES,
    _MAX_ICON_LEVEL,
    _MIN_ICON_LEVEL,
)


def _file_manager_window_cls():
    """Lazy access to ``FileManagerWindow`` to break the import cycle."""
    from ..file_manager_window import FileManagerWindow
    return FileManagerWindow


class FilePane(Gtk.Box):
    """Represents a single pane in the manager."""

    _TYPEAHEAD_TIMEOUT = 1.0

    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "request-operation": (
            GObject.SignalFlags.RUN_FIRST,
            None,
            (str, object),
        ),
    }

    def __init__(self, label: str) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.toolbar = PaneToolbar()
        self.toolbar._pane_label.set_text(label)
        self.append(self.toolbar)

        self._is_remote = label.lower() == "remote"
        self._window: Optional["FileManagerWindow"] = None
        # Icon zoom level (index into _LIST_ICON_SIZES / _GRID_ICON_SIZES).
        # Set silently here so __init__ paths that build factory widgets get a
        # sane initial size; the parent FileManagerWindow may overwrite this
        # with the user's persisted value before the first directory load.
        self._icon_size_level: int = _DEFAULT_ICON_LEVEL
        # Track currently bound icon widgets so zoom updates them in place
        # (O(visible)) instead of forcing a full list-store rebuild. Use plain
        # sets (not WeakSet): PyGObject can drop the Python wrapper for a live
        # GTK widget between bind/unbind cycles, which would make WeakSet
        # entries vanish unpredictably. The factory's bind/unbind pair makes
        # explicit add/discard reliable.
        self._bound_list_icons: set = set()
        # Currently-bound list row boxes, so deferred folder item-counts can be
        # written into visible rows in place (no list-store rebuild).
        self._bound_list_boxes: set = set()
        self._bound_grid_images: set = set()

        self._stack = Gtk.Stack()
        self._stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._stack.set_hexpand(True)
        self._stack.set_vexpand(True)

        self._list_store = Gio.ListStore(item_type=Gtk.StringObject)
        self._selection_model = Gtk.MultiSelection.new(self._list_store)
        self._selection_anchor: Optional[int] = None

        self._suppress_next_context_menu: bool = False

        list_factory = Gtk.SignalListItemFactory()
        list_factory.connect("setup", self._on_list_setup)
        list_factory.connect("bind", self._on_list_bind)
        list_factory.connect("unbind", self._on_list_unbind)
        list_view = Gtk.ListView(model=self._selection_model, factory=list_factory)
        list_view.add_css_class("rich-list")
        list_view.set_can_focus(True)  # Enable keyboard focus for typeahead
        # Navigate on row activation (double click / Enter)
        self._list_view = list_view
        list_view.connect("activate", self._on_list_activate)

        # Wrap list view in a scrolled window for proper scrolling
        list_scrolled = Gtk.ScrolledWindow()
        list_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        list_scrolled.set_child(list_view)

        grid_factory = Gtk.SignalListItemFactory()
        grid_factory.connect("setup", self._on_grid_setup)
        grid_factory.connect("bind", self._on_grid_bind)
        grid_factory.connect("unbind", self._on_grid_unbind)
        grid_view = Gtk.GridView(
            model=self._selection_model,
            factory=grid_factory,
            max_columns=6,
        )
        grid_view.set_enable_rubberband(True)
        grid_view.set_can_focus(True)  # Enable keyboard focus for typeahead
        self._grid_view = grid_view
        # Navigate on grid item activation (double click / Enter)
        grid_view.connect("activate", self._on_grid_activate)

        # Wrap grid view in a scrolled window for proper scrolling
        grid_scrolled = Gtk.ScrolledWindow()
        grid_scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        grid_scrolled.set_child(grid_view)

        # Ctrl/Cmd + wheel = zoom icons. Attach to each scrolled view so the
        # modifier+scroll combination is captured before the regular scroll
        # reaches the ScrolledWindow.
        for scrolled in (list_scrolled, grid_scrolled):
            scroll_ctrl = Gtk.EventControllerScroll.new(
                Gtk.EventControllerScrollFlags.VERTICAL
            )
            scroll_ctrl.connect("scroll", self._on_pane_scroll)
            scrolled.add_controller(scroll_ctrl)

        self._stack.add_named(list_scrolled, "list")
        self._stack.add_named(grid_scrolled, "grid")


        overlay = Adw.ToastOverlay()
        self._overlay = overlay
        self._current_toast = None  # Keep reference to current toast for dismissal

        content_overlay = Gtk.Overlay()
        content_overlay.set_child(self._stack)

        overlay.set_child(content_overlay)
        self.append(overlay)

        # Add drop target for file operations - use string type for better compatibility
        drop_target = Gtk.DropTarget.new(type=GObject.TYPE_STRING, actions=Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drop_target.connect("drop", self._on_drop_string)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        self.add_controller(drop_target)
        
        logger.debug(f"Added drop target to pane: {self._is_remote}")

        self._partner_pane: Optional["FilePane"] = None

        self._action_buttons: Dict[str, Gtk.Button] = {}
        action_bar = Gtk.ActionBar()
        action_bar.add_css_class("inline-toolbar")

        def _create_action_button(
            name: str,
            icon_name: str,
            label: str,
            callback: Callable[[Gtk.Button], None],
        ) -> Gtk.Button:
            from sshpilot import icon_utils
            button = Gtk.Button()
            
            # Only upload and download buttons get text labels
            if name in ["upload", "download"]:
                # ButtonContent only supports set_icon_name, but we want bundled icons
                # So we'll use an Image widget with a label
                image = icon_utils.new_image_from_icon_name(icon_name)
                # Create a box to hold both icon and label
                box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
                box.append(image)
                label_widget = Gtk.Label(label=label)
                box.append(label_widget)
                button.set_child(box)
            else:
                # Icon-only buttons for other actions - use Image widget as child
                image = icon_utils.new_image_from_icon_name(icon_name)
                button.set_child(image)
                button.set_tooltip_text(label)
            
            # Improve button alignment and styling
            button.set_valign(Gtk.Align.CENTER)
            button.set_has_frame(False)
            button.add_css_class("flat")
            
            button.connect("clicked", callback)
            self._action_buttons[name] = button
            return button

        download_button = _create_action_button(
            "download",
            "document-save-symbolic",
            "Download",
            lambda _button: self._on_download_clicked(_button),
        )
        upload_button = _create_action_button(
            "upload",
            "document-send-symbolic",
            "Upload",
            lambda _button: self._on_upload_clicked(_button),
        )
        copy_button = _create_action_button(
            "copy",
            "edit-copy-symbolic",
            "Copy",
            lambda _button: self._emit_entry_operation("copy"),
        )
        cut_button = _create_action_button(
            "cut",
            "edit-cut-symbolic",
            "Cut",
            lambda _button: self._emit_entry_operation("cut"),
        )
        paste_button = _create_action_button(
            "paste",
            "edit-paste-symbolic",
            "Paste",
            lambda _button: self._emit_paste_operation(),
        )
        edit_button = _create_action_button(
            "edit",
            "text-editor-symbolic",
            "Edit",
            lambda _button: self._on_menu_edit(),
        )
        rename_button = _create_action_button(
            "rename",
            "document-edit-symbolic",
            "Rename",
            lambda _button: self._emit_entry_operation("rename"),
        )
        delete_button = _create_action_button(
            "delete",
            "user-trash-symbolic",
            "Delete",
            lambda _button: self._emit_entry_operation("delete"),
        )
        download_button.set_visible(self._is_remote)
        upload_button.set_visible(not self._is_remote)
        edit_button.set_visible(False)  # Initially hidden, shown when text file is selected

        # Add Request Access button for local pane in Flatpak (always show when in Flatpak)
        request_access_button = None
        if not self._is_remote and is_flatpak():
            request_access_button = _create_action_button(
                "request_access",
                "folder-open-symbolic",
                "Request Access",
                lambda _button: self._on_request_access_clicked(),
            )
            # Use ButtonContent for this special button to make it more prominent
            content = Adw.ButtonContent()
            content.set_icon_name("folder-open-symbolic")
            content.set_label("Request Access")
            request_access_button.set_child(content)
            # Ensure the button uses the suggested-action styling with visible background
            request_access_button.set_has_frame(True)
            request_access_button.remove_css_class("flat")
            request_access_button.add_css_class("suggested-action")
            # Store reference to the button so we can hide it later
            self._request_access_button = request_access_button
        else:
            self._request_access_button = None

        action_bar.pack_start(upload_button)
        action_bar.pack_start(download_button)
        if request_access_button:
            action_bar.pack_start(request_access_button)
        action_bar.pack_end(delete_button)
        action_bar.pack_end(rename_button)
        action_bar.pack_end(edit_button)
        action_bar.pack_end(cut_button)
        action_bar.pack_end(copy_button)
        action_bar.pack_end(paste_button)

        self._action_bar = action_bar
        self.append(action_bar)

        self._can_paste: bool = False

        # Connect to view-changed signal from toolbar
        self.toolbar.connect("view-changed", self._on_view_toggle)
        self.toolbar.connect("show-hidden-toggled", self._on_toolbar_show_hidden_toggled)
        self.toolbar.connect("zoom-changed", self._on_toolbar_zoom_changed)
        self.toolbar.path_entry.connect("activate", self._on_path_entry)
        # Wire navigation buttons
        self.toolbar.controls.up_button.connect("clicked", self._on_up_clicked)
        self.toolbar.controls.back_button.connect("clicked", self._on_back_clicked)
        self.toolbar.controls.refresh_button.connect("clicked", self._on_refresh_clicked)
        self.toolbar.controls.new_folder_button.connect(
            "clicked", lambda *_: self.emit("request-operation", "mkdir", None)
        )
        # Upload/download functionality is now available through action bar and context menu only

        self._history: List[str] = []
        self._current_path = "/"
        self._entries: List[FileEntry] = []
        self._cached_entries: List[FileEntry] = []
        self._raw_entries: List[FileEntry] = []
        self._show_hidden = False
        self.toolbar.set_show_hidden_state(self._show_hidden)
        self._sort_key = "name"  # Default sort by name
        self._sort_descending = False  # Default ascending order

        self._suppress_history_push: bool = False
        self._selection_model.connect("selection-changed", self._on_selection_changed)

        self._menu_actions: Dict[str, Gio.SimpleAction] = {}
        self._menu_action_callbacks: Dict[str, Callable[[], None]] = {}  # Store callbacks for direct access
        self._menu_action_group = Gio.SimpleActionGroup()
        self.insert_action_group("pane", self._menu_action_group)
        self._menu_popover: Gtk.Popover = self._create_menu_model()
        self._add_context_controller(list_view)
        self._add_context_controller(grid_view)

        for view in (list_view, grid_view):
            controller = Gtk.EventControllerKey.new()
            controller.connect("key-pressed", self._on_typeahead_key_pressed)
            view.add_controller(controller)
            self._attach_shortcuts(view)


        self._update_menu_state()
        # Set up sorting actions for the split button
        self._setup_sorting_actions()
        
        # Initialize view button icon and direction states
        self._update_view_button_icon()
        self._update_sort_direction_states()

        self._typeahead_buffer: str = ""
        self._typeahead_last_time: float = 0.0

    # -- drop zone & drag support -------------------------------------

    def set_partner_pane(self, partner: Optional["FilePane"]) -> None:
        self._partner_pane = partner






    # -- callbacks ------------------------------------------------------

    def _attach_shortcuts(self, view: Gtk.Widget) -> None:
        controller = Gtk.ShortcutController()
        controller.set_scope(Gtk.ShortcutScope.LOCAL)

        def add_shortcut(trigger: Gtk.ShortcutTrigger, handler: Callable[[], bool]) -> None:
            if trigger is None:
                return
            action = Gtk.CallbackAction.new(lambda _widget, _args: handler())
            controller.add_shortcut(Gtk.Shortcut.new(trigger, action))

        def add_trigger_string(trigger_str: str, handler: Callable[[], bool]) -> None:
            if not trigger_str:
                return
            trigger = Gtk.ShortcutTrigger.parse_string(trigger_str)
            add_shortcut(trigger, handler)

        add_trigger_string("<primary>l", self._shortcut_focus_path_entry)
        add_trigger_string("<primary>r", self._shortcut_refresh)
        add_shortcut(Gtk.KeyvalTrigger.new(Gdk.KEY_F5, Gdk.ModifierType(0)), self._shortcut_refresh)
        add_trigger_string("<primary>c", lambda: self._shortcut_operation("copy"))
        add_trigger_string("<primary>x", lambda: self._shortcut_operation("cut"))
        add_trigger_string("<primary>v", lambda: self._shortcut_operation("paste"))
        add_trigger_string(
            "<shift><primary>v",
            lambda: self._shortcut_operation("paste", force_move=True),
        )

        delete_triggers = [
            Gtk.KeyvalTrigger.new(Gdk.KEY_Delete, Gdk.ModifierType(0)),
            Gtk.KeyvalTrigger.new(Gdk.KEY_KP_Delete, Gdk.ModifierType(0)),
            Gtk.KeyvalTrigger.new(Gdk.KEY_Delete, Gdk.ModifierType.SHIFT_MASK),
            Gtk.KeyvalTrigger.new(Gdk.KEY_KP_Delete, Gdk.ModifierType.SHIFT_MASK),
        ]
        for trigger in delete_triggers:
            add_shortcut(trigger, self._shortcut_delete)

        view.add_controller(controller)

    def _shortcut_focus_path_entry(self) -> bool:
        entry = getattr(self.toolbar, "path_entry", None)
        if isinstance(entry, Gtk.Entry):
            try:
                entry.grab_focus()
                entry.select_region(0, -1)
            except Exception:
                pass
        return True

    def _shortcut_refresh(self) -> bool:
        self._on_refresh_clicked(None)
        return True

    def _shortcut_delete(self) -> bool:
        self._emit_entry_operation("delete")
        return True

    def _on_view_toggle(self, toolbar, view_name: str) -> None:
        self._stack.set_visible_child_name(view_name)
        # Update the split button icon to reflect current view
        self._update_view_button_icon()

    def _on_toolbar_show_hidden_toggled(self, _toolbar, show_hidden: bool) -> None:
        self.set_show_hidden(show_hidden)

    def _on_toolbar_zoom_changed(self, _toolbar, level: int) -> None:
        self.set_icon_size_level(level)

    def _on_path_entry(self, entry: Gtk.Entry) -> None:
        self.emit("path-changed", entry.get_text() or "/")

    def _on_list_setup(self, factory: Gtk.SignalListItemFactory, item):
        from ..icon_utils import new_image_from_icon_name
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        icon = new_image_from_icon_name("folder-symbolic", size=self._list_icon_px())
        icon.set_valign(Gtk.Align.CENTER)
        name_label = Gtk.Label(xalign=0)
        name_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        name_label.set_max_width_chars(40)
        name_label.set_hexpand(True)
        metadata_label = Gtk.Label(xalign=1)
        metadata_label.set_halign(Gtk.Align.END)
        metadata_label.set_ellipsize(Pango.EllipsizeMode.END)
        metadata_label.add_css_class("dim-label")
        box.append(icon)
        box.append(name_label)
        box.append(metadata_label)
        box.set_hexpand(True)
        # Store references as Python attributes instead of deprecated set_data
        box.icon = icon
        box.name_label = name_label
        box.metadata_label = metadata_label
        
        # Add drag source for file operations
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        box.add_controller(drag_source)
        
        # Add right-click gesture to select item and show context menu
        right_click_gesture = Gtk.GestureClick()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_list_item_right_click, item)
        box.add_controller(right_click_gesture)
        
        item.set_child(box)

    def _on_list_bind(self, factory: Gtk.SignalListItemFactory, item):
        box = item.get_child()
        # Access references as Python attributes instead of deprecated get_data
        icon: Gtk.Image = box.icon
        name_label: Gtk.Label = box.name_label
        metadata_label: Gtk.Label = box.metadata_label

        # Row widgets are pooled by GtkListView; reset the pixel size every
        # bind so zoom changes are visible without rebuilding the widget.
        icon.set_pixel_size(self._list_icon_px())
        # Track this icon so a zoom event can update it in place without
        # rebuilding the list store.
        self._bound_list_icons.add(icon)

        position = item.get_position()
        entry: Optional[FileEntry] = None
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            
        # Store position in the box for drag operations
        box.drag_position = position

        if entry is None:
            value = item.get_item().get_string()
            name_label.set_text(value)
            name_label.set_tooltip_text(value)
            metadata_label.set_text("—")
            metadata_label.set_tooltip_text(None)
            from ..icon_utils import set_icon_from_name
            is_dir = value.endswith('/')
            raw_name = value[:-1] if is_dir else value
            set_icon_from_name(icon, self._resolve_entry_icon(raw_name, is_dir))
            return

        display_name = entry.name + ("/" if entry.is_dir else "")
        name_label.set_text(display_name)
        name_label.set_tooltip_text(display_name)

        if entry.is_dir:
            if entry.item_count is not None:
                count_text = f"{entry.item_count} items"
                metadata_label.set_text(count_text)
                metadata_label.set_tooltip_text(count_text)
            else:
                metadata_label.set_text("—")
                metadata_label.set_tooltip_text(None)
        else:
            size_text = self._format_size(entry.size)
            metadata_label.set_text(size_text)
            metadata_label.set_tooltip_text(size_text)

        from ..icon_utils import set_icon_from_name
        set_icon_from_name(icon, self._resolve_entry_icon(entry.name, entry.is_dir))

        box._pane_entry = entry
        box._pane_index = position
        self._bound_list_boxes.add(box)

    def _on_list_unbind(self, factory: Gtk.SignalListItemFactory, item):
        box = item.get_child()
        if box is None:
            return
        icon = getattr(box, "icon", None)
        if icon is not None:
            self._bound_list_icons.discard(icon)
        self._bound_list_boxes.discard(box)

    def update_item_counts(self, path: str, counts) -> None:
        """Apply background-computed folder item-counts to the current listing.

        Mutates the shared ``FileEntry`` objects (so rows bound later read the
        count in ``_on_list_bind``) and refreshes the subtitle of any currently
        visible folder row in place — no list-store rebuild, so scroll position
        and selection are preserved. Ignored if the user has navigated away.
        """
        if not counts or path != self._current_path or not self._is_remote:
            return
        for entry in self._cached_entries:
            if entry.is_dir and entry.name in counts:
                entry.item_count = counts[entry.name]
        for box in list(self._bound_list_boxes):
            entry = getattr(box, "_pane_entry", None)
            if entry is None or not entry.is_dir or entry.name not in counts:
                continue
            label = getattr(box, "metadata_label", None)
            if label is not None:
                count_text = f"{entry.item_count} items"
                label.set_text(count_text)
                label.set_tooltip_text(count_text)

    def _resolve_entry_icon(self, name: str, is_dir: bool) -> str:
        """Return the Adwaita mimetype icon name to use for a directory entry."""
        from ..file_type_icons import get_icon_for_name
        return get_icon_for_name(name, is_dir)

    def _list_icon_px(self) -> int:
        return _LIST_ICON_SIZES[self._icon_size_level]

    def _grid_icon_px(self) -> int:
        return _GRID_ICON_SIZES[self._icon_size_level]

    def set_icon_size_level(self, level: int) -> None:
        """Update the icon zoom level for this pane and resize visible rows."""
        clamped = max(_MIN_ICON_LEVEL, min(_MAX_ICON_LEVEL, level))
        if clamped == self._icon_size_level:
            return
        self._icon_size_level = clamped
        # Resize the currently bound icon widgets in place. This is O(visible)
        # — far cheaper than rebuilding the list store, which produces a
        # noticeable freeze on large remote directories. New rows that get
        # bound while scrolling will pick up the size from the bind callback
        # (which reads self._icon_size_level directly). queue_resize() forces
        # GtkGridView to re-measure cell sizes; set_pixel_size alone updates
        # the image's request but the grid caches its cell extents.
        list_px = self._list_icon_px()
        for icon in list(self._bound_list_icons):
            try:
                icon.set_pixel_size(list_px)
                icon.queue_resize()
            except Exception:
                pass
        grid_px = self._grid_icon_px()
        for image in list(self._bound_grid_images):
            try:
                image.set_pixel_size(grid_px)
                image.queue_resize()
            except Exception:
                pass
        # Nudge the views themselves so cached layouts (especially GridView's
        # column-width calc) get refreshed.
        for view in (getattr(self, "_list_view", None), getattr(self, "_grid_view", None)):
            if view is not None:
                try:
                    view.queue_resize()
                except Exception:
                    pass
        # Keep the toolbar's slider in sync — e.g. when the level was changed
        # via Ctrl+wheel rather than by the user dragging the slider itself.
        toolbar = getattr(self, "toolbar", None)
        if toolbar is not None and hasattr(toolbar, "set_zoom_level"):
            try:
                toolbar.set_zoom_level(self._icon_size_level)
            except Exception as exc:
                logger.debug("Failed to sync toolbar slider: %s", exc)
        # Persist whichever pane was zoomed last as the new default for any
        # newly opened file manager windows.
        self._persist_icon_size_level()

    def _request_zoom(self, direction: int) -> None:
        """Zoom this pane by *direction* (+1 / -1)."""
        self.set_icon_size_level(self._icon_size_level + direction)

    @staticmethod
    def _load_saved_icon_size_level() -> int:
        """Return the persisted default icon zoom level for new panes."""
        try:
            from ..config import Config
            fm = Config().get_file_manager_config() or {}
            value = int(fm.get('icon_size_level', _DEFAULT_ICON_LEVEL))
        except Exception as exc:
            logger.debug("Could not read file_manager.icon_size_level: %s", exc)
            return _DEFAULT_ICON_LEVEL
        return max(_MIN_ICON_LEVEL, min(_MAX_ICON_LEVEL, value))

    def _persist_icon_size_level(self) -> None:
        """Save this pane's current level as the default for new windows."""
        try:
            from ..config import Config
            Config().set_setting('file_manager.icon_size_level', self._icon_size_level)
        except Exception as exc:
            logger.debug("Failed to persist file_manager.icon_size_level: %s", exc)

    def _on_pane_scroll(self, controller: Gtk.EventControllerScroll, dx: float, dy: float) -> bool:
        """Intercept Ctrl/Cmd + wheel to zoom icons; otherwise let it scroll."""
        try:
            event = controller.get_current_event()
            state = event.get_modifier_state() if event is not None else Gdk.ModifierType(0)
        except Exception:
            state = Gdk.ModifierType(0)

        if is_macos():
            primary = bool(state & Gdk.ModifierType.META_MASK)
        else:
            primary = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if not primary:
            return False  # propagate; ScrolledWindow handles normal scrolling

        if dy > 0:
            direction = -1
        elif dy < 0:
            direction = +1
        else:
            return False

        self._request_zoom(direction)
        return True  # consume; don't also scroll the view

    @staticmethod
    def _format_size(size_bytes: int) -> str:
        if size_bytes < 1024:
            return f"{size_bytes} B"
        units = ["KB", "MB", "GB", "TB", "PB"]
        value = float(size_bytes)
        for unit in units:
            value /= 1024.0
            if value < 1024.0:
                return f"{value:.1f} {unit}"
        return f"{value:.1f} EB"

    def _on_grid_setup(self, factory: Gtk.SignalListItemFactory, item):
        button = Gtk.Button()
        button.set_has_frame(False)
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=6,
        )
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)

        from ..icon_utils import new_image_from_icon_name
        image = new_image_from_icon_name("folder-symbolic", size=self._grid_icon_px())
        image.set_halign(Gtk.Align.CENTER)
        content.append(image)

        label = Gtk.Label()
        label.set_halign(Gtk.Align.CENTER)
        label.set_justify(Gtk.Justification.CENTER)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_wrap(True)
        label.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_lines(2)
        # Force normal weight: Gtk.Button styling makes its label bold by
        # default, which looks wrong for filenames in a grid cell.
        normal_weight_attrs = Pango.AttrList()
        normal_weight_attrs.insert(Pango.attr_weight_new(Pango.Weight.NORMAL))
        label.set_attributes(normal_weight_attrs)
        content.append(label)

        button.set_child(content)

        # GestureClick allows inspecting modifier state and click counts before
        # the button consumes the event. Use this to keep selection behaviour in
        # sync with Gtk.GridView expectations.
        click_gesture = Gtk.GestureClick()
        click_gesture.set_button(Gdk.BUTTON_PRIMARY)
        if hasattr(click_gesture, "set_exclusive"):
            try:
                click_gesture.set_exclusive(True)
            except Exception:
                pass
        propagation_phase = getattr(Gtk, "PropagationPhase", None)
        if propagation_phase is not None and hasattr(click_gesture, "set_propagation_phase"):
            try:
                click_gesture.set_propagation_phase(propagation_phase.CAPTURE)
            except Exception:
                pass
        click_gesture.connect("pressed", self._on_grid_cell_pressed, button)
        button.add_controller(click_gesture)
        
        # Add right-click gesture to select item and show context menu
        right_click_gesture = Gtk.GestureClick()
        right_click_gesture.set_button(Gdk.BUTTON_SECONDARY)
        right_click_gesture.connect("pressed", self._on_grid_item_right_click, item)
        button.add_controller(right_click_gesture)
        
        # Add drag source for file operations
        drag_source = Gtk.DragSource()
        drag_source.set_actions(Gdk.DragAction.COPY | Gdk.DragAction.MOVE)
        drag_source.connect("prepare", self._on_drag_prepare)
        drag_source.connect("drag-begin", self._on_drag_begin)
        drag_source.connect("drag-end", self._on_drag_end)
        button.add_controller(drag_source)
        
        item.set_child(button)

    def _on_grid_bind(self, factory: Gtk.SignalListItemFactory, item):
        # Grid view uses the same icon for now but honours the entry name as
        # tooltip so users can differentiate.
        button = item.get_child()
        content = button.get_child()
        image = content.get_first_child()
        label = content.get_last_child()

        # Grid cells are pooled by GtkGridView; reset pixel size every bind so
        # the current zoom level wins after re-binding.
        image.set_pixel_size(self._grid_icon_px())
        # Track this image so a zoom event can update it in place.
        self._bound_grid_images.add(image)

        value = item.get_item().get_string()
        display_text = value[:-1] if value.endswith('/') else value

        label.set_text(display_text)
        label.set_tooltip_text(display_text)
        button.set_tooltip_text(display_text)

        entry: Optional[FileEntry] = None
        position = item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]

        # Update the image icon based on type (using the resolved FileEntry when
        # available so the icon reflects the real filename rather than the
        # display string with its trailing '/').
        from ..icon_utils import set_icon_from_name
        if entry is not None:
            set_icon_from_name(image, self._resolve_entry_icon(entry.name, entry.is_dir))
        else:
            is_dir = value.endswith('/')
            raw_name = value[:-1] if is_dir else value
            set_icon_from_name(image, self._resolve_entry_icon(raw_name, is_dir))
            
        # Store position in the button for drag operations
        button.drag_position = position


    def _on_grid_unbind(self, factory: Gtk.SignalListItemFactory, item):
        button = item.get_child()
        if button is None:
            return
        content = button.get_child()
        if content is not None:
            image = content.get_first_child()
            if image is not None:
                self._bound_grid_images.discard(image)

    def _on_grid_cell_pressed(
        self,
        gesture: Gtk.GestureClick,
        n_press: int,
        _x: float,
        _y: float,
        button: Gtk.Button,
    ) -> None:
        position = getattr(button, "drag_position", None)
        if position is None or not (0 <= position < len(self._entries)):
            return

        if n_press == 1:
            self._update_grid_selection_for_press(position, gesture)
            return

        if n_press >= 2:
            try:
                gesture.set_state(Gtk.EventSequenceState.CLAIMED)
            except Exception:
                pass
            self._suppress_next_context_menu = True
            self._navigate_to_entry(position)

    def _update_grid_selection_for_press(
        self, position: int, gesture: Gtk.GestureClick
    ) -> None:
        state = gesture.get_current_event_state()
        if state is None:
            state = Gdk.ModifierType(0)

        primary_mask = getattr(Gdk.ModifierType, "CONTROL_MASK", 0)
        if is_macos():
            primary_mask |= (
                getattr(Gdk.ModifierType, "META_MASK", 0)
                | getattr(Gdk.ModifierType, "SUPER_MASK", 0)
            )

        has_primary = bool(state & primary_mask)
        has_shift = bool(state & getattr(Gdk.ModifierType, "SHIFT_MASK", 0))

        if has_shift and self._selection_anchor is not None:
            start = min(self._selection_anchor, position)
            end = max(self._selection_anchor, position)
            self._selection_model.unselect_all()
            for index in range(start, end + 1):
                self._selection_model.select_item(index, False)
            self._selection_anchor = position
        elif has_shift:
            self._selection_model.select_item(position, True)
            self._selection_anchor = position
        elif has_primary:
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            if is_selected:
                self._selection_model.unselect_item(position)
            else:
                self._selection_model.select_item(position, False)
            self._selection_anchor = position
        else:
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            if not is_selected or self._selection_model is None:
                try:
                    self._selection_model.unselect_all()
                except Exception:
                    pass
                self._selection_model.select_item(position, False)
            self._selection_anchor = position

    def _on_selection_changed(self, model, position, n_items):
        self._update_menu_state()

    def _setup_sorting_actions(self) -> None:
        """Set up sorting actions for the split button menu."""
        # Create actions for sorting
        self._menu_actions["sort-by-name"] = Gio.SimpleAction.new("sort-by-name", None)
        self._menu_actions["sort-by-size"] = Gio.SimpleAction.new("sort-by-size", None)
        self._menu_actions["sort-by-modified"] = Gio.SimpleAction.new("sort-by-modified", None)
        
        # Create stateful actions for sort direction (radio buttons)
        self._menu_actions["sort-direction-asc"] = Gio.SimpleAction.new_stateful(
            "sort-direction-asc", None, GLib.Variant.new_boolean(not self._sort_descending)
        )
        self._menu_actions["sort-direction-desc"] = Gio.SimpleAction.new_stateful(
            "sort-direction-desc", None, GLib.Variant.new_boolean(self._sort_descending)
        )
        
        # Connect action handlers
        self._menu_actions["sort-by-name"].connect("activate", lambda *_: self._on_sort_by("name"))
        self._menu_actions["sort-by-size"].connect("activate", lambda *_: self._on_sort_by("size"))
        self._menu_actions["sort-by-modified"].connect("activate", lambda *_: self._on_sort_by("modified"))
        self._menu_actions["sort-direction-asc"].connect("activate", lambda *_: self._on_sort_direction(False))
        self._menu_actions["sort-direction-desc"].connect("activate", lambda *_: self._on_sort_direction(True))
        
        # Add actions to action group
        for action in self._menu_actions.values():
            self._menu_action_group.add_action(action)

    def _on_sort_by(self, sort_key: str) -> None:
        """Handle sort by selection from menu."""
        if self._sort_key != sort_key:
            self._sort_key = sort_key
            self._refresh_sorted_entries(preserve_selection=True)

    def _on_sort_direction(self, descending: bool) -> None:
        """Handle sort direction selection from menu."""
        if self._sort_descending != descending:
            self._sort_descending = descending
            self._refresh_sorted_entries(preserve_selection=True)
            self._update_sort_direction_states()

    def _update_view_button_icon(self) -> None:
        """Update the split button icon based on current view mode."""
        # Check which view is currently active
        if hasattr(self.toolbar, '_current_view') and self.toolbar._current_view == "list":
            icon_name = "view-list-symbolic"
        else:
            icon_name = "view-grid-symbolic"
        
        # Adw.SplitButton uses set_icon_name()
        self.toolbar.sort_split_button.set_icon_name(icon_name)

    def _update_sort_direction_states(self) -> None:
        """Update the radio button states for sort direction."""
        asc_action = self._menu_actions["sort-direction-asc"]
        desc_action = self._menu_actions["sort-direction-desc"]
        
        asc_action.set_state(GLib.Variant.new_boolean(not self._sort_descending))
        desc_action.set_state(GLib.Variant.new_boolean(self._sort_descending))

    def _create_menu_model(self) -> Gtk.Popover:
        # Create menu actions first
        def _add_action(name: str, callback: Callable[[], None]) -> None:
            if name not in self._menu_actions:
                action = Gio.SimpleAction.new(name, None)
                # Store callback for direct access
                self._menu_action_callbacks[name] = callback

                def _on_activate(_action: Gio.SimpleAction, _param: Optional[GLib.Variant]) -> None:
                    try:
                        logger.debug(f"_add_action: _on_activate called for action '{name}'")
                        callback()
                        logger.debug(f"_add_action: callback for '{name}' completed successfully")
                    except Exception as e:
                        logger.error(f"_add_action: Error in callback for '{name}': {e}", exc_info=True)

                action.connect("activate", _on_activate)
                self._menu_action_group.add_action(action)
                self._menu_actions[name] = action

        _add_action("download", self._on_menu_download)
        _add_action("upload", self._on_menu_upload)
        _add_action("edit", self._on_menu_edit)
        _add_action("copy", lambda: self._emit_entry_operation("copy"))
        _add_action("cut", lambda: self._emit_entry_operation("cut"))
        _add_action("paste", self._emit_paste_operation)
        _add_action("rename", lambda: self._emit_entry_operation("rename"))
        _add_action("delete", lambda: self._emit_entry_operation("delete"))
        _add_action("new_folder", lambda: self.emit("request-operation", "mkdir", None))
        _add_action("new_file", lambda: self.emit("request-operation", "newfile", None))
        _add_action("properties", self._on_menu_properties)

        # Create popover with listbox (same style as connection list)
        popover = Gtk.Popover.new()
        popover.set_has_arrow(True)
        
        # Create listbox for menu items (same margins as connection list)
        listbox = Gtk.ListBox(margin_top=2, margin_bottom=2, margin_start=2, margin_end=2)
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        popover.set_child(listbox)
        
        return popover


    def _on_list_item_right_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, list_item: Gtk.ListItem) -> None:
        """Handle right-click on a list item: select it and show context menu."""
        position = list_item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            # Check if the clicked item is already selected
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            
            # If the clicked item is not already selected, clear selection and select only this item
            # If it is already selected, preserve the current selection
            if not is_selected:
                self._selection_model.unselect_all()
                self._selection_model.select_item(position, False)
            else:
                # Ensure the clicked item is selected (should already be, but be safe)
                self._selection_model.select_item(position, False)
            self._selection_anchor = position
        
        # Show context menu at click position
        box = list_item.get_child()
        if box:
            # Convert coordinates to the view widget's coordinate space
            view_widget = self._list_view
            widget_x, widget_y = box.translate_coordinates(view_widget, x, y)
            if widget_x is not None and widget_y is not None:
                self._show_context_menu(view_widget, widget_x, widget_y)
            else:
                # Fallback: use the box coordinates
                self._show_context_menu(box, x, y)

    def _on_grid_item_right_click(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, list_item: Gtk.ListItem) -> None:
        """Handle right-click on a grid item: select it and show context menu."""
        position = list_item.get_position()
        if position is not None and 0 <= position < len(self._entries):
            # Check if the clicked item is already selected
            is_selected = False
            if hasattr(self._selection_model, "is_selected"):
                try:
                    is_selected = self._selection_model.is_selected(position)
                except Exception:
                    is_selected = False
            
            # If the clicked item is not already selected, clear selection and select only this item
            # If it is already selected, preserve the current selection
            if not is_selected:
                self._selection_model.unselect_all()
                self._selection_model.select_item(position, False)
            else:
                # Ensure the clicked item is selected (should already be, but be safe)
                self._selection_model.select_item(position, False)
            self._selection_anchor = position
        
        # Show context menu at click position
        button = list_item.get_child()
        if button:
            # Convert coordinates to the view widget's coordinate space
            view_widget = self._grid_view
            widget_x, widget_y = button.translate_coordinates(view_widget, x, y)
            if widget_x is not None and widget_y is not None:
                self._show_context_menu(view_widget, widget_x, widget_y)
            else:
                # Fallback: use the button coordinates
                self._show_context_menu(button, x, y)

    def _add_context_controller(self, widget: Gtk.Widget) -> None:
        gesture = Gtk.GestureClick()
        gesture.set_button(Gdk.BUTTON_SECONDARY)

        def _on_pressed(_gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
            # Check if click is on an item or empty space
            # If on empty space, clear selection before showing menu
            if self._is_click_on_empty_space(widget, x, y):
                self._selection_model.unselect_all()
                self._selection_anchor = None
            self._show_context_menu(widget, x, y)

        gesture.connect("pressed", _on_pressed)
        widget.add_controller(gesture)

        long_press = Gtk.GestureLongPress()

        def _on_long_press(_gesture: Gtk.GestureLongPress, x: float, y: float) -> None:
            # Check if click is on an item or empty space
            if self._is_click_on_empty_space(widget, x, y):
                self._selection_model.unselect_all()
                self._selection_anchor = None
            self._show_context_menu(widget, x, y)

        long_press.connect("pressed", _on_long_press)
        widget.add_controller(long_press)




    def _show_context_menu(self, widget: Gtk.Widget, x: float, y: float) -> None:
        if getattr(self, '_suppress_next_context_menu', False):
            self._suppress_next_context_menu = False
            return
        # Selection is now handled by item-level gestures or cleared for empty space
        # No need to update selection here
        self._update_menu_state()
        try:
            widget.grab_focus()
        except Exception:
            pass
        
        # Get the listbox from the popover
        listbox = self._menu_popover.get_child()
        if not isinstance(listbox, Gtk.ListBox):
            return
        
        # Clear existing items
        while listbox.get_first_child() is not None:
            listbox.remove(listbox.get_first_child())
        
        # Check if items are selected
        try:
            if not hasattr(self, '_entries') or not self._entries:
                has_selection = False
            else:
                selected_entries = self.get_selected_entries()
                has_selection = len(selected_entries) > 0
        except AttributeError:
            has_selection = False
        
        # Build menu items using Adw.ActionRow (same style as connection list)
        def _add_menu_item(title: str, icon_name: str, action_name: str) -> None:
            row = Adw.ActionRow(title=title)
            # Use our helper function to prefer bundled icons
            from sshpilot import icon_utils
            icon = icon_utils.new_image_from_icon_name(icon_name)
            row.add_prefix(icon)
            row.set_activatable(True)
            def _on_activated(*_):
                try:
                    logger.debug(f"_show_context_menu: Menu item '{title}' (action '{action_name}') activated")
                    # Get the callback and call it directly
                    callback = self._menu_action_callbacks.get(action_name)
                    if callback:
                        logger.debug(f"_show_context_menu: Found callback for '{action_name}', calling directly")
                        callback()
                        logger.debug(f"_show_context_menu: Callback for '{action_name}' completed")
                    else:
                        logger.error(f"_show_context_menu: Callback for '{action_name}' not found. Available callbacks: {list(self._menu_action_callbacks.keys())}")
                        # Fallback: try to activate the action
                        action = self._menu_actions.get(action_name)
                        if action:
                            logger.debug(f"_show_context_menu: Falling back to action.activate() for '{action_name}'")
                            action.activate(None)
                except Exception as e:
                    logger.error(f"_show_context_menu: Failed to execute action '{action_name}': {e}", exc_info=True)
                finally:
                    self._menu_popover.popdown()
            row.connect('activated', _on_activated)
            listbox.append(row)
        
        # Add Download/Upload based on pane type and selection
        if self._is_remote and has_selection:
            _add_menu_item("Download", "document-save-symbolic", "download")
        elif not self._is_remote and has_selection:
            _add_menu_item("Upload…", "document-send-symbolic", "upload")
        
        # Add Edit for any single file (both local and remote)
        if has_selection:
            selected_entries = self.get_selected_entries()
            if len(selected_entries) == 1 and not selected_entries[0].is_dir:
                _add_menu_item("Edit", "text-editor-symbolic", "edit")
        
        # Add clipboard operations if items are selected
        if has_selection:
            _add_menu_item("Copy", "edit-copy-symbolic", "copy")
            _add_menu_item("Cut", "edit-cut-symbolic", "cut")
        
        # Add Paste if clipboard has items
        if getattr(self, "_can_paste", False):
            _add_menu_item("Paste", "edit-paste-symbolic", "paste")
        
        # Add management operations if items are selected
        if has_selection:
            _add_menu_item("Rename…", "document-edit-symbolic", "rename")
            _add_menu_item("Delete", "user-trash-symbolic", "delete")
        
        # Add New Folder / New File only if no items are selected (before Properties)
        if not has_selection:
            _add_menu_item("New Folder", "folder-new-symbolic", "new_folder")
            _add_menu_item("New File", "document-new-symbolic", "new_file")
        
        # Always add Properties (at the end)
        _add_menu_item("Properties…", "document-properties-symbolic", "properties")
        
        # Create a rectangle for the popover positioning
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        
        # Set parent and show popover
        if self._menu_popover.get_parent() != widget:
            self._menu_popover.set_parent(widget)
        
        self._menu_popover.set_pointing_to(rect)
        self._menu_popover.popup()

    def _is_click_on_empty_space(self, widget: Gtk.Widget, x: float, y: float) -> bool:
        """Check if the click is on empty space (not on an item)."""
        # Determine which view is active
        visible_child = self._stack.get_visible_child()
        if visible_child is None:
            return True
        
        # Find the actual view widget (list or grid) in the scrolled window
        view_widget = None
        for child in visible_child:
            if isinstance(child, Gtk.ScrolledWindow):
                scrolled_child = child.get_child()
                if scrolled_child == self._list_view:
                    view_widget = self._list_view
                elif scrolled_child == self._grid_view:
                    view_widget = self._grid_view
                break
        
        if view_widget is None:
            return True
        
        try:
            # Convert coordinates to view widget's coordinate space
            widget_x, widget_y = widget.translate_coordinates(view_widget, x, y)
            if widget_x is None or widget_y is None:
                return True
            
            # Use pick() to find which child widget is at the coordinates
            picked = view_widget.pick(widget_x, widget_y, Gtk.PickFlags.DEFAULT)
            if picked is None:
                return True
            
            # Check if we picked an actual item (not just the view widget itself)
            # For ListView: check if we picked a list item or its child
            if isinstance(view_widget, Gtk.ListView):
                # Walk up the widget tree to see if we hit a list item
                current = picked
                while current and current != view_widget:
                    # If we find a widget that has the drag_position attribute, it's an item
                    if hasattr(current, 'drag_position'):
                        return False
                    # If we find a box that's a list item child, it's an item
                    if isinstance(current, Gtk.Box) and hasattr(current, '_pane_entry'):
                        return False
                    current = current.get_parent()
                # If we only hit the view widget itself, it's empty space
                return picked == view_widget
            
            # For GridView: check if we picked a button (grid item)
            elif isinstance(view_widget, Gtk.GridView):
                # Walk up the widget tree to see if we hit a button
                current = picked
                while current and current != view_widget:
                    # If we find a button with drag_position, it's an item
                    if isinstance(current, Gtk.Button) and hasattr(current, 'drag_position'):
                        return False
                    current = current.get_parent()
                # If we only hit the view widget itself, it's empty space
                return picked == view_widget
            
            return True
        except Exception as e:
            logger.debug(f"Error checking if click is on empty space: {e}")
            return True

    def _get_selected_indices(self) -> List[int]:
        indices: List[int] = []
        total = len(self._entries)
        if hasattr(self._selection_model, "is_selected"):
            for index in range(total):
                try:
                    if self._selection_model.is_selected(index):
                        indices.append(index)
                except AttributeError:
                    break
        else:
            getter = getattr(self._selection_model, "get_selected", None)
            if callable(getter):
                try:
                    selected_index = getter()
                except Exception:
                    selected_index = None
                if isinstance(selected_index, int) and 0 <= selected_index < total:
                    indices.append(selected_index)
        return indices

    def _get_primary_selection_index(self) -> Optional[int]:
        indices = self._get_selected_indices()
        return indices[0] if indices else None

    def get_selected_entries(self) -> List[FileEntry]:
        return [self._entries[index] for index in self._get_selected_indices()]


    def _update_menu_state(self) -> None:
        selected_entries = self.get_selected_entries()
        selection_count = len(selected_entries)
        has_selection = selection_count > 0
        single_selection = selection_count == 1

        def _set_enabled(name: str, enabled: bool) -> None:
            action = self._menu_actions.get(name)
            if action is not None:
                action.set_enabled(enabled)

        def _set_button(name: str, enabled: bool) -> None:
            button = self._action_buttons.get(name)
            if button is not None:
                button.set_sensitive(enabled)


        # For context menu, actions are always enabled since menu items are shown/hidden dynamically
        can_paste = bool(getattr(self, "_can_paste", False))

        _set_enabled("download", self._is_remote and has_selection)
        _set_enabled("upload", (not self._is_remote) and has_selection)
        _set_enabled("copy", has_selection)
        _set_enabled("cut", has_selection)
        _set_enabled("paste", can_paste)
        # Edit is enabled for single file selection (any file type)
        can_edit = single_selection and not selected_entries[0].is_dir if single_selection else False
        
        _set_enabled("edit", can_edit)
        _set_enabled("rename", single_selection)
        _set_enabled("delete", has_selection)
        _set_enabled("properties", single_selection)
        # new_folder is available in context menu only now

        # Action bar buttons still use the old logic
        _set_button("download", self._is_remote and has_selection)
        _set_button("upload", (not self._is_remote) and has_selection)
        _set_button("copy", has_selection)
        _set_button("cut", has_selection)
        _set_button("paste", can_paste)
        _set_button("rename", single_selection)
        _set_button("edit", can_edit)
        
        # Show/hide edit button based on selection (always show for single file)
        edit_button = self._action_buttons.get("edit")
        if edit_button is not None:
            edit_button.set_visible(can_edit)
        _set_button("delete", has_selection)

    def _emit_entry_operation(self, action: str) -> None:
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select at least one item first")
            return
        if action == "rename" and len(entries) != 1:
            self.show_toast("Select a single item to rename")
            return
        payload = {"entries": entries, "directory": self._current_path}
        self.emit("request-operation", action, payload)

    def _emit_paste_operation(self, *, force_move: bool = False) -> None:
        payload = {"directory": self._current_path}
        if force_move:
            payload["force_move"] = True
        self.emit("request-operation", "paste", payload)

    def _shortcut_operation(self, action: str, *, force_move: bool = False) -> bool:
        if action in {"copy", "cut", "rename", "delete"}:
            self._emit_entry_operation(action)
            return True
        if action == "paste":
            self._emit_paste_operation(force_move=force_move)
            return True
        return False

    def set_can_paste(self, can_paste: bool) -> None:
        current = bool(getattr(self, "_can_paste", False))
        if current == can_paste:
            return
        self._can_paste = can_paste
        self._update_menu_state()

    def set_show_hidden(self, show_hidden: bool, *, preserve_selection: bool = True) -> None:
        """Update the hidden file visibility state and refresh entries."""

        self.toolbar.set_show_hidden_state(show_hidden)
        if self._show_hidden == show_hidden:
            return

        self._show_hidden = show_hidden
        self._apply_entry_filter(preserve_selection=preserve_selection)

    def _on_menu_download(self) -> None:
        if not self._is_remote:
            return
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to download first")
            return
        self._on_download_clicked(None)

    def _on_menu_upload(self) -> None:
        if self._is_remote:
            return
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to upload first")
            return
        self._on_upload_clicked(None)


    def get_selected_entry(self) -> Optional[FileEntry]:
        selected_entries = self.get_selected_entries()
        if not selected_entries:
            return None
        return selected_entries[0]

    def set_file_manager_window(self, window: "FileManagerWindow") -> None:
        """Associate this pane with its owning file manager window."""

        self._window = window

    def _get_file_manager_window(self) -> Optional["FileManagerWindow"]:
        """Return the controlling FileManagerWindow if available."""

        window = getattr(self, "_window", None)
        if window is not None:
            return window

        root = self.get_root()
        if isinstance(root, _file_manager_window_cls()):
            self._window = root
            return root

        # When embedded as a tab, traverse up the widget tree to find FileManagerWindow
        parent = self.get_parent()
        while parent is not None:
            if isinstance(parent, _file_manager_window_cls()):
                self._window = parent
                return parent
            parent = parent.get_parent()

        return None

    def _on_upload_clicked(self, _button: Gtk.Button) -> None:
        window = self._get_file_manager_window()
        if not isinstance(window, _file_manager_window_cls()):
            self.show_toast("File manager is not available")
            return

        local_pane = getattr(window, "_left_pane", None)
        if not isinstance(local_pane, FilePane):
            self.show_toast("Local pane is unavailable")
            return

        destination_pane: Optional[FilePane]
        if self._is_remote:
            destination_pane = self
        else:
            destination_pane = getattr(window, "_right_pane", None)
            if not isinstance(destination_pane, FilePane) or not destination_pane._is_remote:
                destination_pane = None

        if destination_pane is None:
            self.show_toast("Remote pane is unavailable")
            return

        entries = local_pane.get_selected_entries()
        if not entries:
            self.show_toast("Select items in the local pane to upload")
            return

        # Use the actual current path instead of the display path from path entry
        # This handles Flatpak portal paths correctly
        base_dir = getattr(local_pane, '_current_path', None)
        if not base_dir:
            # Fallback to normalized path entry text for non-portal paths
            base_dir = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        source_paths = [pathlib.Path(os.path.join(base_dir, entry.name)) for entry in entries]

        destination = destination_pane.toolbar.path_entry.get_text() or "/"
        payload = {"paths": source_paths, "destination": destination}
        self.emit("request-operation", "upload", payload)
        if len(entries) == 1:
            self.show_toast(f"Uploading {entries[0].name}…")
        else:
            self.show_toast(f"Uploading {len(entries)} items…")


    def _on_download_clicked(self, _button: Gtk.Button) -> None:
        entries = self.get_selected_entries()
        if not entries:
            self.show_toast("Select items to download")
            return

        window = self._get_file_manager_window()
        if not isinstance(window, _file_manager_window_cls()):
            self.show_toast("File manager is not available")
            return

        local_pane = getattr(window, "_left_pane", None)
        if local_pane is None:
            self.show_toast("Local pane is unavailable")
            return

        # Use the actual current path instead of the display path from path entry
        # This handles Flatpak portal paths correctly
        destination_root = getattr(local_pane, '_current_path', None)
        if not destination_root:
            # Fallback to normalized path entry text for non-portal paths
            destination_root = window._normalize_local_path(local_pane.toolbar.path_entry.get_text())
        
        if not os.path.isdir(destination_root):
            self.show_toast("Local destination is not accessible")
            return
        payload = {
            "entries": entries,
            "directory": self._current_path,
            "destination": pathlib.Path(destination_root),
        }
        self.emit("request-operation", "download", payload)
        if len(entries) == 1:
            self.show_toast(f"Downloading {entries[0].name}…")
        else:
            self.show_toast(f"Downloading {len(entries)} items…")

    def _on_request_access_clicked(self) -> None:
        """Handle Request Access button click in Flatpak environment."""
        # Create a confirmation dialog
        window = self.get_root()
        dialog = Adw.MessageDialog.new(
            window,
            "Request Folder Access",
            "You are using the app in a sandbox. Please grant access to your home folder to use the File Manager."
        )
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("ok", "OK")
        dialog.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("ok")
        dialog.set_close_response("cancel")
        
        def on_response(dialog, response):
            if response == "ok":
                self._show_folder_picker()
        
        dialog.connect("response", on_response)
        dialog.present()

    def _hide_request_access_button(self) -> None:
        """Hide the Request Access button after access has been granted.
        In Flatpak, always keep the button visible as requested."""
        if hasattr(self, '_request_access_button') and self._request_access_button:
            # Don't hide the button in Flatpak - always keep it visible
            if not is_flatpak():
                self._request_access_button.set_visible(False)
                logger.debug("Hid Request Access button after granting access")
            else:
                logger.debug("Keeping Request Access button visible in Flatpak")

    def _show_folder_picker(self) -> None:
        """Show a portal-aware folder picker for Flatpak with persistent access."""
        dlg = Gtk.FileChooserNative(
            title="Select Folder to Grant Access",
            action=Gtk.FileChooserAction.SELECT_FOLDER,
            transient_for=self.get_root(),
            modal=True,
        )
        
        def _resp(_dlg, resp):
            if resp == Gtk.ResponseType.ACCEPT:
                gfile = dlg.get_file()
                if gfile:
                    try:
                        path = gfile.get_path()
                        logger.debug(f"FileChooserNative returned path: {path}")
                        
                        # Grant persistent access via Document portal
                        doc_id = _grant_persistent_access(gfile)
                        if doc_id:
                            _save_doc(path, doc_id)
                            logger.info(f"Persisted access to {path} (ID={doc_id})")
                        else:
                            logger.warning(f"Could not grant persistent access to: {path}")
                        
                        # Switch to it immediately
                        self.toolbar.path_entry.set_text(path)
                        self.toolbar.path_entry.emit("activate")
                        self.show_toast(f"Access granted to: {_pretty_path_for_display(path)}")
                        
                        # Hide the Request Access button since access is now granted
                        self._hide_request_access_button()
                    except Exception as e:
                        logger.warning(f"Failed to persist folder access: {e}")
                        # Still navigate to folder even if persistence fails
                        path = gfile.get_path()
                        if path:
                            self.toolbar.path_entry.set_text(path)
                            self.toolbar.path_entry.emit("activate")
                            self.show_toast(f"Access granted to: {_pretty_path_for_display(path)}")
                            # Hide the Request Access button since access is now granted
                            self._hide_request_access_button()
            dlg.destroy()
        
        dlg.connect("response", _resp)
        dlg.show()

    def restore_persisted_folder(self) -> None:
        """Restore access to a previously granted folder on app launch (Flatpak only)."""
        if not is_flatpak():
            return
            
        logger.debug("Attempting to restore persisted folder...")
        portal_result = _load_first_doc_path()
        if portal_result:
            portal_path, doc_id, entry = portal_result
            logger.debug(f"Found persisted path: {portal_path} (doc_id={doc_id})")
            try:
                # Directly trigger the path change instead of relying on path entry activation
                self.emit("path-changed", portal_path)
                logger.info(f"Restored access to folder: {portal_path}")
            except Exception as e:
                logger.warning(f"Failed to restore folder access: {e}")
        else:
            logger.debug("No persisted path found")

    def _set_current_pathbar_text(self, path: str) -> None:
        """Set the path bar text with human-friendly display formatting."""
        display_path = _pretty_path_for_display(path)
        self.toolbar.path_entry.set_text(display_path)

    @staticmethod
    def _dialog_dismissed(error: GLib.Error) -> bool:
        dialog_error = getattr(Gtk, "DialogError", None)
        if dialog_error is not None and error.matches(dialog_error, dialog_error.DISMISSED):
            return True
        return error.matches(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED)

    def _build_properties_details(self, entry: FileEntry, is_current_directory: bool = False) -> Dict[str, str]:
        base_path = self._current_path or "/"
        if is_current_directory:
            # For current directory, use the base path as location
            location = base_path
        else:
            location = os.path.join(base_path, entry.name)

        entry_type = "Folder" if entry.is_dir else "File"
        if entry.is_dir:
            size_text = "—"
        else:
            size_text = self._format_size(entry.size)

        try:
            modified_dt = datetime.fromtimestamp(entry.modified)
            modified_text = modified_dt.strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, OverflowError, ValueError, TypeError):
            modified_text = "Unknown"

        return {
            "name": entry.name,
            "type": entry_type,
            "size": size_text,
            "modified": modified_text,
            "location": location,
        }

    def _is_text_file(self, entry: FileEntry) -> bool:
        """Check if a file is likely a text file based on name/extension."""
        if entry.is_dir:
            return False
        
        # Check mimetype
        mimetype, _ = mimetypes.guess_type(entry.name)
        if mimetype:
            if mimetype.startswith('text/'):
                return True
            # Also allow common code file types
            text_mimes = [
                'application/json',
                'application/javascript',
                'application/xml',
                'application/x-sh',
                'application/x-python',
            ]
            if mimetype in text_mimes:
                return True
        
        # Check by extension
        _, ext = os.path.splitext(entry.name.lower())
        text_extensions = {
            '.txt', '.md', '.rst', '.log',
            '.py', '.pyw', '.pyx', '.pyi',
            '.js', '.jsx', '.ts', '.tsx',
            '.html', '.htm', '.xhtml', '.xml', '.css', '.scss', '.sass',
            '.json', '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf',
            '.sh', '.bash', '.zsh', '.fish', '.ps1',
            '.c', '.cpp', '.cc', '.cxx', '.h', '.hpp', '.hxx',
            '.java', '.kt', '.scala', '.go', '.rs', '.rb', '.pl', '.pm',
            '.php', '.php3', '.php4', '.php5', '.phtml',
            '.sql', '.lua', '.vim', '.vimrc',
            '.dockerfile', '.makefile', '.cmake',
            '.properties', '.env', '.gitignore', '.gitattributes',
        }
        if ext in text_extensions:
            return True
        
        # Check if filename suggests a text file
        text_patterns = ['readme', 'license', 'changelog', 'authors', 'contributors', 'makefile']
        name_lower = entry.name.lower()
        for pattern in text_patterns:
            if pattern in name_lower:
                return True
        
        return False
    
    def _on_menu_edit(self) -> None:
        """Handle Edit menu action - open file in editor."""
        entry = self.get_selected_entry()
        if entry is None:
            self.show_toast("No file selected")
            return
        
        if entry.is_dir:
            self.show_toast("Cannot edit directories")
            return
        
        # Get file manager window
        window = self._get_file_manager_window()
        if window is None or not isinstance(window, _file_manager_window_cls()):
            self.show_toast("Cannot edit file - window not available")
            return
        
        if self._is_remote:
            # Remote file editing
            sftp_manager = getattr(window, '_manager', None)
            if sftp_manager is None:
                self.show_toast("Cannot edit file - connection not available")
                return
            
            # Build remote path
            file_path = posixpath.join(self._current_path or "/", entry.name)
            
            # Create editor window for remote file
            try:
                editor = RemoteFileEditorWindow(
                    parent=window,
                    file_path=file_path,
                    file_name=entry.name,
                    is_local=False,
                    sftp_manager=sftp_manager,
                    file_manager_window=window,
                )
                editor.present()
            except Exception as e:
                logger.error(f"Failed to open editor: {e}", exc_info=True)
                self.show_toast(f"Failed to open editor: {e}")
        else:
            # Local file editing
            file_path = os.path.join(self._current_path or os.path.expanduser("~"), entry.name)
            file_path = os.path.abspath(os.path.expanduser(file_path))
            
            # Create editor window for local file
            try:
                editor = RemoteFileEditorWindow(
                    parent=window,
                    file_path=file_path,
                    file_name=entry.name,
                    is_local=True,
                    sftp_manager=None,
                    file_manager_window=window,
                )
                editor.present()
            except Exception as e:
                logger.error(f"Failed to open editor: {e}", exc_info=True)
                self.show_toast(f"Failed to open editor: {e}")

    def _on_menu_properties(self) -> None:
        entry = self.get_selected_entry()
        if entry is None:
            # No item selected - show properties for current directory
            current_path = self._current_path or "/"
            logger.debug(f"_on_menu_properties: No selection, showing properties for current directory: {current_path}")
            
            # Get directory name and parent path
            # PropertiesDialog expects entry.name and current_path where entry is located
            if current_path == "/":
                # Special case for root directory
                dir_name = "/"
                parent_path = "/"
            else:
                # Normalize path (remove trailing slash)
                normalized_path = current_path.rstrip("/")
                dir_name = os.path.basename(normalized_path) or normalized_path
                parent_path = os.path.dirname(normalized_path) or "/"
                # If parent_path is empty after dirname, use "/"
                if not parent_path:
                    parent_path = "/"
            
            logger.debug(f"_on_menu_properties: dir_name={dir_name}, parent_path={parent_path}, is_remote={self._is_remote}")
            
            # Create a FileEntry for the current directory
            try:
                if self._is_remote:
                    # For remote, create a basic entry with item count
                    if hasattr(self, '_entries') and self._entries is not None:
                        item_count = len(self._entries)
                    else:
                        item_count = None
                    logger.debug(f"_on_menu_properties: Creating remote directory entry with item_count={item_count}")
                    entry = FileEntry(
                        name=dir_name,
                        is_dir=True,
                        size=0,
                        modified=0.0,
                        item_count=item_count
                    )
                else:
                    # For local, get actual directory stats
                    if os.path.isdir(current_path):
                        stat_info = os.stat(current_path)
                        # Count items in directory
                        try:
                            item_count = len(list(os.scandir(current_path)))
                        except Exception:
                            item_count = None
                        
                        entry = FileEntry(
                            name=dir_name,
                            is_dir=True,
                            size=0,  # Directories don't have a meaningful size
                            modified=stat_info.st_mtime,
                            item_count=item_count
                        )
                        logger.debug(f"_on_menu_properties: Created local directory entry with modified={stat_info.st_mtime}, item_count={item_count}")
                    else:
                        logger.warning(f"_on_menu_properties: Current directory is not accessible: {current_path}")
                        self.show_toast("Current directory is not accessible")
                        return
            except Exception as e:
                logger.error(f"Error creating directory entry for properties: {e}", exc_info=True)
                self.show_toast("Unable to get directory properties")
                return
            
            # Mark that this is the current directory
            # Use parent path for PropertiesDialog so it can construct the full path correctly
            is_current_dir = True
            properties_path = parent_path
            logger.debug(f"_on_menu_properties: Using properties_path={properties_path} for current directory")
        else:
            is_current_dir = False
            properties_path = None
            logger.debug(f"_on_menu_properties: Showing properties for selected entry: {entry.name}")
        
        try:
            details = self._build_properties_details(entry, is_current_directory=is_current_dir)
            logger.debug(f"_on_menu_properties: Built properties details: {details}")
            self._show_properties_dialog(entry, details, properties_path=properties_path)
        except Exception as e:
            logger.error(f"Error showing properties dialog: {e}", exc_info=True)
            self.show_toast(f"Failed to show properties: {e}")

    def _show_properties_dialog(self, entry: FileEntry, details: Dict[str, str], properties_path: Optional[str] = None) -> None:
        """Show modern properties dialog.
        
        Args:
            entry: The file entry to show properties for
            details: Properties details dictionary
            properties_path: Optional path to use instead of self._current_path (for current directory)
        """
        window = self.get_root()
        if window is None:
            logger.error("FilePane: Cannot show properties dialog - window is None")
            self.show_toast("Cannot show properties - window not available")
            return
        
        try:
            # Get SFTP manager if this is a remote pane
            # Use _get_file_manager_window() to find the FileManagerWindow even when embedded as a tab
            sftp_manager = None
            if self._is_remote:
                file_manager_window = self._get_file_manager_window()
                if file_manager_window is not None:
                    sftp_manager = getattr(file_manager_window, '_manager', None)
                    logger.debug(f"FilePane: Getting SFTP manager for properties dialog: is_remote={self._is_remote}, file_manager_window={file_manager_window}, manager={sftp_manager}")
                else:
                    logger.debug(f"FilePane: Could not find FileManagerWindow for remote pane")
            else:
                logger.debug(f"FilePane: Not getting SFTP manager: is_remote={self._is_remote}")
            
            # Use provided path or fall back to current path
            path_for_dialog = properties_path if properties_path is not None else self._current_path
            
            logger.debug(f"FilePane: Creating PropertiesDialog with entry.name={entry.name}, path={path_for_dialog}, is_remote={self._is_remote}")
            
            # Create and show the modern properties dialog
            dialog = PropertiesDialog(entry, path_for_dialog, window, sftp_manager)
            logger.debug(f"FilePane: Created PropertiesDialog with sftp_manager={sftp_manager}, path={path_for_dialog}")
            dialog.present()
            logger.debug(f"FilePane: PropertiesDialog presented successfully")
        except Exception as e:
            logger.error(f"FilePane: Failed to show properties dialog: {e}", exc_info=True)
            # Fallback to simple message dialog if modern dialog fails
            try:
                self._show_fallback_properties_dialog(entry, details, window)
            except Exception as fallback_error:
                logger.error(f"FilePane: Fallback properties dialog also failed: {fallback_error}", exc_info=True)
                self.show_toast(f"Failed to show properties: {e}")

    def _show_fallback_properties_dialog(self, entry: FileEntry, details: Dict[str, str], window: Gtk.Window) -> None:
        """Fallback to simple properties dialog if modern dialog fails."""
        heading = f"{entry.name} Properties" if entry.name else "Properties"
        body_lines = [
            f"Name: {details['name']}",
            f"Type: {details['type']}",
            f"Size: {details['size']}",
            f"Modified: {details['modified']}",
            f"Location: {details['location']}",
        ]
        body_text = "\n".join(body_lines)

        try:
            dialog = Adw.MessageDialog(
                transient_for=window,
                modal=True,
                heading=heading,
                body=body_text
            )
            dialog.add_response("ok", "OK")
            dialog.set_default_response("ok")
            dialog.connect("response", lambda d, *_: d.destroy())
            dialog.present()
        except Exception:
            # Final fallback to basic Gtk dialog
            dialog = Gtk.MessageDialog(
                transient_for=window,
                modal=True,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text=heading,
                secondary_text=body_text
            )
            dialog.connect("response", lambda d, *_: d.destroy())
            dialog.present()

    # -- public API -----------------------------------------------------

    def show_entries(self, path: str, entries: Iterable[FileEntry]) -> None:
        entries_list = list(entries)
        pane_type = "remote" if self._is_remote else "local"
        logger.debug(f"FilePane.show_entries: {pane_type} pane updating with {len(entries_list)} entries for path {path}")
        
        self._current_path = path
        self._set_current_pathbar_text(path)
        self._cached_entries = entries_list
        self._apply_entry_filter(preserve_selection=False)
        
        logger.debug(f"FilePane.show_entries: {pane_type} pane update completed")

    def highlight_entry(self, name: str) -> None:
        if not name:
            return
        match: Optional[int] = None
        for index, entry in enumerate(self._entries):
            if entry.name == name:
                match = index
                break
        if match is None:
            return
        self._selection_model.unselect_all()
        self._selection_anchor = None
        self._selection_model.select_item(match, False)
        self._selection_anchor = match
        self._scroll_to_position(match)

    def _apply_entry_filter(self, *, preserve_selection: bool) -> None:
        selected_names: set[str] = set()
        if preserve_selection:
            for entry in self.get_selected_entries():
                selected_names.add(entry.name)

        # Filter for hidden files and store as raw entries
        self._raw_entries = [
            entry
            for entry in self._cached_entries
            if self._show_hidden or not entry.name.startswith(".")
        ]

        # Apply sorting to get final entries
        self._entries = self._sort_entries(self._raw_entries)
        
        # Update the list store
        self._list_store.remove_all()
        restored_selection: List[int] = []
        for idx, entry in enumerate(self._entries):
            suffix = "/" if entry.is_dir else ""
            self._list_store.append(Gtk.StringObject.new(entry.name + suffix))
            if preserve_selection and entry.name in selected_names:
                restored_selection.append(idx)

        self._selection_model.unselect_all()
        self._selection_anchor = None
        for index in restored_selection:
            self._selection_model.select_item(index, False)
        if restored_selection:
            self._selection_anchor = restored_selection[-1]

        self._update_menu_state()



    # -- navigation helpers --------------------------------------------

    def _navigate_to_entry(self, position: Optional[int]) -> None:
        if position is None or not (0 <= position < len(self._entries)):
            return

        try:
            entry = self._entries[position]
        except IndexError:
            return

        if not getattr(entry, "is_dir", False):
            return

        base_path = self._current_path or ""
        target_path = os.path.join(base_path, entry.name)
        self.emit("path-changed", target_path)

    def _on_list_activate(self, _list_view: Gtk.ListView, position: int) -> None:
        self._navigate_to_entry(position)

    def _sort_entries(self, entries: Iterable[FileEntry]) -> List[FileEntry]:
        def key_func(item: FileEntry):
            if self._sort_key == "size":
                return item.size
            if self._sort_key == "modified":
                return item.modified
            return item.name.casefold()

        dirs = [entry for entry in entries if entry.is_dir]
        files = [entry for entry in entries if not entry.is_dir]

        dirs_sorted = sorted(dirs, key=key_func, reverse=self._sort_descending)
        files_sorted = sorted(files, key=key_func, reverse=self._sort_descending)
        return dirs_sorted + files_sorted

    def _refresh_sorted_entries(self, *, preserve_selection: bool) -> None:
        # Simply re-apply the filter which now includes sorting
        self._apply_entry_filter(preserve_selection=preserve_selection)

    def _on_grid_activate(self, _grid_view: Gtk.GridView, position: int) -> None:
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            if entry.is_dir:
                self.emit("path-changed", os.path.join(self._current_path, entry.name))

    def _on_drag_prepare(self, drag_source: Gtk.DragSource, x: float, y: float) -> Gdk.ContentProvider:
        """Prepare drag data when drag operation starts."""
        # Get the widget that initiated the drag
        widget = drag_source.get_widget()
        
        # Get the position that was stored during binding
        position = getattr(widget, 'drag_position', None)
        
        logger.debug(f"Drag prepare: position={position}, entries_count={len(self._entries)}")
        
        # Create a JSON representation of the drag data so arbitrary characters
        # in paths or filenames are preserved without relying on delimiter
        # parsing.  Consumers expect the payload under the "payload" key.
        payload_dict: Optional[Dict[str, Any]] = None
        if position is not None and 0 <= position < len(self._entries):
            entry = self._entries[position]
            entry_path = os.path.join(self._current_path or "", entry.name)
            payload_dict = {
                "pane_id": id(self),
                "path": self._current_path,
                "position": position,
                "entry_name": entry.name,
                "entry_path": entry_path,
            }

        drag_data = {
            "format": "sshpilot_drag",
            "payload": payload_dict,
        }
        drag_data_string = json.dumps(drag_data, separators=(",", ":"), sort_keys=True)

        return Gdk.ContentProvider.new_for_value(drag_data_string)

    def _on_drag_begin(self, drag_source: Gtk.DragSource, drag: Gdk.Drag) -> None:
        """Called when drag operation begins - set drag icon."""
        logger.debug(f"Drag begin: pane={self._is_remote}")
        if is_macos():
            # macOS provides its own drag preview; avoid setting a custom icon.
            return
        # Create a simple icon for the drag operation
        widget = drag_source.get_widget()
        if widget:
            # Create a paintable from the widget to use as drag icon
            paintable = Gtk.WidgetPaintable.new(widget)
            drag_source.set_icon(paintable, 0, 0)

    def _on_drag_end(self, drag_source: Gtk.DragSource, drag: Gdk.Drag, delete_data: bool) -> None:
        """Called when drag operation ends."""
        # Clean up any drag-related state if needed
        pass

    def _on_drop_string(self, drop_target: Gtk.DropTarget, value: str, x: float, y: float) -> bool:
        """Handle dropped files from string data."""
        logger.debug(f"Drop received: value={value}")

        if not isinstance(value, str):
            logger.debug("Drop rejected: non-string drag data")
            return False

        try:
            container = json.loads(value)
        except json.JSONDecodeError as exc:
            logger.debug("Drop rejected: invalid JSON drag data (%s)", exc)
            return False

        if not isinstance(container, dict):
            logger.debug("Drop rejected: drag container is not a mapping")
            return False

        if container.get("format") != "sshpilot_drag":
            logger.debug("Drop rejected: unexpected drag format")
            return False

        payload = container.get("payload")
        if not isinstance(payload, dict):
            logger.debug("Drop rejected: payload missing or invalid")
            return False

        missing_keys = [key for key in ("pane_id", "entry_name") if key not in payload]
        if missing_keys:
            logger.debug("Drop rejected: payload missing keys %s", missing_keys)
            return False

        try:
            source_pane_id = int(payload.get("pane_id"))
        except (TypeError, ValueError):
            logger.debug("Drop rejected: missing source pane id")
            return False

        position_raw = payload.get("position")
        try:
            position = int(position_raw)
        except (TypeError, ValueError):
            position = -1

        entry_name = payload.get("entry_name")
        if not isinstance(entry_name, str):
            logger.debug("Drop rejected: invalid entry name in payload")
            return False

        source_path = payload.get("path")
        if source_path is not None and not isinstance(source_path, str):
            logger.debug("Drop rejected: invalid source path in payload")
            return False

        stored_entry_path = payload.get("entry_path")
        if stored_entry_path is not None and not isinstance(stored_entry_path, str):
            stored_entry_path = None

        # Find the source pane by ID
        source_pane = None
        window = self._get_file_manager_window()
        if isinstance(window, _file_manager_window_cls()):
            if id(window._left_pane) == source_pane_id:
                source_pane = window._left_pane
            elif id(window._right_pane) == source_pane_id:
                source_pane = window._right_pane
        
        if source_pane is None:
            logger.debug("Drop rejected: source pane not found")
            return False
            
        logger.debug(
            "Drop data: source_pane=%s, target_pane=%s, position=%s",
            source_pane._is_remote,
            self._is_remote,
            position,
        )

        # Don't allow dropping on the same pane
        if source_pane == self:
            logger.debug("Drop rejected: same pane")
            return False

        def _normalise_path(path: Optional[str]) -> Optional[str]:
            if not isinstance(path, str):
                return None
            if path == "":
                return ""
            return os.path.normpath(path)

        expected_source_path = source_path
        if expected_source_path is None and stored_entry_path:
            expected_source_path = os.path.dirname(stored_entry_path)

        current_source_path = getattr(source_pane, "_current_path", None)
        expected_source_path_norm = _normalise_path(expected_source_path)
        current_source_path_norm = _normalise_path(current_source_path)

        if (
            expected_source_path_norm is not None
            and current_source_path_norm is not None
            and expected_source_path_norm != current_source_path_norm
        ):
            logger.debug(
                "Drop rejected: source path changed (expected=%s, current=%s)",
                expected_source_path_norm,
                current_source_path_norm,
            )
            self.show_toast("Dragged item is no longer available")
            return False

        entry = next(
            (item for item in getattr(source_pane, "_entries", []) if item.name == entry_name),
            None,
        )
        if entry is None:
            logger.debug("Drop rejected: entry %s not found in source pane", entry_name)
            self.show_toast("Dragged item is no longer available")
            return False

        current_entry_path = os.path.join(current_source_path or "", entry.name)
        stored_entry_path_norm = _normalise_path(stored_entry_path)
        current_entry_path_norm = _normalise_path(current_entry_path)
        if (
            stored_entry_path_norm is not None
            and current_entry_path_norm is not None
            and stored_entry_path_norm != current_entry_path_norm
        ):
            logger.debug(
                "Drop rejected: entry path changed (expected=%s, current=%s)",
                stored_entry_path_norm,
                current_entry_path_norm,
            )
            self.show_toast("Dragged item is no longer available")
            return False

        if stored_entry_path is not None:
            source_file_path = stored_entry_path
        elif expected_source_path is not None:
            source_file_path = os.path.join(expected_source_path, entry.name)
        else:
            source_file_path = current_entry_path

        logger.debug(f"Drop operation: {entry.name} from {source_file_path}")

        # If the user dropped onto a folder in this pane, route the file INTO
        # that folder rather than into the pane's current directory.
        target_folder = self._resolve_drop_target_folder(x, y)
        if target_folder is not None:
            logger.debug(
                "Drop targeted folder: %s (within %s)",
                target_folder.name, self._current_path,
            )

        # Determine operation type based on source and target panes
        if self._is_remote and not source_pane._is_remote:
            # Local to remote - upload
            logger.debug("Starting upload operation")
            self._handle_upload_from_drag(source_file_path, entry, target_folder)
        elif not self._is_remote and source_pane._is_remote:
            # Remote to local - download
            logger.debug("Starting download operation")
            self._handle_download_from_drag(source_file_path, entry, target_folder)
        else:
            # Same type of pane - not supported for now
            logger.debug("Drop rejected: same pane type")
            return False

        return True

    def _handle_upload_from_drag(self, source_path: str, entry: FileEntry,
                                  target_folder: Optional[FileEntry] = None) -> None:
        """Handle upload operation from drag and drop.

        When *target_folder* is supplied, the file is uploaded INTO that
        folder rather than into the pane's current directory.
        """
        try:
            import pathlib
            source_path_obj = pathlib.Path(source_path)
            # Build destination — drop-on-folder means destination parent is
            # current_path/target_folder, not current_path itself.
            dest_parent = self._current_path
            if target_folder is not None:
                dest_parent = posixpath.join(self._current_path, target_folder.name)
            destination_path = posixpath.join(dest_parent, entry.name)

            # Get the file manager window to access the SFTP manager
            window = self._get_file_manager_window()
            if not isinstance(window, _file_manager_window_cls()):
                self.show_toast("Upload failed: Invalid window context")
                return

            manager = window._manager

            # Check for file conflicts first
            files_to_transfer = [(str(source_path_obj), destination_path)]

            def _proceed_with_upload(resolved_files: List[Tuple[str, str]]) -> None:
                for local_path_str, dest_path in resolved_files:
                    path_obj = pathlib.Path(local_path_str)

                    if entry.is_dir:
                        # Upload directory
                        future = manager.upload_directory(path_obj, dest_path)
                    else:
                        # Upload file
                        future = manager.upload(path_obj, dest_path)

                    # Show progress dialog for upload
                    window._show_progress_dialog(
                        "upload", entry.name, future,
                        source_path=str(path_obj),
                        destination_path=dest_path,
                    )
                    # Don't try to highlight the dropped file if it landed in
                    # a subfolder — it won't appear in the current listing.
                    window._attach_refresh(
                        future,
                        refresh_remote=self,
                        highlight_name=None if target_folder is not None else entry.name,
                    )

            window._check_file_conflicts(files_to_transfer, "upload", _proceed_with_upload)

        except Exception as e:
            self.show_toast(f"Upload failed: {str(e)}")

    def _handle_download_from_drag(self, source_path: str, entry: FileEntry,
                                    target_folder: Optional[FileEntry] = None) -> None:
        """Handle download operation from drag and drop.

        When *target_folder* is supplied, the file is downloaded INTO that
        folder rather than into the pane's current directory.
        """
        try:
            import pathlib
            dest_parent = pathlib.Path(self._current_path)
            if target_folder is not None:
                dest_parent = dest_parent / target_folder.name
            destination_path = dest_parent / entry.name

            # Get the file manager window to access the SFTP manager
            window = self._get_file_manager_window()
            if not isinstance(window, _file_manager_window_cls()):
                self.show_toast("Download failed: Invalid window context")
                return

            manager = window._manager

            # Check for file conflicts first
            files_to_transfer = [(source_path, str(destination_path))]

            def _proceed_with_download(resolved_files: List[Tuple[str, str]]) -> None:
                for source, target_path_str in resolved_files:
                    target_path = pathlib.Path(target_path_str)

                    if entry.is_dir:
                        # Download directory
                        future = manager.download_directory(source, target_path)
                    else:
                        # Download file
                        future = manager.download(source, target_path)

                    # Show progress dialog for download
                    window._show_progress_dialog(
                        "download", entry.name, future,
                        source_path=source,
                        destination_path=str(target_path),
                    )
                    # Skip highlight when target was a subfolder.
                    window._attach_refresh(
                        future,
                        refresh_local_path=str(self._current_path),
                        highlight_name=None if target_folder is not None else entry.name,
                    )

            window._check_file_conflicts(files_to_transfer, "download", _proceed_with_download)

        except Exception as e:
            self.show_toast(f"Download failed: {str(e)}")

    def _on_drop_enter(self, drop_target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        """Called when drag enters drop target."""
        logger.debug(f"Drop enter: pane={self._is_remote}")
        # Add visual feedback - could highlight the drop area
        self.add_css_class("drop-target-active")
        return Gdk.DragAction.COPY

    def _on_drop_leave(self, drop_target: Gtk.DropTarget) -> None:
        """Called when drag leaves drop target."""
        logger.debug(f"Drop leave: pane={self._is_remote}")
        # Remove visual feedback
        self.remove_css_class("drop-target-active")

    def _resolve_drop_target_folder(self, x: float, y: float) -> Optional[FileEntry]:
        """Hit-test (x, y) and return the folder entry under the cursor, if any.

        Returns ``None`` when the cursor isn't over a directory row — callers
        then drop into the pane's current path. Walks up from the picked
        leaf widget looking for the per-row ``drag_position`` attribute set
        by ``_on_list_bind`` / ``_on_grid_bind``; that gives us an index into
        ``self._entries`` so we can check ``is_dir``.
        """
        try:
            picked = self.pick(x, y, Gtk.PickFlags.DEFAULT)
        except Exception:
            return None
        widget = picked
        entries = getattr(self, "_entries", None) or []
        while widget is not None and widget is not self:
            position = getattr(widget, "drag_position", None)
            if isinstance(position, int) and 0 <= position < len(entries):
                entry = entries[position]
                # If they dropped on a file (not a folder), explicitly return
                # None — the caller falls back to the current path rather
                # than producing a nonsensical "copy into a file" attempt.
                return entry if entry.is_dir else None
            widget = widget.get_parent()
        return None

    def _on_up_clicked(self, _button) -> None:
        parent = os.path.dirname(self._current_path.rstrip('/')) or '/'
        # Avoid navigating past root repeatedly
        if parent != self._current_path:
            self.emit("path-changed", parent)

    def _on_back_clicked(self, _button) -> None:
        prev = self.pop_history()
        if prev:
            # Suppress history push for back navigation
            self._suppress_history_push = True
            self.emit("path-changed", prev)

    def _on_refresh_clicked(self, _button) -> None:
        # Refresh the current directory
        current_path = self._current_path or "/"
        self.emit("path-changed", current_path)

    def push_history(self, path: str) -> None:
        if self._history and self._history[-1] == path:
            return
        self._history.append(path)

    def pop_history(self) -> Optional[str]:
        if len(self._history) > 1:
            self._history.pop()
            return self._history[-1]
        return None

    def show_toast(self, text: str, timeout: int = -1) -> None:
        """Show a toast message safely."""
        try:
            # Dismiss any existing toast first
            if self._current_toast:
                self._current_toast.dismiss()
                self._current_toast = None
            
            toast = Adw.Toast.new(text)
            if timeout >= 0:
                toast.set_timeout(timeout)
            self._overlay.add_toast(toast)
            self._current_toast = toast  # Keep reference for dismissal
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    def dismiss_toasts(self) -> None:
        """Dismiss all toasts from the overlay."""
        try:
            # Dismiss the current toast if it exists
            if self._current_toast:
                self._current_toast.dismiss()
                self._current_toast = None
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass

    # -- type-ahead search ----------------------------------------------

    def _current_time(self) -> float:
        getter = getattr(GLib, "get_monotonic_time", None)
        if callable(getter):
            try:
                return getter() / 1_000_000
            except Exception:
                pass
        return time.monotonic()

    def _find_prefix_match(self, prefix: str, start_index: int) -> Optional[int]:
        if not prefix or not self._entries:
            return None

        total = len(self._entries)
        if total <= 0:
            return None

        start = 0 if start_index is None else start_index
        if start < 0:
            start = 0

        prefix_casefold = prefix.casefold()
        for offset in range(total):
            index = (start + offset) % total
            if self._entries[index].name.casefold().startswith(prefix_casefold):
                return index
        return None

    def _scroll_to_position(self, position: int) -> None:
        visible = self._stack.get_visible_child_name()
        view: Optional[Gtk.Widget] = None
        if visible == "list":
            view = self._list_view
        elif visible == "grid":
            view = self._grid_view

        if view is None:
            return

        scroll_to = getattr(view, "scroll_to", None)
        if callable(scroll_to):
            flags = getattr(Gtk, "ListScrollFlags", None)
            focus_flag = getattr(flags, "FOCUS", 1) if flags is not None else 1
            try:
                scroll_to(position, focus_flag)
            except Exception:
                pass

    def _on_typeahead_key_pressed(
        self,
        _controller: Gtk.EventControllerKey,
        keyval: int,
        _keycode: int,
        state: Gdk.ModifierType,
    ) -> bool:
        if not self._entries:
            return False

        if state & (
            Gdk.ModifierType.CONTROL_MASK
            | Gdk.ModifierType.ALT_MASK
            | getattr(Gdk.ModifierType, "ALT_MASK", 0)
            | getattr(Gdk.ModifierType, "SUPER_MASK", 0)
        ):
            return False

        char_code = Gdk.keyval_to_unicode(keyval)
        if not char_code:
            return False

        char = chr(char_code)
        if not char or not char.isprintable():
            return False

        now = self._current_time()
        if now - self._typeahead_last_time > self._TYPEAHEAD_TIMEOUT:
            self._typeahead_buffer = ""

        self._typeahead_last_time = now

        repeat_cycle = (
            bool(self._typeahead_buffer)
            and len(self._typeahead_buffer) == 1
            and char.casefold() == self._typeahead_buffer.casefold()
        )

        selected = self._get_primary_selection_index()
        if selected is None or selected < 0:
            selected_index = 0
        else:
            selected_index = selected

        start_index = selected_index
        match: Optional[int] = None
        prefix: Optional[str] = None

        if repeat_cycle:
            candidate = self._typeahead_buffer + char
            match = self._find_prefix_match(candidate, start_index)
            if match is not None:
                self._typeahead_buffer = candidate
            else:
                start_index += 1
                prefix = self._typeahead_buffer
        else:
            self._typeahead_buffer += char
            prefix = self._typeahead_buffer

        if match is None and prefix is not None:
            match = self._find_prefix_match(prefix, start_index)

        if match is None and not repeat_cycle:
            self._typeahead_buffer = char
            match = self._find_prefix_match(self._typeahead_buffer, selected_index)

        if match is None:
            return False

        setter = getattr(self._selection_model, "select_item", None)
        if callable(setter):
            setter(match, True)
        else:
            fallback = getattr(self._selection_model, "set_selected", None)
            if callable(fallback):
                fallback(match)

        self._scroll_to_position(match)
        return True
