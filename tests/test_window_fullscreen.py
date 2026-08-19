"""Regression tests for window-owned fullscreen (issue #1102).

Fullscreen used to be owned by ``TerminalWidget`` (``terminal._fullscreen``,
a ``FullscreenController`` holding the saved chrome state plus the only F11 key
controller). Closing the terminal that entered fullscreen therefore destroyed
the only object that knew how to leave it: the window stayed fullscreen with
the header bar, tab bar and sidebar hidden, the ``terminal-fullscreen-mode``
CSS still applied, and no F11 handler anywhere on the Start page. Issue #1102
is exactly that dead end — reachable even sooner by dismissing the fullscreen
banner, which is why there is no banner any more.

These tests pin the corrected ownership: the window owns one
``WindowFullscreenController``; a terminal may only *delegate* a request to it.

Terminal fullscreen is immersive: header bar and tab bar move into the content
``Adw.ToolbarView``'s top-bar group, which is switched to
``extend-content-to-top-edge`` with ``reveal-top-bars`` off. The terminal then
gets the whole window, and pointing at the top edge reveals both bars together
as one overlay surface that costs the terminal no allocation.

The fakes below model the GTK behavior that makes all of this observable — the
tab view's page list and selection, the split view's sidebar API, the window's
fullscreen/CSS state, and the ToolbarView's reveal/extend semantics including
what they do to the content's height. Production methods (``show_start_tab``,
``_update_tab_button_visibility``, ``_toggle_sidebar_visibility``,
``has_user_tabs``, ``toggle_fullscreen``, ...) are *bound*, not reimplemented,
so the real tab-close-to-Start transition is what gets exercised.
"""

import sys
import types

import pytest

pytest.importorskip("gi")

from gi.repository import Adw, Gdk


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class _FakeWidget:
    def __init__(self, visible=True, height=0):
        self._visible = visible
        self.height = height

    def get_height(self):
        return self.height if self._visible else 0

    def get_visible(self):
        return self._visible

    def set_visible(self, visible):
        self._visible = bool(visible)

    def show(self):
        self._visible = True

    def hide(self):
        self._visible = False


class _FakeSplitView(_FakeWidget):
    """Adw.OverlaySplitView stand-in that also answers the Gtk.Paned API."""

    def __init__(self):
        super().__init__(True)
        self._show_sidebar = True
        self._collapsed = False
        self._show_content = False
        self._start_child = _FakeWidget(True)

    def get_show_sidebar(self):
        return self._show_sidebar

    def set_show_sidebar(self, value):
        self._show_sidebar = bool(value)
        self._start_child.set_visible(bool(value))

    def get_collapsed(self):
        return self._collapsed

    def set_collapsed(self, value):
        self._collapsed = bool(value)

    def get_show_content(self):
        return self._show_content

    def set_show_content(self, value):
        self._show_content = bool(value)

    def get_start_child(self):
        return self._start_child


class _FakePage:
    def __init__(self, child, title=''):
        self._child = child
        self._title = title
        self.pinned = False

    def get_child(self):
        return self._child

    def set_title(self, title):
        self._title = title

    def get_title(self):
        return self._title

    def set_icon(self, _icon):
        return None


class _FakeTabView:
    """Adw.TabView stand-in: page list, selection, and the two signals the
    fullscreen controller subscribes to."""

    def __init__(self, window):
        self.window = window
        self.pages = []
        self.selected = None
        self._handlers = {}
        self.css_classes = set()

    # -- signals -----------------------------------------------------------
    def connect(self, signal, handler, *args):
        self._handlers.setdefault(signal, []).append((handler, args))
        return len(self._handlers[signal])

    def _emit(self, signal, *emit_args):
        for handler, args in list(self._handlers.get(signal, ())):
            handler(self, *emit_args, *args)

    # -- pages -------------------------------------------------------------
    def get_n_pages(self):
        return len(self.pages)

    def get_pages(self):
        return list(self.pages)

    def prepend(self, child):
        page = _FakePage(child)
        self.pages.insert(0, page)
        return page

    def append(self, child):
        page = _FakePage(child)
        self.pages.append(page)
        return page

    def set_page_pinned(self, page, pinned):
        page.pinned = pinned

    def get_selected_page(self):
        return self.selected

    def set_selected_page(self, page):
        changed = self.selected is not page
        self.selected = page
        if changed:
            self._emit('notify::selected-page', None)

    def close_page(self, page):
        index = self.pages.index(page)
        self.pages.remove(page)
        if self.selected is page:
            self.selected = self.pages[min(index, len(self.pages) - 1)] if self.pages else None
        # Mirror MainWindow.on_tab_detached's tail (the window's own handler,
        # which is connected before the fullscreen controller's).
        self.window._update_tab_button_visibility()
        if not self.window.has_user_tabs():
            self.window.show_start_tab()
        self._emit('page-detached', page, index)

    # -- css ---------------------------------------------------------------
    def add_css_class(self, name):
        self.css_classes.add(name)

    def remove_css_class(self, name):
        self.css_classes.discard(name)


