import sys
import types


def _ensure_cairo_stub():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()


# ── SessionManager persistence ─────────────────────────────────────────────────

def test_session_manager_round_trip(tmp_path, monkeypatch):
    from sshpilot import session_manager as sm

    monkeypatch.setattr(sm, 'get_config_dir', lambda: str(tmp_path))

    manager = sm.SessionManager()
    assert manager.list_session_names() == []

    data = {'tabs': [{'type': 'ssh', 'nickname': 'srv', 'custom_title': None}]}
    manager.save_session('Work', data)
    manager.save_session('Admin', {'tabs': []})

    # Case-insensitive sort
    assert manager.list_session_names() == ['Admin', 'Work']
    assert manager.has_session('Work')
    assert manager.get_session('Work') == data

    # Reload from disk via a fresh instance
    reloaded = sm.SessionManager()
    assert reloaded.get_session('Work') == data
    assert reloaded.list_session_names() == ['Admin', 'Work']

    assert reloaded.delete_session('Admin') is True
    assert reloaded.delete_session('Missing') is False
    assert reloaded.list_session_names() == ['Work']


def test_session_manager_previous_round_trip(tmp_path, monkeypatch):
    from sshpilot import session_manager as sm

    monkeypatch.setattr(sm, 'get_config_dir', lambda: str(tmp_path))

    manager = sm.SessionManager()
    assert manager.get_previous() is None

    prev = {'tabs': [{'type': 'local'}]}
    manager.save_previous(prev)

    reloaded = sm.SessionManager()
    assert reloaded.get_previous() == prev


def test_session_manager_empty_name_rejected(tmp_path, monkeypatch):
    from sshpilot import session_manager as sm

    monkeypatch.setattr(sm, 'get_config_dir', lambda: str(tmp_path))
    manager = sm.SessionManager()

    try:
        manager.save_session('   ', {'tabs': []})
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for empty session name")


def test_session_manager_rename(tmp_path, monkeypatch):
    from sshpilot import session_manager as sm

    monkeypatch.setattr(sm, 'get_config_dir', lambda: str(tmp_path))
    manager = sm.SessionManager()

    data = {'tabs': [{'type': 'local'}]}
    manager.save_session('Old', data)
    manager.set_pinned('Old', True)

    manager.rename_session('Old', 'New')
    assert not manager.has_session('Old')
    assert manager.has_session('New')
    # Payload and pinned state preserved
    assert manager.get_session('New').get('tabs') == data['tabs']
    assert manager.is_pinned('New')

    # Renaming to an existing name is rejected
    manager.save_session('Other', {'tabs': []})
    try:
        manager.rename_session('New', 'Other')
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError when renaming to existing name")


def test_session_manager_pin_round_trip(tmp_path, monkeypatch):
    from sshpilot import session_manager as sm

    monkeypatch.setattr(sm, 'get_config_dir', lambda: str(tmp_path))
    manager = sm.SessionManager()

    manager.save_session('A', {'tabs': []})
    manager.save_session('B', {'tabs': []})
    assert manager.get_pinned_session_names() == []

    manager.set_pinned('A', True)
    assert manager.is_pinned('A')
    assert manager.get_pinned_session_names() == ['A']

    # Re-saving over a pinned session preserves the pin
    manager.save_session('A', {'tabs': [{'type': 'local'}]})
    assert manager.is_pinned('A')

    # Reload from disk keeps pinned state
    reloaded = sm.SessionManager()
    assert reloaded.get_pinned_session_names() == ['A']

    reloaded.set_pinned('A', False)
    assert reloaded.get_pinned_session_names() == []


# ── capture_session ─────────────────────────────────────────────────────────────

def _make_page(child, custom_title=None):
    page = types.SimpleNamespace(get_child=lambda: child)
    page.custom_tab_title = custom_title
    return page


