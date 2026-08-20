"""Regression tests for the transient Start-activation Omnisearch attention.

When the Start tab becomes current, ``OmniSearchController.request_attention``
briefly blooms the docked search box with a soft accent halo — a separate,
transient class from the real ``:focus-within`` ring. The pulse must:

* fire once per genuine inactive -> active Start transition;
* never fire while Omnisearch is open or focused;
* never touch keyboard focus;
* always remove its class and timer, even if Start is left mid-pulse or the
  widget is torn down.

These tests bind the production methods onto minimal stand-ins (no GTK), the
same style as ``tests/test_start_tab_focus.py``, and drive the trigger through
the real ``window_tabs.on_tab_selected`` Start branch.
"""

import inspect
import sys
import types

import pytest


def _omni_module():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()
    from sshpilot import omni_search as module
    return module


class _TimeoutQueue:
    """Deterministic stand-in for GLib.timeout_add / source_remove."""

    def __init__(self):
        self.scheduled = {}
        self.removed = []
        self._next = 0

    def timeout_add(self, _ms, callback):
        self._next += 1
        self.scheduled[self._next] = callback
        return self._next

    def source_remove(self, source_id):
        self.removed.append(source_id)
        self.scheduled.pop(source_id, None)

    def pending(self):
        return set(self.scheduled)

    def fire(self, source_id):
        callback = self.scheduled.pop(source_id)
        return callback()


class _FakeWindow:
    def __init__(self, start_selected=True, nav_is_work=True):
        self._start_selected = start_selected
        self._work_page = object()
        self.nav_view = types.SimpleNamespace(
            get_visible_page=lambda: self._work_page if nav_is_work else object()
        )

    def is_start_tab_selected(self):
        return self._start_selected


class _FakeFocusWidget:
    def __init__(self, focused=False):
        self.focused = focused

    def has_focus(self):
        return self.focused


class _FakeContent:
    def __init__(self, mapped=True):
        self.css_classes = set()
        self.mapped = mapped
        self.handlers = {}
        self._next_handler = 0

    def add_css_class(self, name):
        self.css_classes.add(name)

    def remove_css_class(self, name):
        self.css_classes.discard(name)

    def has_css_class(self, name):
        return name in self.css_classes

    def get_mapped(self):
        return self.mapped

    def connect(self, signal, callback):
        self._next_handler += 1
        self.handlers[(signal, self._next_handler)] = callback
        return self._next_handler

    def disconnect(self, handler_id):
        for (signal, hid) in list(self.handlers):
            if hid == handler_id:
                del self.handlers[(signal, hid)]

    def emit_map(self):
        self.mapped = True
        callbacks = [
            callback
            for (signal, _hid), callback in self.handlers.items()
            if signal == "map"
        ]
        for callback in callbacks:
            callback()


class _FakePopup:
    def __init__(self):
        self.visible = False


@pytest.fixture
def timeout_queue(monkeypatch):
    module = _omni_module()
    queue = _TimeoutQueue()
    monkeypatch.setattr(module.GLib, 'timeout_add', queue.timeout_add, raising=False)
    monkeypatch.setattr(module.GLib, 'source_remove', queue.source_remove, raising=False)
    return queue


def _make_omni(window=None, *, popup_open=False, entry_focused=False, mapped=True):
    module = _omni_module()
    controller = types.SimpleNamespace()
    for name in (
        '_start_is_visible',
        'request_attention',
        '_cancel_attention',
        '_on_attention_timeout',
        '_on_attention_owner_destroyed',
        '_attention_owner_is_mapped',
        '_defer_attention_until_map',
        '_on_attention_map',
        '_on_attention_start_delay',
        '_start_attention_pulse',
        '_entry_focus_widget',
    ):
        setattr(controller, name, getattr(
            module.OmniSearchController, name
        ).__get__(controller, type(controller)))
    controller.window = window if window is not None else _FakeWindow()
    controller.popup = _FakePopup()
    controller.popup.visible = popup_open
    entry = _FakeFocusWidget(entry_focused)
    controller._entry = entry
    controller._entry_focus_widget = lambda: entry
    controller.content = _FakeContent(mapped=mapped)
    controller._attention_source_id = None
    controller._attention_start_source_id = None
    controller._attention_map_handler_id = None
    return controller


# --- pulse lifecycle -------------------------------------------------------

