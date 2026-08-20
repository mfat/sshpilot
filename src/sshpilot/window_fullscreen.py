"""Window-owned fullscreen for MainWindow (issue #1102).

Fullscreen is *window* state, not terminal state. A terminal may ask the window
to toggle fullscreen, but it never owns the saved chrome, the key controllers,
the exit affordance, or the CSS — otherwise closing that terminal destroys the
only object that knows how to get back out, which is exactly issue #1102
(fullscreen with no header, no tab bar, no sidebar and no working F11).

Ownership
---------
``MainWindow.fullscreen_controller`` is the single ``WindowFullscreenController``
for the window. It holds:

* ``active``      — whether the window is in application fullscreen;
* ``origin``      — ``START`` or ``TERMINAL``: where *this* fullscreen session
  began. It never changes because the active page changed; it decides only
  whether closing the last terminal also leaves fullscreen;
* ``_saved``      — the exact chrome state captured on entry, restored verbatim
  on exit (no hard-coded defaults);
* ``presentation`` — ``'start'`` or ``'terminal'``: how the *currently active*
  page is presented while fullscreen. Start keeps its normal chrome; a terminal
  hides the sidebar and banners and folds the header bar and tab bar into the
  auto-hiding top overlay described below.

The controller subscribes to the tab view itself (``notify::selected-page`` and
``page-detached``), so presentation follows the active page without any terminal
having to report in, and nothing here ever holds a reference to a terminal.

Real window state
-----------------
The window's ``fullscreened`` property is authoritative, not ``active``. The
controller listens for ``notify::fullscreened`` and reconciles: fullscreen
entered behind its back (a desktop shortcut, macOS's native control) is
*adopted* — chrome captured, presentation applied — and fullscreen left behind
its back runs the same teardown as a normal exit. Its own transitions are
tagged with a pending marker so the resulting notification is recognised as
already-consistent instead of recursing.

Keyboard
--------
The fullscreen accelerator is an ordinary registry entry — the
``win.toggle-fullscreen`` action, defaulting to F11 (Control+Command+F on
macOS, which keeps F11 for the OS) — so it is listed in the shortcut overview
and rebindable in the customizer, and nothing here duplicates it with a
hard-coded key handler. Escape *is* handled here, in the *bubble* phase, which
is reached only when the focused widget declined the key: a focused VTE
consumes Escape as ordinary terminal input, so fullscreen never steals it, and
a global accelerator could not make that distinction.

Immersive terminal chrome
-------------------------
Terminal fullscreen is edge-to-edge: the header bar and tab bar are moved into
the content ``Adw.ToolbarView``'s top-bar group, which is switched to
``extend-content-to-top-edge`` with ``reveal-top-bars`` off. That is one
surface with one reveal property — no competing revealers or timers — and
because the content extends underneath, revealing costs the terminal no
allocation and produces no row/column jump.

Pointing at the top edge reveals both bars together; the overlay stays up while
the pointer is anywhere over them (the region is measured from their real
heights, not hard-coded) and hides once it leaves. The revealed header bar is
the ordinary one, so its window controls, menu and fullscreen toggle are all
live, and the tab bar is the ordinary one, so tabs stay interactive and
switching them does not disturb fullscreen.

The revealed surface is opaque: libadwaita renders *flat* top bars transparent
once the content extends under them, so the toolbar style is switched to raised
for the duration (and restored on exit). No background is hardcoded — the theme
still decides the colour.

Start keeps its normal chrome: immersive mode is the terminal presentation
only.

This is not platform-gated. macOS was special-cased at first, on the theory
that the OS owns the fullscreen reveal — but SSH Pilot calls
``maybe_set_native_controls(header_bar, False)``, so its window controls there
are client-side GTK widgets inside the content, and macOS's native reveal
uncovers the *menu bar*, not this app's chrome. Leaving the tab bar pinned
visible on macOS therefore just wasted a strip of screen with nothing revealing
it. What macOS does own is the fullscreen *transition*, which is honoured
through notify::fullscreened above, and its traffic lights, which this module
never draws or emulates.

The one macOS accommodation is the menu-bar strip itself: while fullscreen the
header bar carries the ``macos-fullscreen-spacer`` CSS class, whose raised
min-height reserves the strip the auto-hidden menu bar slides over when the
pointer reaches the top edge, so the bar's controls stay visible below it. It
is applied on entry and removed on exit like the fullscreen button.
"""