class _FakeTerminal:
    """Minimal TerminalWidget stand-in."""

    def __init__(self, window):
        self._root = window

    def get_root(self):
        return self._root


# The same enum the controller uses, so this works under the real Adw and
# under the suite's stubbed gi alike.
_ToolbarStyle = Adw.ToolbarStyle


class _FakeToolbarView:
    """Adw.ToolbarView stand-in modelling reveal/extend and their layout effect.

    ``extend-content-to-top-edge`` is what makes the revealed bars an *overlay*:
    the content keeps the full height whether or not the bars are revealed. With
    it off, revealed bars consume height from the content, which is how the
    non-fullscreen window is laid out.
    """

    WINDOW_HEIGHT = 1000

    def __init__(self):
        self.top_bars = []
        self.content = None
        self._extend = False
        self._reveal = True
        self._top_bar_style = _ToolbarStyle.FLAT

    def add_top_bar(self, widget):
        self.top_bars.append(widget)

    def remove(self, widget):
        if widget in self.top_bars:
            self.top_bars.remove(widget)

    def set_content(self, widget):
        self.content = widget

    def set_extend_content_to_top_edge(self, value):
        self._extend = bool(value)

    def get_extend_content_to_top_edge(self):
        return self._extend

    def set_reveal_top_bars(self, value):
        self._reveal = bool(value)

    def get_reveal_top_bars(self):
        return self._reveal

    def set_top_bar_style(self, style):
        self._top_bar_style = style

    def get_top_bar_style(self):
        return self._top_bar_style

    def bars_are_opaque(self):
        """libadwaita renders flat top bars transparent over extended content."""
        return not self._extend or self._top_bar_style != _ToolbarStyle.FLAT

    # -- what the user actually sees ---------------------------------------
    def is_painted(self, widget):
        if not widget.get_visible():
            return False
        if widget in self.top_bars:
            return self._reveal
        return True

    def content_height(self):
        if self._extend:
            return self.WINDOW_HEIGHT
        if not self._reveal:
            return self.WINDOW_HEIGHT
        used = sum(b.height for b in self.top_bars if b.get_visible())
        return self.WINDOW_HEIGHT - used


class _FakeBox:
    """Gtk.Box stand-in for tab_content_box (tab bar above the tab view)."""

    def __init__(self, children=()):
        self.children = list(children)

    def append(self, widget):
        self.children.append(widget)

    def prepend(self, widget):
        self.children.insert(0, widget)

    def remove(self, widget):
        if widget in self.children:
            self.children.remove(widget)


class _FakeButton(_FakeWidget):
    """Header-bar button whose clicked() runs the real handler."""

    def __init__(self, on_click, visible=False):
        super().__init__(visible)
        self._on_click = on_click

    def clicked(self):
        return self._on_click()


class _FakeOverlay:
    def __init__(self):
        self.overlays = []

    def add_overlay(self, widget):
        self.overlays.append(widget)

    def remove_overlay(self, widget):
        if widget in self.overlays:
            self.overlays.remove(widget)


