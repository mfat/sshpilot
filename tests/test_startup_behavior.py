import sys
import types


def _ensure_cairo_stub():
    if 'cairo' not in sys.modules:
        sys.modules['cairo'] = types.SimpleNamespace()


def _setup_glib_stub(window_module, calls, monkeypatch):
    """Install GLib stubs that track idle and timeout usage."""

    def idle_add(callback, *args, **kwargs):
        calls.setdefault('idle', []).append(callback)
        if callable(callback):
            callback(*args, **kwargs)
        return 0

    def timeout_add(interval, callback, *args, **kwargs):
        calls.setdefault('timeout', []).append(interval)
        if callable(callback):
            callback(*args, **kwargs)
        return 0

    monkeypatch.setattr(window_module.GLib, 'idle_add', idle_add, raising=False)
    monkeypatch.setattr(window_module.GLib, 'timeout_add', timeout_add, raising=False)


def _build_main_window(window_module, startup_value, calls):
    window = window_module.MainWindow.__new__(window_module.MainWindow)
    window._startup_tasks_scheduled = False
    window._pending_focus_operations = []
    window._startup_complete = False
    window._install_sidebar_css = lambda: None
    window._queue_focus_operation = lambda func: calls.setdefault('queued', []).append(func)
    window._on_startup_complete = lambda: calls.setdefault('startup_complete', []).append(True) or False
    window._focus_connection_list_first_row = lambda: calls.setdefault('welcome_focus', []).append(True)

    class StubConfig:
        def get_setting(self, key, default=None):
            if key == 'app-startup-behavior':
                return startup_value if startup_value is not None else default
            return default

    window.config = StubConfig()

    window.terminal_manager = types.SimpleNamespace(
        show_local_terminal=lambda: calls.setdefault('terminal_shown', []).append(True)
    )

    terminal_widget = types.SimpleNamespace(
        vte=types.SimpleNamespace(
            grab_focus=lambda: calls.setdefault('terminal_focused', []).append(True)
        )
    )
    page = types.SimpleNamespace(get_child=lambda: terminal_widget)
    window.tab_view = types.SimpleNamespace(get_selected_page=lambda: page)

    return window


def test_startup_defaults_to_welcome(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot import window as window_module

    calls = {}
    _setup_glib_stub(window_module, calls, monkeypatch)
    main_window = _build_main_window(window_module, startup_value=None, calls=calls)

    main_window._schedule_startup_tasks()

    assert 'terminal_shown' not in calls
    assert calls.get('welcome_focus') == [True]
    assert 100 in calls.get('timeout', [])
    assert calls.get('idle', []) == []


def test_startup_honors_terminal_preference(monkeypatch):
    _ensure_cairo_stub()
    from sshpilot import window as window_module

    calls = {}
    _setup_glib_stub(window_module, calls, monkeypatch)
    main_window = _build_main_window(window_module, startup_value='terminal', calls=calls)

    main_window._schedule_startup_tasks()

    assert calls.get('terminal_shown') == [True]
    assert calls.get('idle')
    assert 100 not in calls.get('timeout', [])
    assert calls.get('queued') and callable(calls['queued'][0])


def test_preferences_toggle_persists_choice():
    from sshpilot import preferences

    class StubConfig:
        def __init__(self):
            self.saved = []

        def set_setting(self, key, value):
            self.saved.append((key, value))

    class StubRadio:
        def __init__(self, active):
            self._active = active

        def get_active(self):
            return self._active

    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs.config = StubConfig()

    prefs.terminal_startup_radio = StubRadio(active=True)
    preferences.PreferencesWindow.on_startup_behavior_changed(prefs, None)
    assert prefs.config.saved[-1] == ('app-startup-behavior', 'terminal')

    prefs.terminal_startup_radio = StubRadio(active=False)
    preferences.PreferencesWindow.on_startup_behavior_changed(prefs, None)
    assert prefs.config.saved[-1] == ('app-startup-behavior', 'welcome')


