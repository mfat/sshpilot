"""Regression tests for the Start-activation Omnisearch attention tracer.

When the Start tab becomes current, ``OmniSearchController.request_attention``
briefly runs a short accent-colored segment clockwise around the docked
search box's rounded border — a Cairo-drawn tracer, deliberately independent of
both the resting neutral outline and the real ``:focus-within`` ring. The
tracer must:

* fire once per genuine inactive -> active Start transition;
* never fire while Omnisearch is open or focused;
* never touch keyboard focus;
* always retire its frame callback/state, even if Start is left mid-lap or
  the widget is torn down;
* travel clockwise for the configured number of laps, following live geometry.

These tests bind the production methods onto minimal stand-ins (no painting),
the same style as ``tests/test_start_tab_focus.py``, and drive the trigger
through the real ``window_tabs.on_tab_selected`` Start branch. Geometry and
progress maths are unit-tested without GTK; the clockwise dash direction is
verified on a real raster surface when gi cairo is importable.
"""

import inspect
import math
import types

import pytest

from sshpilot import omni_search as module


# --- pure geometry / progress ----------------------------------------------

def test_rounded_rect_perimeter_matches_arc_formula():
    w, h, r = 100.0, 40.0, 8.0
    expected = 2 * (w - 2 * r) + 2 * (h - 2 * r) + 2 * math.pi * r
    assert module._rounded_rect_perimeter(w, h, r) == pytest.approx(expected)


def test_rounded_rect_perimeter_clamps_radius():
    assert module._rounded_rect_perimeter(10, 40, 8) == pytest.approx(
        2 * (10 - 2 * 5) + 2 * (40 - 2 * 5) + 2 * math.pi * 5
    )
    assert module._rounded_rect_perimeter(10, 40, 0) == pytest.approx(2 * (10 + 40))


def test_tracer_progress_clamps_by_duration():
    assert module._tracer_progress(0, 700) == 0.0
    assert module._tracer_progress(350_000, 700) == pytest.approx(0.5)
    assert module._tracer_progress(700_000, 700) == 1.0
    assert module._tracer_progress(-50, 700) == 0.0
    assert module._tracer_progress(1_000_000, 700) == 1.0
    assert module._tracer_progress(100, 0) == 1.0


def test_tracer_progress_wraps_per_lap_then_retires():
    # Per-lap phase counts up within each lap.
    assert module._tracer_progress(350_000, 700, laps=3) == pytest.approx(0.5)
    assert module._tracer_progress(700_000, 700, laps=3) == pytest.approx(0.0)
    # Third lap phase, then retirement at the total duration.
    assert module._tracer_progress(1_750_000, 700, laps=3) == pytest.approx(0.5)
    assert module._tracer_progress(2_100_000, 700, laps=3) == 1.0
    assert module._tracer_progress(3_000_000, 700, laps=3) == 1.0


def test_tracer_progress_loops_forever_when_laps_zero():
    assert module._tracer_progress(0, 700, laps=0) == 0.0
    assert module._tracer_progress(700_000, 700, laps=0) == pytest.approx(0.0)
    assert module._tracer_progress(21_700_000, 700, laps=0) == pytest.approx(0.0)
    assert module._tracer_progress(21_350_000, 700, laps=0) == pytest.approx(0.5)


def test_tracer_dash_offset_spans_one_perimeter():
    perimeter = 266.272311059609
    assert module._tracer_dash_offset(0.0, perimeter) == 0.0
    assert module._tracer_dash_offset(0.5, perimeter) == pytest.approx(-perimeter / 2)
    assert module._tracer_dash_offset(1.0, perimeter) == pytest.approx(-perimeter)


# --- stand-ins --------------------------------------------------------------

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


class _FrameClock:
    """Mirror of Gtk.FrameClock.get_frame_time (microseconds)."""

    def __init__(self, time_us=0):
        self.time_us = time_us

    def get_frame_time(self):
        return self.time_us


class _FakeTickArea:
    """Stand-in for the attention DrawingArea's tick/draw plumbing."""

    def __init__(self):
        self.callback = None
        self.draws = 0

    def add_tick_callback(self, callback):
        assert self.callback is None, "tracers must never stack"
        self.callback = callback
        return 1

    def remove_tick_callback(self, _source_id):
        self.callback = None

    def queue_draw(self):
        self.draws += 1

    def fire(self, time_us):
        assert self.callback is not None
        return self.callback(self, _FrameClock(time_us))


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
    """Gtk.Overlay stand-in: only map-hook connect/disconnect matter here."""

    def __init__(self, mapped=True):
        self.mapped = mapped
        self.handlers = {}
        self._next = 0

    def get_mapped(self):
        return self.mapped

    def connect(self, signal, callback):
        self._next += 1
        self.handlers[(signal, self._next)] = callback
        return self._next

    def disconnect(self, handler_id):
        for (signal, hid) in list(self.handlers):
            if hid == handler_id:
                del self.handlers[(signal, hid)]

    def emit_map(self):
        self.mapped = True
        for (signal, _hid), callback in list(self.handlers.items()):
            if signal == "map":
                callback()