class _FullscreenWindow:
    """Stub MainWindow wired to the real fullscreen/tab methods under test."""

    def __init__(self, window_module):
        MainWindow = window_module.MainWindow
        for name in (
            '_is_start_tab_page',
            'has_user_tabs',
            'is_start_tab_selected',
            'show_start_tab',
            '_update_tab_button_visibility',
            '_toggle_sidebar_visibility',
            'toggle_fullscreen',
        ):
            setattr(self, name, getattr(MainWindow, name).__get__(self, type(self)))

        # Window chrome.
        self.split_view = _FakeSplitView()
        self._split_variant = 'overlay'
        self._sidebar_visible = True
        self.header_bar = _FakeWidget(True, height=46)
        self.tab_bar = _FakeWidget(False, height=38)
        self.tab_button = _FakeWidget(False)
        self.update_banner_container = _FakeWidget(False)
        self.tips_banner_container = _FakeWidget(False)
        self.broadcast_banner = _FakeWidget(False)
        self.fullscreen_button = _FakeButton(lambda: self.toggle_fullscreen())

        # Content ToolbarView: header bar is its only top bar normally; the
        # tab bar lives in the content, above the tab view.
        self.terminal_area = _FakeWidget(True)
        self.tab_content_box = _FakeBox([self.tab_bar, self.terminal_area])
        self._content_toolbar_view = _FakeToolbarView()
        self._content_toolbar_view.add_top_bar(self.header_bar)
        self._content_toolbar_view.set_content(self.tab_content_box)
        self.sidebar_toggle_button = types.SimpleNamespace(
            active=False,
            set_active=lambda v: setattr(self.sidebar_toggle_button, 'active', v),
            get_active=lambda: self.sidebar_toggle_button.active,
        )

        # Overlay host for the transient top controls.
        self._global_overlay = _FakeOverlay()

        # Window-level state observed by the tests.
        self.window_is_fullscreen = False
        self.signal_handlers = {}
        self.fullscreen_calls = 0
        self.unfullscreen_calls = 0
        self.maximized = False
        self.css_classes = set()
        self.controllers = []

        # Tabs.
        self.tab_view = _FakeTabView(self)
        self.welcome_view = object()
        self._start_tab_page = self.tab_view.prepend(self.welcome_view)
        self._start_tab_page.set_title('Start')
        self.tab_view.set_selected_page(self._start_tab_page)

        self.config = types.SimpleNamespace(get_setting=lambda key, default=None: default)
        self._sidebar_minimal = False

    # -- Gtk.Window API ----------------------------------------------------
    def connect(self, signal, handler, *args):
        self.signal_handlers.setdefault(signal, []).append((handler, args))
        return len(self.signal_handlers[signal])

    def _emit(self, signal, *emit_args):
        for handler, args in list(self.signal_handlers.get(signal, ())):
            handler(self, *emit_args, *args)

    def is_fullscreen(self):
        return self.window_is_fullscreen

    def _set_fullscreened(self, value):
        """Change the property and notify, exactly as GTK does.

        Emitting synchronously is stricter than real GTK (which notifies once
        the compositor confirms): a re-entrancy guard that holds here holds
        for the asynchronous case too.
        """
        value = bool(value)
        if self.window_is_fullscreen == value:
            return
        self.window_is_fullscreen = value
        self._emit('notify::fullscreened', None)

    def fullscreen(self):
        self.fullscreen_calls += 1
        self._set_fullscreened(True)

    def unfullscreen(self):
        self.unfullscreen_calls += 1
        self._set_fullscreened(False)

    # The window manager / OS changing fullscreen behind the app's back.
    def set_fullscreen_externally(self, value):
        self._set_fullscreened(value)

    def is_maximized(self):
        return self.maximized

    def maximize(self):
        self.maximized = True

    def add_css_class(self, name):
        self.css_classes.add(name)

    def remove_css_class(self, name):
        self.css_classes.discard(name)

    def add_controller(self, controller):
        self.controllers.append(controller)

    def remove_controller(self, controller):
        if controller in self.controllers:
            self.controllers.remove(controller)

    # -- MainWindow collaborators stubbed out ------------------------------
    def _create_start_tab(self):
        return None

    def _update_content_theme_for_selected_tab(self):
        return None

    def _update_layout_toggle_state(self):
        return None

    def _schedule_start_tab_focus(self):
        return None

    def _apply_sidebar_visible(self, visible):
        self._toggle_sidebar_visibility(visible)

    def _sidebar_mode_is_minimal(self):
        return False

    def set_sidebar_minimal(self, minimal, animate=True):
        return None

    # -- scenario helpers --------------------------------------------------
    def open_terminal(self, title='session'):
        terminal = _FakeTerminal(self)
        page = self.tab_view.append(terminal)
        page.set_title(title)
        self._update_tab_button_visibility()
        self.tab_view.set_selected_page(page)
        return terminal, page

    def sidebar_visible(self):
        return self.split_view.get_show_sidebar()

    # -- what the user sees at the top of the window -----------------------
    def header_shown(self):
        return self._content_toolbar_view.is_painted(self.header_bar)

    def tab_bar_shown(self):
        return self._content_toolbar_view.is_painted(self.tab_bar)

    def terminal_allocation(self):
        """Height the terminal actually gets.

        Chrome still sitting in the content flow (a tab bar that has not been
        moved into the top-bar group) eats into it; overlaid chrome does not.
        """
        height = self._content_toolbar_view.content_height()
        if self.tab_bar in self.tab_content_box.children and self.tab_bar.get_visible():
            height -= self.tab_bar.height
        return height


def _window(monkeypatch=None, macos=False):
    wm = _window_module()
    from sshpilot import window_fullscreen as wf

    if monkeypatch is not None:
        monkeypatch.setattr(wf, 'is_macos', lambda: macos)

    win = _FullscreenWindow(wm)
    controller = wf.WindowFullscreenController(win)
    win.fullscreen_controller = controller
    controller.install()
    return win, controller


def _press(controller, keyval, phase='capture'):
    if phase == 'capture':
        return controller.on_capture_key_pressed(None, keyval, 0, 0)
    return controller.on_bubble_key_pressed(None, keyval, 0, 0)


def _f11(controller):
    return _press(controller, Gdk.KEY_F11)


# --------------------------------------------------------------------------
# 1. Start supports F11
# --------------------------------------------------------------------------


