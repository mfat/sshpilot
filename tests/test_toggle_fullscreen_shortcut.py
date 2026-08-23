"""Toggle Fullscreen is a first-class, customizable shortcut on every platform.

Fullscreen used to be reachable only through a hard-coded F11 handler inside
``WindowFullscreenController``: invisible to the shortcut overview, impossible
to rebind, and wrong on macOS, where F11 belongs to the OS.

It goes through the ordinary registry now — ``register_window_shortcut`` for
defaults/overrides/persistence, ``ACTION_LABELS`` for the customizer, the
``win.toggle-fullscreen`` action for dispatch — so exactly one path can fire
for a given key event.
"""

import pytest

# Import manually instead of importorskip: when sibling tests have replaced the
# Gtk stub in sys.modules these imports raise AttributeError (not ImportError),
# which importorskip would report as a collection error.
try:
    import sshpilot.main as main_mod
    import sshpilot.actions as actions_mod
    import sshpilot.shortcut_editor as editor_mod
    import sshpilot.shortcut_utils as utils_mod
    import sshpilot.window_help as help_mod
except Exception:  # pragma: no cover - depends on test execution order
    main_mod = actions_mod = editor_mod = utils_mod = help_mod = None

pytestmark = pytest.mark.skipif(
    main_mod is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)

ACTION = 'toggle-fullscreen'
DETAILED = f'win.{ACTION}'


class _FakeConfig:
    def __init__(self, overrides=None):
        self._overrides = dict(overrides or {})

    def get_shortcut_override(self, name):
        return self._overrides.get(name)

    # What the customizer does on assign / reset.
    def set_shortcut_override(self, name, accelerators):
        if accelerators is None:
            self._overrides.pop(name, None)
        else:
            self._overrides[name] = accelerators


class _FakeApp:
    if main_mod is not None:
        register_window_shortcut = main_mod.SshPilotApplication.register_window_shortcut
        _apply_shortcut_for_action = main_mod.SshPilotApplication._apply_shortcut_for_action
        apply_shortcut_overrides = main_mod.SshPilotApplication.apply_shortcut_overrides
        get_effective_shortcuts = main_mod.SshPilotApplication.get_effective_shortcuts
        get_registered_shortcut_defaults = (
            main_mod.SshPilotApplication.get_registered_shortcut_defaults
        )

    def __init__(self, overrides=None):
        self._action_order = []
        self._default_shortcuts = {}
        self._custom_shortcut_names = set()
        self._accelerators_enabled = True
        self.config = _FakeConfig(overrides)
        self.accel_calls = {}

    def lookup_action(self, name):
        return None  # window action: not on the app

    def set_accels_for_action(self, detailed_name, accels):
        self.accel_calls[detailed_name] = list(accels)


class _FakeAction:
    """Gio.SimpleAction stand-in (the suite's stubbed gi has no real one)."""

    def __init__(self, name):
        self._name = name
        self._handlers = []

    def get_name(self):
        return self._name

    def connect(self, signal, handler):
        assert signal == 'activate'
        self._handlers.append(handler)

    def activate(self, parameter=None):
        for handler in self._handlers:
            handler(self, parameter)


class _FakeWindow:
    """Window exposing just the fullscreen action surface under test."""

    def __init__(self, app):
        self._app = app
        self.toggles = 0
        self.actions = {}

    def get_application(self):
        return self._app

    def add_action(self, action):
        self.actions[action.get_name()] = action

    def toggle_fullscreen(self):
        self.toggles += 1

    # -- accelerator dispatch, the way GTK routes a key to its action ------
    def press(self, accelerator):
        """Activate every action registered for *accelerator*.

        Returns how many fired, so a stale binding (0) and a double-dispatch
        (2) are both visible.
        """
        fired = 0
        for detailed, accels in self._app.accel_calls.items():
            if accelerator in accels:
                name = detailed.split('.', 1)[1]
                action = self.actions.get(name)
                if action is not None:
                    action.activate(None)
                    fired += 1
        return fired


def _register(app, *, mac):
    app.register_window_shortcut(
        ACTION, utils_mod.default_fullscreen_shortcuts(mac)
    )


@pytest.fixture
def simple_action(monkeypatch):
    """Give register_fullscreen_action a real-enough Gio.SimpleAction."""
    monkeypatch.setattr(
        actions_mod.Gio.SimpleAction,
        'new',
        staticmethod(lambda name, _param_type: _FakeAction(name)),
        raising=False,
    )


def _window_with_action(app):
    window = _FakeWindow(app)
    actions_mod.register_fullscreen_action(window)
    return window


# -- catalog ---------------------------------------------------------------


def test_shortcut_catalog_contains_toggle_fullscreen():
    assert editor_mod.ACTION_LABELS.get(ACTION) == 'Toggle Fullscreen'


def test_shortcut_overview_lists_toggle_fullscreen():
    assert any(
        name == ACTION for name, _title in help_mod.GENERAL_SHORTCUT_ACTIONS
    )


# -- platform defaults -----------------------------------------------------


def test_linux_and_windows_default_is_f11():
    assert utils_mod.default_fullscreen_shortcuts(False) == ['F11']


def test_macos_default_is_control_command_f():
    assert utils_mod.default_fullscreen_shortcuts(True) == ['<Control><Meta>f']


def test_macos_default_never_claims_f11():
    """F11 must stay with macOS for its native behaviour."""
    assert 'F11' not in utils_mod.default_fullscreen_shortcuts(True)


