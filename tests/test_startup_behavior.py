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
    monkeypatch.setattr(window_module, 'install_sidebar_css', lambda: None, raising=False)


def _build_main_window(window_module, startup_value, calls):
    window = window_module.MainWindow.__new__(window_module.MainWindow)
    window._startup_tasks_scheduled = False
    window._pending_focus_operations = []
    window._startup_complete = False
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
    # Welcome focus is scheduled twice: early (100 ms) for immediate feedback and
    # late (700 ms) to win focus back after startup settles.
    assert calls.get('welcome_focus') == [True, True]
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
    # The terminal is created synchronously now (deferring via idle_add flashed
    # the welcome page first), so no idle work is scheduled for this path.
    assert not calls.get('idle')
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


def _storage_info(verbose=True, config=None):
    """Build only the storage section — the full gather resolves the GLib config
    dir, which is unreliable headless."""
    from sshpilot.startup_info import StartupInfo

    info = StartupInfo.__new__(StartupInfo)
    info.verbose = verbose
    info.isolated = False
    info._config = config
    return info._get_storage_info()


def test_startup_info_storage_unavailable_without_daemon():
    """Without a daemon client/controller the storage section reports unreachable —
    it never falls back to instantiating a local SecretManager."""
    storage = _storage_info()
    assert storage['metadata_status'] == 'unreachable'
    assert storage['effective_backend'] == 'none'
    assert storage['available_backends'] == []
    assert storage['selected_backend'] == 'none'
    assert storage['session_locked'] is False
    assert storage['libsecret']['accessible'] is False
    assert storage['keyring']['accessible'] is False


def test_startup_info_storage_pending_while_client_selection_in_progress():
    """While the async daemon client selection hasn't settled yet, the source
    (really the window) has no secrets_controller/client attached at all — this
    must be reported as "pending"/unknown, never fabricated as "unavailable".
    Regression: startup diagnostics used to fire on a single GLib.idle_add
    immediately after window construction, well before
    MainWindow._apply_client_selection runs, so the Secure Storage section
    always printed "libsecret: not available" even when libsecret was fine."""

    class FakeConfigSource:
        _api_client_selection_pending = True

    storage = _storage_info(config=FakeConfigSource())
    assert storage['metadata_status'] == 'pending'
    assert storage['libsecret']['available'] is None
    assert storage['keyring']['available'] is None


def test_startup_info_storage_reads_through_daemon_controller():
    """When config exposes a secrets controller, storage metadata comes from the
    daemon secrets API (registry/state/configuration)."""

    class Descriptor:
        name = "bitwarden"
        available = True

    class FakeRegistry:
        backends = (Descriptor(),)
        selected_backend = "bitwarden"

    class FakeState:
        effective_backend = "bitwarden"
        selected_backend = "bitwarden"
        needs_unlock = True

    class FakeConfiguration:
        backend = "bitwarden"
        session_timeout = 300
        remember_in_keyring = False

    class FakeController:
        def load_registry(self):
            return FakeRegistry()

        def load_state(self):
            return FakeState()

        def load_configuration(self):
            return FakeConfiguration()

    class FakeConfigSource:
        secrets_controller = FakeController()

    storage = _storage_info(config=FakeConfigSource())
    assert storage['effective_backend'] == "bitwarden"
    assert storage['available_backends'] == ["bitwarden"]
    assert storage['selected_backend'] == "bitwarden"
    assert storage['session_locked'] is True
    assert storage['backend'] == "bitwarden"
    assert storage['session_timeout'] == 300
    assert storage['remember_in_keyring'] is False


def test_startup_info_storage_reads_through_daemon_client():
    """A raw daemon client with the secrets capability is used for storage metadata."""
    class Descriptor:
        name = "keepassxc"
        available = True

    class FakeRegistry:
        backends = (Descriptor(),)
        selected_backend = "keepassxc"

    class FakeState:
        effective_backend = "keepassxc"
        selected_backend = "keepassxc"
        needs_unlock = False

    class FakeConfiguration:
        backend = "keepassxc"
        session_timeout = 600
        remember_in_keyring = True

    class FakeClient:
        def get_capabilities(self):
            from sshpilot.api.capabilities import Capabilities, Capability

            return Capabilities(
                protocol_version="1",
                api_implementation_version="test",
                client=None,
                core=None,
                supported=frozenset({Capability.SECRETS_READ}),
                compatibility=None,
            )

        def get_secret_backends(self):
            return FakeRegistry()

        def get_secret_state(self):
            return FakeState()

        def get_secret_configuration(self):
            return FakeConfiguration()

    class FakeConfigSource:
        client = FakeClient()

    storage = _storage_info(config=FakeConfigSource())
    assert storage['effective_backend'] == "keepassxc"
    assert storage['available_backends'] == ["keepassxc"]


def test_startup_info_has_no_secret_storage_import():
    """GTK diagnostics must never import secret_storage (daemon owns backends)."""
    import ast
    import inspect
    from sshpilot import startup_info

    tree = ast.parse(inspect.getsource(startup_info))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        assert not any("secret_storage" in (name or "") for name in names), (
            f"startup_info.py:{node.lineno} imports secret_storage"
        )


def test_verbose_startup_info_reports_semantic_daemon_config_without_paths(capsys):
    """The verbose path must match the semantic-only config DTO."""
    from sshpilot.startup_info import StartupInfo

    info = StartupInfo.__new__(StartupInfo)
    info.info = {
        "version": {"version": "test"},
        "platform": {"system": "Linux", "distro": "test", "architecture": "x86_64", "flatpak": False},
        "python": {"version": "3", "implementation": "CPython"},
        "libraries": {},
        "tools": {
            "ssh": {"available": False},
            "sshpass": {"available": False, "executable": False},
            "ssh_askpass": {"available": False},
        },
        "storage": {"effective_backend": "none", "libsecret": {}, "keyring": {}},
        "config": {"operation_mode": "isolated", "config_authority": "daemon"},
    }

    info.print_info()
    output = capsys.readouterr().out
    assert "Operation mode: isolated" in output
    assert "SSH configuration authority: daemon" in output
    assert "Config directory:" not in output
    assert "SSH directory:" not in output


def test_startup_diagnostics_run_off_the_main_thread():
    """print_startup_info() reads the secret backend registry through a
    blocking daemon RPC (SecretBackendsController.load_registry()); building
    that registry can shell out to e.g. `bw login --check`, observed taking
    3+ seconds. Calling it directly from the GLib timeout callback used to
    freeze the whole GTK main loop for that long — nothing in it touches
    widgets, so it must run on a background thread instead."""
    import threading

    import pytest

    pytest.importorskip("gi")
    from sshpilot import main as main_module

    call_thread = []
    ready = threading.Event()

    def fake_print_startup_info(**_kwargs):
        call_thread.append(threading.current_thread())
        ready.set()

    orig = main_module.print_startup_info
    main_module.print_startup_info = fake_print_startup_info
    try:
        app = main_module.SshPilotApplication.__new__(main_module.SshPilotApplication)
        app.isolated_mode = False
        app.verbose_override = False
        app.window = types.SimpleNamespace(
            _api_client_selection_pending=False,
            _confirmed_operation_mode=None,
        )

        calling_thread = threading.current_thread()
        app._schedule_startup_diagnostics()

        assert ready.wait(2), "print_startup_info was never called"
        assert call_thread[0] is not calling_thread
    finally:
        main_module.print_startup_info = orig