def test_start_page_f11_enters_and_exits_fullscreen_without_any_terminal():
    win, fc = _window()
    assert win.tab_view.get_n_pages() == 1  # Start only, no terminal exists

    assert _f11(fc) is True
    assert fc.active is True
    assert win.window_is_fullscreen is True
    assert fc.origin == 'start'

    assert _f11(fc) is True
    assert fc.active is False
    assert win.window_is_fullscreen is False


def test_start_fullscreen_preserves_start_chrome():
    win, fc = _window()
    _f11(fc)
    # Start keeps its normal UI: sidebar and header stay as they were.
    assert win.sidebar_visible() is True
    assert win.header_bar.get_visible() is True
    assert 'terminal-fullscreen-mode' not in win.css_classes


# --------------------------------------------------------------------------
# 2. Terminal fullscreen survives terminal replacement
# --------------------------------------------------------------------------


def test_fullscreen_survives_switching_to_another_terminal():
    win, fc = _window()
    _terminal_a, page_a = win.open_terminal('A')
    _terminal_b, page_b = win.open_terminal('B')

    win.tab_view.set_selected_page(page_a)
    _f11(fc)
    assert fc.active is True
    assert fc.origin == 'terminal'

    win.tab_view.set_selected_page(page_b)

    # One owner for the window, unchanged across the switch.
    assert win.fullscreen_controller is fc
    assert fc.active is True
    assert win.window_is_fullscreen is True
    assert fc.presentation == 'terminal'

    # F11 from B drives the same state machine.
    _f11(fc)
    assert fc.active is False
    assert win.window_is_fullscreen is False


def test_terminal_widget_does_not_own_fullscreen_state():
    """A TerminalWidget must not construct its own fullscreen controller."""
    from sshpilot import terminal as terminal_module

    source = terminal_module.__file__
    assert not hasattr(terminal_module, 'FullscreenController'), (
        "terminal.py still imports a terminal-owned FullscreenController; "
        f"see {source}"
    )


def test_terminal_toggle_fullscreen_delegates_to_the_window_controller():
    from sshpilot.terminal import TerminalWidget

    win, fc = _window()
    terminal = TerminalWidget.__new__(TerminalWidget)
    terminal.get_root = lambda: win

    terminal.toggle_fullscreen()
    assert fc.active is True
    terminal.toggle_fullscreen()
    assert fc.active is False


# --------------------------------------------------------------------------
# 3. Closing active terminal while another remains
# --------------------------------------------------------------------------


def test_closing_active_terminal_keeps_fullscreen_when_another_remains():
    win, fc = _window()
    _a, page_a = win.open_terminal('A')
    _b, page_b = win.open_terminal('B')
    win.tab_view.set_selected_page(page_b)

    _f11(fc)
    win.tab_view.close_page(page_b)

    assert win.tab_view.get_selected_page() is page_a
    assert fc.active is True
    assert win.window_is_fullscreen is True
    assert win.sidebar_visible() is False
    # Still the immersive terminal presentation, driven by the surviving tab.
    assert fc.presentation == 'terminal'
    assert win.header_shown() is False
    assert win.tab_bar_shown() is False


# --------------------------------------------------------------------------
# 4. Terminal-origin fullscreen exits after the last terminal closes
# --------------------------------------------------------------------------


def test_terminal_origin_fullscreen_exits_when_last_terminal_closes():
    win, fc = _window()
    before = {
        'sidebar': win.sidebar_visible(),
        'header': win.header_shown(),
        'tab_bar': win.tab_bar_shown(),
    }

    _a, page_a = win.open_terminal('A')
    _f11(fc)
    assert fc.origin == 'terminal'

    win.tab_view.close_page(page_a)

    assert win.tab_view.get_selected_page() is win._start_tab_page
    assert fc.active is False
    assert win.window_is_fullscreen is False
    assert win.sidebar_visible() == before['sidebar']
    assert win.header_shown() == before['header']
    assert win.tab_bar_shown() == before['tab_bar']
    assert win.tab_bar in win.tab_content_box.children
    assert win._content_toolbar_view.top_bars == [win.header_bar]
    assert win._content_toolbar_view.get_extend_content_to_top_edge() is False
    assert 'terminal-fullscreen-mode' not in win.css_classes


# --------------------------------------------------------------------------
# 5. Start-origin fullscreen persists
# --------------------------------------------------------------------------


def test_start_origin_fullscreen_persists_through_a_terminal_lifecycle():
    win, fc = _window()
    _f11(fc)
    assert fc.origin == 'start'

    _a, page_a = win.open_terminal('A')
    assert fc.active is True
    assert fc.presentation == 'terminal'

    win.tab_view.close_page(page_a)

    assert win.tab_view.get_selected_page() is win._start_tab_page
    assert fc.active is True
    assert win.window_is_fullscreen is True
    assert fc.origin == 'start'

    _f11(fc)
    assert fc.active is False
    assert win.window_is_fullscreen is False


