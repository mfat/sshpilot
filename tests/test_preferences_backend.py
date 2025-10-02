import importlib
import types


def test_backend_choices_missing_dependency(monkeypatch):
    from sshpilot.preferences import PreferencesWindow

    window = PreferencesWindow.__new__(PreferencesWindow)
    monkeypatch.setattr(window, "_detect_pyxterm_backend", lambda: (False, "missing"))

    choices = PreferencesWindow._build_backend_choices(window)

    pyxterm_choice = next(choice for choice in choices if choice['id'] == 'pyxterm')
    assert not pyxterm_choice['available']
    assert 'requires' in pyxterm_choice['label']


def test_detect_pyxterm_backend_uses_vendored(monkeypatch):
    from sshpilot.preferences import PreferencesWindow

    window = PreferencesWindow.__new__(PreferencesWindow)
    original_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *args, **kwargs):
        if name == 'pyxtermjs':
            return None
        return original_find_spec(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", fake_find_spec)

    available, error = PreferencesWindow._detect_pyxterm_backend(window)

    assert available
    assert error is None


def test_backend_row_updates_config(monkeypatch):
    from sshpilot.preferences import PreferencesWindow

    window = PreferencesWindow.__new__(PreferencesWindow)
    record: dict[str, object] = {}

    class ConfigStub:
        def set_setting(self, key, value):
            record['key'] = key
            record['value'] = value

        def get_setting(self, key, default=None):
            return default

    refreshed = {'count': 0}

    class ManagerStub:
        def refresh_backends(self):
            refreshed['count'] += 1

    window.config = ConfigStub()
    window.parent_window = types.SimpleNamespace(terminal_manager=ManagerStub())
    window.backend_row = types.SimpleNamespace(set_subtitle=lambda value: record.setdefault('subtitle', value))
    window._backend_choice_data = [
        {'id': 'vte', 'available': True, 'description': 'VTE'},
        {'id': 'pyxterm', 'available': True, 'description': 'PyXterm'},
    ]
    window._backend_last_valid_index = 0

    class ComboStub:
        def __init__(self, selected):
            self._selected = selected
            self.reset_to = None

        def get_selected(self):
            return self._selected

        def set_selected(self, value):
            self.reset_to = value

    combo = ComboStub(1)
    window._on_backend_row_changed(combo, None)

    assert record['key'] == 'terminal.backend'
    assert record['value'] == 'pyxterm'
    assert record['subtitle'] == 'PyXterm'
    assert refreshed['count'] == 1
    assert combo.reset_to is None


def test_backend_row_rejects_unavailable(monkeypatch):
    from sshpilot.preferences import PreferencesWindow

    window = PreferencesWindow.__new__(PreferencesWindow)
    window.config = types.SimpleNamespace(set_setting=lambda *a, **k: None, get_setting=lambda *a, **k: None)
    window.parent_window = types.SimpleNamespace(terminal_manager=types.SimpleNamespace(refresh_backends=lambda: None))
    window.backend_row = types.SimpleNamespace(set_subtitle=lambda value: None)
    window._backend_choice_data = [
        {'id': 'vte', 'available': True, 'description': 'VTE'},
        {'id': 'pyxterm', 'available': False, 'description': 'missing'},
    ]
    window._backend_last_valid_index = 0

    class ComboStub:
        def __init__(self):
            self.reset_to = None

        def get_selected(self):
            return 1

        def set_selected(self, value):
            self.reset_to = value

    combo = ComboStub()
    window._on_backend_row_changed(combo, None)

    assert combo.reset_to == 0
