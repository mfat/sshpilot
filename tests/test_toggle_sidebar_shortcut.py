"""Toggle Sidebar (F9) shortcut goes through the customizable registry."""

import pytest

# Import manually instead of importorskip: when sibling tests have replaced
# the Gtk stub in sys.modules, these imports raise AttributeError (not
# ImportError), which importorskip would report as a collection error.
try:
    import sshpilot.main as main_mod
    import sshpilot.actions as actions_mod
except Exception:  # pragma: no cover - depends on test execution order
    main_mod = actions_mod = None

pytestmark = pytest.mark.skipif(
    main_mod is None or actions_mod is None,
    reason="GTK stubs unavailable or polluted by sibling tests",
)


class _FakeConfig:
    def __init__(self, overrides=None):
        self._overrides = overrides or {}

    def get_shortcut_override(self, name):
        return self._overrides.get(name)


class _FakeApp:
    if main_mod is not None:
        register_window_shortcut = main_mod.SshPilotApplication.register_window_shortcut
        _apply_shortcut_for_action = main_mod.SshPilotApplication._apply_shortcut_for_action

    def __init__(self, overrides=None):
        self._action_order = []
        self._default_shortcuts = {}
        self._accelerators_enabled = True
        self.config = _FakeConfig(overrides)
        self.accel_calls = {}

    def lookup_action(self, name):
        return None  # window action: not on the app

    def set_accels_for_action(self, detailed_name, accels):
        self.accel_calls[detailed_name] = list(accels)


def test_register_window_shortcut_applies_default():
    app = _FakeApp()
    app.register_window_shortcut('toggle_sidebar', ['F9'])
    assert app.accel_calls.get('win.toggle_sidebar') == ['F9']
    assert 'toggle_sidebar' in app._action_order
    assert app._default_shortcuts['toggle_sidebar'] == ['F9']


def test_config_override_wins_over_default():
    app = _FakeApp(overrides={'toggle_sidebar': ['<Primary>F9']})
    app.register_window_shortcut('toggle_sidebar', ['F9'])
    assert app.accel_calls.get('win.toggle_sidebar') == ['<Primary>F9']


def test_disabled_override_clears_accels():
    app = _FakeApp(overrides={'toggle_sidebar': []})
    app.register_window_shortcut('toggle_sidebar', ['F9'])
    assert app.accel_calls.get('win.toggle_sidebar') == []


def test_suspended_accelerators_clear_accels():
    app = _FakeApp()
    app._accelerators_enabled = False
    app.register_window_shortcut('toggle_sidebar', ['F9'])
    assert app.accel_calls.get('win.toggle_sidebar') == []


def test_update_sidebar_accelerators_delegates_to_registry():
    app = _FakeApp(overrides={'toggle_sidebar': ['<Alt>s']})
    app.register_window_shortcut('toggle_sidebar', ['F9'])
    app.accel_calls.clear()

    class _FakeWindow:
        _update_sidebar_accelerators = (
            actions_mod.WindowActions._update_sidebar_accelerators
        )

        def get_application(self):
            return app

    _FakeWindow()._update_sidebar_accelerators()
    assert app.accel_calls.get('win.toggle_sidebar') == ['<Alt>s']