# --------------------------------------------------------------------------
# 6. Terminal fullscreen is immersive: chrome auto-hides, tabs stay reachable
# --------------------------------------------------------------------------


@pytest.mark.parametrize('n_terminals', [1, 3])
def test_terminal_fullscreen_hides_all_top_chrome_by_default(n_terminals):
    win, fc = _window()
    for index in range(n_terminals):
        win.open_terminal(f'T{index}')
    full_window = win._content_toolbar_view.WINDOW_HEIGHT

    _f11(fc)

    assert fc.presentation == 'terminal'
    assert win.sidebar_visible() is False
    assert win.header_shown() is False
    assert win.tab_bar_shown() is False
    # The terminal fills the fullscreen window: nothing is left in the flow.
    assert win.terminal_allocation() == full_window


def test_terminal_fullscreen_moves_both_bars_into_one_overlay_surface():
    """One surface, not two revealers: both bars are top bars of the same
    ToolbarView, which extends the content under them."""
    win, fc = _window()
    win.open_terminal('A')

    _f11(fc)

    toolbar = win._content_toolbar_view
    assert toolbar.top_bars == [win.header_bar, win.tab_bar]
    assert win.tab_bar not in win.tab_content_box.children
    assert toolbar.get_extend_content_to_top_edge() is True
    assert toolbar.get_reveal_top_bars() is False


def test_revealed_chrome_is_opaque_over_terminal_output():
    """Flat top bars go transparent once the content extends under them.

    A see-through header bar and tab bar over live terminal output is
    unreadable, so immersive mode raises the toolbar style and puts the
    previous style back on the way out.
    """
    win, fc = _window()
    win.open_terminal('A')
    toolbar = win._content_toolbar_view
    style_before = toolbar.get_top_bar_style()

    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)

    assert toolbar.get_top_bar_style() == _ToolbarStyle.RAISED
    assert toolbar.bars_are_opaque() is True

    _f11(fc)
    assert toolbar.get_top_bar_style() == style_before


def test_top_edge_reveals_header_and_tab_bar_together():
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)
    allocation_before = win.terminal_allocation()

    fc.on_pointer_motion(None, 100.0, 0.0)

    assert fc.top_chrome_revealed is True
    assert win.header_shown() is True
    assert win.tab_bar_shown() is True
    # The exit control lives in the revealed header bar.
    assert win.fullscreen_button.get_visible() is True
    # Still fullscreen, and the terminal did not move or resize.
    assert fc.active is True
    assert win.window_is_fullscreen is True
    assert win.terminal_allocation() == allocation_before


def test_overlay_stays_revealed_moving_from_header_to_tab_bar():
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)

    # Down through the header bar (46px) and onto the tab bar (next 38px).
    for y in (10.0, 40.0, 50.0, 80.0):
        fc.on_pointer_motion(None, 100.0, y)
        assert fc.top_chrome_revealed is True, y
        assert win.header_shown() is True
        assert win.tab_bar_shown() is True


def test_overlay_hides_after_pointer_leaves_the_entire_reveal_region():
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)

    fc.on_pointer_motion(None, 100.0, 400.0)

    assert fc.top_chrome_revealed is False
    assert win.header_shown() is False
    assert win.tab_bar_shown() is False
    assert fc.active is True
    assert win.window_is_fullscreen is True


def test_pointer_leaving_the_window_hides_the_overlay():
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)

    fc.on_pointer_leave(None)

    assert fc.top_chrome_revealed is False
    assert fc.active is True


def test_switching_tabs_while_revealed_does_not_exit_fullscreen():
    win, fc = _window()
    _a, page_a = win.open_terminal('A')
    _b, page_b = win.open_terminal('B')
    win.tab_view.set_selected_page(page_a)
    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)

    win.tab_view.set_selected_page(page_b)

    assert fc.active is True
    assert win.window_is_fullscreen is True
    assert fc.presentation == 'terminal'
    # Tabs stay reachable: the overlay is still up and the bars still painted.
    assert fc.top_chrome_revealed is True
    assert win.tab_bar_shown() is True


def test_revealed_headerbar_toggle_exits_and_restores_exact_state():
    win, fc = _window()
    win.open_terminal('A')
    win._toggle_sidebar_visibility(False)  # non-default chrome before entering
    before = {
        'sidebar': win.sidebar_visible(),
        'header': win.header_shown(),
        'tab_bar': win.tab_bar_shown(),
        'allocation': win.terminal_allocation(),
    }

    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)
    assert win.fullscreen_button.get_visible() is True

    win.fullscreen_button.clicked()

    assert fc.active is False
    assert win.window_is_fullscreen is False
    assert fc.top_chrome_revealed is False
    assert win.fullscreen_button.get_visible() is False
    assert win.sidebar_visible() == before['sidebar']
    assert win.header_shown() == before['header']
    assert win.tab_bar_shown() == before['tab_bar']
    assert win.terminal_allocation() == before['allocation']
    assert win.tab_bar in win.tab_content_box.children
    assert win._content_toolbar_view.top_bars == [win.header_bar]
    assert win._content_toolbar_view.get_extend_content_to_top_edge() is False


