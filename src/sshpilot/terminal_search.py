"""Terminal search overlay, extracted from TerminalWidget.

A composed object that ``TerminalWidget`` owns (``terminal._search``). It builds
and owns the search bar widgets and holds all search state, driving the active
backend (sync VTE or async PyXterm). It keeps a back-reference to the terminal
(``self.t``) for the backend, the VTE widget, and theme-color restoration.

Behavior is intentionally identical to the previous in-widget implementation;
this is a structural move, not a rewrite. ``TerminalWidget`` keeps thin
forwarders for the names external code binds (``_show_search_overlay``,
``_hide_search_overlay``, ``handle_search_result``, ``handle_search_results``,
``search_text``, and the ``search_revealer`` property).
"""

import logging
import re
from gettext import gettext as _

from gi.repository import Gtk, Gdk, Vte

logger = logging.getLogger(__name__)


class TerminalSearch:
    def __init__(self, terminal):
        self.t = terminal

        self._search_key_controller = None
        self._last_search_text = ''
        self._last_search_case_sensitive = False
        self._last_search_regex = False
        self._search_has_match = False

        self._build_ui()

    # -- UI construction --------------------------------------------------------

    def _build_ui(self):
        # Search overlay elements (revealer styled like other banners)
        self.search_revealer = Gtk.Revealer()
        self.search_revealer.set_reveal_child(False)
        self.search_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.search_revealer.set_halign(Gtk.Align.FILL)
        self.search_revealer.set_valign(Gtk.Align.START)
        self.search_revealer.set_hexpand(True)

        search_banner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        search_banner.add_css_class('banner')
        search_banner.set_hexpand(True)

        search_header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        search_header.set_margin_start(12)
        search_header.set_margin_end(12)
        search_header.set_margin_top(8)
        search_header.set_margin_bottom(4)

        search_title = Gtk.Label(label=_("Search Terminal"))
        search_title.set_xalign(0)
        search_title.set_hexpand(True)
        search_title.add_css_class('title-4')
        search_header.append(search_title)

        from sshpilot import icon_utils
        self.search_close_button = Gtk.Button()
        icon_utils.set_button_icon(self.search_close_button, 'window-close-symbolic')
        self.search_close_button.add_css_class('flat')
        self.search_close_button.set_valign(Gtk.Align.CENTER)
        self.search_close_button.connect('clicked', lambda *_a: self._hide_search_overlay())
        self.search_close_button.set_tooltip_text(_("Close terminal search"))
        search_header.append(self.search_close_button)

        search_banner.append(search_header)

        search_controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        search_controls.set_margin_start(12)
        search_controls.set_margin_end(12)
        search_controls.set_margin_bottom(8)

        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(_("Search terminal history"))
        self.search_entry.set_hexpand(True)
        self.search_entry.connect('search-changed', self._on_search_entry_changed)
        self.search_entry.connect('activate', self._on_search_entry_activate)
        self.search_entry.connect('stop-search', self._on_search_entry_stop)

        entry_key_controller = Gtk.EventControllerKey()
        entry_key_controller.connect('key-pressed', self._on_search_entry_key_pressed)
        self.search_entry.add_controller(entry_key_controller)

        search_controls.append(self.search_entry)

        self.search_count_label = Gtk.Label(label="")
        self.search_count_label.add_css_class("dim-label")
        self.search_count_label.set_width_chars(7)
        self.search_count_label.set_xalign(1)
        search_controls.append(self.search_count_label)

        self.search_prev_button = Gtk.Button()
        icon_utils.set_button_icon(self.search_prev_button, 'go-up-symbolic')
        self.search_prev_button.set_tooltip_text(_("Find previous match"))
        self.search_prev_button.connect('clicked', self._on_search_previous)
        self.search_prev_button.set_sensitive(False)
        search_controls.append(self.search_prev_button)

        self.search_next_button = Gtk.Button()
        icon_utils.set_button_icon(self.search_next_button, 'go-down-symbolic')
        self.search_next_button.set_tooltip_text(_("Find next match"))
        self.search_next_button.connect('clicked', self._on_search_next)
        self.search_next_button.set_sensitive(False)
        search_controls.append(self.search_next_button)

        search_banner.append(search_controls)
        self.search_revealer.set_child(search_banner)

        # Install CSS for search banner to ensure solid background
        try:
            display = Gdk.Display.get_default()
            if display and not getattr(display, '_sshpilot_search_banner_css_installed', False):
                css_provider = Gtk.CssProvider()
                css_provider.load_from_data(b"""
                    .search-banner {
                        background-color: @headerbar_bg_color;
                        color: @headerbar_fg_color;
                        border-bottom: 1px solid @borders;
                    }
                """)
                Gtk.StyleContext.add_provider_for_display(
                    display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                )
                setattr(display, '_sshpilot_search_banner_css_installed', True)
        except Exception:
            pass

        # Add the search-banner CSS class to ensure solid background
        search_banner.add_css_class('search-banner')

    # -- Key controller (installed on the VTE widget) ---------------------------

    def _ensure_search_key_controller(self):
        """Attach the search shortcut controller to the terminal if needed."""
        if getattr(self, '_search_key_controller', None) is not None:
            return

        try:
            controller = Gtk.EventControllerKey()
            controller.connect('key-pressed', self._on_vte_search_key_pressed)
            if self.t.vte is not None:
                self.t.vte.add_controller(controller)
            elif self.t.terminal_widget is not None:
                self.t.terminal_widget.add_controller(controller)
            self._search_key_controller = controller
            logger.debug("Search key controller installed")
        except Exception as exc:
            logger.debug("Failed to install search key controller: %s", exc)

    def teardown_key_controller(self):
        """Detach the search key controller from the VTE widget."""
        search_ctrl = getattr(self, '_search_key_controller', None)
        if search_ctrl is not None:
            try:
                if hasattr(self.t.vte, 'remove_controller'):
                    self.t.vte.remove_controller(search_ctrl)
            except Exception as exc:
                logger.debug("Failed to remove search key controller: %s", exc)
            finally:
                self._search_key_controller = None

    def _on_vte_search_key_pressed(self, controller, keyval, keycode, state):
        """Handle global terminal search shortcuts on the VTE widget."""
        try:
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            primary = bool(state & Gdk.ModifierType.CONTROL_MASK)
            meta = bool(state & Gdk.ModifierType.META_MASK)

            if keyval in (Gdk.KEY_f, Gdk.KEY_F) and (primary or meta):
                if hasattr(self, 'search_revealer') and self.search_revealer.get_reveal_child():
                    self._hide_search_overlay()
                else:
                    self._show_search_overlay(select_all=True)
                return True

            if keyval in (Gdk.KEY_g, Gdk.KEY_G) and (primary or meta):
                if shift:
                    self._on_search_previous()
                else:
                    self._on_search_next()
                return True

            if keyval == Gdk.KEY_Escape and hasattr(self, 'search_revealer') and self.search_revealer.get_reveal_child():
                self._hide_search_overlay()
                return True
        except Exception as exc:
            logger.debug("Terminal search key handling failed: %s", exc)
        return False

    # -- Overlay show/hide ------------------------------------------------------

    def _apply_search_highlight_colors(self):
        """Switch VTE highlight to a high-contrast color while search is active."""
        try:
            if self.t.vte is None:
                return
            search_bg = Gdk.RGBA()
            search_bg.parse('#F4D03F')  # Bright amber — visible on dark and light backgrounds
            search_fg = Gdk.RGBA()
            search_fg.parse('#000000')
            if hasattr(self.t.vte, 'set_color_highlight'):
                self.t.vte.set_color_highlight(search_bg)
            if hasattr(self.t.vte, 'set_color_highlight_foreground'):
                self.t.vte.set_color_highlight_foreground(search_fg)
        except Exception as exc:
            logger.debug("Failed to apply search highlight colors: %s", exc)

    def _show_search_overlay(self, select_all: bool = False):
        """Reveal the terminal search overlay and focus the search entry."""
        try:
            if not hasattr(self, 'search_revealer') or not self.search_revealer:
                return
            self._apply_search_highlight_colors()
            self.search_revealer.set_reveal_child(True)
            if hasattr(self, 'search_entry') and self.search_entry:
                if select_all:
                    try:
                        self.search_entry.select_region(0, -1)
                    except Exception:
                        pass
                self.search_entry.grab_focus()
                self._set_search_navigation_sensitive(bool(self.search_entry.get_text()))
        except Exception as exc:
            logger.debug("Failed to show search overlay: %s", exc)

    def _hide_search_overlay(self):
        """Hide the search overlay and return focus to the terminal."""
        try:
            if hasattr(self, 'search_revealer') and self.search_revealer:
                self.search_revealer.set_reveal_child(False)
            self._set_search_error_state(False)
            self._update_search_count_label(-1, 0)
            if self.t.backend and hasattr(self.t.backend, "clear_search_decorations"):
                try:
                    self.t.backend.clear_search_decorations()
                except Exception:
                    pass
            if self.t.backend:
                self.t.backend.grab_focus()
            # Restore theme selection color now that search is closed
            self.t._apply_cursor_and_selection_colors()
        except Exception as exc:
            logger.debug("Failed to hide search overlay: %s", exc)

    def _set_search_navigation_sensitive(self, active: bool):
        """Enable or disable navigation buttons based on active search state."""
        try:
            for button in (getattr(self, 'search_prev_button', None), getattr(self, 'search_next_button', None)):
                if button is not None:
                    button.set_sensitive(bool(active))
        except Exception as exc:
            logger.debug("Failed to update search navigation sensitivity: %s", exc)

    def _set_search_error_state(self, has_error: bool):
        """Toggle error styling on the search entry when matches are not found."""
        entry = getattr(self, 'search_entry', None)
        if not entry:
            return
        try:
            if has_error:
                entry.add_css_class('error')
            else:
                entry.remove_css_class('error')
        except Exception:
            pass

    def _clear_search_pattern(self):
        """Clear any active search pattern from the terminal."""
        self._last_search_text = ''
        self._last_search_case_sensitive = False
        self._last_search_regex = False
        self._search_has_match = False
        self._set_search_navigation_sensitive(False)
        self._set_search_error_state(False)
        self._update_search_count_label(-1, 0)
        try:
            if self.t.backend:
                if hasattr(self.t.backend, "search_set_query"):
                    self.t.backend.search_set_query(None)
                else:
                    self.t.backend.search_set_regex(None)
        except Exception:
            pass

    def _update_search_count_label(self, result_index: int, result_count: int) -> None:
        label = getattr(self, "search_count_label", None)
        if label is None:
            return
        try:
            if result_count <= 0:
                label.set_text("")
            elif result_index < 0:
                # SearchAddon: resultIndex == -1 when highlight threshold exceeded.
                label.set_text(f"{result_count}+")
            else:
                label.set_text(f"{result_index + 1}/{result_count}")
        except Exception:
            pass

    def handle_search_result(
        self,
        found: bool,
        *,
        result_index: int = -1,
        result_count: int = 0,
    ) -> None:
        """PyXterm async findNext/findPrevious result (script-message)."""
        text = ""
        if getattr(self, "search_entry", None):
            text = self.search_entry.get_text() or ""
        if result_count > 0:
            self._search_has_match = True
        else:
            self._search_has_match = bool(found)
        if text:
            self._set_search_error_state(not self._search_has_match)
        else:
            self._set_search_error_state(False)
        if result_count > 0 or result_index >= 0:
            self._update_search_count_label(result_index, result_count)

    def handle_search_results(self, result_index: int, result_count: int) -> None:
        """PyXterm SearchAddon.onDidChangeResults (decorations enabled)."""
        text = ""
        if getattr(self, "search_entry", None):
            text = self.search_entry.get_text() or ""
        self._search_has_match = result_count > 0
        if text:
            self._set_search_error_state(result_count <= 0)
        self._update_search_count_label(result_index, result_count)

    def _is_pyxterm_backend(self) -> bool:
        backend = self.t.backend
        if backend is None:
            return False
        return type(backend).__name__ in ("PyXtermTerminalBackend", "PyXtermBridgeBackend")

    def _update_search_pattern(self, text: str, *, case_sensitive: bool = False, regex: bool = False,
                                move_forward: bool = True, update_entry: bool = False) -> bool:
        """Apply or update the search pattern on the active terminal backend."""
        if not text:
            self._clear_search_pattern()
            return False

        pattern_changed = (
            text != self._last_search_text or
            case_sensitive != self._last_search_case_sensitive or
            regex != self._last_search_regex
        )

        try:
            if pattern_changed:
                self._search_has_match = False
                self._update_search_count_label(-1, 0)
                if self.t.backend:
                    if hasattr(self.t.backend, "search_set_query"):
                        # Pass the raw user term; backends apply escape/flags themselves.
                        self.t.backend.search_set_query(
                            text, case_sensitive=case_sensitive, regex=regex
                        )
                    elif hasattr(self.t.backend, "vte") and self.t.backend.vte:
                        pattern = text if regex else re.escape(text)
                        if not case_sensitive and not pattern.startswith("(?i)"):
                            pattern = "(?i)" + pattern
                        search_regex = Vte.Regex.new_for_search(pattern, -1, 0)
                        self.t.backend.search_set_regex(search_regex)
                        if hasattr(self.t.backend.vte, "search_set_wrap_around"):
                            self.t.backend.vte.search_set_wrap_around(True)

                self._last_search_text = text
                self._last_search_case_sensitive = case_sensitive
                self._last_search_regex = regex

            self._set_search_navigation_sensitive(True)

            if move_forward:
                return self._run_search(True, update_entry=update_entry, from_text_change=True)

            if update_entry:
                self._set_search_error_state(False)

            return True
        except Exception as exc:
            logger.error(f"Search failed: {exc}")
            if update_entry:
                self._set_search_error_state(True)
            return False

    def _run_search(self, forward: bool = True, *, update_entry: bool = False,
                    from_text_change: bool = False) -> bool:
        """Execute search navigation in the requested direction."""
        try:
            if self.t.backend:
                found = self.t.backend.search_find_next() if forward else self.t.backend.search_find_previous()
            else:
                found = False
        except Exception as exc:
            logger.error(f"Search navigation failed: {exc}")
            found = False

        # PyXterm reports found/not-found asynchronously via handle_search_result.
        if self._is_pyxterm_backend():
            return True

        if found:
            self._search_has_match = True

        if update_entry:
            # Only show the error state when the pattern has no matches at all.
            # When navigating (Enter / Ctrl+G), VTE may return False for a single
            # already-highlighted match even with wrap-around enabled — that is not
            # a "no results" condition, so don't turn the entry red.
            no_match = not found and (from_text_change or not self._search_has_match)
            self._set_search_error_state(no_match)

        return bool(found)

    def _on_search_entry_changed(self, entry):
        """React to text edits in the search entry."""
        text = entry.get_text() if entry else ''
        if not text:
            self._clear_search_pattern()
            return
        self._update_search_pattern(text, move_forward=True, update_entry=True)

    def _on_search_entry_activate(self, entry):
        """Handle Enter key in the search entry."""
        self._on_search_next()

    def _on_search_entry_stop(self, entry):
        """Handle stop-search events (Escape or clear button)."""
        if not entry.get_text():
            self._clear_search_pattern()
        self._hide_search_overlay()

    def _on_search_entry_key_pressed(self, controller, keyval, keycode, state):
        """Handle additional shortcuts while the search entry is focused."""
        try:
            shift = bool(state & Gdk.ModifierType.SHIFT_MASK)
            primary = bool(state & Gdk.ModifierType.CONTROL_MASK)
            meta = bool(state & Gdk.ModifierType.META_MASK)

            if keyval in (Gdk.KEY_g, Gdk.KEY_G) and (primary or meta):
                if shift:
                    self._on_search_previous()
                else:
                    self._on_search_next()
                return True

            if keyval in (Gdk.KEY_f, Gdk.KEY_F) and (primary or meta):
                self._hide_search_overlay()
                return True

            if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and shift:
                self._on_search_previous()
                return True

            if keyval == Gdk.KEY_Escape:
                self._hide_search_overlay()
                return True
        except Exception as exc:
            logger.debug("Search entry key handling failed: %s", exc)
        return False

    def _on_search_next(self, *_args):
        """Navigate to the next search match."""
        text = ''
        if hasattr(self, 'search_entry') and self.search_entry:
            text = self.search_entry.get_text()

        if text:
            if not self._update_search_pattern(text, move_forward=False, update_entry=True):
                return False
        elif not self._last_search_text:
            return False

        return self._run_search(True, update_entry=True)

    def _on_search_previous(self, *_args):
        """Navigate to the previous search match."""
        text = ''
        if hasattr(self, 'search_entry') and self.search_entry:
            text = self.search_entry.get_text()

        if text:
            if not self._update_search_pattern(text, move_forward=False, update_entry=True):
                return False
        elif not self._last_search_text:
            return False

        return self._run_search(False, update_entry=True)

    def search_text(self, text, case_sensitive=False, regex=False):
        """Search for text in terminal"""
        return self._update_search_pattern(
            text,
            case_sensitive=case_sensitive,
            regex=regex,
            move_forward=True,
            update_entry=False,
        )
