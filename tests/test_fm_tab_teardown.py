"""Regression tests for file-manager tab teardown (the shutdown-segfault path).

A file-manager embed carries Python 'destroy' handlers that segfault if the
embed is finalized during garbage collection, so the app tears the embed's
controller down synchronously at tab-close / shutdown. These tests pin the
teardown *ordering and bookkeeping*, the idempotency guard, the embed lookup
recursion, and the on_tab_detached routing.

Scope (honesty): under the GTK-stubbed test harness this verifies call order,
registry bookkeeping, and routing decisions only. It cannot prove that the GTK
'destroy' handlers physically detached or that no finalizer fires during a real
GC — that guarantee stays in manual QA on a real GTK build.
"""

import sys
import types


def _ensure_cairo_stub():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()


def _window_module():
    _ensure_cairo_stub()
    from sshpilot import window as window_module
    return window_module


def _make_embed(controller):
    return types.SimpleNamespace(_controller=controller)


def _make_controller(order):
    """A controller that records the order of its teardown calls."""
    ctrl = types.SimpleNamespace()
    ctrl._cleanup_manager = lambda: order.append('cleanup')
    ctrl.destroy = lambda: order.append('destroy')
    return ctrl


# ── pure static helper: _teardown_embed_controller ──────────────────────────

def test_teardown_controller_order_and_bookkeeping():
    wm = _window_module()
    order = []
    ctrl = _make_controller(order)
    embed = _make_embed(ctrl)
    registry = ['other', ctrl]

    result = wm.MainWindow._teardown_embed_controller(embed, registry)

    assert result is True
    # registry removal happens before cleanup/destroy; controller nulled last.
    assert ctrl not in registry
    assert order == ['cleanup', 'destroy']
    assert embed._controller is None


def test_teardown_controller_is_idempotent():
    wm = _window_module()
    order = []
    embed = _make_embed(_make_controller(order))
    registry = []

    assert wm.MainWindow._teardown_embed_controller(embed, registry) is True
    # Second call: controller already None → no work, no extra cleanup/destroy.
    assert wm.MainWindow._teardown_embed_controller(embed, registry) is False
    assert order == ['cleanup', 'destroy']


def test_teardown_controller_none_embed_or_controller():
    wm = _window_module()
    assert wm.MainWindow._teardown_embed_controller(None, []) is False
    assert wm.MainWindow._teardown_embed_controller(_make_embed(None), []) is False


def test_teardown_controller_not_in_registry_is_fine():
    wm = _window_module()
    order = []
    ctrl = _make_controller(order)
    embed = _make_embed(ctrl)

    # Controller absent from the registry must not raise; teardown still runs.
    assert wm.MainWindow._teardown_embed_controller(embed, []) is True
    assert order == ['cleanup', 'destroy']


# ── wrapper: _teardown_file_manager_embed (idempotency through the real path) ─

def test_wrapper_tears_down_once_then_noops():
    wm = _window_module()
    win = wm.MainWindow.__new__(wm.MainWindow)
    order = []
    ctrl = _make_controller(order)
    embed = _make_embed(ctrl)
    win._internal_file_manager_windows = [ctrl]

    win._teardown_file_manager_embed(embed)
    assert embed._controller is None
    assert ctrl not in win._internal_file_manager_windows
    assert order == ['cleanup', 'destroy']

    # Idempotent: a second call (e.g. shutdown after detach) is a no-op.
    win._teardown_file_manager_embed(embed)
    assert order == ['cleanup', 'destroy']


# ── embed lookup recurses through wrapper boxes ─────────────────────────────

def test_embed_lookup_recurses_into_subtree():
    wm = _window_module()
    from sshpilot.file_manager_integration import FileManagerTabEmbed

    win = wm.MainWindow.__new__(wm.MainWindow)
    embed = FileManagerTabEmbed.__new__(FileManagerTabEmbed)
    embed._controller = object()

    # A placeholder box wrapping the embed: box.get_first_child() -> embed.
    leaf = types.SimpleNamespace(
        get_first_child=lambda: embed,
        get_next_sibling=lambda: None,
    )
    # embed itself terminates the walk.
    embed.get_first_child = lambda: None
    embed.get_next_sibling = lambda: None

    assert win._file_manager_embed_for_child(leaf) is embed
    assert win._file_manager_embed_for_child(None) is None