import logging

from gi.repository import Gtk, Gdk, Adw

from . import platform_utils

logger = logging.getLogger(__name__)

#: Fullscreen origins.
ORIGIN_START = 'start'
ORIGIN_TERMINAL = 'terminal'

#: Presentations.
PRESENTATION_START = 'start'
PRESENTATION_TERMINAL = 'terminal'

#: Window CSS class applied while a terminal is presented fullscreen.
FULLSCREEN_CSS_CLASS = 'terminal-fullscreen-mode'

#: Pointer must be within this many pixels of the top edge to reveal the
#: chrome.
TOP_EDGE_TRIGGER_PX = 4
#: Fallback reveal-region height for when the bars cannot be measured (they
#: report 0 until they have been allocated at least once). Deliberately not a
#: plausible measured total, so a silent fallback is obvious in a trace.
TOP_EDGE_FALLBACK_REGION_PX = 72

#: CSS class that gives the header bar room for the macOS menu bar while
#: fullscreen.
MACOS_FULLSCREEN_SPACER_CSS_CLASS = 'macos-fullscreen-spacer'
#: Total header-bar min-height while fullscreen on macOS, so the bar's
#: controls sit below the auto-hidden menu bar when it reveals over the
#: window's top edge.
MACOS_FULLSCREEN_SPACER_MIN_HEIGHT_PX = 64

_macos_fullscreen_spacer_css_installed = False


