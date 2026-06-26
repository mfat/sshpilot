"""Text editor window for editing remote and local files.

This module provides a text editor window that supports both remote (SFTP) and
local file editing with syntax highlighting, search/replace, and undo/redo
functionality.
"""

from __future__ import annotations

import os
import pathlib
import re
import shlex
import tempfile
from concurrent.futures import Future
from typing import Any, Callable, TYPE_CHECKING, Optional

from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk, Pango

from .platform_utils import is_macos

# Try to import GtkSourceView for syntax highlighting
try:
    import gi
    gi.require_version('GtkSource', '5')
    from gi.repository import GtkSource
    _HAS_GTKSOURCE = True
except (ImportError, ValueError, AttributeError):
    _HAS_GTKSOURCE = False
    GtkSource = None

import logging

if TYPE_CHECKING:
    from .file_manager_window import FileManagerWindow

logger = logging.getLogger(__name__)


_BUNDLED_LANG_PATH = "resource:///io/github/mfat/sshpilot/language-specs/"
_lang_manager = None


def _get_language_manager():
    """Return a private GtkSourceView LanguageManager that includes sshPilot's
    bundled language-specs (e.g. the custom ``sshconfig`` definition).

    A fresh manager is used rather than ``get_default()`` because GtkSourceView
    only permits ``set_search_path()`` before a manager has been queried — the
    shared default manager is used elsewhere, so mutating it triggers a
    ``lm->ids == NULL`` CRITICAL. A fresh instance still carries the built-in
    search paths, so all bundled languages keep working. Cached (configured once
    before first use)."""
    global _lang_manager
    if _lang_manager is None and GtkSource is not None:
        lm = GtkSource.LanguageManager()
        try:
            paths = list(lm.get_search_path() or [])
            if _BUNDLED_LANG_PATH not in paths:
                lm.set_search_path([_BUNDLED_LANG_PATH] + paths)
        except Exception as e:  # noqa: BLE001 — highlighting is non-essential
            logger.debug("Could not register bundled language-specs: %s", e)
        _lang_manager = lm
    return _lang_manager


_OUTLINE_RE = re.compile(r'^[ \t]*(Host|Match)\b(.*)$', re.IGNORECASE)


def parse_ssh_config_outline(text: str):
    """Return ``[(line_index, kind, label)]`` for each ``Host``/``Match`` header
    in *text* (0-based line index). Comments (``# Host …``) and value keywords
    (``HostName``) are not matched."""
    out = []
    for i, line in enumerate((text or "").splitlines()):
        m = _OUTLINE_RE.match(line)
        if not m:
            continue
        kind = m.group(1).lower()
        rest = (m.group(2) or "").strip()
        label = rest if rest else m.group(1)
        out.append((i, kind, label))
    return out


def prettify_path(path: str, home: Optional[str]) -> str:
    """Collapse a leading home directory to ``~`` for display.

    ``home`` should be the same home the app uses for paths (GLib.get_home_dir()),
    so the collapse is consistent inside and outside a Flatpak sandbox. Paths not
    under ``home`` (and remote paths) are returned unchanged.
    """
    if not path or not home:
        return path or ""
    if path == home:
        return "~"
    if path.startswith(home + os.sep):
        return "~" + path[len(home):]
    return path