def test_start_presentation_keeps_normal_chrome_and_never_auto_hides():
    """Immersive chrome is the terminal presentation only."""
    win, fc = _window()
    _f11(fc)  # Start-origin fullscreen, Start active

    assert fc.presentation == 'start'
    assert win.header_shown() is True
    assert win._content_toolbar_view.get_extend_content_to_top_edge() is False
    assert win.tab_bar in win.tab_content_box.children

    fc.on_pointer_motion(None, 100.0, 0.0)
    assert fc.top_chrome_revealed is False


def test_returning_to_start_while_fullscreen_restores_normal_chrome():
    win, fc = _window()
    _f11(fc)  # Start origin
    _a, page_a = win.open_terminal('A')
    assert fc.presentation == 'terminal'
    assert win.header_shown() is False

    win.tab_view.set_selected_page(win._start_tab_page)

    assert fc.active is True
    assert fc.presentation == 'start'
    assert win.header_shown() is True
    assert win.tab_bar in win.tab_content_box.children
    assert win._content_toolbar_view.get_extend_content_to_top_edge() is False


# --------------------------------------------------------------------------
# 7. Exact restoration
# --------------------------------------------------------------------------


def test_exit_restores_the_exact_pre_fullscreen_chrome():
    win, fc = _window()
    win.open_terminal('A')

    # Non-default chrome: sidebar manually hidden, banners showing.
    win._toggle_sidebar_visibility(False)
    win.update_banner_container.set_visible(True)
    win.tips_banner_container.set_visible(True)
    win.broadcast_banner.set_visible(True)

    snapshot = {
        'sidebar': win.sidebar_visible(),
        'header': win.header_shown(),
        'tab_bar': win.tab_bar_shown(),
        'allocation': win.terminal_allocation(),
        'update': win.update_banner_container.get_visible(),
        'tips': win.tips_banner_container.get_visible(),
        'broadcast': win.broadcast_banner.get_visible(),
    }

    _f11(fc)
    _f11(fc)

    assert win.sidebar_visible() == snapshot['sidebar']
    assert win.header_shown() == snapshot['header']
    assert win.tab_bar_shown() == snapshot['tab_bar']
    assert win.terminal_allocation() == snapshot['allocation']
    assert win.update_banner_container.get_visible() == snapshot['update']
    assert win.tips_banner_container.get_visible() == snapshot['tips']
    assert win.broadcast_banner.get_visible() == snapshot['broadcast']
    assert win.css_classes == set()


def test_exit_does_not_reinstate_a_manually_hidden_sidebar():
    win, fc = _window()
    win.open_terminal('A')
    win._toggle_sidebar_visibility(False)

    _f11(fc)
    assert win.sidebar_visible() is False
    _f11(fc)
    assert win.sidebar_visible() is False


# --------------------------------------------------------------------------
# 8. Terminal destruction cannot strand fullscreen (issue #1102)
# --------------------------------------------------------------------------


def test_destroying_the_terminal_cannot_strand_the_window_in_fullscreen():
    win, fc = _window()
    terminal, page = win.open_terminal('A')

    _f11(fc)

    # Step 4: the terminal that entered fullscreen goes away entirely.
    win.tab_view.close_page(page)
    del terminal

    # Step 5: back on Start with a usable window.
    assert fc.active is False
    assert win.window_is_fullscreen is False
    assert 'terminal-fullscreen-mode' not in win.css_classes
    assert win.header_shown() is True
    assert win.sidebar_visible() is True
    assert win.tab_bar in win.tab_content_box.children

    # F11 still works from Start afterwards.
    assert _f11(fc) is True
    assert fc.active is True
    assert _f11(fc) is True
    assert fc.active is False


def test_entering_fullscreen_creates_no_banner():
    """The transient top controls are the only exit affordance.

    A fullscreen banner sat in the content flow, pushed the terminal down on
    entry, and could be dismissed — after which #1102 left no way out at all.
    Nothing may re-introduce one.
    """
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)

    assert not hasattr(fc, 'banner_visible')
    assert not hasattr(fc, 'dismiss_banner')
    assert not any(name.endswith('_banner_container') for name in vars(fc))
    # Nothing is injected over the content: the real chrome *is* the overlay.
    assert win._global_overlay.overlays == []