def test_embeds_lookup_finds_all_in_subtree():
    """A split tab can hold several files panels; teardown must find them all."""
    wm = _window_module()
    from sshpilot.file_manager_integration import FileManagerTabEmbed

    win = wm.MainWindow.__new__(wm.MainWindow)

    def _embed():
        e = FileManagerTabEmbed.__new__(FileManagerTabEmbed)
        e._controller = object()
        e.get_first_child = lambda: None
        e.get_next_sibling = lambda: None
        return e

    e1, e2 = _embed(), _embed()
    e1.get_next_sibling = lambda: e2  # two sibling embeds under one wrapper
    root = types.SimpleNamespace(get_first_child=lambda: e1,
                                 get_next_sibling=lambda: None)

    assert win._file_manager_embeds_for_child(root) == [e1, e2]
    assert win._file_manager_embeds_for_child(None) == []


# ── on_tab_detached routing ─────────────────────────────────────────────────

def test_on_tab_detached_skips_teardown_when_moving_to_pane():
    wm = _window_module()
    win = wm.MainWindow.__new__(wm.MainWindow)
    win._moving_tab_to_pane = True
    calls = []
    win._teardown_file_manager_embed = lambda embed: calls.append(embed)
    win._update_tab_button_visibility = lambda: None
    win.show_start_tab = lambda: None
    tab_view = types.SimpleNamespace(get_n_pages=lambda: 2)

    win.on_tab_detached(tab_view, page=None, position=0)

    # The terminal is being moved into a split pane and stays live, so its
    # embed must NOT be torn down.
    assert calls == []


def test_on_tab_detached_tears_down_fm_embed():
    wm = _window_module()
    win = wm.MainWindow.__new__(wm.MainWindow)
    win._moving_tab_to_pane = False

    sentinel_embed = object()
    child = object()
    torn = []
    win._file_manager_embeds_for_child = lambda c: [sentinel_embed] if c is child else []
    win._teardown_file_manager_embed = lambda embed: torn.append(embed)
    win.terminal_to_connection = {}
    win._update_tab_button_visibility = lambda: None
    win._update_layout_toggle_state = lambda: None
    win.has_user_tabs = lambda: True

    page = types.SimpleNamespace(get_child=lambda: child)
    win.on_tab_detached(tab_view=types.SimpleNamespace(get_n_pages=lambda: 1),
                        page=page, position=0)

    assert torn == [sentinel_embed]


def test_on_tab_detached_clears_terminal_panel_before_embed_teardown():
    """An FM tab's terminal panel is disconnected before the controller dies."""
    wm = _window_module()
    win = wm.MainWindow.__new__(wm.MainWindow)
    win._moving_tab_to_pane = False

    order = []
    child = object()
    embed = types.SimpleNamespace(
        clear_terminal_panel=lambda: order.append('clear-terminal'),
    )
    win._file_manager_embeds_for_child = lambda c: [embed] if c is child else []
    win._teardown_file_manager_embed = lambda e: order.append('teardown-embed')
    win.terminal_to_connection = {}
    win._update_tab_button_visibility = lambda: None
    win._update_layout_toggle_state = lambda: None
    win.has_user_tabs = lambda: True

    page = types.SimpleNamespace(get_child=lambda: child)
    win.on_tab_detached(tab_view=types.SimpleNamespace(get_n_pages=lambda: 1),
                        page=page, position=0)

    assert order == ['clear-terminal', 'teardown-embed']


# ── FileManagerTabEmbed.clear_terminal_panel (duck-typed, no GTK build) ─────

def _fake_embed_with_panel(order):
    paned = types.SimpleNamespace(
        set_end_child=lambda c: order.append(('end_child', c)),
        set_start_child=lambda c: order.append(('start_child', c)),
    )
    fake = types.SimpleNamespace(
        _terminal_panel=object(),
        _terminal_panel_paned=paned,
        _terminal_panel_teardown=lambda: order.append('teardown'),
        _content=object(),
        remove=lambda w: order.append(('remove', w)),
        append=lambda w: order.append(('append', w)),
    )
    return fake, paned


def test_clear_terminal_panel_runs_teardown_before_detach():
    _ensure_cairo_stub()
    from sshpilot.file_manager_integration import FileManagerTabEmbed

    order = []
    fake, paned = _fake_embed_with_panel(order)
    FileManagerTabEmbed.clear_terminal_panel(fake)

    assert order[0] == 'teardown'
    assert ('remove', paned) in order
    assert order[-1] == ('append', fake._content)
    assert fake._terminal_panel is None
    assert fake._terminal_panel_paned is None

    # Idempotent: a second call is a no-op.
    order.clear()
    FileManagerTabEmbed.clear_terminal_panel(fake)
    assert order == []