def test_request_attention_applies_class_and_schedules_cleanup(timeout_queue):
    omni = _make_omni()
    omni.request_attention()

    assert omni.content.has_css_class('omni-search-attention')
    assert omni._attention_source_id is not None
    pending = timeout_queue.pending()
    assert len(pending) == 1
    # Wired to the cleanup callback so the class cannot stick.
    registered = timeout_queue.scheduled[next(iter(pending))]
    assert registered.__name__ == '_on_attention_timeout'


def test_timeout_removes_class_and_clears_source(timeout_queue):
    omni = _make_omni()
    omni.request_attention()
    source_id = omni._attention_source_id

    result = timeout_queue.fire(source_id)

    assert result == _omni_module().GLib.SOURCE_REMOVE
    assert omni._attention_source_id is None
    assert not omni.content.has_css_class('omni-search-attention')


def test_repeat_request_restarts_pulse_without_stacking(timeout_queue):
    omni = _make_omni()
    omni.request_attention()
    first = omni._attention_source_id

    omni.request_attention()

    # The old timer is replaced, so bursts of activation requests produce a
    # single pulse every time.
    assert first in timeout_queue.removed
    assert timeout_queue.pending() == {omni._attention_source_id}
    assert omni.content.has_css_class('omni-search-attention')

    timeout_queue.fire(omni._attention_source_id)
    assert not omni.content.has_css_class('omni-search-attention')
    assert omni._attention_source_id is None


def test_cancel_attention_is_idempotent_when_nothing_pending(timeout_queue):
    omni = _make_omni()
    omni._cancel_attention()
    assert omni._attention_source_id is None
    assert not omni.content.has_css_class('omni-search-attention')


# --- guards ----------------------------------------------------------------

def test_no_pulse_when_start_hidden(timeout_queue):
    omni = _make_omni(_FakeWindow(start_selected=False))
    omni.request_attention()

    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == set()


def test_no_pulse_when_nav_pushed_over_work(timeout_queue):
    omni = _make_omni(_FakeWindow(start_selected=True, nav_is_work=False))
    omni.request_attention()

    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == set()


def test_no_pulse_when_popup_already_open(timeout_queue):
    omni = _make_omni(popup_open=True)
    omni.request_attention()

    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == set()


def test_no_pulse_when_entry_already_focused(timeout_queue):
    omni = _make_omni(entry_focused=True)
    omni.request_attention()

    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == set()


def test_attention_never_changes_focus(timeout_queue):
    omni = _make_omni(entry_focused=False)
    omni.request_attention()

    assert not omni._entry.focused
    assert omni.content.has_css_class('omni-search-attention')


# --- teardown / edge cases -------------------------------------------------

def test_leaving_start_before_timeout_still_cleans_up(timeout_queue):
    omni = _make_omni()
    omni.request_attention()
    source_id = omni._attention_source_id

    omni.window._start_selected = False
    timeout_queue.fire(source_id)

    assert not omni.content.has_css_class('omni-search-attention')
    assert omni._attention_source_id is None


# --- deferred pulse (initial presentation before first paint) ---------------