def _ensure_macos_fullscreen_spacer_css() -> None:
    """Install the CSS that grows the header bar during macOS fullscreen.

    In native macOS fullscreen the menu bar auto-hides, and moving the
    pointer to the top edge makes it slide down *over* the window's own top
    strip — where SSH Pilot's client-side header bar sits (its window
    controls are ordinary GTK widgets; ``maybe_set_native_controls`` is
    off). Reserving the menu-bar height keeps the bar's buttons below the
    revealed menu bar. Installed once per process, macOS-only callers.
    """
    global _macos_fullscreen_spacer_css_installed
    if _macos_fullscreen_spacer_css_installed:
        return
    try:
        provider = Gtk.CssProvider()
        provider.load_from_data(f"""
headerbar.{MACOS_FULLSCREEN_SPACER_CSS_CLASS} {{
    min-height: {MACOS_FULLSCREEN_SPACER_MIN_HEIGHT_PX}px;
}}
""".encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
        _macos_fullscreen_spacer_css_installed = True
    except Exception:  # pragma: no cover - headless/test doubles
        logger.debug('macOS fullscreen spacer CSS install failed', exc_info=True)


class WindowFullscreenController:
    """The one fullscreen state machine per application window."""

    def __init__(self, window):
        # The window outlives the controller; a terminal never does, which is
        # why no terminal is referenced anywhere in this class.
        self.window = window

        self.active = False
        self.origin = None
        self.presentation = None
        self._saved = None

        self._bubble_controller = None
        self._motion_controller = None

        self._immersive_active = False
        self._top_chrome_revealed = False
        self._saved_extend_content = None
        self._saved_reveal_top_bars = None
        self._saved_top_bar_style = None
        self._tab_bar_relocated = False

        # Set while a transition this controller requested is in flight, so the
        # resulting notify::fullscreened is not mistaken for an external one.
        # Not a timer: it is cleared by the very notification it predicts.
        self._pending_transition = None

        self._installed = False

    # -- introspection -----------------------------------------------------

    @property
    def terminal_presentation_active(self) -> bool:
        """True while a terminal is being presented fullscreen."""
        return self.active and self.presentation == PRESENTATION_TERMINAL

    @property
    def top_chrome_revealed(self) -> bool:
        """True while the header bar + tab bar overlay is showing."""
        return self._top_chrome_revealed

    # -- installation ------------------------------------------------------

    def install(self) -> None:
        """Attach the window-level key/pointer controllers and tab hooks."""
        if self._installed:
            return
        self._installed = True
        self._install_key_controllers()
        self._install_motion_controller()
        self._install_tab_hooks()
        self._install_window_state_hook()

    def _install_key_controllers(self) -> None:
        """Only Escape is handled here.

        The fullscreen accelerator itself belongs to the shortcut registry
        (``win.toggle-fullscreen``), so it is customizable and listed in the
        overview, and there is no second hard-coded path that could fire for
        the same key event. Escape cannot work that way: it is live terminal
        input, and a global accelerator would take it away from VTE.
        """
        try:
            # Bubble phase: only keys the focused widget declined arrive here,
            # so a terminal keeps its own Escape.
            bubble = Gtk.EventControllerKey()
            bubble.set_propagation_phase(Gtk.PropagationPhase.BUBBLE)
            bubble.connect('key-pressed', self.on_bubble_key_pressed)
            self.window.add_controller(bubble)
            self._bubble_controller = bubble
            logger.debug('Window fullscreen Escape controller installed')
        except Exception:
            logger.debug('Failed to install fullscreen key controller', exc_info=True)

    def _install_motion_controller(self) -> None:
        try:
            motion = Gtk.EventControllerMotion()
            motion.connect('motion', self.on_pointer_motion)
            motion.connect('leave', self.on_pointer_leave)
            self.window.add_controller(motion)
            self._motion_controller = motion
        except Exception:
            logger.debug('Failed to install top-edge motion controller', exc_info=True)

    def _install_window_state_hook(self) -> None:
        """Follow the real window fullscreen state, however it changes."""
        try:
            self.window.connect(
                'notify::fullscreened', self._on_window_fullscreened_changed
            )
        except Exception:
            logger.debug('Failed to hook notify::fullscreened', exc_info=True)

    def _install_tab_hooks(self) -> None:
        tab_view = getattr(self.window, 'tab_view', None)
        if tab_view is None:
            return
        try:
            tab_view.connect('notify::selected-page', self._on_tab_view_changed)
            tab_view.connect('page-detached', self._on_tab_view_changed)
        except Exception:
            logger.debug('Failed to hook fullscreen into the tab view', exc_info=True)

    def _on_tab_view_changed(self, *_args) -> None:
        self.update_presentation_for_active_page()

    # -- public API --------------------------------------------------------

    def toggle(self) -> None:
        if self.active:
            self.exit()
        else:
            self.enter()

    def enter(self, origin=None) -> None:
        """Enter fullscreen, capturing the exact chrome state first."""
        self._begin_fullscreen(origin, request_window_fullscreen=True)

    def exit(self) -> None:
        """Leave fullscreen and restore the exact captured chrome state."""
        self._end_fullscreen(request_window_unfullscreen=True)

    def _begin_fullscreen(self, origin=None, *, request_window_fullscreen: bool) -> None:
        """Take up fullscreen.

        With ``request_window_fullscreen`` false the window is already
        fullscreen (the WM or the OS put it there) and nothing is asked of it.
        """
        if self.active:
            return
        try:
            # Snapshot before anything touches the chrome.
            self._saved = self._capture_ui_state()
            self.origin = origin or (
                ORIGIN_START if self._active_page_is_start() else ORIGIN_TERMINAL
            )
            self.active = True

            if request_window_fullscreen:
                self._set_window_fullscreen(True)
            self._show_fullscreen_button(True)
            self._apply_presentation()
            self._apply_macos_fullscreen_spacer()
            logger.debug(
                'Entered fullscreen (origin=%s, external=%s)',
                self.origin, not request_window_fullscreen,
            )
        except Exception:
            logger.error('Failed to enter fullscreen', exc_info=True)

    def _end_fullscreen(self, *, request_window_unfullscreen: bool) -> None:
        """Give up fullscreen.

        With ``request_window_unfullscreen`` false the window has already left
        fullscreen externally, so the WM owns the window state and nothing is
        pushed back at it — only the application-side teardown runs.
        """
        if not self.active:
            return
        try:
            self._set_top_chrome_revealed(False)
            self._show_fullscreen_button(False)
            self._clear_terminal_presentation()
            self._remove_macos_fullscreen_spacer()
            self._restore_ui_state(self._saved)
            if request_window_unfullscreen:
                self._set_window_fullscreen(False)
        except Exception:
            logger.error('Failed to exit fullscreen cleanly', exc_info=True)
        finally:
            # Never leave the state machine wedged, even if a restore step
            # raised: F11 must keep working.
            self.active = False
            self.origin = None
            self.presentation = None
            self._saved = None
            self._sync_tab_bar_visibility()
            logger.debug(
                'Exited fullscreen (external=%s)', not request_window_unfullscreen
            )

    def update_presentation_for_active_page(self) -> None:
        """Re-apply presentation after a tab switch/close. Idempotent."""
        if not self.active:
            return
        on_start = self._active_page_is_start()
        if (on_start
                and self.origin == ORIGIN_TERMINAL
                and not self._has_user_tabs()):
            # The fullscreen session began in a terminal and the last terminal
            # is gone: fullscreen has nothing left to present.
            self.exit()
            return
        self._apply_presentation()

    # -- exit affordance ---------------------------------------------------

    def _show_fullscreen_button(self, visible: bool) -> None:
        """The header bar's own fullscreen toggle, live only while fullscreen.

        It is an ordinary header-bar button, not a bespoke overlay control, so
        revealing the chrome reveals it along with the window controls and menu.
        """
        self._set_widget_visible('fullscreen_button', visible)

    # -- macOS menu-bar accommodation --------------------------------------

    def _apply_macos_fullscreen_spacer(self) -> None:
        """Give the header bar room for the macOS menu bar while fullscreen.

        In native macOS fullscreen the auto-hidden menu bar slides down over
        the window's top edge when the pointer reaches it — exactly where the
        app's own client-side header bar sits. Bumping the bar's min-height
        (via the ``macos-fullscreen-spacer`` CSS class) keeps its controls
        below the revealed menu bar. No-op anywhere else.
        """
        if not platform_utils.is_macos():
            return
        header_bar = getattr(self.window, 'header_bar', None)
        if header_bar is None:
            return
        _ensure_macos_fullscreen_spacer_css()
        try:
            header_bar.add_css_class(MACOS_FULLSCREEN_SPACER_CSS_CLASS)
        except Exception:
            logger.debug('Failed to apply the macOS fullscreen spacer', exc_info=True)

    def _remove_macos_fullscreen_spacer(self) -> None:
        if not platform_utils.is_macos():
            return
        header_bar = getattr(self.window, 'header_bar', None)
        if header_bar is None:
            return
        try:
            header_bar.remove_css_class(MACOS_FULLSCREEN_SPACER_CSS_CLASS)
        except Exception:
            logger.debug('Failed to remove the macOS fullscreen spacer', exc_info=True)

    # -- key handling ------------------------------------------------------

    def on_bubble_key_pressed(self, _controller, keyval, _keycode, _state) -> bool:
        """Escape, but only once the focused widget has declined it."""
        if keyval == Gdk.KEY_Escape and self.active:
            self.exit()
            return True
        return False

    # -- pointer handling --------------------------------------------------

    def on_pointer_motion(self, _controller, _x, y) -> None:
        if not self._immersive_active:
            return
        if not self._top_chrome_revealed:
            if y <= TOP_EDGE_TRIGGER_PX:
                self._set_top_chrome_revealed(True)
        elif y > self._reveal_region_height():
            self._set_top_chrome_revealed(False)

    def on_pointer_leave(self, _controller=None) -> None:
        self._set_top_chrome_revealed(False)

    def _reveal_region_height(self) -> float:
        """How far down the revealed chrome reaches.

        Measured from the bars themselves so the pointer keeps the overlay up
        across the whole surface whatever height the theme gives them.
        """
        height = 0
        for name in ('header_bar', 'tab_bar'):
            widget = getattr(self.window, name, None)
            if widget is None:
                continue
            try:
                height += int(widget.get_height() or 0)
            except Exception:
                logger.debug('Could not measure %s', name, exc_info=True)
        return height or TOP_EDGE_FALLBACK_REGION_PX

    def _set_top_chrome_revealed(self, revealed: bool) -> None:
        if not self._immersive_active:
            self._top_chrome_revealed = False
            return
        revealed = bool(revealed)
        self._top_chrome_revealed = revealed
        toolbar = self._toolbar_view()
        if toolbar is None:
            return
        try:
            toolbar.set_reveal_top_bars(revealed)
        except Exception:
            logger.debug('Failed to toggle the top chrome overlay', exc_info=True)

    # -- immersive chrome (Linux / Windows) --------------------------------

    def _toolbar_view(self):
        return getattr(self.window, '_content_toolbar_view', None)

    def _enter_immersive_chrome(self) -> bool:
        """Fold the header bar + tab bar into one auto-hiding overlay surface.

        Returns False when the window has no content ToolbarView (the legacy
        non-Adw split fallback), so the caller can degrade to plain hiding.
        """
        if self._immersive_active:
            return True
        toolbar = self._toolbar_view()
        if toolbar is None:
            return False
        try:
            self._saved_extend_content = toolbar.get_extend_content_to_top_edge()
            self._saved_reveal_top_bars = toolbar.get_reveal_top_bars()
            self._saved_top_bar_style = toolbar.get_top_bar_style()
            self._relocate_tab_bar(into_top_bars=True)
            # Flat top bars are transparent once the content extends under
            # them; raised gives the revealed surface a real background.
            toolbar.set_top_bar_style(Adw.ToolbarStyle.RAISED)
            # Content under the bars: revealing them costs the terminal no
            # allocation, so there is no row/column jump.
            toolbar.set_extend_content_to_top_edge(True)
            toolbar.set_reveal_top_bars(False)
            self._immersive_active = True
            self._top_chrome_revealed = False
            logger.debug('Immersive terminal chrome enabled')
            return True
        except Exception:
            logger.debug('Failed to enable immersive chrome', exc_info=True)
            return False

    def _exit_immersive_chrome(self) -> None:
        """Put the chrome back exactly where it was."""
        if not self._immersive_active:
            return
        self._immersive_active = False
        self._top_chrome_revealed = False
        toolbar = self._toolbar_view()
        try:
            self._relocate_tab_bar(into_top_bars=False)
            if toolbar is not None:
                if self._saved_extend_content is not None:
                    toolbar.set_extend_content_to_top_edge(self._saved_extend_content)
                if self._saved_reveal_top_bars is not None:
                    toolbar.set_reveal_top_bars(self._saved_reveal_top_bars)
                if self._saved_top_bar_style is not None:
                    toolbar.set_top_bar_style(self._saved_top_bar_style)
        except Exception:
            logger.debug('Failed to restore chrome layout', exc_info=True)
        finally:
            self._saved_extend_content = None
            self._saved_reveal_top_bars = None
            self._saved_top_bar_style = None

    def _relocate_tab_bar(self, *, into_top_bars: bool) -> None:
        """Move the tab bar between the content flow and the top-bar group.

        This is what makes one overlay out of two bars: while fullscreen the
        tab bar is a top bar of the same ToolbarView as the header, so a single
        reveal drives both.
        """
        toolbar = self._toolbar_view()
        tab_bar = getattr(self.window, 'tab_bar', None)
        box = getattr(self.window, 'tab_content_box', None)
        if toolbar is None or tab_bar is None or box is None:
            return
        try:
            if into_top_bars:
                if self._tab_bar_relocated:
                    return
                box.remove(tab_bar)
                toolbar.add_top_bar(tab_bar)
                self._tab_bar_relocated = True
            else:
                if not self._tab_bar_relocated:
                    return
                toolbar.remove(tab_bar)
                box.prepend(tab_bar)
                self._tab_bar_relocated = False
        except Exception:
            logger.debug('Failed to relocate the tab bar', exc_info=True)


    # -- presentation ------------------------------------------------------

    def _apply_presentation(self) -> None:
        if self._active_page_is_start():
            self._apply_start_presentation()
        else:
            self._apply_terminal_presentation()

    def _apply_start_presentation(self) -> None:
        """Start keeps its ordinary chrome; only the window is fullscreen.

        Immersive auto-hiding chrome is the terminal presentation only — Start
        navigation has to stay visible and usable.
        """
        self.presentation = PRESENTATION_START
        self._clear_terminal_presentation()
        self._restore_ui_state(self._saved, restore_tab_bar=False)
        self._sync_tab_bar_visibility()

    def _apply_terminal_presentation(self) -> None:
        """Edge-to-edge terminal with the chrome folded into a top overlay."""
        self.presentation = PRESENTATION_TERMINAL
        self._set_sidebar_visible(False)
        self._set_widget_visible('update_banner_container', False)
        self._set_widget_visible('tips_banner_container', False)
        self._set_widget_visible('broadcast_banner', False)

        if self._enter_immersive_chrome():
            # Both bars are top bars of the ToolbarView now, hidden until the
            # pointer reaches the top edge. They stay `visible` as widgets —
            # the ToolbarView's reveal is what shows and hides the surface.
            self._set_widget_visible('header_bar', True)
            self._sync_tab_bar_visibility()
        else:
            # Legacy non-Adw split fallback with no ToolbarView: nothing can
            # overlay, so hide the header and leave the tab bar in the flow
            # rather than making tabs unreachable.
            self._set_widget_visible('header_bar', False)
            self._sync_tab_bar_visibility()
        self._add_fullscreen_css()

    def _clear_terminal_presentation(self) -> None:
        self._exit_immersive_chrome()
        self._remove_fullscreen_css()

    # -- chrome state ------------------------------------------------------

    def _capture_ui_state(self) -> dict:
        return {
            'sidebar_visible': self._get_sidebar_visible(),
            'header_visible': self._get_widget_visible('header_bar'),
            'tab_bar_visible': self._get_widget_visible('tab_bar'),
            'update_banner_visible': self._get_widget_visible('update_banner_container'),
            'tips_banner_visible': self._get_widget_visible('tips_banner_container'),
            'broadcast_banner_visible': self._get_widget_visible('broadcast_banner'),
            'maximized': self._get_window_maximized(),
        }

    def _restore_ui_state(self, saved, restore_tab_bar: bool = True) -> None:
        if not saved:
            return
        self._set_sidebar_visible(saved.get('sidebar_visible'))
        self._set_widget_visible('header_bar', saved.get('header_visible'))
        if restore_tab_bar:
            self._set_widget_visible('tab_bar', saved.get('tab_bar_visible'))
        self._set_widget_visible('update_banner_container', saved.get('update_banner_visible'))
        self._set_widget_visible('tips_banner_container', saved.get('tips_banner_visible'))
        self._set_widget_visible('broadcast_banner', saved.get('broadcast_banner_visible'))

    def _sync_tab_bar_visibility(self) -> None:
        """Hand the tab bar back to its normal owner."""
        updater = getattr(self.window, '_update_tab_button_visibility', None)
        if callable(updater):
            try:
                updater()
            except Exception:
                logger.debug('Tab bar visibility resync failed', exc_info=True)

    # -- window / widget plumbing -----------------------------------------

    def _get_widget_visible(self, name):
        widget = getattr(self.window, name, None)
        if widget is None:
            return None
        try:
            return widget.get_visible()
        except Exception:
            return None

    def _set_widget_visible(self, name, visible) -> None:
        if visible is None:
            return
        widget = getattr(self.window, name, None)
        if widget is None:
            return
        try:
            widget.set_visible(bool(visible))
        except Exception:
            logger.debug('Failed to set %s visibility', name, exc_info=True)

    def _get_sidebar_visible(self):
        split_view = getattr(self.window, 'split_view', None)
        if split_view is None:
            return None
        variant = getattr(self.window, '_split_variant', None)
        try:
            if variant == 'navigation':
                visible = getattr(self.window, '_sidebar_visible', None)
                if visible is not None:
                    return bool(visible)
                return not split_view.get_collapsed()
            if variant == 'paned':
                child = split_view.get_start_child()
                return child.get_visible() if child is not None else None
            if hasattr(split_view, 'get_show_sidebar'):
                return split_view.get_show_sidebar()
        except Exception:
            logger.debug('Failed to read sidebar visibility', exc_info=True)
        return None

    def _set_sidebar_visible(self, visible) -> None:
        if visible is None:
            return
        # The window owns the split-variant switch; reuse it rather than
        # re-deriving OverlaySplitView / NavigationSplitView / Paned handling.
        toggle = getattr(self.window, '_toggle_sidebar_visibility', None)
        if callable(toggle):
            try:
                toggle(bool(visible))
                return
            except Exception:
                logger.debug('Sidebar toggle failed', exc_info=True)
        split_view = getattr(self.window, 'split_view', None)
        if split_view is not None and hasattr(split_view, 'set_show_sidebar'):
            try:
                split_view.set_show_sidebar(bool(visible))
            except Exception:
                logger.debug('Sidebar fallback toggle failed', exc_info=True)

    def _get_window_maximized(self) -> bool:
        try:
            if hasattr(self.window, 'is_maximized'):
                return bool(self.window.is_maximized())
            if hasattr(self.window, 'get_maximized'):
                return bool(self.window.get_maximized())
        except Exception:
            pass
        return False

    def _window_is_fullscreen(self) -> bool:
        """Read the window's real fullscreen state.

        Gtk.Window exposes the `fullscreened` property; the matching getter is
        `is_fullscreen()` here, with `get_fullscreened()` on versions that ship
        it, so try both before falling back to the property itself.
        """
        for name in ('is_fullscreen', 'get_fullscreened'):
            getter = getattr(self.window, name, None)
            if callable(getter):
                try:
                    return bool(getter())
                except Exception:
                    logger.debug('%s() failed', name, exc_info=True)
        try:
            return bool(self.window.get_property('fullscreened'))
        except Exception:
            logger.debug('Could not read the fullscreened property', exc_info=True)
        return False

    def _on_window_fullscreened_changed(self, window, _pspec=None) -> None:
        """Reconcile with the window's real fullscreen state.

        The property is authoritative: whatever it says wins, whether the
        change came from F11, the header-bar button, macOS's native control or
        the desktop's own shortcut.
        """
        del window  # self.window is the one that matters
        actual = self._window_is_fullscreen()

        pending = self._pending_transition
        if pending is not None:
            self._pending_transition = None
            if actual == (pending == 'enter'):
                # Our own transition landing. `active` was already set, and the
                # chrome was already captured/restored — doing either again
                # would re-snapshot fullscreen chrome as if it were normal.
                return
            # The window did not end up where we asked; fall through and
            # reconcile against reality.

        if actual == self.active:
            return

        if actual:
            # Adopted, not requested: the window is already fullscreen.
            self._begin_fullscreen(request_window_fullscreen=False)
        else:
            # Already out of fullscreen; only the application side is left.
            self._end_fullscreen(request_window_unfullscreen=False)

    def _set_window_fullscreen(self, fullscreen: bool) -> None:
        # Predict the notification this causes so it is not read as external.
        self._pending_transition = 'enter' if fullscreen else 'exit'
        try:
            if fullscreen:
                if hasattr(self.window, 'fullscreen'):
                    self.window.fullscreen()
                elif hasattr(self.window, 'set_fullscreen'):
                    self.window.set_fullscreen(True)
                return
            if hasattr(self.window, 'unfullscreen'):
                self.window.unfullscreen()
            elif hasattr(self.window, 'set_fullscreen'):
                self.window.set_fullscreen(False)
            if self._saved and self._saved.get('maximized'):
                if hasattr(self.window, 'maximize'):
                    self.window.maximize()
                elif hasattr(self.window, 'set_maximized'):
                    self.window.set_maximized(True)
        except Exception:
            logger.debug('Failed to change window fullscreen state', exc_info=True)

    # -- CSS ---------------------------------------------------------------

    def _add_fullscreen_css(self) -> None:
        """Mark the window so themes can target fullscreen presentation.

        Nothing else is styled from here: the opaque revealed chrome comes from
        the raised toolbar style, not from an app-level background override
        that would fight the user's theme.
        """
        try:
            self.window.add_css_class(FULLSCREEN_CSS_CLASS)
        except Exception:
            logger.debug('Failed to add fullscreen CSS class', exc_info=True)

    def _remove_fullscreen_css(self) -> None:
        try:
            self.window.remove_css_class(FULLSCREEN_CSS_CLASS)
        except Exception:
            logger.debug('Failed to remove fullscreen CSS class', exc_info=True)

    # -- active page -------------------------------------------------------

    def _active_page_is_start(self) -> bool:
        checker = getattr(self.window, 'is_start_tab_selected', None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                logger.debug('Failed to read the active tab', exc_info=True)
        return True

    def _has_user_tabs(self) -> bool:
        checker = getattr(self.window, 'has_user_tabs', None)
        if callable(checker):
            try:
                return bool(checker())
            except Exception:
                logger.debug('Failed to count user tabs', exc_info=True)
        return False

    # -- teardown ----------------------------------------------------------

    def shutdown(self) -> None:
        """Release window-level resources (called on window close)."""
        try:
            if self.active:
                self.exit()
        finally:
            for attr in ('_bubble_controller', '_motion_controller'):
                controller = getattr(self, attr, None)
                if controller is None:
                    continue
                try:
                    self.window.remove_controller(controller)
                except Exception:
                    logger.debug('Failed to remove %s', attr, exc_info=True)
                setattr(self, attr, None)
            self._installed = False