def test_controller_holds_no_reference_to_any_terminal():
    win, fc = _window()
    terminal, page = win.open_terminal('A')
    _f11(fc)
    win.tab_view.close_page(page)

    referenced = [
        value for value in vars(fc).values()
        if isinstance(value, _FakeTerminal)
    ]
    assert referenced == []


# --------------------------------------------------------------------------
# 9. Escape
# --------------------------------------------------------------------------


def test_escape_exits_fullscreen_when_it_is_not_consumed():
    win, fc = _window()
    _f11(fc)
    assert fc.active is True

    # Bubble phase: the focused widget already declined the key.
    assert _press(fc, Gdk.KEY_Escape, phase='bubble') is True
    assert fc.active is False
    assert win.window_is_fullscreen is False


def test_escape_is_ignored_when_not_fullscreen():
    _win, fc = _window()
    assert _press(fc, Gdk.KEY_Escape, phase='bubble') is False


def test_escape_is_never_intercepted_before_the_focused_widget():
    """The capture-phase handler must not swallow Escape.

    Escape is live terminal input (vi, less, readline). Only the bubble phase —
    reached solely when the focused widget declined the key — may exit
    fullscreen.
    """
    _win, fc = _window()
    _f11(fc)
    assert _press(fc, Gdk.KEY_Escape, phase='capture') is False
    assert fc.active is True


def test_f11_is_handled_in_the_capture_phase():
    """F11 must stay a reliable toggle even when a terminal has focus."""
    _win, fc = _window()
    assert _press(fc, Gdk.KEY_F11, phase='capture') is True
    assert fc.active is True


# --------------------------------------------------------------------------
# 10. Reveal-region mechanics (Linux / Windows)
# --------------------------------------------------------------------------


def test_immersive_chrome_is_enabled_off_macos(monkeypatch):
    _win, fc = _window(monkeypatch, macos=False)
    assert fc.immersive_chrome_enabled is True


def test_chrome_never_reveals_outside_fullscreen(monkeypatch):
    win, fc = _window(monkeypatch, macos=False)
    win.open_terminal('A')
    fc.on_pointer_motion(None, 100.0, 0.0)
    assert fc.top_chrome_revealed is False
    assert win.header_shown() is True


def test_reveal_region_is_measured_from_the_bars_not_hard_coded(monkeypatch):
    """The pointer must keep the overlay up across the *whole* revealed area,
    however tall the theme makes it."""
    win, fc = _window(monkeypatch, macos=False)
    win.open_terminal('A')
    win.header_bar.height = 60
    win.tab_bar.height = 44
    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)

    fc.on_pointer_motion(None, 100.0, 100.0)  # inside 60 + 44
    assert fc.top_chrome_revealed is True

    fc.on_pointer_motion(None, 100.0, 120.0)  # past it
    assert fc.top_chrome_revealed is False


def test_overlay_survives_the_terminal_that_entered_fullscreen(monkeypatch):
    win, fc = _window(monkeypatch, macos=False)
    terminal, page = win.open_terminal('A')
    win.open_terminal('B')
    _f11(fc)

    win.tab_view.close_page(page)
    del terminal

    fc.on_pointer_motion(None, 100.0, 0.0)
    assert fc.top_chrome_revealed is True
    assert win.header_shown() is True
    win.fullscreen_button.clicked()
    assert fc.active is False


# --------------------------------------------------------------------------
# 12. Synchronization with the real window / WM fullscreen state
# --------------------------------------------------------------------------


def test_external_fullscreen_entry_sync():
    """The WM (or macOS native control) puts the window fullscreen."""
    win, fc = _window()
    win.open_terminal('A')
    assert fc.active is False
    calls_before = win.fullscreen_calls

    win.set_fullscreen_externally(True)

    assert fc.active is True
    assert fc.origin == 'terminal'          # coherent with the active page
    assert fc.presentation == 'terminal'    # presentation actually applied
    assert win.header_shown() is False
    assert win.sidebar_visible() is False
    # Adopted, not re-requested: no second fullscreen() at the window.
    assert win.fullscreen_calls == calls_before


def test_external_fullscreen_entry_from_start_takes_start_origin():
    win, fc = _window()

    win.set_fullscreen_externally(True)

    assert fc.active is True
    assert fc.origin == 'start'
    assert fc.presentation == 'start'
    assert win.header_shown() is True


