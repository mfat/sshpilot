"""Regression tests for Start-tab sidebar focus (issue #1175).

Closing the last session tab returns to the pinned Start tab. That transition
used to schedule ``_focus_connection_list_first_row`` — from ``show_start_tab``
*and* again from ``on_tab_selected`` — and while the helper's startup guard kept
it from re-*selecting* row 0, it still called ``first_row.grab_focus()``
unconditionally. GTK brings a newly focused row into view, so a user who had
scrolled to the bottom of a long sidebar was yanked back to the top.

The fix makes ``_focus_connection_list_first_row`` startup-only and gives the
Start tab a single focus owner, ``_focus_start_tab_sidebar``, which at runtime
prefers the already-selected row and never falls back to row 0.

The fakes below model the one GTK behavior that makes the bug observable:
``grab_focus()`` on a row scrolls that row into view.
"""

import sys
import types

import pytest


def _window_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import window as window_module
    return window_module


ROW_HEIGHT = 40
VIEWPORT_ROWS = 10
TOTAL_ROWS = 40


class _FakeAdjustment:
    """Stand-in for the sidebar's vertical Gtk.Adjustment."""

    def __init__(self):
        self.value = 0.0

    def get_value(self):
        return self.value

    def set_value(self, value):
        self.value = float(value)


class _FakeRow:
    def __init__(self, index, list_box):
        self.index = index
        self._list_box = list_box
        # Every row in these tests stands for a connection.
        self.connection = object()

    def get_parent(self):
        return self._list_box

    def grab_focus(self):
        window = self._list_box.window
        window.focus_widget = self
        window.grab_focus_calls.append(self.index)
        # GTK scrolls a freshly focused row into view. Only move the
        # adjustment when the row is actually outside the viewport, which is
        # what makes "focus row 0" reset a scrolled sidebar to the top.
        top = self._list_box.window.vadjustment.get_value()
        bottom = top + VIEWPORT_ROWS * ROW_HEIGHT
        row_top = self.index * ROW_HEIGHT
        if row_top < top:
            window.vadjustment.set_value(row_top)
        elif row_top + ROW_HEIGHT > bottom:
            window.vadjustment.set_value(row_top + ROW_HEIGHT - VIEWPORT_ROWS * ROW_HEIGHT)

    def __repr__(self):  # pragma: no cover - debugging aid
        return f'<row {self.index}>'


class _FakeListBox:
    def __init__(self, window, n_rows):
        self.window = window
        self.rows = [_FakeRow(i, self) for i in range(n_rows)]
        self.selected = []
        self._parent = object()

    def get_parent(self):
        return self._parent

    def get_row_at_index(self, index):
        if 0 <= index < len(self.rows):
            return self.rows[index]
        return None

    def get_selected_rows(self):
        return list(self.selected)

    def get_selected_row(self):
        return self.selected[0] if self.selected else None

    def select_row(self, row):
        self.selected = [row]

    def unselect_all(self):
        self.selected = []

    def grab_focus(self):
        self.window.focus_widget = self
        self.window.grab_focus_calls.append('list_box')


class _TerminalWidget:
    """Whatever held focus in the session tab; destroyed when the tab closes."""

    def get_parent(self):
        return None