def test_capture_session_schema(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot import window as window_module
    # capture_session uses window's *module-level* TerminalWidget in an
    # isinstance() check. In full-suite order a sibling may have imported window
    # (binding TerminalWidget) before sshpilot.terminal was re-imported as a fresh
    # module, so a separate ``from sshpilot.terminal import TerminalWidget`` would
    # yield a different class and the regular/local tabs would be silently
    # dropped. Use the exact class the app checks against.
    TerminalWidget = window_module.TerminalWidget
    from sshpilot.split_view import SplitViewTab

    win = window_module.MainWindow.__new__(window_module.MainWindow)

    # SSH terminal
    ssh_term = TerminalWidget.__new__(TerminalWidget)
    ssh_term._is_local_terminal = lambda: False
    ssh_conn = types.SimpleNamespace(nickname='server1')

    # Local terminal
    local_term = TerminalWidget.__new__(TerminalWidget)
    local_term._is_local_terminal = lambda: True
    local_conn = types.SimpleNamespace(nickname='Local Terminal')

    # Split view with one connection per pane
    split = SplitViewTab.__new__(SplitViewTab)
    split.get_layout_mode = lambda: SplitViewTab.HORIZONTAL
    pane_term = TerminalWidget.__new__(TerminalWidget)
    pane_conn = types.SimpleNamespace(nickname='server2')
    pane = types.SimpleNamespace(get_terminals=lambda: [pane_term])
    empty_pane = types.SimpleNamespace(get_terminals=lambda: [])
    split._panes = [pane, empty_pane]

    win.terminal_to_connection = {
        ssh_term: ssh_conn,
        local_term: local_conn,
        pane_term: pane_conn,
    }

    pages = [
        _make_page(ssh_term, custom_title='prod'),
        _make_page(local_term),
        _make_page(split),
    ]
    win.tab_view = types.SimpleNamespace(
        get_n_pages=lambda: len(pages),
        get_nth_page=lambda i: pages[i],
    )

    session = win.capture_session()

    assert session == {
        'tabs': [
            {'type': 'ssh', 'nickname': 'server1', 'custom_title': 'prod'},
            {'type': 'local'},
            {
                'type': 'split',
                'layout': 'horizontal',
                'custom_title': None,
                'panes': [[{'nickname': 'server2'}]],
            },
        ]
    }


# ── restore_session dispatch ─────────────────────────────────────────────────────

def test_restore_session_dispatch_and_replace(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot import window as window_module

    win = window_module.MainWindow.__new__(window_module.MainWindow)

    calls = {'closed': 0, 'ssh': [], 'local': 0, 'split': []}

    win._close_all_tabs = lambda: calls.__setitem__('closed', calls['closed'] + 1)
    win._restore_split_tab = lambda entry: calls['split'].append(entry)

    connections = {
        'server1': types.SimpleNamespace(nickname='server1'),
    }
    win.connection_manager = types.SimpleNamespace(
        find_connection_by_nickname=lambda nick: connections.get(nick)
    )
    win.terminal_manager = types.SimpleNamespace(
        connect_to_host=lambda conn, force_new=False: calls['ssh'].append((conn.nickname, force_new)),
        show_local_terminal=lambda: calls.__setitem__('local', calls['local'] + 1),
    )
    # No selected page so the custom-title branch is skipped safely
    win.tab_view = types.SimpleNamespace(get_selected_page=lambda: None)

    data = {
        'tabs': [
            {'type': 'ssh', 'nickname': 'server1', 'custom_title': 'x'},
            {'type': 'ssh', 'nickname': 'missing'},
            {'type': 'local'},
            {'type': 'split', 'layout': 'vertical', 'panes': [[{'nickname': 'server1'}]]},
        ]
    }

    win.restore_session(data, replace=True)

    assert calls['closed'] == 1
    assert calls['ssh'] == [('server1', True)]  # missing connection skipped
    assert calls['local'] == 1
    assert calls['split'] == [data['tabs'][3]]


def test_restore_session_no_replace_skips_close():
    _ensure_cairo_stub()
    from sshpilot import window as window_module

    win = window_module.MainWindow.__new__(window_module.MainWindow)
    calls = {'closed': 0}
    win._close_all_tabs = lambda: calls.__setitem__('closed', calls['closed'] + 1)
    win.connection_manager = types.SimpleNamespace(find_connection_by_nickname=lambda nick: None)
    win.terminal_manager = types.SimpleNamespace()

    win.restore_session({'tabs': []}, replace=False)
    assert calls['closed'] == 0


# ── preferences persistence ──────────────────────────────────────────────────────

class _StubConfig:
    def __init__(self):
        self.saved = []

    def set_setting(self, key, value):
        self.saved.append((key, value))


class _StubRadio:
    def __init__(self, active):
        self._active = active

    def get_active(self):
        return self._active


def test_preferences_persists_previous_session():
    from sshpilot import preferences

    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs.config = _StubConfig()
    prefs.terminal_startup_radio = _StubRadio(active=False)
    prefs.previous_session_startup_radio = _StubRadio(active=True)
    prefs.saved_session_startup_radio = _StubRadio(active=False)

    preferences.PreferencesWindow.on_startup_behavior_changed(prefs, None)
    assert prefs.config.saved[-1] == ('app-startup-behavior', 'previous-session')


def test_preferences_persists_saved_session():
    from sshpilot import preferences

    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs.config = _StubConfig()
    prefs.terminal_startup_radio = _StubRadio(active=False)
    prefs.previous_session_startup_radio = _StubRadio(active=False)
    prefs.saved_session_startup_radio = _StubRadio(active=True)

    preferences.PreferencesWindow.on_startup_behavior_changed(prefs, None)
    assert prefs.config.saved[-1] == ('app-startup-behavior', 'saved-session')


def test_preferences_persists_session_name():
    from sshpilot import preferences

    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs.config = _StubConfig()
    prefs._startup_session_names = ['Work', 'Admin']

    combo = types.SimpleNamespace(get_selected=lambda: 1)
    preferences.PreferencesWindow.on_startup_session_changed(prefs, combo)
    assert prefs.config.saved[-1] == ('app-startup-session-name', 'Admin')