def test_external_fullscreen_exit_sync():
    """The WM (or macOS native control) leaves fullscreen behind our back."""
    win, fc = _window()
    win.open_terminal('A')
    win._toggle_sidebar_visibility(False)   # non-default chrome to restore
    before = {
        'sidebar': win.sidebar_visible(),
        'header': win.header_shown(),
        'tab_bar': win.tab_bar_shown(),
        'allocation': win.terminal_allocation(),
    }
    _f11(fc)
    fc.on_pointer_motion(None, 100.0, 0.0)   # transient controls up
    calls_before = win.unfullscreen_calls

    win.set_fullscreen_externally(False)

    assert fc.active is False
    assert fc.origin is None
    assert fc._saved is None
    assert fc.presentation is None
    assert fc.top_chrome_revealed is False
    assert win.fullscreen_button.get_visible() is False
    assert 'terminal-fullscreen-mode' not in win.css_classes
    # Exact chrome restoration, including the ToolbarView parenting.
    assert win.sidebar_visible() == before['sidebar']
    assert win.header_shown() == before['header']
    assert win.tab_bar_shown() == before['tab_bar']
    assert win.terminal_allocation() == before['allocation']
    assert win.tab_bar in win.tab_content_box.children
    assert win._content_toolbar_view.top_bars == [win.header_bar]
    # The exit already happened externally: do not push it back at the WM.
    assert win.unfullscreen_calls == calls_before
    # Ready for another toggle.
    assert _f11(fc) is True
    assert fc.active is True


def test_controller_entry_does_not_loop():
    """enter() -> fullscreen() -> notify must not re-enter or re-snapshot."""
    win, fc = _window()
    win.open_terminal('A')
    win._toggle_sidebar_visibility(False)

    captures = []
    original_capture = fc._capture_ui_state

    def counting_capture():
        state = original_capture()
        captures.append(state)
        return state

    fc._capture_ui_state = counting_capture

    _f11(fc)

    assert fc.active is True
    assert win.fullscreen_calls == 1
    # One logical enter: the snapshot is taken once, and it is the *pre*
    # fullscreen chrome (a second capture would record the hidden sidebar).
    assert len(captures) == 1
    assert fc._saved['sidebar_visible'] is False   # what it was before entering
    assert fc.presentation == 'terminal'


def test_controller_exit_does_not_loop():
    """exit() -> unfullscreen() -> notify must not restore twice."""
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)

    restores = []
    original_restore = fc._restore_ui_state

    def counting_restore(saved, **kwargs):
        restores.append(saved)
        return original_restore(saved, **kwargs)

    fc._restore_ui_state = counting_restore

    _f11(fc)

    assert fc.active is False
    assert win.unfullscreen_calls == 1
    assert len(restores) == 1
    assert win.header_shown() is True
    assert win.tab_bar in win.tab_content_box.children


def test_window_manager_refusing_fullscreen_is_reconciled():
    """A request that the WM does not honour must not wedge the controller."""
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)
    assert fc.active is True

    # The WM drops the window straight back out of fullscreen.
    win.set_fullscreen_externally(False)

    assert fc.active is False
    assert fc.origin is None
    assert win.header_shown() is True
    assert _f11(fc) is True
    assert fc.active is True


def test_redundant_notify_with_consistent_state_is_a_noop():
    win, fc = _window()
    win.open_terminal('A')
    _f11(fc)
    saved = fc._saved

    # A notify that tells us what we already know changes nothing.
    fc._on_window_fullscreened_changed(win, None)

    assert fc.active is True
    assert fc._saved is saved
    assert fc.presentation == 'terminal'


# --------------------------------------------------------------------------
# 11. macOS defers to the native fullscreen experience
# --------------------------------------------------------------------------


def test_macos_does_not_emulate_the_top_edge_reveal(monkeypatch):
    """macOS owns fullscreen and its own window-control reveal.

    SSH Pilot must not build a competing auto-hiding chrome surface there: the
    ToolbarView is left in its normal layout and the pointer does nothing.
    """
    win, fc = _window(monkeypatch, macos=True)
    win.open_terminal('A')
    _f11(fc)

    assert fc.immersive_chrome_enabled is False
    assert win._content_toolbar_view.get_extend_content_to_top_edge() is False
    assert win._content_toolbar_view.top_bars == [win.header_bar]
    assert win.tab_bar in win.tab_content_box.children

    fc.on_pointer_motion(None, 100.0, 0.0)
    assert fc.top_chrome_revealed is False

    # Application fullscreen state stays coherent regardless.
    assert fc.active is True
    assert win.window_is_fullscreen is True
    _f11(fc)
    assert fc.active is False
    assert win.window_is_fullscreen is False


def test_macos_keeps_tabs_reachable_without_an_overlay(monkeypatch):
    """With no reveal gesture to lean on, the tab bar stays in the flow."""
    win, fc = _window(monkeypatch, macos=True)
    win.open_terminal('A')
    _f11(fc)

    assert win.tab_bar_shown() is True
    assert win.header_shown() is False


def test_macos_still_exits_fullscreen_when_the_last_terminal_closes(monkeypatch):
    win, fc = _window(monkeypatch, macos=True)
    _a, page_a = win.open_terminal('A')
    _f11(fc)
    win.tab_view.close_page(page_a)

    assert fc.active is False
    assert win.window_is_fullscreen is False