class _FocusWindow:
    """Stub window wired to the real focus methods under test."""

    def __init__(self, window_module, n_rows=TOTAL_ROWS):
        MainWindow = window_module.MainWindow
        # Bind the production implementations, not copies.
        for name in (
            '_connection_list_is_focusable',
            '_first_selected_connection_list_row',
            '_startup_first_row_focus_allowed',
            '_focus_connection_list_first_row',
            '_schedule_start_tab_focus',
            '_focus_start_tab_sidebar',
            '_focus_is_in_connection_list',
            '_select_only_row',
            '_is_start_tab_page',
            'is_start_tab_selected',
            'show_start_tab',
            'on_tab_selected',
            '_update_command_snippets_button_visibility',
        ):
            setattr(self, name, getattr(MainWindow, name).__get__(self, type(self)))

        self.connection_list = _FakeListBox(self, n_rows)
        self.vadjustment = _FakeAdjustment()
        self.focus_widget = None
        self.grab_focus_calls = []

        # Startup bookkeeping mirrored from MainWindow.__init__.
        self._startup_complete = False
        self._initial_connection_list_focus_done = False
        self._start_tab_focus_idle_id = None
        self._omni_search = None

        # Tab plumbing touched by show_start_tab / on_tab_selected.
        self._start_tab_page = object()
        self.tab_view = types.SimpleNamespace(
            selected=self._start_tab_page,
            set_selected_page=self._set_selected_page,
            get_selected_page=lambda: self.tab_view.selected,
        )
        self.user_tabs = []
        self.config = types.SimpleNamespace(get_setting=lambda key, default=None: default)
        self._sidebar_minimal = False

    # -- focus observation -------------------------------------------------
    def get_focus(self):
        return self.focus_widget

    # -- tab plumbing ------------------------------------------------------
    def _set_selected_page(self, page):
        changed = self.tab_view.selected is not page
        self.tab_view.selected = page
        # Adw.TabView only emits notify::selected-page on an actual change.
        if changed:
            self.on_tab_selected(self.tab_view)

    def _create_start_tab(self):
        return None

    def has_user_tabs(self):
        return bool(self.user_tabs)

    def _update_content_theme_for_selected_tab(self):
        return None

    def _update_layout_toggle_state(self):
        return None

    def _apply_sidebar_visible(self, _visible):
        return None

    def _sidebar_mode_is_minimal(self):
        return False

    def set_sidebar_minimal(self, _minimal):
        return None

    # -- scenario helpers --------------------------------------------------
    def finish_startup(self):
        self._startup_complete = True
        self._initial_connection_list_focus_done = True

    def scroll_to_row(self, index):
        """Put ``index`` at the bottom of the viewport, as scrolling down does."""
        self.vadjustment.set_value((index + 1) * ROW_HEIGHT - VIEWPORT_ROWS * ROW_HEIGHT)

    def open_session_tab(self):
        page = object()
        self.user_tabs.append(page)
        self.focus_widget = _TerminalWidget()
        self.tab_view.set_selected_page(page)
        return page

    def close_last_session_tab(self):
        """Mirror on_tab_detached: drop the page, then fall back to Start."""
        self.user_tabs.pop()
        # Focus dies with the tab only if it lived there — a user who tabbed
        # back to the sidebar first keeps it.
        if not self._focus_is_in_connection_list():
            self.focus_widget = None
        self.show_start_tab()


class _IdleQueue:
    def __init__(self):
        self.pending = []
        self._next_id = 0

    def idle_add(self, callback, *args, **kwargs):
        self._next_id += 1
        self.pending.append((callback, args))
        return self._next_id

    def process(self):
        """Run every queued idle callback, as the GLib main loop would."""
        while self.pending:
            callback, args = self.pending.pop(0)
            callback(*args)


@pytest.fixture
def idle_queue(monkeypatch):
    from sshpilot import window_tabs as tabs_module

    window_module = _window_module()
    queue = _IdleQueue()
    monkeypatch.setattr(window_module.GLib, 'idle_add', queue.idle_add, raising=False)
    monkeypatch.setattr(tabs_module.GLib, 'idle_add', queue.idle_add, raising=False)
    return queue


@pytest.fixture
def window():
    return _FocusWindow(_window_module())


# --- startup ---------------------------------------------------------------

def test_startup_selects_and_focuses_first_row(window, idle_queue):
    """On startup the first connection row still gets selection and focus."""
    window.show_start_tab()
    idle_queue.process()

    first_row = window.connection_list.get_row_at_index(0)
    assert window.connection_list.get_selected_rows() == [first_row]
    assert window.focus_widget is first_row


def test_startup_focuses_a_row_not_the_container(window, idle_queue):
    """Keyboard navigation needs a *row* to hold focus, not the ListBox.

    GTK4 arrow-key navigation in a Gtk.ListBox only works when a row has
    focus; focusing the container leaves no row focused and the arrows dead.
    """
    window.show_start_tab()
    idle_queue.process()

    assert isinstance(window.focus_widget, _FakeRow)
    assert 'list_box' not in window.grab_focus_calls


def test_startup_first_row_focus_survives_a_late_store_projection(window, idle_queue):
    """The daemon can populate the sidebar after the 500 ms startup timer.

    ``on_projection_reset`` is the first moment rows actually exist, so
    first-row focus must still be allowed there even though
    ``_startup_complete`` is already True (commit 52e6eb16).
    """
    window._startup_complete = True
    window._initial_connection_list_focus_done = False

    assert window._startup_first_row_focus_allowed() is True
    window._focus_connection_list_first_row()
    assert window.focus_widget is window.connection_list.get_row_at_index(0)


# --- runtime return to Start ----------------------------------------------

def _scrolled_to_bottom_with_selection(window, index=35):
    window.finish_startup()
    row = window.connection_list.get_row_at_index(index)
    window._select_only_row(row)
    window.scroll_to_row(index)
    window.focus_widget = row
    return row