class _FakePopup:
    def __init__(self):
        self.visible = False


class _FakeRGBA:
    red, green, blue, alpha = 0.2, 0.5, 0.8, 1.0


@pytest.fixture
def timeout_queue(monkeypatch):
    queue = _TimeoutQueue()
    monkeypatch.setattr(module.GLib, 'timeout_add', queue.timeout_add, raising=False)
    monkeypatch.setattr(module.GLib, 'source_remove', queue.source_remove, raising=False)
    return queue


def _make_omni(window=None, *, popup_open=False, entry_focused=False, mapped=True):
    # A dynamic type lets the production class property read through
    # transparently while bound methods land on the instance.
    stub_class = type('_OmniStub', (object,), {
        'attention_active': module.OmniSearchController.attention_active,
    })
    controller = stub_class()
    for name in (
        '_start_is_visible',
        'request_attention',
        '_cancel_attention',
        '_on_attention_owner_destroyed',
        '_attention_owner_is_mapped',
        '_defer_attention_until_map',
        '_on_attention_map',
        '_on_attention_start_delay',
        '_start_attention',
        '_on_attention_tick',
        '_entry_focus_widget',
        '_animations_disabled',
        '_accent_color',
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
    controller._attention_area = _FakeTickArea()
    controller._attention_active = False
    controller._attention_progress = 0.0
    controller._attention_start_us = None
    controller._attention_color = None
    controller._attention_tick_id = None
    controller._attention_start_source_id = None
    controller._attention_map_handler_id = None
    # Decorative lookups degrade gracefully; tests pin them explicitly.
    controller._animations_disabled = lambda: False
    controller._accent_color = lambda: _FakeRGBA()
    return controller


# --- tracer lifecycle -------------------------------------------------------

def test_request_attention_starts_tracer_and_ends_after_configured_laps(
    timeout_queue,
):
    omni = _make_omni()
    omni.request_attention()
    total_us = module._ATTENTION_MS * module._ATTENTION_LAPS * 1000

    assert omni.attention_active
    assert omni._attention_tick_id is not None
    assert omni._attention_area.callback is not None
    assert not omni._entry.focused

    # First tick stamps the animation origin; mid-lap it keeps running.
    assert omni._attention_area.fire(0) is True
    assert omni.attention_active
    assert omni._attention_start_us == 0

    # Just before the configured lap count completes: still running.
    assert omni._attention_area.fire(total_us - 1) is True
    assert omni.attention_active

    # The configured lap count complete: the tracer retires itself.
    assert omni._attention_area.fire(total_us) is False
    assert not omni.attention_active
    assert omni._attention_tick_id is None


def test_repeat_request_restarts_without_stacking(timeout_queue):
    omni = _make_omni()
    omni.request_attention()
    omni._attention_area.fire(1000)

    omni.request_attention()

    # The old tick was cancelled and exactly one fresh tracer takes its place.
    assert omni.attention_active
    assert omni._attention_area.callback is not None
    assert omni._attention_start_us is None  # re-armed origin

    origin_us = module._ATTENTION_MS * 1000
    total_us = module._ATTENTION_MS * module._ATTENTION_LAPS * 1000
    omni._attention_area.fire(origin_us)  # stamps origin
    assert omni._attention_area.fire(origin_us + total_us) is False
    assert not omni.attention_active
    assert omni._attention_tick_id is None


def test_cancel_attention_clears_everything_and_is_idempotent(timeout_queue):
    omni = _make_omni()
    omni.request_attention()

    omni._cancel_attention()
    assert not omni.attention_active
    assert omni._attention_tick_id is None
    assert omni._attention_area.callback is None
    assert omni._attention_start_us is None
    assert omni._attention_progress == 0.0

    omni._cancel_attention()  # harmless twice


# --- guards ----------------------------------------------------------------

def test_no_tracer_when_start_hidden(timeout_queue):
    omni = _make_omni(_FakeWindow(start_selected=False))
    omni.request_attention()

    assert not omni.attention_active
    assert omni._attention_area.callback is None


def test_no_tracer_when_nav_pushed_over_work(timeout_queue):
    omni = _make_omni(_FakeWindow(start_selected=True, nav_is_work=False))
    omni.request_attention()

    assert not omni.attention_active
    assert omni._attention_area.callback is None


def test_no_tracer_when_popup_already_open(timeout_queue):
    omni = _make_omni(popup_open=True)
    omni.request_attention()

    assert not omni.attention_active
    assert omni._attention_area.callback is None


def test_no_tracer_when_entry_already_focused(timeout_queue):
    omni = _make_omni(entry_focused=True)
    omni.request_attention()

    assert not omni.attention_active
    assert omni._attention_area.callback is None


def test_attention_never_changes_focus(timeout_queue):
    omni = _make_omni(entry_focused=False)
    omni.request_attention()

    assert omni.attention_active
    assert not omni._entry.focused


def test_reduced_motion_disables_tracer(timeout_queue):
    omni = _make_omni()
    omni._animations_disabled = lambda: True
    omni.request_attention()

    assert not omni.attention_active
    assert omni._attention_area.callback is None


def test_unresolvable_accent_color_degrades_gracefully(timeout_queue):
    omni = _make_omni()
    omni._accent_color = lambda: None
    omni.request_attention()

    assert not omni.attention_active
    assert omni._attention_area.callback is None


# --- teardown / edge cases --------------------------------------------------

def test_leaving_start_mid_lap_stops_tracer(timeout_queue):
    omni = _make_omni()
    omni.request_attention()
    omni._attention_area.fire(1000)
    assert omni.attention_active

    omni.window._start_selected = False
    assert omni._attention_area.fire(2000) is False

    assert not omni.attention_active
    assert omni._attention_tick_id is None


def test_destroy_retires_tracer(timeout_queue):
    omni = _make_omni()
    omni.request_attention()

    omni._on_attention_owner_destroyed()

    assert not omni.attention_active
    assert omni._attention_tick_id is None
    assert omni._attention_area.callback is None


# --- deferred startup presentation ------------------------------------------

def test_unmapped_request_defers_until_map_then_settles(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()

    # Nothing running yet: the window has not been presented, so a tick now
    # would animate an unmapped widget.
    assert not omni.attention_active
    assert omni._attention_area.callback is None
    assert omni._attention_map_handler_id is not None
    assert timeout_queue.pending() == set()

    omni.content.emit_map()

    # Mapped, but the tracer waits a settle beat before starting.
    assert not omni.attention_active
    assert timeout_queue.pending() == {omni._attention_start_source_id}

    timeout_queue.fire(omni._attention_start_source_id)

    assert omni.attention_active
    assert omni._attention_area.callback is not None
    assert omni._attention_map_handler_id is None

    origin_us = module._ATTENTION_MS * 1000
    total_us = module._ATTENTION_MS * module._ATTENTION_LAPS * 1000
    omni._attention_area.fire(origin_us)  # stamps origin
    assert omni._attention_area.fire(origin_us + total_us) is False
    assert not omni.attention_active


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

    timeout_queue.fire(omni._attention_start_source_id)
    assert omni.attention_active
    assert omni._attention_area.callback is not None


def test_cancel_removes_pending_map_hook(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()
    hook = omni._attention_map_handler_id

    omni._cancel_attention()

    assert hook is not None
    assert omni._attention_map_handler_id is None
    assert ('map', hook) not in omni.content.handlers

    omni.content.emit_map()
    assert not omni.attention_active
    assert omni._attention_area.callback is None


def test_cancel_removes_pending_start_delay(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()
    omni.content.emit_map()
    start_source = omni._attention_start_source_id
    assert start_source is not None

    omni._cancel_attention()

    assert start_source in timeout_queue.removed
    assert omni._attention_start_source_id is None
    assert not omni.attention_active
    assert omni._attention_area.callback is None
    assert timeout_queue.pending() == set()


def test_map_deferral_rechecks_visibility_guards(timeout_queue):
    omni = _make_omni(mapped=False)
    omni.request_attention()

    omni.window._start_selected = False
    omni.content.emit_map()

    assert not omni.attention_active
    assert timeout_queue.pending() == {omni._attention_start_source_id}

    timeout_queue.fire(omni._attention_start_source_id)

    assert not omni.attention_active
    assert omni._attention_start_source_id is None
    assert timeout_queue.pending() == set()


# --- window integration -----------------------------------------------------

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


# --- structural guard -------------------------------------------------------

def test_show_cancels_pending_attention():
    """Opening Omnisearch must retire any in-flight tracer (edge case: user
    activates search while the tracer is still running)."""
    from sshpilot.omni_search import OmniSearchController

    source = inspect.getsource(OmniSearchController.show)
    assert '_cancel_attention' in source


def test_request_attention_does_not_grab_focus_or_open_popup():
    """The attention path must never call show()/grab_focus(); it only arms
    a decorative tick callback."""
    from sshpilot.omni_search import OmniSearchController

    source = inspect.getsource(OmniSearchController.request_attention)
    assert 'grab_focus' not in source
    assert 'popup.show' not in source


# --- real raster direction check (skipped without pycairo) ------------------

def _real_pycairo():
    """Return the real pycairo module or None (stubbed CI). gi's cairo typelib
    does not expose ImageSurface, so the raster probes render via pycairo and
    monkeypatch it in as the module's ``Gcairo`` (the draw helper only uses
    ``LineCap.ROUND`` from it)."""
    try:
        import cairo

        if not getattr(cairo, '__file__', None):
            return None
        return cairo
    except Exception:
        return None


@pytest.mark.parametrize('frac_a, frac_b, dx, dy', [
    (0.125, 0.375, +1, +1),  # top edge -> right edge: X grows, Y grows
    (0.375, 0.625, -1, +1),  # right edge -> bottom edge: X shrinks, Y grows
    (0.625, 0.875, -1, -1),  # bottom edge -> left edge: X shrinks, Y shrinks
    (0.875, 0.125, +1, -1),  # left edge -> top edge (wrap): X grows, Y shrinks
])
def test_tracer_travels_clockwise(frac_a, frac_b, dx, dy, monkeypatch):
    """The dashed tracer must sweep clockwise around the rounded rect. At each
    quarter-lap step the painted segment's centroid crosses to the next side;
    the signed sign per step (derived from a clockwise lap) proves the
    direction — a counterclockwise implementation would flip the signs."""
    cairo = _real_pycairo()
    if cairo is None:
        pytest.skip('real pycairo unavailable')
    monkeypatch.setattr(module, 'Gcairo', cairo)

    width, height = 120, 48

    def centroid_at(progress):
        surface = cairo.ImageSurface(cairo.Format.ARGB32, width, height)
        cr = cairo.Context(surface)
        module._draw_attention_tracer(cr, width, height, progress, _FakeRGBA())
        data = surface.get_data()
        total = 0
        sx = sy = 0
        stride = surface.get_stride()
        for y in range(height):
            row = y * stride
            for x in range(width):
                # ARGB32 (little-endian) memory order: B, G, R, A. The tracer
                # color is (0.2, 0.5, 0.8, 1.0) -> B byte ~204, well above blank.
                if data[row + x * 4] > 200:
                    total += 1
                    sx += x
                    sy += y
        assert total > 0, 'no tracer pixels painted at progress %s' % progress
        return sx / total, sy / total

    def assert_step(a, b, step_dx, step_dy, margin=6.0):
        ca = centroid_at(a)
        cb = centroid_at(b)
        delta_x = cb[0] - ca[0]
        delta_y = cb[1] - ca[1]
        assert abs(delta_x) > margin or abs(delta_y) > margin, (a, b, ca, cb)
        if step_dx > 0:
            assert delta_x > margin, (a, b, ca, cb)
        else:
            assert delta_x < -margin, (a, b, ca, cb)
        if step_dy > 0:
            assert delta_y > margin, (a, b, ca, cb)
        else:
            assert delta_y < -margin, (a, b, ca, cb)

    assert_step(frac_a, frac_b, dx, dy)


def test_tracer_segment_is_short_relative_to_border(monkeypatch):
    """One visible segment only: a 20% dash on the full perimeter leaves the
    painted coverage near the expected 20% of the inner outline."""
    cairo = _real_pycairo()
    if cairo is None:
        pytest.skip('real pycairo unavailable')
    monkeypatch.setattr(module, 'Gcairo', cairo)

    width, height = 120, 48
    surface = cairo.ImageSurface(cairo.Format.ARGB32, width, height)
    cr = cairo.Context(surface)
    module._draw_attention_tracer(cr, width, height, 0.5, _FakeRGBA())

    data = surface.get_data()
    painted = 0
    stride = surface.get_stride()
    for y in range(height):
        row = y * stride
        for x in range(width):
            if data[row + x * 4] > 200:
                painted += 1
    perimeter = module._rounded_rect_perimeter(
        width - 2, height - 2,
        min(module._ATTENTION_RADIUS, min(width, height) / 2 - 1),
    )
    # Painted pixels ≈ on-path segment length x stroke width (plus a little
    # antialiasing bleed, absorbed by the tolerance).
    expected = (
        perimeter * module._ATTENTION_SEGMENT_FRACTION * module._ATTENTION_STROKE_WIDTH
    )
    assert painted == pytest.approx(expected, rel=0.4)
