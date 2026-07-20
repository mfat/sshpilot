"""Terminal fullscreen mode, extracted from TerminalWidget.

A composed controller that ``TerminalWidget`` owns (``terminal._fullscreen``).
It holds all fullscreen state and drives the toplevel window (sidebar, header
bar, tab bar, banners, CSS, key controllers) when toggling fullscreen. It keeps
a back-reference to the terminal (``self.t``) for ``get_root()``, the backend,
the VTE widget, focus, and controller attachment.

Behavior is intentionally identical to the previous in-widget implementation;
this is a structural move. The only consolidation: the two identical
``restore_focus`` closures (enter/exit) are now a single ``_restore_focus``
method.
"""

import logging
from gettext import gettext as _

from gi.repository import Gtk, GLib, Adw  # Gdk is imported locally where needed

logger = logging.getLogger(__name__)


class FullscreenController:
    def __init__(self, terminal):
        self.t = terminal

        self._is_fullscreen = False
        self._fullscreen_sidebar_visible = None
        self._fullscreen_header_visible = None
        self._fullscreen_tab_bar_visible = None
        self._fullscreen_css_provider = None
        self._was_maximized = False
        self._fullscreen_banner_container = None
        self._fullscreen_banner_dismiss_button = None
        self._fullscreen_key_controller = None
        self._fullscreen_sidebar_collapsed = None
        # Previously created lazily inside _enter_fullscreen; initialized here for
        # consistency so exit-time hasattr/None checks behave the same on cold paths.
        self._fullscreen_sidebar_show_content = None
        self._fullscreen_update_banner_visible = None
        self._fullscreen_tips_banner_visible = None
        self._fullscreen_broadcast_banner_visible = None

    def _restore_focus(self):
        try:
            if hasattr(self.t, 'backend') and self.t.backend:
                self.t.backend.grab_focus()
            elif hasattr(self.t, 'vte') and hasattr(self.t.vte, 'grab_focus'):
                self.t.vte.grab_focus()
            elif hasattr(self.t, 'grab_focus'):
                self.t.grab_focus()
        except Exception as e:
            logger.debug(f"Failed to restore focus after fullscreen change: {e}")
        return False

    def setup_shortcut(self):
        """Setup F11 keyboard shortcut for fullscreen toggle and ESC to exit fullscreen."""
        try:
            from gi.repository import Gdk

            # Create keyboard controller for F11 and ESC
            key_controller = Gtk.EventControllerKey()
            key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

            def on_key_pressed(controller, keyval, keycode, state):
                # F11 key - toggle fullscreen
                if keyval == Gdk.KEY_F11:
                    self.toggle_fullscreen()
                    return True
                # ESC key - exit fullscreen if currently in fullscreen
                elif keyval == Gdk.KEY_Escape and self._is_fullscreen:
                    self._exit_fullscreen()
                    return True  # Consume ESC to prevent NavigationSplitView from showing sidebar
                return False

            key_controller.connect('key-pressed', on_key_pressed)
            self.t.add_controller(key_controller)
            logger.debug("Fullscreen shortcut (F11) and ESC exit registered")
        except Exception as e:
            logger.debug(f"Failed to setup fullscreen shortcut: {e}", exc_info=True)

    def toggle_fullscreen(self):
        """Toggle fullscreen mode for the terminal widget."""
        if self._is_fullscreen:
            self._exit_fullscreen()
        else:
            self._enter_fullscreen()

    def _enter_fullscreen(self):
        """Enter fullscreen mode - hide sidebar, header bar, and tab bar."""
        if self._is_fullscreen:
            return

        try:
            root = self.t.get_root()
            if not root:
                logger.debug("Cannot enter fullscreen: window not found")
                return

            # Store current state
            self._fullscreen_sidebar_visible = None
            self._fullscreen_sidebar_show_content = None
            self._fullscreen_header_visible = None
            self._fullscreen_tab_bar_visible = None
            self._fullscreen_update_banner_visible = None
            self._fullscreen_tips_banner_visible = None
            self._fullscreen_broadcast_banner_visible = None

            # Store window state before going fullscreen
            try:
                # Check if window is maximized
                if hasattr(root, 'is_maximized'):
                    self._was_maximized = root.is_maximized()
                elif hasattr(root, 'get_maximized'):
                    self._was_maximized = root.get_maximized()
            except Exception:
                self._was_maximized = False

            # Hide sidebar if it exists
            if hasattr(root, 'split_view'):
                try:
                    # Check split view type using _split_variant attribute or method detection
                    split_variant = getattr(root, '_split_variant', None)
                    HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
                    HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')

                    if HAS_OVERLAY_SPLIT and split_variant == 'overlay':
                        # OverlaySplitView: use set_show_sidebar (simpler API)
                        if hasattr(root.split_view, 'get_show_sidebar'):
                            self._fullscreen_sidebar_visible = root.split_view.get_show_sidebar()
                            root.split_view.set_show_sidebar(False)
                            logger.debug("OverlaySplitView sidebar hidden for fullscreen")
                    elif HAS_NAV_SPLIT and split_variant == 'navigation':
                        # NavigationSplitView: use collapsed and show_content
                        try:
                            # Store original state
                            self._fullscreen_sidebar_collapsed = root.split_view.get_collapsed()
                            self._fullscreen_sidebar_show_content = root.split_view.get_show_content()
                            # Hide sidebar: collapse and show content (content visible, sidebar hidden)
                            root.split_view.set_collapsed(True)
                            root.split_view.set_show_content(True)
                            logger.debug("NavigationSplitView sidebar hidden for fullscreen")
                        except Exception as e:
                            logger.debug(f"Failed to hide NavigationSplitView sidebar: {e}")
                    elif split_variant == 'paned':
                        # Gtk.Paned: hide the start child widget
                        sidebar_widget = root.split_view.get_start_child()
                        if sidebar_widget:
                            self._fullscreen_sidebar_visible = sidebar_widget.get_visible()
                            sidebar_widget.set_visible(False)
                            logger.debug("Gtk.Paned sidebar hidden for fullscreen")
                    else:
                        # Fallback: try common methods
                        if hasattr(root.split_view, 'get_show_sidebar'):
                            self._fullscreen_sidebar_visible = root.split_view.get_show_sidebar()
                            root.split_view.set_show_sidebar(False)
                        elif hasattr(root.split_view, 'get_sidebar_visible'):
                            self._fullscreen_sidebar_visible = root.split_view.get_sidebar_visible()
                            root.split_view.set_sidebar_visible(False)
                except Exception as e:
                    logger.debug(f"Failed to hide sidebar: {e}", exc_info=True)

            # Hide header bar - it's added to ToolbarView via add_top_bar()
            # Try multiple methods to ensure it's hidden
            if hasattr(root, 'header_bar'):
                try:
                    self._fullscreen_header_visible = root.header_bar.get_visible()
                    # Method 1: Direct visibility
                    root.header_bar.set_visible(False)
                    # Method 2: Also try hide() method
                    if hasattr(root.header_bar, 'hide'):
                        root.header_bar.hide()
                    logger.debug("Header bar hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide header bar: {e}", exc_info=True)
            else:
                logger.debug("header_bar attribute not found on root window")

            # Hide tab bar if it exists
            if hasattr(root, 'tab_bar'):
                try:
                    self._fullscreen_tab_bar_visible = root.tab_bar.get_visible()
                    root.tab_bar.set_visible(False)
                    # Also try hide() method
                    if hasattr(root.tab_bar, 'hide'):
                        root.tab_bar.hide()
                    logger.debug("Tab bar hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide tab bar: {e}", exc_info=True)
            else:
                logger.debug("tab_bar attribute not found on root window")

            # Hide update banner if it exists
            if hasattr(root, 'update_banner_container'):
                try:
                    self._fullscreen_update_banner_visible = root.update_banner_container.get_visible()
                    root.update_banner_container.set_visible(False)
                    logger.debug("Update banner hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide update banner: {e}", exc_info=True)

            # Hide tips banner if it exists
            if hasattr(root, 'tips_banner_container'):
                try:
                    self._fullscreen_tips_banner_visible = root.tips_banner_container.get_visible()
                    root.tips_banner_container.set_visible(False)
                    logger.debug("Tips banner hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide tips banner: {e}", exc_info=True)

            # Hide broadcast banner if it exists
            if hasattr(root, 'broadcast_banner'):
                try:
                    self._fullscreen_broadcast_banner_visible = root.broadcast_banner.get_visible()
                    root.broadcast_banner.set_visible(False)
                    logger.debug("Broadcast banner hidden for fullscreen")
                except Exception as e:
                    logger.debug(f"Failed to hide broadcast banner: {e}", exc_info=True)

            # Add CSS class to window for targeted hiding of header bar and tab bar
            try:
                root.add_css_class('terminal-fullscreen-mode')
                logger.debug("Added terminal-fullscreen-mode CSS class to window")
            except Exception as e:
                logger.debug(f"Failed to add CSS class: {e}")

            # Use CSS as a fallback to hide header bar and tab bar
            try:
                from gi.repository import Gdk
                display = Gdk.Display.get_default()
                if display:
                    css_provider = Gtk.CssProvider()
                    css = """
                    .terminal-fullscreen-mode headerbar {
                        opacity: 0 !important;
                        min-height: 0 !important;
                        max-height: 0 !important;
                        margin: 0 !important;
                        padding: 0 !important;
                    }
                    .terminal-fullscreen-mode tabbar {
                        opacity: 0 !important;
                        min-height: 0 !important;
                        max-height: 0 !important;
                        margin: 0 !important;
                        padding: 0 !important;
                    }
                    """
                    css_provider.load_from_data(css.encode('utf-8'))
                    Gtk.StyleContext.add_provider_for_display(
                        display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                    )
                    self._fullscreen_css_provider = css_provider
                    logger.debug("Applied CSS to hide header bar and tab bar")
            except Exception as e:
                logger.debug(f"Failed to apply CSS for fullscreen: {e}")

            # Make the window fullscreen (takes up entire screen)
            try:
                if hasattr(root, 'fullscreen'):
                    root.fullscreen()
                elif hasattr(root, 'set_fullscreen'):
                    root.set_fullscreen(True)
                logger.debug("Window set to fullscreen")
            except Exception as e:
                logger.debug(f"Failed to set window fullscreen: {e}", exc_info=True)

            # Add window-level key controller to catch ESC before NavigationSplitView handles it
            try:
                from gi.repository import Gdk
                self._fullscreen_key_controller = Gtk.EventControllerKey()
                self._fullscreen_key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)

                def on_fullscreen_key_pressed(controller, keyval, keycode, state):
                    # ESC key - exit fullscreen
                    if keyval == Gdk.KEY_Escape and self._is_fullscreen:
                        self._exit_fullscreen()
                        return True  # Consume ESC to prevent NavigationSplitView from showing sidebar
                    return False

                self._fullscreen_key_controller.connect('key-pressed', on_fullscreen_key_pressed)
                root.add_controller(self._fullscreen_key_controller)
                logger.debug("Window-level ESC handler added for fullscreen mode")
            except Exception as e:
                logger.debug(f"Failed to add window-level ESC handler: {e}", exc_info=True)

            # Create fullscreen banner container if it doesn't exist (but don't show it yet)
            if self._fullscreen_banner_container is None:
                self._create_fullscreen_banner()

            # Add banner to window level (above terminal) if not already added
            if self._fullscreen_banner_container and self._fullscreen_banner_container.get_parent() is None:
                # Find the content wrapper that contains banners and content_stack
                try:
                    if hasattr(root, 'tab_overview'):
                        parent = root.tab_overview.get_parent()
                        if parent:
                            # Prepend banner at the beginning to appear above everything
                            parent.prepend(self._fullscreen_banner_container)
                            logger.debug("Fullscreen banner added to window content area")
                    elif hasattr(root, 'tab_content_box'):
                        parent = root.tab_content_box.get_parent()
                        if parent:
                            parent.prepend(self._fullscreen_banner_container)
                            logger.debug("Fullscreen banner added to window content area")
                except Exception as e:
                    logger.debug(f"Failed to add fullscreen banner to window: {e}", exc_info=True)

            # Show fullscreen banner with help text
            self._show_fullscreen_banner()

            self._is_fullscreen = True
            logger.debug("Entered terminal fullscreen mode")

            # Restore focus to terminal after fullscreen operations
            GLib.idle_add(self._restore_focus)
        except Exception as e:
            logger.error(f"Failed to enter fullscreen: {e}", exc_info=True)

    def _exit_fullscreen(self):
        """Exit fullscreen mode - restore sidebar, header bar, and tab bar."""
        if not self._is_fullscreen:
            return

        try:
            root = self.t.get_root()
            if not root:
                logger.debug("Cannot exit fullscreen: window not found")
                self._is_fullscreen = False
                return

            # Restore sidebar
            if hasattr(root, 'split_view') and self._fullscreen_sidebar_visible is not None:
                try:
                    # Check split view type using _split_variant attribute or method detection
                    split_variant = getattr(root, '_split_variant', None)
                    HAS_OVERLAY_SPLIT = hasattr(Adw, 'OverlaySplitView')
                    HAS_NAV_SPLIT = hasattr(Adw, 'NavigationSplitView')

                    if HAS_OVERLAY_SPLIT and split_variant == 'overlay':
                        # OverlaySplitView: use set_show_sidebar (simpler API)
                        if hasattr(root.split_view, 'set_show_sidebar'):
                            root.split_view.set_show_sidebar(self._fullscreen_sidebar_visible)
                            logger.debug("OverlaySplitView sidebar restored")
                    elif HAS_NAV_SPLIT and split_variant == 'navigation':
                        # NavigationSplitView: restore using collapsed and show_content
                        try:
                            if hasattr(self, '_fullscreen_sidebar_collapsed') and hasattr(self, '_fullscreen_sidebar_show_content'):
                                root.split_view.set_collapsed(self._fullscreen_sidebar_collapsed)
                                root.split_view.set_show_content(self._fullscreen_sidebar_show_content)
                                logger.debug(f"NavigationSplitView restored: collapsed={self._fullscreen_sidebar_collapsed}, show_content={self._fullscreen_sidebar_show_content}")
                            else:
                                # Fallback: if we don't have stored state, un-collapse to show both
                                root.split_view.set_collapsed(False)
                                logger.debug("NavigationSplitView restored to default (un-collapsed)")
                        except Exception as e:
                            logger.debug(f"Failed to restore NavigationSplitView sidebar: {e}")
                    elif split_variant == 'paned':
                        # Gtk.Paned: show the start child widget
                        sidebar_widget = root.split_view.get_start_child()
                        if sidebar_widget:
                            sidebar_widget.set_visible(self._fullscreen_sidebar_visible)
                            logger.debug("Gtk.Paned sidebar restored")
                    else:
                        # Fallback: try common methods
                        if hasattr(root.split_view, 'set_show_sidebar'):
                            root.split_view.set_show_sidebar(self._fullscreen_sidebar_visible)
                        elif hasattr(root.split_view, 'set_sidebar_visible'):
                            root.split_view.set_sidebar_visible(self._fullscreen_sidebar_visible)
                except Exception as e:
                    logger.debug(f"Failed to restore sidebar: {e}", exc_info=True)

            # Remove CSS class from window
            try:
                root.remove_css_class('terminal-fullscreen-mode')
                logger.debug("Removed terminal-fullscreen-mode CSS class from window")
            except Exception as e:
                logger.debug(f"Failed to remove CSS class: {e}")

            # Remove CSS provider if it was added
            if self._fullscreen_css_provider:
                try:
                    from gi.repository import Gdk
                    display = Gdk.Display.get_default()
                    if display:
                        Gtk.StyleContext.remove_provider_for_display(
                            display, self._fullscreen_css_provider
                        )
                    self._fullscreen_css_provider = None
                    logger.debug("Removed fullscreen CSS provider")
                except Exception as e:
                    logger.debug(f"Failed to remove CSS provider: {e}")

            # Restore header bar
            if hasattr(root, 'header_bar') and self._fullscreen_header_visible is not None:
                try:
                    root.header_bar.set_visible(self._fullscreen_header_visible)
                    # Also try show() method
                    if hasattr(root.header_bar, 'show'):
                        root.header_bar.show()
                except Exception as e:
                    logger.debug(f"Failed to restore header bar: {e}")

            # Restore tab bar
            if hasattr(root, 'tab_bar') and self._fullscreen_tab_bar_visible is not None:
                try:
                    root.tab_bar.set_visible(self._fullscreen_tab_bar_visible)
                    # Also try show() method
                    if hasattr(root.tab_bar, 'show'):
                        root.tab_bar.show()
                except Exception as e:
                    logger.debug(f"Failed to restore tab bar: {e}")

            # Restore update banner
            if hasattr(root, 'update_banner_container') and self._fullscreen_update_banner_visible is not None:
                try:
                    root.update_banner_container.set_visible(self._fullscreen_update_banner_visible)
                except Exception as e:
                    logger.debug(f"Failed to restore update banner: {e}")

            # Restore tips banner
            if hasattr(root, 'tips_banner_container') and self._fullscreen_tips_banner_visible is not None:
                try:
                    root.tips_banner_container.set_visible(self._fullscreen_tips_banner_visible)
                except Exception as e:
                    logger.debug(f"Failed to restore tips banner: {e}")

            # Restore broadcast banner
            if hasattr(root, 'broadcast_banner') and self._fullscreen_broadcast_banner_visible is not None:
                try:
                    root.broadcast_banner.set_visible(self._fullscreen_broadcast_banner_visible)
                except Exception as e:
                    logger.debug(f"Failed to restore broadcast banner: {e}")

            # Unfullscreen the window
            try:
                if hasattr(root, 'unfullscreen'):
                    root.unfullscreen()
                elif hasattr(root, 'set_fullscreen'):
                    root.set_fullscreen(False)
                logger.debug("Window unfullscreen")
            except Exception as e:
                logger.debug(f"Failed to unfullscreen window: {e}", exc_info=True)

            # Restore maximized state if it was maximized before
            if self._was_maximized:
                try:
                    if hasattr(root, 'maximize'):
                        root.maximize()
                    elif hasattr(root, 'set_maximized'):
                        root.set_maximized(True)
                except Exception as e:
                    logger.debug(f"Failed to restore maximized state: {e}")

            # Remove window-level key controller
            if self._fullscreen_key_controller:
                try:
                    root.remove_controller(self._fullscreen_key_controller)
                    self._fullscreen_key_controller = None
                    logger.debug("Window-level ESC handler removed")
                except Exception as e:
                    logger.debug(f"Failed to remove window-level ESC handler: {e}", exc_info=True)

            # Hide fullscreen banner
            self._hide_fullscreen_banner()

            # Remove banner from window when exiting fullscreen
            if self._fullscreen_banner_container:
                try:
                    parent = self._fullscreen_banner_container.get_parent()
                    if parent:
                        parent.remove(self._fullscreen_banner_container)
                        logger.debug("Fullscreen banner removed from window")
                except Exception as e:
                    logger.debug(f"Failed to remove fullscreen banner from window: {e}", exc_info=True)

            self._is_fullscreen = False
            logger.debug("Exited terminal fullscreen mode")

            # Restore focus to terminal after exiting fullscreen
            GLib.idle_add(self._restore_focus)
        except Exception as e:
            logger.error(f"Failed to exit fullscreen: {e}", exc_info=True)
            self._is_fullscreen = False

    def _create_fullscreen_banner(self):
        """Create fullscreen banner container (but don't show it yet)."""
        try:
            # Create banner container if it doesn't exist (matching update banner style)
            if self._fullscreen_banner_container is None:
                # Use overlay to position dismiss button on top of banner (same as update banner)
                banner_overlay = Gtk.Overlay()
                # Make overlay expand to full width
                banner_overlay.set_hexpand(True)

                fullscreen_banner = Adw.Banner()
                fullscreen_banner.set_title(_("Press F11 to exit fullscreen mode"))
                fullscreen_banner.set_button_label(_("Exit Fullscreen"))

                # Set button style to "suggested" and connect click handler
                try:
                    # Use set_button_style if available (Adw 1.7+)
                    if hasattr(fullscreen_banner, 'set_button_style'):
                        # Use enum value for suggested style
                        fullscreen_banner.set_button_style(Adw.BannerButtonStyle.SUGGESTED)
                    else:
                        # Fallback: Apply suggested style via CSS
                        from gi.repository import Gdk
                        display = Gdk.Display.get_default()
                        if display:
                            css_provider = Gtk.CssProvider()
                            css = """
                            banner.fullscreen-banner button {
                                background-color: @suggested_bg_color;
                                color: @suggested_fg_color;
                            }
                            banner.fullscreen-banner button:hover {
                                background-color: @suggested_hover_bg_color;
                            }
                            banner.fullscreen-banner button:active {
                                background-color: @suggested_active_bg_color;
                            }
                            """
                            css_provider.load_from_data(css.encode('utf-8'))
                            Gtk.StyleContext.add_provider_for_display(
                                display, css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
                            )
                            fullscreen_banner.add_css_class('fullscreen-banner')

                    # Connect button click handler
                    def on_banner_button_clicked(banner):
                        self.toggle_fullscreen()

                    fullscreen_banner.connect('button-clicked', on_banner_button_clicked)
                except Exception as e:
                    logger.debug(f"Failed to style fullscreen banner button: {e}", exc_info=True)

                # Make banner expand to fill available width (Adw.Banner supports hexpand per docs)
                fullscreen_banner.set_hexpand(True)
                banner_overlay.set_child(fullscreen_banner)

                # Create dismiss button with text, positioned at the left (same as update banner)
                dismiss_button = Gtk.Button()
                dismiss_button.set_label(_('Dismiss'))
                dismiss_button.set_halign(Gtk.Align.START)
                dismiss_button.set_valign(Gtk.Align.CENTER)
                dismiss_button.set_margin_start(12)
                dismiss_button.connect('clicked', self._on_fullscreen_banner_dismiss)
                banner_overlay.add_overlay(dismiss_button)

                self._fullscreen_banner_container = banner_overlay
                self._fullscreen_banner_dismiss_button = dismiss_button

                # Make banner container expand to fill full width
                self._fullscreen_banner_container.set_hexpand(True)
                self._fullscreen_banner_container.set_vexpand(False)

                # Banner will be added to window level when entering fullscreen
                # Store reference but don't add to any container yet
                self._fullscreen_banner_container.set_visible(False)  # Hidden by default

                # Configure banner positioning for full width
                self._fullscreen_banner_container.set_halign(Gtk.Align.FILL)
                self._fullscreen_banner_container.set_valign(Gtk.Align.START)
                # Remove margins to make it truly full width
                self._fullscreen_banner_container.set_margin_start(0)
                self._fullscreen_banner_container.set_margin_end(0)
                self._fullscreen_banner_container.set_margin_top(0)

                logger.debug("Fullscreen banner container created")
        except Exception as e:
            logger.error(f"Failed to create fullscreen banner: {e}", exc_info=True)

    def _show_fullscreen_banner(self):
        """Show fullscreen banner with help text and exit button."""
        try:
            # Ensure banner container exists
            if self._fullscreen_banner_container is None:
                self._create_fullscreen_banner()

            # Show the banner
            if self._fullscreen_banner_container:
                banner = self._fullscreen_banner_container.get_child()
                if banner and isinstance(banner, Adw.Banner):
                    banner.set_revealed(True)
                self._fullscreen_banner_container.set_visible(True)

            logger.debug("Fullscreen banner shown")
        except Exception as e:
            logger.debug(f"Failed to show fullscreen banner: {e}", exc_info=True)

    def _on_fullscreen_banner_dismiss(self, button):
        """Handle dismiss button click on fullscreen banner."""
        logger.debug("Fullscreen banner dismissed by user")
        self._hide_fullscreen_banner()

    def _hide_fullscreen_banner(self):
        """Hide fullscreen banner."""
        try:
            if self._fullscreen_banner_container:
                banner = self._fullscreen_banner_container.get_child()
                if banner and isinstance(banner, Adw.Banner):
                    banner.set_revealed(False)
                self._fullscreen_banner_container.set_visible(False)
            logger.debug("Fullscreen banner hidden")
        except Exception as e:
            logger.debug(f"Failed to hide fullscreen banner: {e}", exc_info=True)