def test_unmapped_request_defers_pulse_until_map(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()

    # Nothing applied yet: the window has not been presented, so a timer now
    # would expire before the first paint.
    assert not omni.content.has_css_class('omni-search-attention')
    assert omni._attention_source_id is None
    assert omni._attention_start_source_id is None
    assert omni._attention_map_handler_id is not None
    assert timeout_queue.pending() == set()

    omni.content.emit_map()

    # Mapped, but the bloom waits a settle beat before starting.
    assert not omni.content.has_css_class('omni-search-attention')
    assert omni._attention_source_id is None
    assert omni._attention_map_handler_id is None
    assert timeout_queue.pending() == {omni._attention_start_source_id}

    timeout_queue.fire(omni._attention_start_source_id)

    assert omni.content.has_css_class('omni-search-attention')
    assert omni._attention_start_source_id is None
    assert timeout_queue.pending() == {omni._attention_source_id}

    timeout_queue.fire(omni._attention_source_id)
    assert not omni.content.has_css_class('omni-search-attention')


def test_repeat_unmapped_requests_replace_map_hook(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()
    first_hook = omni._attention_map_handler_id
    omni.request_attention()

    # One pending hook, exactly: the earlier one was replaced.
    assert first_hook is not None
    assert omni._attention_map_handler_id != first_hook
    pending = [
        signal for (signal, _hid) in omni.content.handlers if signal == 'map'
    ]
    assert pending == ['map']

    omni.content.emit_map()
    omni.content.emit_map()
    assert timeout_queue.pending() == {omni._attention_start_source_id}
    assert len(timeout_queue.removed) == 0

    timeout_queue.fire(omni._attention_start_source_id)
    assert timeout_queue.pending() == {omni._attention_source_id}
    assert len(timeout_queue.removed) == 0


def test_cancel_removes_pending_map_hook(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()
    hook = omni._attention_map_handler_id

    omni._cancel_attention()

    assert hook is not None
    assert omni._attention_map_handler_id is None
    assert ('map', hook) not in omni.content.handlers

    omni.content.emit_map()
    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == set()


def test_map_deferral_rechecks_visibility_guards(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()

    omni.window._start_selected = False
    omni.content.emit_map()

    # Mapped, but Start is no longer the selected tab: the settle delay fires
    # and the guarded request declines, so still no pulse.
    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == {omni._attention_start_source_id}

    timeout_queue.fire(omni._attention_start_source_id)

    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == set()
    assert omni._attention_start_source_id is None


def test_cancel_removes_pending_start_delay(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()
    omni.content.emit_map()
    start_source = omni._attention_start_source_id
    assert start_source is not None

    omni._cancel_attention()

    assert start_source in timeout_queue.removed
    assert omni._attention_start_source_id is None
    assert not omni.content.has_css_class('omni-search-attention')
    assert timeout_queue.pending() == set()


def test_destroy_cancels_pending_pulse(timeout_queue):
    omni = _make_omni()
    omni.request_attention()
    source_id = omni._attention_source_id

    omni._on_attention_owner_destroyed()

    assert source_id in timeout_queue.removed
    assert omni._attention_source_id is None
    assert not omni.content.has_css_class('omni-search-attention')


# --- window integration ----------------------------------------------------

class _FakeTabView:
    def __init__(self, selected):
        self._selected = selected

    def get_selected_page(self):
        return self._selected


class _FakePage:
    """Tab page stand-in; the production handler requires ``get_child``."""

    def __init__(self, child=None):
        self._child = child if child is not None else types.SimpleNamespace()

    def get_child(self):
        return self._child


class _TabWindow:
    """Minimal MainWindow stub binding the real tab-selection plumbing."""

    def __init__(self, omni_record):
        from sshpilot.window import MainWindow

        self._omni_search = omni_record
        self._start_tab_page = _FakePage()
        self.tab_view = _FakeTabView(self._start_tab_page)
        self.user_tabs = []
        self._sidebar_minimal = False
        self.terminal_to_connection = {}
        self.config = types.SimpleNamespace(get_setting=lambda key, default=None: default)

        for name in (
            '_update_content_theme_for_selected_tab',
            '_update_layout_toggle_state',
            '_is_start_tab_page',
            'has_user_tabs',
            'set_sidebar_minimal',
            '_sidebar_mode_is_minimal',
            '_apply_sidebar_visible',
            'on_tab_selected',
        ):
            setattr(self, name, getattr(MainWindow, name).__get__(self, type(self)))
        self._focus_schedules = []

    def _schedule_start_tab_focus(self):
        self._focus_schedules.append(True)

    def select(self, page):
        changed = self.tab_view._selected is not page
        self.tab_view._selected = page
        if changed:
            self.on_tab_selected(self.tab_view)


class _OmniRecorder:
    def __init__(self):
        self.requests = 0

    def request_attention(self):
        self.requests += 1


def test_terminal_to_start_transition_requests_attention_once():
    recorder = _OmniRecorder()
    win = _TabWindow(recorder)
    terminal_page = object()

    win.select(terminal_page)
    win.select(win._start_tab_page)

    assert recorder.requests == 1


def test_selecting_already_current_start_does_not_request_attention():
    recorder = _OmniRecorder()
    win = _TabWindow(recorder)

    win.select(win._start_tab_page)

    assert recorder.requests == 0
    assert win._focus_schedules == []


def test_no_attention_when_omni_controller_absent():
    win = _TabWindow(None)
    terminal_page = object()
    win.select(terminal_page)
    win.select(win._start_tab_page)

    assert win._focus_schedules == [True]


# --- structural guard ------------------------------------------------------

def test_show_cancels_pending_attention():
    """Opening Omnisearch must retire any in-flight pulse (edge case: user
    activates search while the attention effect is still running)."""
    from sshpilot.omni_search import OmniSearchController

    source = inspect.getsource(OmniSearchController.show)
    assert '_cancel_attention' in source


def test_request_attention_does_not_grab_focus_or_open_popup():
    """The attention path must never call show()/grab_focus(); it is purely a
    CSS class + timer."""
    from sshpilot.omni_search import OmniSearchController

    source = inspect.getsource(OmniSearchController.request_attention)
    assert 'grab_focus' not in source
    assert 'popup.show' not in source