def test_closing_last_session_tab_keeps_sidebar_scroll_and_selection(window, idle_queue):
    """The reported bug: return to Start must not scroll the sidebar to the top."""
    row = _scrolled_to_bottom_with_selection(window)
    scroll_before = window.vadjustment.get_value()
    assert scroll_before > 0

    window.open_session_tab()
    window.close_last_session_tab()
    idle_queue.process()

    assert window.connection_list.get_selected_rows() == [row]
    assert window.focus_widget is row
    assert 0 not in window.grab_focus_calls
    assert window.vadjustment.get_value() == scroll_before


def test_runtime_return_to_start_never_invokes_startup_first_row_focus(window, idle_queue):
    """After startup, Start must not schedule or invoke the startup-only helper."""
    _scrolled_to_bottom_with_selection(window)
    calls = []
    window._focus_connection_list_first_row = lambda: calls.append(True) or False

    window.open_session_tab()
    window.close_last_session_tab()
    idle_queue.process()

    assert calls == []
    assert window._startup_first_row_focus_allowed() is False


def test_startup_only_helper_is_inert_after_startup(window):
    """Called directly after startup, the helper does nothing at all."""
    row = _scrolled_to_bottom_with_selection(window)
    scroll_before = window.vadjustment.get_value()

    window._focus_connection_list_first_row()

    assert window.focus_widget is row
    assert window.grab_focus_calls == []
    assert window.vadjustment.get_value() == scroll_before


def test_start_tab_focus_is_scheduled_once_per_transition(window, idle_queue):
    """show_start_tab and on_tab_selected must collapse into one idle pass."""
    _scrolled_to_bottom_with_selection(window)
    window.open_session_tab()
    idle_queue.pending.clear()

    window.close_last_session_tab()

    assert len(idle_queue.pending) == 1


def test_focus_already_in_sidebar_is_left_untouched(window, idle_queue):
    """Switching to Start from a focused sidebar row must not re-grab focus."""
    row = _scrolled_to_bottom_with_selection(window)
    window.open_session_tab()
    # User tabbed back to the sidebar before closing the session.
    window.focus_widget = row
    window.close_last_session_tab()
    idle_queue.process()

    assert window.grab_focus_calls == []
    assert window.focus_widget is row


def test_no_selection_does_not_fall_back_to_the_first_row(window, idle_queue):
    """With nothing selected at runtime there is no arbitrary row-0 fallback."""
    window.finish_startup()
    window.scroll_to_row(35)
    scroll_before = window.vadjustment.get_value()

    window.open_session_tab()
    window.close_last_session_tab()
    idle_queue.process()

    assert window.connection_list.get_selected_rows() == []
    assert window.grab_focus_calls == []
    assert window.vadjustment.get_value() == scroll_before


# --- structural guard ------------------------------------------------------

def test_start_tab_paths_do_not_reference_the_startup_only_helper():
    """Neither Start-tab entry point may schedule first-row focusing again.

    Both used to call ``GLib.idle_add(self._focus_connection_list_first_row)``.
    They must go through ``_schedule_start_tab_focus`` — the single owner —
    so a re-introduction here cannot resurrect issue #1175.
    """
    import inspect

    from sshpilot.window import MainWindow

    for method in (MainWindow.show_start_tab, MainWindow.on_tab_selected):
        source = inspect.getsource(method)
        assert '_focus_connection_list_first_row' not in source, (
            f'{method.__qualname__} references the startup-only first-row focus '
            'helper; route it through _schedule_start_tab_focus instead'
        )
        assert '_schedule_start_tab_focus' in source, (
            f'{method.__qualname__} no longer schedules the Start-tab focus owner'
        )


def test_stale_start_focus_idle_does_not_steal_focus_after_switching_away(
    window,
    idle_queue,
):
    """A queued Start focus pass must be inert after the user leaves Start.

    ``_schedule_start_tab_focus`` queues at PRIORITY_DEFAULT_IDLE, below GTK's
    event dispatch, so a quick switch back to a session tab is handled first
    and the callback would otherwise pull focus out of the terminal.
    """
    row = _scrolled_to_bottom_with_selection(window)
    session_page = window.open_session_tab()

    window.grab_focus_calls.clear()

    window.show_start_tab()

    assert window.tab_view.get_selected_page() is window._start_tab_page
    assert len(idle_queue.pending) == 1

    terminal_focus = _TerminalWidget()
    window.focus_widget = terminal_focus

    window.tab_view.set_selected_page(session_page)

    assert window.tab_view.get_selected_page() is session_page

    idle_queue.process()

    assert window.focus_widget is terminal_focus
    assert window.grab_focus_calls == []
    assert window._start_tab_focus_idle_id is None
    assert window.connection_list.get_selected_rows() == [row]