@pytest.mark.parametrize(
    'mac,expected', [(False, ['F11']), (True, ['<Control><Meta>f'])]
)
def test_platform_default_reaches_the_accelerator_table(mac, expected):
    app = _FakeApp()
    _register(app, mac=mac)
    assert app.accel_calls.get(DETAILED) == expected
    assert ACTION in app._action_order


def test_macos_registration_does_not_bind_f11(simple_action):
    app = _FakeApp()
    _register(app, mac=True)
    window = _window_with_action(app)

    assert window.press('F11') == 0
    assert window.toggles == 0


# -- dispatch --------------------------------------------------------------


def test_action_routes_to_the_window_fullscreen_toggle(simple_action):
    app = _FakeApp()
    _register(app, mac=False)
    window = _window_with_action(app)

    assert window.press('F11') == 1
    assert window.toggles == 1


# -- customization ---------------------------------------------------------


def test_customized_accelerator_replaces_the_default(simple_action):
    app = _FakeApp()
    _register(app, mac=False)
    window = _window_with_action(app)

    app.config.set_shortcut_override(ACTION, ['<Primary><Shift>f'])
    app.apply_shortcut_overrides()

    assert window.press('F11') == 0            # old binding is inactive
    assert window.toggles == 0
    assert window.press('<Primary><Shift>f') == 1   # exactly once
    assert window.toggles == 1


def test_disabled_override_unbinds_fullscreen_entirely(simple_action):
    app = _FakeApp(overrides={ACTION: []})
    _register(app, mac=False)
    window = _window_with_action(app)

    assert app.accel_calls.get(DETAILED) == []
    assert window.press('F11') == 0


@pytest.mark.parametrize(
    'mac,expected', [(False, ['F11']), (True, ['<Control><Meta>f'])]
)
def test_reset_restores_the_platform_default(mac, expected):
    app = _FakeApp()
    _register(app, mac=mac)
    app.config.set_shortcut_override(ACTION, ['<Primary><Shift>f'])
    app.apply_shortcut_overrides()
    assert app.accel_calls.get(DETAILED) == ['<Primary><Shift>f']

    app.config.set_shortcut_override(ACTION, None)   # reset
    app.apply_shortcut_overrides()

    assert app.accel_calls.get(DETAILED) == expected
    assert app.get_effective_shortcuts(ACTION) == expected


def test_suspended_accelerators_clear_fullscreen():
    """Terminal pass-through mode must release the key like any other."""
    app = _FakeApp()
    app._accelerators_enabled = False
    _register(app, mac=False)
    assert app.accel_calls.get(DETAILED) == []


# -- the overview shows the configured binding, not a literal --------------


def test_overview_renders_the_configured_binding_not_hardcoded_f11(monkeypatch):
    recorded = []

    class _Shortcut:
        # Tolerate keywords this test does not assert on (action_name, ...) so
        # the double does not have to track every Gtk.ShortcutsShortcut
        # argument the help overview grows.
        def __init__(self, title=None, accelerator=None, **_kwargs):
            recorded.append((title, accelerator))

        def set_subtitle(self, _subtitle):
            pass

    class _Group:
        def __init__(self, **_kwargs):
            pass

        def add_shortcut(self, _shortcut):
            return None

    class _Section:
        def add_group(self, _group):
            return None

    monkeypatch.setattr(help_mod.Gtk, 'ShortcutsShortcut', _Shortcut, raising=False)
    monkeypatch.setattr(help_mod.Gtk, 'ShortcutsGroup', _Group, raising=False)

    class _Win:
        _add_safe_current_shortcuts = help_mod.WindowHelpMixin._add_safe_current_shortcuts

        def _get_safe_current_shortcuts(self):
            return {ACTION: ['<Primary><Shift>f']}

    _Win()._add_safe_current_shortcuts(_Section(), '<primary>')

    titles = {title: accel for title, accel in recorded}
    assert titles.get('Toggle Fullscreen') == '<Primary><Shift>f'
    assert 'F11' not in str(titles.get('Toggle Fullscreen'))


def test_button_tooltip_follows_the_configured_binding():
    """A rebind must not leave "F11" stale in the header-bar tooltip."""
    import sshpilot.window as window_mod

    class _Win:
        _update_fullscreen_button_tooltip = (
            window_mod.MainWindow._update_fullscreen_button_tooltip
        )

        def __init__(self, accels):
            self._accels = accels
            self.tooltip = None
            self.fullscreen_button = self

        def set_tooltip_text(self, text):
            self.tooltip = text

        def _get_safe_current_shortcuts(self):
            return {ACTION: self._accels}

    win = _Win(['<Primary><Shift>f'])
    win._update_fullscreen_button_tooltip()
    assert 'F11' not in win.tooltip
    assert 'Exit Fullscreen' in win.tooltip

    # No binding at all: still a usable label, no empty parentheses.
    bare = _Win([])
    bare._update_fullscreen_button_tooltip()
    assert bare.tooltip == 'Exit Fullscreen'


def test_refreshing_accelerators_refreshes_the_tooltip():
    calls = []

    class _Win:
        def _update_fullscreen_button_tooltip(self):
            calls.append(True)

    class _App(_FakeApp):
        _refresh_window_accelerators = (
            main_mod.SshPilotApplication._refresh_window_accelerators
        )

        def get_windows(self):
            return [_Win()]

    _App()._refresh_window_accelerators()
    assert calls == [True]


# -- exactly one path can fire ---------------------------------------------


def test_controller_no_longer_hardcodes_a_key_handler():
    """The registry owns the accelerator; no capture-phase F11 may remain."""
    from sshpilot.window_fullscreen import WindowFullscreenController

    assert not hasattr(WindowFullscreenController, 'on_capture_key_pressed')