class RemoteFileEditorWindow(Adw.Window):
    """FileZilla-style text editor window using GTK SourceView.
    
    Supports both remote (SFTP) and local file editing:
    - Remote: Downloads to temp location, edits, uploads back when saved
    - Local: Edits file directly, saves locally
    """
    
    def __init__(
        self,
        parent: Adw.Window,
        file_path: str,
        file_name: str,
        is_local: bool = False,
        sftp_manager: Optional[Any] = None,
        file_manager_window: Optional["FileManagerWindow"] = None,
        pre_save_validator: Optional[Callable[[str], Optional[str]]] = None,
        language_id: Optional[str] = None,
        show_outline: bool = False,
    ) -> None:
        super().__init__()
        self.set_transient_for(parent)
        self.set_modal(False)  # Allow multiple editors
        self.set_default_size(900, 600)
        self.set_title(f"Edit {file_name}")
        
        self._is_local = is_local
        self._file_path = file_path  # Can be remote path or local path
        self._file_name = file_name
        self._title_text = file_name  # overridable base title (see set_editor_title)
        self._sftp_manager = sftp_manager
        self._file_manager_window = file_manager_window
        # Optional pre-save check (e.g. `ssh -G` for the SSH config). Returns an
        # error string to block the save, or None to allow it.
        self._pre_save_validator = pre_save_validator
        # Optional GtkSourceView language id (e.g. 'sshconfig') + a Host/Match
        # outline sidebar — used by the SSH config editor only.
        self._language_id = language_id
        self._show_outline = show_outline
        self._outline_listbox = None
        self._outline_scroller = None
        self._outline_rows = []  # parallel list of (line_index, kind, label)
        self._outline_refresh_id = 0
        # Style-scheme selector + sidebar toggle state (lazy app config).
        self._app_config = None
        self._scheme_button = None
        self._scheme_chooser = None
        self._sidebar_toggle = None
        self._dark_handler_id = 0
        self._temp_file: Optional[pathlib.Path] = None
        self._file_monitor: Optional[Gio.FileMonitor] = None
        self._file_modified_time: float = 0.0
        self._has_unsaved_changes = False
        self._upload_dialog: Optional[Adw.AlertDialog] = None
        self._is_closing = False
        self._is_loading = True  # Flag to track initial file loading
        self._zoom_level = 1.0  # Current zoom level (1.0 = 100%)
        self._zoom_css_provider: Optional[Gtk.CssProvider] = None

        # "Edit as root" state (remote files only): when on, the file is read via
        # ``sudo cat`` and saved via ``sudo tee`` over the same SSH/auth path.
        self._root_mode = False
        self._sudo_password: Optional[str] = None  # session cache for this editor
        self._root_button: Optional[Gtk.ToggleButton] = None
        self._root_banner: Optional[Adw.Banner] = None
        self._root_toggle_guard = False  # suppress re-entrant ``toggled`` signals

        # Track whether GtkSource is actually usable on this platform
        self._gtksource_enabled = _HAS_GTKSOURCE

        # Search/Replace state
        self._search_entry: Optional[Gtk.Entry] = None
        self._replace_entry: Optional[Gtk.Entry] = None
        self._search_settings: Optional[GtkSource.SearchSettings] = None
        self._search_context: Optional[GtkSource.SearchContext] = None
        
        if self._is_local:
            # For local files, use the file path directly
            self._temp_file = pathlib.Path(file_path)
        else:
            # For remote files, create temp file using Python's tempfile module
            # Use NamedTemporaryFile with delete=False for manual cleanup
            # This is the recommended pattern per Python docs for persistent temp files
            temp_file_obj = tempfile.NamedTemporaryFile(
                mode='w+b',
                prefix=f"sshpilot_edit_{os.getpid()}_",
                suffix=f"_{file_name}",
                delete=False  # Don't auto-delete, we'll clean up manually in _do_close()
            )
            temp_file_obj.close()  # Close the file handle, we'll open it later when needed
            self._temp_file = pathlib.Path(temp_file_obj.name)
        
        # Create UI
        self._setup_ui()
        
        # Load file (download if remote, direct load if local)
        if self._is_local:
            self._load_file_content()
        else:
            self._download_and_load()
    
    def _setup_ui(self) -> None:
        """Set up the editor UI."""
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        
        # Header bar — title with the path shown as a dimmed subtitle (full
        # path on hover).
        header_bar = Adw.HeaderBar()
        self._title_widget = Adw.WindowTitle(
            title=self._title_text, subtitle=self._pretty_path())
        self._title_widget.set_tooltip_text(self._file_path)
        header_bar.set_title_widget(self._title_widget)
        
        # Save button - label depends on local vs remote
        save_label = "Save" if self._is_local else "Save"
        self._save_button = Gtk.Button(label=save_label)
        self._save_button.add_css_class("suggested-action")
        self._save_button.set_sensitive(False)
        self._save_button.connect("clicked", self._on_save_clicked)
        header_bar.pack_end(self._save_button)

        # "Edit as root" toggle — remote files only. Reads/saves the file via
        # sudo over the same SSH/auth path (see _on_root_toggled).
        if not self._is_local and self._sftp_manager is not None:
            self._root_button = Gtk.ToggleButton(label="Edit as root")
            self._root_button.set_tooltip_text(
                "Re-read and save this file as root (sudo)")
            self._root_button.connect("toggled", self._on_root_toggled)
            header_bar.pack_end(self._root_button)

        from sshpilot import icon_utils

        # Sidebar show/hide toggle — leftmost, before undo/redo (only for
        # editors that have an outline).
        if self._show_outline:
            self._sidebar_toggle = Gtk.ToggleButton()
            self._sidebar_toggle.set_icon_name("sidebar-show-symbolic")
            self._sidebar_toggle.set_tooltip_text("Show/Hide sidebar")
            self._sidebar_toggle.set_active(
                bool(self._pref("editor.show_outline_sidebar", True)))
            self._sidebar_toggle.connect("toggled", self._on_sidebar_toggled)
            header_bar.pack_start(self._sidebar_toggle)

        # Undo/Redo buttons
        self._undo_button = icon_utils.new_button_from_icon_name("edit-undo-symbolic")
        self._undo_button.set_tooltip_text("Undo")
        self._undo_button.set_sensitive(False)
        self._undo_button.connect("clicked", self._on_undo_clicked)
        header_bar.pack_start(self._undo_button)
        
        self._redo_button = icon_utils.new_button_from_icon_name("edit-redo-symbolic")
        self._redo_button.set_tooltip_text("Redo")
        self._redo_button.set_sensitive(False)
        self._redo_button.connect("clicked", self._on_redo_clicked)
        header_bar.pack_start(self._redo_button)
        
        # Search button to toggle search bar (only if GtkSource is available)
        if self._gtksource_enabled:
            self._search_button = icon_utils.new_button_from_icon_name("system-search-symbolic")
            self._search_button.set_tooltip_text("Search")
            self._search_button.connect("clicked", self._on_search_button_clicked)
            header_bar.pack_start(self._search_button)
        else:
            self._search_button = None
        
        # Zoom controls
        zoom_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        zoom_box.add_css_class("linked")
        
        self._zoom_out_button = icon_utils.new_button_from_icon_name("zoom-out-symbolic")
        self._zoom_out_button.set_tooltip_text("Zoom Out")
        self._zoom_out_button.connect("clicked", lambda *_: self.zoom_out())
        zoom_box.append(self._zoom_out_button)
        
        self._zoom_reset_button = icon_utils.new_button_from_icon_name("zoom-fit-best-symbolic")
        self._zoom_reset_button.set_tooltip_text("Reset Zoom")
        self._zoom_reset_button.connect("clicked", lambda *_: self.reset_zoom())
        zoom_box.append(self._zoom_reset_button)
        
        self._zoom_in_button = icon_utils.new_button_from_icon_name("zoom-in-symbolic")
        self._zoom_in_button.set_tooltip_text("Zoom In")
        self._zoom_in_button.connect("clicked", lambda *_: self.zoom_in())
        zoom_box.append(self._zoom_in_button)
        
        header_bar.pack_start(zoom_box)

        # Color-scheme chooser — icon-only button on the right, just before Save.
        # Only meaningful with GtkSource.
        if self._gtksource_enabled and hasattr(GtkSource, "StyleSchemeChooserWidget"):
            self._scheme_button = Gtk.MenuButton()
            self._scheme_button.set_child(icon_utils.new_image_from_icon_name("color-symbolic"))
            self._scheme_button.set_tooltip_text("Editor color scheme")
            chooser_scroller = Gtk.ScrolledWindow()
            chooser_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            chooser_scroller.set_min_content_width(240)
            chooser_scroller.set_min_content_height(320)
            self._scheme_chooser = GtkSource.StyleSchemeChooserWidget()
            chooser_scroller.set_child(self._scheme_chooser)
            popover = Gtk.Popover()
            popover.set_child(chooser_scroller)
            self._scheme_button.set_popover(popover)
            header_bar.pack_end(self._scheme_button)

        toolbar_view.add_top_bar(header_bar)
        
        # Toolbar for search/replace (only if GtkSource is available)
        if self._gtksource_enabled:
            self._search_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            toolbar = self._search_toolbar
            toolbar.set_margin_start(6)
            toolbar.set_margin_end(6)
            toolbar.set_margin_top(6)
            toolbar.set_margin_bottom(6)
            
            # Search section
            search_label = Gtk.Label(label="Search:")
            self._search_entry = Gtk.Entry()
            self._search_entry.set_placeholder_text("Search...")
            self._search_entry.set_width_chars(20)
            
            search_prev_btn = Gtk.Button(label="Prev")
            search_prev_btn.connect("clicked", self._on_search_prev_clicked)
            
            search_next_btn = Gtk.Button(label="Next")
            search_next_btn.connect("clicked", self._on_search_next_clicked)
            
            # Replace section
            replace_label = Gtk.Label(label="Replace:")
            self._replace_entry = Gtk.Entry()
            self._replace_entry.set_placeholder_text("Replace with...")
            self._replace_entry.set_width_chars(20)
            
            replace_btn = Gtk.Button(label="Replace")
            replace_btn.connect("clicked", self._on_replace_clicked)
            
            replace_all_btn = Gtk.Button(label="Replace All")
            replace_all_btn.connect("clicked", self._on_replace_all_clicked)
            
            # Pack toolbar
            toolbar.append(search_label)
            toolbar.append(self._search_entry)
            toolbar.append(search_prev_btn)
            toolbar.append(search_next_btn)
            toolbar.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
            toolbar.append(replace_label)
            toolbar.append(self._replace_entry)
            toolbar.append(replace_btn)
            toolbar.append(replace_all_btn)
            
            # Connect search entry signals
            self._search_entry.connect("changed", self._on_search_changed)
            self._search_entry.connect("activate", self._on_search_activate)
            
            # Hide search toolbar by default
            self._search_toolbar.set_visible(False)
        else:
            # Create empty toolbar to avoid errors, but keep it hidden
            self._search_toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            self._search_toolbar.set_visible(False)
            self._search_entry = None
            self._replace_entry = None
        
        # Editor area
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)

        # Create editor widget (SourceView if available, otherwise TextView)
        if not self._init_source_view():
            self._init_text_view()
        
        # Connect to buffer changes
        self._buffer.connect("modified-changed", self._on_buffer_modified_changed)
        
        # Connect undo/redo state changes if using GtkSource
        if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
            self._buffer.connect("notify::can-undo", self._on_undo_state_changed)
            self._buffer.connect("notify::can-redo", self._on_redo_state_changed)
        
        # Create a vertical box to hold toolbar and scrolled window
        content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        # Permission-denied prompt offering to retry as root (remote files only).
        if self._root_button is not None:
            self._root_banner = Adw.Banner(title="Permission denied")
            self._root_banner.set_button_label("Edit as root")
            self._root_banner.connect("button-clicked", self._on_banner_root_clicked)
            self._root_banner.set_revealed(False)
            content_box.append(self._root_banner)
        content_box.append(self._search_toolbar)
        content_box.append(scrolled)
        
        scrolled.set_child(self._source_view)
        
        # Wrap editor in toast overlay for status messages (same style as file manager)
        toast_overlay = Adw.ToastOverlay()
        toast_overlay.set_child(content_box)
        self._toast_overlay = toast_overlay
        self._current_toast = None  # Keep reference for dismissal

        if self._show_outline:
            # Host/Match navigation sidebar (SSH config editor only).
            outline_scroller = Gtk.ScrolledWindow()
            outline_scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            self._outline_scroller = outline_scroller
            # Honour the persisted show/hide preference.
            outline_scroller.set_visible(
                bool(self._pref("editor.show_outline_sidebar", True)))
            self._outline_listbox = Gtk.ListBox()
            self._outline_listbox.add_css_class("navigation-sidebar")
            self._outline_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
            self._outline_listbox.connect("row-activated", self._on_outline_row_activated)
            outline_scroller.set_child(self._outline_listbox)

            paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
            paned.set_start_child(outline_scroller)
            paned.set_end_child(toast_overlay)
            paned.set_resize_start_child(False)
            paned.set_shrink_start_child(False)
            paned.set_position(220)
            toolbar_view.set_content(paned)

            # Populate now and keep it in sync (debounced) as the buffer changes.
            self._refresh_outline()
            self._buffer.connect("changed", self._on_outline_buffer_changed)
        else:
            toolbar_view.set_content(toast_overlay)
        
        # Add keyboard controller for shortcuts
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        
        # Apply same toast CSS styling as file manager
        self._apply_toast_css()
        
        # Connect close request
        self.connect("close-request", self._on_close_request)
        
        # Apply initial zoom
        self._apply_zoom()

        # Show initial loading toast
        self._show_toast("Loading…" if self._is_local else "Downloading…", timeout=2)

    def _init_source_view(self) -> bool:
        """Initialize GtkSourceView if available.

        Returns True if GtkSourceView was created successfully, False if we
        should fall back to Gtk.TextView.
        """
        if not self._gtksource_enabled or GtkSource is None:
            self._gtksource_enabled = False
            return False

        try:
            self._source_view = GtkSource.View()
            self._source_view.set_show_line_numbers(True)
            self._source_view.set_highlight_current_line(False)
            self._source_view.set_auto_indent(True)
            self._source_view.set_indent_width(4)
            self._source_view.set_tab_width(4)
            self._source_view.set_insert_spaces_instead_of_tabs(False)
            self._source_view.set_monospace(True)
            self._source_view.set_wrap_mode(Gtk.WrapMode.WORD)

            language_manager = _get_language_manager()
            language = None
            if self._language_id:
                language = language_manager.get_language(self._language_id)
            if language is None:
                language = language_manager.guess_language(self._file_name, None)
            if language:
                self._buffer = GtkSource.Buffer.new_with_language(language)
            else:
                self._buffer = GtkSource.Buffer()
            self._source_view.set_buffer(self._buffer)

            # Apply the saved/auto color scheme, then start listening for changes
            # (connect after the initial apply so it doesn't fire spuriously).
            self._apply_style_scheme()
            if self._scheme_chooser is not None:
                self._scheme_chooser.connect("notify::style-scheme", self._on_scheme_changed)
            self._connect_dark_follow()

            self._search_settings = GtkSource.SearchSettings()
            self._search_context = GtkSource.SearchContext.new(self._buffer, self._search_settings)
            self._search_context.set_highlight(True)
            self._gtksource_enabled = True
            return True
        except Exception as e:
            logger.warning("GtkSource unavailable; falling back to TextView: %s", e)
            self._gtksource_enabled = False
            self._search_settings = None
            self._search_context = None
            return False

    def _init_text_view(self) -> None:
        """Initialize a basic Gtk.TextView editor as a fallback."""
        self._source_view = Gtk.TextView()
        self._source_view.set_monospace(True)
        self._source_view.set_wrap_mode(Gtk.WrapMode.WORD)
        self._buffer = Gtk.TextBuffer()
        self._source_view.set_buffer(self._buffer)
        self._search_settings = None
        self._search_context = None
        self._gtksource_enabled = False

    # ---------- color scheme + sidebar preferences ----------

    def _get_app_config(self):
        """Lazily obtain the shared app Config (reads/writes the same store)."""
        if self._app_config is None:
            try:
                from .config import Config
                self._app_config = Config()
            except Exception as e:  # noqa: BLE001
                logger.debug("Editor could not load app config: %s", e)
        return self._app_config

    def _pref(self, key: str, default):
        cfg = self._get_app_config()
        try:
            return cfg.get_setting(key, default) if cfg else default
        except Exception:
            return default

    def _resolve_scheme_id(self) -> str:
        """Saved scheme id, or the theme-appropriate default when 'auto'."""
        pref = self._pref("editor.style_scheme", "auto")
        if pref and pref != "auto":
            return pref
        try:
            dark = Adw.StyleManager.get_default().get_dark()
        except Exception:
            dark = False
        return "Adwaita-dark" if dark else "Adwaita"

    def _apply_style_scheme(self) -> None:
        """Set the GtkSourceView style scheme on the buffer (per the docs, a
        scheme should always be set) and reflect it on the chooser button."""
        if not (self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer)):
            return
        sm = GtkSource.StyleSchemeManager.get_default()
        scheme = sm.get_scheme(self._resolve_scheme_id()) or sm.get_scheme("Adwaita")
        if scheme is None:
            return
        self._buffer.set_style_scheme(scheme)
        if self._scheme_chooser is not None:
            # Sync the chooser without re-triggering our own change handler.
            wired = self._scheme_button_wired()
            if wired:
                self._scheme_chooser.handler_block_by_func(self._on_scheme_changed)
            self._scheme_chooser.set_style_scheme(scheme)
            if wired:
                self._scheme_chooser.handler_unblock_by_func(self._on_scheme_changed)

    def _scheme_button_wired(self) -> bool:
        return getattr(self, "_scheme_signal_wired", False)

    def _on_scheme_changed(self, chooser, _pspec) -> None:
        scheme = chooser.get_style_scheme()
        if scheme is None:
            return
        if isinstance(self._buffer, GtkSource.Buffer):
            self._buffer.set_style_scheme(scheme)
        cfg = self._get_app_config()
        if cfg:
            cfg.set_setting("editor.style_scheme", scheme.get_id())

    def _connect_dark_follow(self) -> None:
        """When following the system (pref == 'auto'), recolor on dark toggle."""
        self._scheme_signal_wired = self._scheme_chooser is not None
        pref = self._pref("editor.style_scheme", "auto")
        if pref and pref != "auto":
            return
        try:
            sm = Adw.StyleManager.get_default()
            self._dark_handler_id = sm.connect("notify::dark", self._on_dark_changed)
        except Exception as e:  # noqa: BLE001
            logger.debug("Could not follow system dark mode: %s", e)

    def _on_dark_changed(self, *_a) -> None:
        self._apply_style_scheme()

    def _on_sidebar_toggled(self, button) -> None:
        active = button.get_active()
        if self._outline_scroller is not None:
            self._outline_scroller.set_visible(active)
        cfg = self._get_app_config()
        if cfg:
            cfg.set_setting("editor.show_outline_sidebar", active)

    def _apply_toast_css(self) -> None:
        """Apply the same toast CSS styling as file manager."""
        try:
            css_provider = Gtk.CssProvider()
            toast_css = """
            toast {
                /* Frosted glass effect */
                background-color: alpha(black, 0.6);

                /* Pill shape */
                border-radius: 99px; /* A large value creates the pill shape */

                /* Clean typography */
                color: white;
                font-weight: 500; /* Medium weight for a modern feel */
                font-size: 1.05em;

                /* Subtle details */
                padding: 8px 20px;
                margin: 10px;
                border: 1px solid alpha(white, 0.1);
                box-shadow: 0 5px 15px alpha(black, 0.2);
            }
            
            toast label {
                /* Style toast labels */
                color: white;
                font-weight: 500;
            }
            
            toast button {
                /* Style toast buttons if any */
                color: white;
                background-color: alpha(white, 0.2);
                border: 1px solid alpha(white, 0.3);
                border-radius: 6px;
                padding: 4px 8px;
            }
            
            toast button.circular.flat {
                /* Style close button */
                color: white;
                background-color: alpha(black, 0.6);
                border: 1px solid alpha(white, 0.1);
            }
            """
            css_provider.load_from_data(toast_css.encode())
            display = Gdk.Display.get_default()
            if display:
                Gtk.StyleContext.add_provider_for_display(
                    display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_USER
                )
        except Exception as e:
            logger.debug(f"Failed to apply toast CSS: {e}")
    
    def _show_toast(self, text: str, timeout: int = 3) -> None:
        """Show a toast message safely (same style as FilePane)."""
        try:
            # Dismiss any existing toast first
            if self._current_toast:
                self._current_toast.dismiss()
                self._current_toast = None
            
            toast = Adw.Toast.new(text)
            if timeout >= 0:
                toast.set_timeout(timeout)
            self._toast_overlay.add_toast(toast)
            self._current_toast = toast  # Keep reference for dismissal
        except (AttributeError, RuntimeError, GLib.GError):
            # Overlay might be destroyed or invalid, ignore
            pass
    
    def _download_and_load(self) -> None:
        """Download the remote file and load it into the editor."""
        def download_complete(future: Future) -> None:
            try:
                future.result()  # Wait for download to complete
                GLib.idle_add(self._load_file_content)
            except Exception as e:
                logger.error(f"Failed to download file for editing: {e}", exc_info=True)
                GLib.idle_add(self._on_remote_load_error, str(e))
        
        # Download file (use _file_path which contains the remote path for remote files)
        future = self._sftp_manager.download(self._file_path, self._temp_file)
        future.add_done_callback(download_complete)
    
    def _load_file_content(self) -> None:
        """Load the file content into the editor (downloaded for remote, direct for local)."""
        try:
            if not self._temp_file.exists():
                error_msg = "File not found" if self._is_local else "Downloaded file not found"
                self._show_error(error_msg)
                return
            
            # Read file content
            with open(self._temp_file, 'rb') as f:
                content = f.read()
            
            # Try to decode as UTF-8, fall back to latin-1 if needed
            try:
                text = content.decode('utf-8')
            except UnicodeDecodeError:
                try:
                    text = content.decode('latin-1')
                except UnicodeDecodeError:
                    text = content.decode('utf-8', errors='replace')
            
            # Set content in buffer
            self._buffer.set_text(text)
            self._buffer.set_modified(False)
            
            # Reset undo/redo state after loading
            # Using begin_not_undoable_action/end_not_undoable_action clears the undo history
            # This ensures file loading doesn't create undo history entries
            if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
                try:
                    self._buffer.begin_not_undoable_action()
                    self._buffer.end_not_undoable_action()
                except (AttributeError, TypeError) as e:
                    logger.debug(f"Failed to reset undo stack: {e}")
                # Update undo/redo button states
                if hasattr(self, '_undo_button'):
                    self._undo_button.set_sensitive(False)
                if hasattr(self, '_redo_button'):
                    self._redo_button.set_sensitive(False)
            
            # Get initial modification time
            self._file_modified_time = self._temp_file.stat().st_mtime
            
            # Set up file monitoring
            self._setup_file_monitoring()
            
            # Mark loading as complete - now modification events will be handled
            self._is_loading = False
            
        except Exception as e:
            logger.error(f"Failed to load file content: {e}", exc_info=True)
            self._show_error(f"Failed to load file: {e}")
            # Ensure loading flag is reset even on error
            self._is_loading = False
    
    def _setup_file_monitoring(self) -> None:
        """Set up file monitoring to detect external saves."""
        try:
            gfile = Gio.File.new_for_path(str(self._temp_file))
            self._file_monitor = gfile.monitor_file(Gio.FileMonitorFlags.WATCH_MOVES, None)
            self._file_monitor.connect("changed", self._on_file_changed)
        except Exception as e:
            logger.warning(f"Failed to set up file monitoring: {e}")
    
    def _on_file_changed(
        self,
        monitor: Gio.FileMonitor,
        file: Gio.File,
        other_file: Optional[Gio.File],
        event_type: Gio.FileMonitorEvent,
    ) -> None:
        """Handle file system changes to the temp file."""
        if self._is_closing:
            return
        
        # Only care about changes (not moves/deletes)
        if event_type in (Gio.FileMonitorEvent.CHANGED, Gio.FileMonitorEvent.CHANGES_DONE_HINT):
            try:
                if self._temp_file.exists():
                    new_mtime = self._temp_file.stat().st_mtime
                    if new_mtime > self._file_modified_time:
                        self._file_modified_time = new_mtime
                        GLib.idle_add(self._on_file_saved_externally)
            except Exception:
                pass
    
    def _pretty_path(self) -> str:
        """Path for the title subtitle: local paths get the home dir collapsed
        to ``~``; remote paths are shown verbatim."""
        if not self._is_local:
            return self._file_path
        try:
            home = GLib.get_home_dir()
        except Exception:
            home = os.path.expanduser("~")
        return prettify_path(self._file_path, home)

    def set_editor_title(self, title: str) -> None:
        """Override the base title shown in the header (path subtitle is kept)."""
        self._title_text = title
        self._update_title()

    def _update_title(self, modified: bool = None) -> None:
        """Update the headerbar title (modified marker) and path subtitle."""
        if modified is None:
            modified = self._buffer.get_modified() if self._buffer else False
        base = self._title_text or self._file_name
        if getattr(self, "_title_widget", None) is not None:
            self._title_widget.set_title(f"• {base}" if modified else base)
            subtitle = self._pretty_path()
            if getattr(self, "_root_mode", False):
                subtitle = f"{subtitle}  —  editing as root"
            self._title_widget.set_subtitle(subtitle)

    # ---------- Host/Match outline sidebar ----------

    def _on_outline_buffer_changed(self, _buffer) -> None:
        # Debounce: rebuild shortly after edits settle, not on every keystroke.
        if self._outline_refresh_id:
            GLib.source_remove(self._outline_refresh_id)
        self._outline_refresh_id = GLib.timeout_add(300, self._refresh_outline)

    def _refresh_outline(self) -> bool:
        self._outline_refresh_id = 0
        listbox = self._outline_listbox
        if listbox is None:
            return False
        # Clear existing rows.
        child = listbox.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            listbox.remove(child)
            child = nxt
        start, end = self._buffer.get_bounds()
        text = self._buffer.get_text(start, end, False)
        self._outline_rows = parse_ssh_config_outline(text)
        for line_index, kind, label in self._outline_rows:
            row = Gtk.ListBoxRow()
            row._line_index = line_index  # consumed by row-activated
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for fn in (box.set_margin_top, box.set_margin_bottom,
                       box.set_margin_start, box.set_margin_end):
                fn(6)
            if kind == "match":
                tag = Gtk.Label(label="Match")
                tag.add_css_class("dim-label")
                tag.add_css_class("caption")
                box.append(tag)
            lbl = Gtk.Label(label=label, xalign=0)
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
            lbl.set_hexpand(True)
            box.append(lbl)
            row.set_child(box)
            listbox.append(row)
        return False  # one-shot timeout

    def _on_outline_row_activated(self, _listbox, row) -> None:
        line_index = getattr(row, "_line_index", None)
        if line_index is not None:
            self._jump_to_line(line_index)

    def _jump_to_line(self, line_index: int) -> None:
        try:
            res = self._buffer.get_iter_at_line(line_index)
            it = res[1] if isinstance(res, tuple) else res
            self._buffer.place_cursor(it)
            self._source_view.scroll_to_iter(it, 0.1, True, 0.0, 0.0)
            self._source_view.grab_focus()
        except Exception as e:  # noqa: BLE001
            logger.debug("Failed to jump to line %s: %s", line_index, e)
    
    def _on_file_saved_externally(self) -> None:
        """Handle when the file is saved externally (from the editor)."""
        self._has_unsaved_changes = True
        # Remember that the on-disk file diverged from our buffer so the next
        # save warns before clobbering it (see _on_save_clicked).
        self._externally_modified = True
        self._save_button.set_sensitive(True)
        self._update_title(True)
    
    def _on_buffer_modified_changed(self, buffer: Gtk.TextBuffer) -> None:
        """Handle buffer modification state changes."""
        # Ignore modification events during initial file loading
        if self._is_loading:
            return
        
        modified = buffer.get_modified()
        if modified:
            if not self._has_unsaved_changes:
                self._has_unsaved_changes = True
                self._save_button.set_sensitive(True)
            self._update_title(True)
        else:
            # Buffer is no longer modified
            self._update_title(False)
    
    def _on_undo_state_changed(self, buffer: GtkSource.Buffer, pspec: GObject.ParamSpec) -> None:
        """Handle undo state changes."""
        if hasattr(self, '_undo_button'):
            try:
                if isinstance(buffer, GtkSource.Buffer) and hasattr(buffer, 'can_undo'):
                    self._undo_button.set_sensitive(buffer.can_undo())
            except (AttributeError, TypeError):
                pass
    
    def _on_redo_state_changed(self, buffer: GtkSource.Buffer, pspec: GObject.ParamSpec) -> None:
        """Handle redo state changes."""
        if hasattr(self, '_redo_button'):
            try:
                if isinstance(buffer, GtkSource.Buffer) and hasattr(buffer, 'can_redo'):
                    self._redo_button.set_sensitive(buffer.can_redo())
            except (AttributeError, TypeError):
                pass
    
    def _on_save_clicked(self, _button: Gtk.Button) -> None:
        """Handle save button click - save buffer content locally, upload if remote."""
        if not self._temp_file:
            self._show_error("File not found")
            return

        # Always save buffer content to file
        start, end = self._buffer.get_bounds()
        text = self._buffer.get_text(start, end, False)

        # Optional syntax validation (e.g. `ssh -G` for the SSH config) so a
        # broken file is never written.
        if self._is_local and self._pre_save_validator is not None:
            error = self._pre_save_validator(text)
            if error:
                self._show_error(f"Not saved — invalid SSH config:\n\n{error}")
                return

        # Guard against clobbering a change made on disk since we loaded/last
        # saved (e.g. the connection editor rewrote ~/.ssh/config).
        if self._is_local and getattr(self, '_externally_modified', False):
            dlg = Adw.AlertDialog.new(
                "File changed on disk",
                "This file was modified outside the editor since you opened it. "
                "Saving now overwrites those changes.")
            dlg.add_response("cancel", "Cancel")
            dlg.add_response("overwrite", "Overwrite")
            dlg.set_response_appearance("overwrite", Adw.ResponseAppearance.DESTRUCTIVE)
            dlg.connect("response",
                        lambda _d, r: r == "overwrite" and self._perform_save(text))
            dlg.present(self)
            return

        self._perform_save(text)

    def _perform_save(self, text: str) -> None:
        try:
            # Atomic temp+replace so a crash/full disk can't truncate the file
            # (critical for ~/.ssh/config). mode=None preserves the file's
            # existing permissions (e.g. keeps a 0600 config at 0600).
            from .ssh_config_utils import atomic_write_text
            atomic_write_text(str(self._temp_file), text)
            self._buffer.set_modified(False)
            
            # Reset undo stack after save
            # Using begin_not_undoable_action/end_not_undoable_action clears the undo history
            # This ensures saving doesn't create undo history entries
            if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
                try:
                    self._buffer.begin_not_undoable_action()
                    self._buffer.end_not_undoable_action()
                except (AttributeError, TypeError) as e:
                    logger.debug(f"Failed to reset undo stack: {e}")
                # Update undo/redo button states
                if hasattr(self, '_undo_button'):
                    self._undo_button.set_sensitive(False)
                if hasattr(self, '_redo_button'):
                    self._redo_button.set_sensitive(False)
            self._file_modified_time = self._temp_file.stat().st_mtime
            self._has_unsaved_changes = False
            self._externally_modified = False
            self._update_title(False)
            
            if self._is_local:
                # For local files, just save and refresh the pane
                self._show_toast("Saved", timeout=2)
                self._save_button.set_sensitive(False)
                # Refresh the file manager to show updated file
                if self._file_manager_window:
                    pane = self._file_manager_window._left_pane
                    if pane:
                        GLib.idle_add(lambda: pane.emit("path-changed", pane._current_path))
                return
        except Exception as e:
            self._show_error(f"Failed to save file: {e}")
            return
        
        # For remote files, upload after saving
        if not self._is_local:
            self._upload_file()
    
    def _upload_file(self) -> None:
        """Upload the modified file back to the remote server."""
        if self._root_mode:
            self._upload_file_root()
            return
        self._show_toast("Uploading…", timeout=-1)  # Show until upload completes
        self._save_button.set_sensitive(False)

        def upload_complete(future: Future) -> None:
            try:
                future.result()
                GLib.idle_add(self._on_upload_success)
            except Exception as e:
                logger.error(f"Failed to upload file: {e}", exc_info=True)
                GLib.idle_add(self._on_upload_error, str(e))
        
        future = self._sftp_manager.upload(self._temp_file, self._file_path)
        future.add_done_callback(upload_complete)
    
    def _on_upload_success(self) -> None:
        """Handle successful upload."""
        self._has_unsaved_changes = False
        self._save_button.set_sensitive(False)
        self._update_title(False)
        self._show_toast("Uploaded successfully", timeout=2)
        
        # Reset buffer modified flag since changes have been saved
        self._buffer.set_modified(False)
        
        # Reset undo stack after save
        # Using begin_not_undoable_action/end_not_undoable_action clears the undo history
        # This ensures saving doesn't create undo history entries
        if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
            try:
                self._buffer.begin_not_undoable_action()
                self._buffer.end_not_undoable_action()
            except (AttributeError, TypeError) as e:
                logger.debug(f"Failed to reset undo stack: {e}")
            # Update undo/redo button states
            if hasattr(self, '_undo_button'):
                self._undo_button.set_sensitive(False)
            if hasattr(self, '_redo_button'):
                self._redo_button.set_sensitive(False)
        
        # Refresh the file manager to show updated file
        if self._file_manager_window:
            pane = self._file_manager_window._right_pane
            if pane:
                pane.emit("path-changed", pane._current_path)
    
    def _on_upload_error(self, error: str) -> None:
        """Handle upload error."""
        self._save_button.set_sensitive(True)
        # A login-user upload that hits permission-denied: offer root instead of
        # a dead-end error (no-op when already in root mode / for local files).
        if self._looks_permission_denied(error) and self._offer_root_banner():
            self._show_toast("Permission denied — try “Edit as root”.", timeout=4)
            return
        self._show_toast(f"Upload failed: {error}", timeout=4)
        self._show_error(f"Failed to upload file: {error}")

    # ================================================================
    # Edit as root (remote files only): sudo cat to read, sudo tee to save,
    # over the same SSH/auth path as the SFTP session.
    # ================================================================
    @staticmethod
    def _root_read_cmd(path: str, *, has_pw: bool) -> str:
        """Remote command to read ``path`` as root. ``sudo -S -p ''`` reads the
        password from stdin; ``sudo -n`` is the passwordless path."""
        sudo = "sudo -S -p ''" if has_pw else "sudo -n"
        return f"{sudo} -- cat -- {shlex.quote(path)}"

    @staticmethod
    def _root_write_cmd(path: str, *, has_pw: bool) -> str:
        """Remote command to write stdin to ``path`` as root via ``tee`` (which
        preserves an existing file's owner/mode). ``> /dev/null`` keeps the file
        content from being echoed back over the SSH channel."""
        sudo = "sudo -S -p ''" if has_pw else "sudo -n"
        return f"{sudo} -- tee -- {shlex.quote(path)} > /dev/null"

    def _root_host_user(self) -> tuple[str, str]:
        """(host, username) keyring identity — shared with the SSH login and the
        docker console, so a saved sudo password is reused across all three."""
        mgr = self._sftp_manager
        return (getattr(mgr, "host", "") or "",
                getattr(mgr, "username", "") or "")

    @staticmethod
    def _looks_permission_denied(text: str) -> bool:
        low = (text or "").lower()
        return ("permission denied" in low or "access denied" in low
                or "operation not permitted" in low)

    def _offer_root_banner(self) -> bool:
        """Reveal the 'edit as root' banner; returns True if it was shown."""
        if self._root_button is None or self._root_mode or self._root_banner is None:
            return False
        self._root_banner.set_revealed(True)
        return True

    def _on_banner_root_clicked(self, _banner: Adw.Banner) -> None:
        self._root_banner.set_revealed(False)
        if self._root_button is not None and not self._root_button.get_active():
            self._root_button.set_active(True)  # fires _on_root_toggled

    def _on_remote_load_error(self, message: str) -> None:
        if self._looks_permission_denied(message) and self._offer_root_banner():
            self._show_toast("Permission denied — try “Edit as root”.", timeout=4)
            return
        self._show_error(f"Failed to download file: {message}")

    def _set_root_active(self, active: bool) -> None:
        """Set the toggle without re-triggering the ``toggled`` handler."""
        self._root_toggle_guard = True
        try:
            if self._root_button is not None:
                self._root_button.set_active(active)
        finally:
            self._root_toggle_guard = False

    def _update_root_ui(self) -> None:
        self._set_root_active(self._root_mode)
        if self._root_button is not None:
            if self._root_mode:
                self._root_button.add_css_class("destructive-action")
            else:
                self._root_button.remove_css_class("destructive-action")
        if self._root_mode and self._root_banner is not None:
            self._root_banner.set_revealed(False)
        self._update_title()

    def _on_root_toggled(self, button: Gtk.ToggleButton) -> None:
        if self._root_toggle_guard:
            return
        if button.get_active():
            if self._buffer.get_modified():
                dlg = Adw.AlertDialog.new(
                    "Discard changes?",
                    "Editing as root re-reads the file from the host and discards "
                    "your unsaved changes.")
                dlg.add_response("cancel", "Cancel")
                dlg.add_response("discard", "Discard and continue")
                dlg.set_response_appearance(
                    "discard", Adw.ResponseAppearance.DESTRUCTIVE)

                def _resp(_d, r):
                    if r == "discard":
                        self._begin_root_load()
                    else:
                        self._set_root_active(False)

                dlg.connect("response", _resp)
                dlg.present(self)
                return
            self._begin_root_load()
        else:
            # Back to the normal login-user view.
            self._root_mode = False
            self._update_root_ui()
            self._download_and_load()

    def _begin_root_load(self) -> None:
        host, user = self._root_host_user()
        pw = self._sudo_password
        from_keyring = False
        if not pw:
            from .askpass_utils import lookup_sudo_password
            kp = lookup_sudo_password(host, user)
            if kp:
                pw, from_keyring = kp, True
        self._show_toast("Reading as root…", timeout=-1)
        self._run_root_read(pw, from_keyring)

    def _run_root_read(self, pw: Optional[str], from_keyring: bool) -> None:
        cmd = self._root_read_cmd(self._file_path, has_pw=bool(pw))
        data = (pw + "\n").encode("utf-8") if pw else None
        future = self._sftp_manager.run_command_async(cmd, input=data)

        def _done(fut: Future) -> None:
            try:
                rc, out, err = fut.result()
            except Exception as e:  # noqa: BLE001
                rc, out, err = -1, b"", str(e)
            GLib.idle_add(self._root_read_done, rc, out, err, pw, from_keyring)

        future.add_done_callback(_done)

    def _root_read_done(self, rc: int, out: bytes, err: str,
                        pw: Optional[str], from_keyring: bool) -> None:
        from .askpass_utils import is_sudo_denied_error, clear_sudo_password
        if rc == 0:
            self._sudo_password = pw  # None == passwordless sudo
            self._root_mode = True
            try:
                with open(self._temp_file, "wb") as f:
                    f.write(out)
            except Exception as e:  # noqa: BLE001
                self._root_mode = False
                self._update_root_ui()
                self._show_error(f"Failed to read file as root: {e}")
                return
            self._update_root_ui()
            self._load_file_content()
            self._show_toast("Editing as root", timeout=2)
            return
        if is_sudo_denied_error(err):
            self._sudo_password = None
            self._root_mode = False
            self._update_root_ui()
            self._show_toast(
                "Your user isn't allowed to run sudo on this host.", timeout=4)
            return
        # Wrong or missing password — drop a stale keyring entry, then prompt.
        if from_keyring:
            host, user = self._root_host_user()
            clear_sudo_password(host, user)
        self._sudo_password = None
        self._prompt_root_password(
            retry=lambda p: self._run_root_read(p, False),
            on_cancel=self._cancel_root_mode)

    def _cancel_root_mode(self) -> None:
        self._root_mode = False
        self._update_root_ui()

    def _prompt_root_password(self, *, retry: Callable[[str], None],
                             on_cancel: Callable[[], None]) -> None:
        """Ask for the host's sudo password (GUI, modal) and either retry the
        operation with it or back off. Reuses the shared password dialog +
        keyring 'on_store' path used by the docker console."""
        host, user = self._root_host_user()
        from .window import show_ssh_password_dialog
        from .askpass_utils import store_sudo_password
        on_store = (lambda p: store_sudo_password(host, user, p)) if host else None
        password = show_ssh_password_dialog(
            from_widget=self,
            display_name=host or self._file_name,
            host=host,
            username=user,
            heading="Sudo password required",
            body=(f"Editing “{self._file_name}” as root needs a sudo password"
                  + (f" on “{host}”" if host else "") + ".\n\n"
                  "Enter your sudo password:"),
            store_label="Save sudo password",
            on_store=on_store,
        )
        if not password:
            on_cancel()
            return
        retry(password)

    def _upload_file_root(self) -> None:
        self._show_toast("Saving as root…", timeout=-1)
        self._save_button.set_sensitive(False)
        try:
            with open(self._temp_file, "rb") as f:
                data = f.read()
        except Exception as e:  # noqa: BLE001
            self._on_upload_error(str(e))
            return
        pw = self._sudo_password
        cmd = self._root_write_cmd(self._file_path, has_pw=bool(pw))
        stdin = ((pw + "\n").encode("utf-8") + data) if pw else data
        future = self._sftp_manager.run_command_async(cmd, input=stdin)

        def _done(fut: Future) -> None:
            try:
                rc, _out, err = fut.result()
            except Exception as e:  # noqa: BLE001
                rc, err = -1, str(e)
            GLib.idle_add(self._root_write_done, rc, err)

        future.add_done_callback(_done)

    def _root_write_done(self, rc: int, err: str) -> None:
        from .askpass_utils import is_sudo_denied_error, clear_sudo_password
        if rc == 0:
            self._on_upload_success()
            return
        if is_sudo_denied_error(err):
            self._on_upload_error(
                "Your user isn't allowed to run sudo on this host.")
            return
        # Wrong/missing password — clear stale keyring, re-prompt, retry the save.
        host, user = self._root_host_user()
        clear_sudo_password(host, user)
        self._sudo_password = None

        def _retry(p: str) -> None:
            self._sudo_password = p
            self._upload_file_root()

        self._prompt_root_password(
            retry=_retry,
            on_cancel=lambda: self._on_upload_error("Sudo password required."))
    
    def _on_close_clicked(self, _button: Gtk.Button) -> None:
        """Handle close button click."""
        self._check_and_close()
    
    def _on_close_request(self, _window: Adw.Window) -> bool:
        """Handle window close request."""
        self._check_and_close()
        return True  # Prevent default close
    
    def _check_and_close(self) -> None:
        """Check for unsaved changes and close if okay."""
        # Only check if the buffer has been modified (user made actual changes)
        has_changes = self._buffer.get_modified()
        
        if has_changes:
            # Show confirmation dialog - text differs for local vs remote
            if self._is_local:
                dialog_text = f"You have unsaved changes to {self._file_name}. Save changes before closing?"
                save_label = "Save"
            else:
                dialog_text = f"You have unsaved changes to {self._file_name}. Upload changes before closing?"
                save_label = "Save & Upload"
            
            dialog = Adw.AlertDialog.new(
                "Unsaved Changes",
                dialog_text
            )
            dialog.add_response("cancel", "Cancel")
            dialog.add_response("discard", "Discard Changes")
            dialog.add_response("save", save_label)
            dialog.set_default_response("save")
            dialog.set_close_response("cancel")
            
            def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
                if response == "save":
                    self._on_save_clicked(None)
                    # Wait a bit then close
                    GLib.timeout_add(500, self._do_close)
                elif response == "discard":
                    self._do_close()
            
            dialog.connect("response", on_response)
            dialog.present(self)
        else:
            self._do_close()
    
    def _do_close(self) -> None:
        """Actually close the window and clean up."""
        self._is_closing = True

        # Stop following system dark-mode changes (avoid leaking the handler).
        if self._dark_handler_id:
            try:
                Adw.StyleManager.get_default().disconnect(self._dark_handler_id)
            except Exception:
                pass
            self._dark_handler_id = 0

        # Stop monitoring
        if self._file_monitor:
            self._file_monitor.cancel()
            self._file_monitor = None
        
        # Clean up temp file (only for remote files - local files shouldn't be deleted)
        if not self._is_local and self._temp_file and self._temp_file.exists():
            try:
                self._temp_file.unlink()
            except Exception:
                pass
        
        self.destroy()
    
    def _show_error(self, message: str) -> None:
        """Show an error dialog."""
        dialog = Adw.AlertDialog.new("Error", message)
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)
    
    # ---------- Search/Replace methods ----------
    
    def _update_search_settings(self) -> None:
        """Update search settings from search entry."""
        logger.debug("_update_search_settings: called")
        
        if not self._gtksource_enabled:
            logger.debug("_update_search_settings: GtkSource is not enabled")
            return
        
        if not self._search_settings:
            logger.debug("_update_search_settings: _search_settings is None")
            return
        
        if not self._search_entry:
            logger.debug("_update_search_settings: _search_entry is None")
            return
        
        text = self._search_entry.get_text()
        logger.debug(f"_update_search_settings: text='{text}'")
        
        if text:
            self._search_settings.set_search_text(text)
            logger.debug(f"_update_search_settings: set_search_text('{text}')")
        else:
            self._search_settings.set_search_text(None)
            logger.debug("_update_search_settings: set_search_text(None)")
        
        # Configure search behavior
        self._search_settings.set_case_sensitive(False)
        self._search_settings.set_wrap_around(True)
        logger.debug("_update_search_settings: configured case_sensitive=False, wrap_around=True")
    
    def _search_next(self) -> None:
        """Search for next occurrence."""
        logger.debug("_search_next: called")
        
        if not self._gtksource_enabled:
            logger.debug("_search_next: GtkSource is not enabled")
            return
        
        if not self._search_context:
            logger.debug("_search_next: _search_context is None")
            return
        
        if not self._search_entry:
            logger.debug("_search_next: _search_entry is None")
            return
        
        # Ensure search settings are up to date
        self._update_search_settings()
        
        # Check search text
        search_text = self._search_entry.get_text() if self._search_entry else None
        logger.debug(f"_search_next: search_text='{search_text}'")
        
        if not search_text:
            logger.debug("_search_next: no search text, returning")
            return
        
        # Check search settings
        if self._search_settings:
            settings_text = self._search_settings.get_search_text()
            logger.debug(f"_search_next: search_settings.search_text='{settings_text}'")
        
        # If there's a selection, start from just after the end of the selection
        # Otherwise, start from the insert mark
        try:
            # get_selection_bounds() returns (has_selection, start_iter, end_iter) when selection exists
            # Raises ValueError when no selection
            has_selection, start, end = self._buffer.get_selection_bounds()
            logger.debug(f"_search_next: has_selection={has_selection}")
            
            if has_selection:
                # Start searching from just after the end of the current selection
                iter_ = end.copy()
                start_offset = iter_.get_offset()
                # Advance by one character to skip the current match
                if not iter_.is_end():
                    iter_.forward_char()
                end_offset = iter_.get_offset()
                logger.debug(f"_search_next: using selection end, start_offset={start_offset}, end_offset={end_offset}")
            else:
                # No selection, start from insert mark (cursor position)
                insert_mark = self._buffer.get_insert()
                iter_ = self._buffer.get_iter_at_mark(insert_mark)
                offset = iter_.get_offset()
                logger.debug(f"_search_next: using cursor position, offset={offset}")
        except ValueError:
            # No selection - get_selection_bounds() raises ValueError when there's no selection
            # Start from insert mark (cursor position)
            logger.debug("_search_next: no selection, using cursor position")
            insert_mark = self._buffer.get_insert()
            iter_ = self._buffer.get_iter_at_mark(insert_mark)
            offset = iter_.get_offset()
            logger.debug(f"_search_next: cursor offset={offset}")
        
        # Log iterator position before search
        iter_offset = iter_.get_offset()
        buffer_size = self._buffer.get_char_count()
        logger.debug(f"_search_next: calling forward() with iter_offset={iter_offset}, buffer_size={buffer_size}")
        
        ok, match_start, match_end, wrapped = self._search_context.forward(iter_)
        logger.debug(f"_search_next: forward() returned ok={ok}, wrapped={wrapped}")
        
        if ok:
            match_start_offset = match_start.get_offset()
            match_end_offset = match_end.get_offset()
            logger.debug(f"_search_next: match found at start_offset={match_start_offset}, end_offset={match_end_offset}")
            self._buffer.select_range(match_start, match_end)
            # Move cursor to the end of the match so next search starts from after it
            insert_mark = self._buffer.get_insert()
            self._buffer.move_mark(insert_mark, match_end)
            logger.debug(f"_search_next: moved cursor to end of match at offset={match_end_offset}")
            self._source_view.scroll_to_iter(match_start, 0.1, True, 0.0, 0.0)
        else:
            logger.debug("_search_next: no match found")
    
    def _search_prev(self) -> None:
        """Search for previous occurrence."""
        if not self._gtksource_enabled or not self._search_context:
            return
        
        insert_mark = self._buffer.get_insert()
        iter_ = self._buffer.get_iter_at_mark(insert_mark)
        
        ok, match_start, match_end, wrapped = self._search_context.backward(iter_)
        if ok:
            self._buffer.select_range(match_start, match_end)
            self._source_view.scroll_to_iter(match_start, 0.1, True, 0.0, 0.0)
    
    def _on_search_button_clicked(self, button: Gtk.Button) -> None:
        """Handle search button click - toggle search bar visibility."""
        if not self._gtksource_enabled or not self._search_toolbar:
            return
        visible = self._search_toolbar.get_visible()
        self._search_toolbar.set_visible(not visible)
        if not visible and self._search_entry:
            # Show and focus search entry when opening
            self._search_entry.grab_focus()
    
    def _on_search_changed(self, editable: Gtk.Editable) -> None:
        """Handle search entry text change."""
        self._update_search_settings()
        
        # According to GtkSource docs (https://gedit-text-editor.org/developer-docs/libgedit-gtksourceview-300/GtkSourceSearchContext.html),
        # "The buffer is scanned asynchronously, so it doesn't block the user interface.
        # For each search, the buffer is scanned at most once. After that, navigating through
        # the occurrences doesn't require to re-scan the buffer entirely."
        #
        # When search text changes, it's a new search pattern, so async scanning needs to start.
        # The async scanning is triggered when a search operation (forward/backward) is performed.
        # On macOS, highlighting may not appear until the async scanning starts. To ensure
        # highlighting works immediately when the user types, we trigger a search operation
        # which starts the async scanning process.
        #
        # Based on gedit source code patterns, we need to:
        # 1. Trigger the search to start async scanning
        # 2. Allow highlighting to appear before restoring cursor
        # 3. Use idle_add to restore cursor after UI update, preserving user's typing position
        if self._gtksource_enabled and self._search_context and self._search_entry:
            search_text = self._search_entry.get_text()
            if search_text:
                try:
                    # Save current cursor position before search
                    insert_mark = self._buffer.get_insert()
                    cursor_iter = self._buffer.get_iter_at_mark(insert_mark)
                    cursor_offset = cursor_iter.get_offset()
                    
                    # Trigger async scanning by performing a search from the start
                    # This starts the async scanning process which enables highlighting
                    # When search text changes, this ensures the new pattern is scanned
                    start_iter = self._buffer.get_start_iter()
                    ok, match_start, match_end, wrapped = self._search_context.forward(start_iter)
                    
                    # Restore cursor to original position after UI has updated
                    # Using idle_add ensures highlighting appears before cursor is restored
                    # This is especially important on macOS where highlighting may be delayed
                    def restore_cursor():
                        try:
                            cursor_iter = self._buffer.get_iter_at_offset(cursor_offset)
                            self._buffer.place_cursor(cursor_iter)
                        except Exception:
                            pass  # Cursor position may be invalid if buffer changed
                        return False  # Don't repeat
                    
                    GLib.idle_add(restore_cursor)
                    
                    # Scanning has now started (or restarted for new pattern), highlighting is active
                except Exception as e:
                    logger.debug(f"Error triggering search scan: {e}")
    
    def _on_search_activate(self, entry: Gtk.Entry) -> None:
        """Handle Enter key in search entry."""
        self._update_search_settings()
        self._search_next()
    
    def _on_search_next_clicked(self, button: Gtk.Button) -> None:
        """Handle search next button click."""
        logger.debug("_on_search_next_clicked: button clicked")
        self._update_search_settings()
        self._search_next()
    
    def _on_search_prev_clicked(self, button: Gtk.Button) -> None:
        """Handle search previous button click."""
        self._update_search_settings()
        self._search_prev()
    
    def _on_replace_clicked(self, button: Gtk.Button) -> None:
        """Replace current match and move to next."""
        if not self._gtksource_enabled or not self._search_context or not self._replace_entry:
            return
        
        self._update_search_settings()
        replace_text = self._replace_entry.get_text()
        
        # Check if there's a selection that might be a match
        try:
            has_selection, sel_start, sel_end = self._buffer.get_selection_bounds()
        except (ValueError, TypeError):
            has_selection = False
            sel_start = None
            sel_end = None
        
        # Get starting position for search
        if has_selection and sel_start is not None:
            # Start from the beginning of the selection
            search_start = sel_start.copy()
        else:
            # Start from cursor position
            insert_mark = self._buffer.get_insert()
            search_start = self._buffer.get_iter_at_mark(insert_mark)
        
        # Find the match using the search context
        # According to docs, replace() requires iterators from a valid search match
        ok, match_start, match_end, wrapped = self._search_context.forward(search_start)
        
        if not ok:
            # No match found
            return
        
        # Verify the match is at the expected position
        # If we had a selection, the match should start at or before the selection end
        if has_selection and sel_end is not None:
            # Check if match overlaps with selection (match should start before or at selection end)
            if match_start.compare(sel_end) > 0:
                # Match is after selection, which means selection wasn't a match
                # Use the found match
                pass
            elif match_end.compare(sel_start) < 0:
                # Match is before selection, shouldn't happen with forward search
                # Use the found match
                pass
        
        # Replace the match using the iterators from the search context
        # replace() requires: match_start, match_end, replace_text, replace_length
        # replace_length should be in bytes or -1 for null-terminated UTF-8 string
        # According to docs, after replace(), the iterators are revalidated to point to the replaced text
        ok = self._search_context.replace(match_start, match_end, replace_text, -1)
        if not ok:
            # Replace failed - the iterators might not correspond to a valid match
            return
        
        # After replace(), match_start and match_end iterators are revalidated to point to the replaced text
        # Select the replaced text and scroll to it
        self._buffer.select_range(match_start, match_end)
        self._source_view.scroll_to_iter(match_start, 0.1, True, 0.0, 0.0)
        
        # Move to next occurrence automatically
        # The iterators now point to the replaced text, so we can search from the end
        self._search_next()
    
    def _on_replace_all_clicked(self, button: Gtk.Button) -> None:
        """Replace all matches."""
        if not self._gtksource_enabled or not self._search_context or not self._replace_entry:
            return
        
        self._update_search_settings()
        replace_text = self._replace_entry.get_text()
        if not replace_text:
            return
        
        # replace_all() needs the replace text and its length (in bytes) or -1 for null-terminated UTF-8
        # Using -1 allows GTK to handle UTF-8 encoding automatically
        self._search_context.replace_all(replace_text, -1)
    
    # ---------- Undo/Redo methods ----------
    
    def _on_undo_clicked(self, button: Gtk.Button) -> None:
        """Handle undo button click."""
        if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
            if self._buffer.can_undo():
                self._buffer.undo()
    
    def _on_redo_clicked(self, button: Gtk.Button) -> None:
        """Handle redo button click."""
        if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
            if self._buffer.can_redo():
                self._buffer.redo()
    
    # ---------- Keyboard shortcuts ----------
    
    def _on_key_pressed(self, controller: Gtk.EventControllerKey, keyval: int, keycode: int, state: Gdk.ModifierType) -> bool:
        """Handle keyboard shortcuts."""
        ctrl = state & Gdk.ModifierType.CONTROL_MASK
        meta = state & Gdk.ModifierType.META_MASK
        shift = state & Gdk.ModifierType.SHIFT_MASK
        
        # Primary modifier: Meta on macOS, Ctrl on Linux/Windows
        primary = meta if is_macos() else ctrl
        
        # Primary+S -> save
        if primary and keyval == Gdk.KEY_s and not shift:
            if hasattr(self, '_save_button') and self._save_button.get_sensitive():
                self._on_save_clicked(self._save_button)
                return True
        
        # Primary+Plus/Equal -> zoom in
        if primary and (keyval == Gdk.KEY_plus or keyval == Gdk.KEY_equal):
            self.zoom_in()
            return True
        
        # Primary+Minus -> zoom out
        if primary and keyval == Gdk.KEY_minus:
            self.zoom_out()
            return True
        
        # Primary+0 -> reset zoom
        if primary and keyval == Gdk.KEY_0:
            self.reset_zoom()
            return True
        
        # Primary+F -> show search bar and focus search (only if GtkSource is available)
        if primary and keyval == Gdk.KEY_f:
            if self._gtksource_enabled and self._search_toolbar:
                # Show search bar if hidden
                if not self._search_toolbar.get_visible():
                    self._search_toolbar.set_visible(True)
                if self._search_entry:
                    self._search_entry.grab_focus()
                return True
        
        # Ctrl+Z -> undo
        if ctrl and keyval == Gdk.KEY_z and not shift:
            if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
                if self._buffer.can_undo():
                    self._buffer.undo()
                    return True
        
        # Ctrl+Shift+Z OR Ctrl+Y -> redo
        if (ctrl and shift and keyval == Gdk.KEY_z) or (ctrl and keyval == Gdk.KEY_y):
            if self._gtksource_enabled and isinstance(self._buffer, GtkSource.Buffer):
                if self._buffer.can_redo():
                    self._buffer.redo()
                    return True
        
        return False
    
    # ---------- Zoom controls ----------
    
    def _apply_zoom(self) -> None:
        """Apply current zoom level using CSS"""
        try:
            # Remove previous CSS provider if it exists
            if self._zoom_css_provider:
                style_context = self._source_view.get_style_context()
                style_context.remove_provider(self._zoom_css_provider)
            
            # Create CSS for zoom
            # Use a more specific selector that works for both GtkSource.View and Gtk.TextView
            zoom_percent = int(self._zoom_level * 100)
            css = f"""
            textview, text {{
                font-size: {zoom_percent}%;
            }}
            """
            
            # Apply CSS
            self._zoom_css_provider = Gtk.CssProvider()
            self._zoom_css_provider.load_from_data(css.encode('utf-8'))
            style_context = self._source_view.get_style_context()
            style_context.add_provider(
                self._zoom_css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        except Exception as e:
            logger.error(f"Failed to apply zoom: {e}")
    
    def zoom_in(self) -> None:
        """Zoom in the editor font"""
        self._zoom_level = min(self._zoom_level + 0.1, 3.0)  # Max zoom 300%
        self._apply_zoom()
        logger.debug(f"Editor zoomed in to {self._zoom_level:.1f}x")
    
    def zoom_out(self) -> None:
        """Zoom out the editor font"""
        self._zoom_level = max(self._zoom_level - 0.1, 0.5)  # Min zoom 50%
        self._apply_zoom()
        logger.debug(f"Editor zoomed out to {self._zoom_level:.1f}x")
    
    def reset_zoom(self) -> None:
        """Reset editor zoom to default (1.0x)"""
        self._zoom_level = 1.0
        self._apply_zoom()
        logger.debug("Editor zoom reset to 1.0x")

