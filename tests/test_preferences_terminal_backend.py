"""The Terminal Backend picker lists pyte, libvterm and libghostty-vt as separate entries."""

from unittest import mock

from sshpilot import preferences

CHOICES = [
    {'id': 'vte', 'available': True},
    {'id': 'pyte', 'model': 'pyte', 'available': True},
    {'id': 'pyte', 'model': 'vterm', 'available': True},
    {'id': 'pyte', 'model': 'ghostty', 'available': True},
]


def _prefs(*, backend='vte', model='pyte'):
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    settings = {'terminal.backend': backend, 'terminal.builtin_model': model}
    prefs.config = mock.Mock()
    prefs.config.get_setting.side_effect = lambda key, default=None: settings.get(key, default)
    prefs.config.set_setting.side_effect = settings.__setitem__
    prefs._settings = settings
    prefs._backend_choice_data = [dict(c) for c in CHOICES]
    prefs._update_backend_row_subtitle = lambda index: None
    prefs._update_encoding_row_visibility = lambda: None
    prefs._update_autocomplete_visibility = lambda: None
    prefs._update_wheel_scroll_visibility = lambda: None
    return prefs


def test_current_entry_matches_backend_and_model():
    assert preferences.PreferencesWindow._backend_choice_index(_prefs(), 'vte') == 0
    prefs = _prefs(backend='pyte', model='vterm')
    assert preferences.PreferencesWindow._backend_choice_index(prefs, 'pyte') == 2
    prefs = _prefs(backend='pyte', model='bogus')
    assert preferences.PreferencesWindow._backend_choice_index(prefs, 'pyte') == 1


def test_choosing_libvterm_saves_backend_and_model():
    prefs = _prefs()

    preferences.PreferencesWindow._apply_backend_change(prefs, 2, 'pyte')

    assert prefs._settings == {'terminal.backend': 'pyte', 'terminal.builtin_model': 'vterm'}


def test_choosing_libghostty_saves_backend_and_model():
    prefs = _prefs()
    preferences.PreferencesWindow._apply_backend_change(prefs, 3, 'pyte')
    assert prefs._settings == {'terminal.backend': 'pyte', 'terminal.builtin_model': 'ghostty'}
    prefs = _prefs(backend='pyte', model='ghostty')
    assert preferences.PreferencesWindow._backend_choice_index(prefs, 'pyte') == 3


def test_switching_between_models_counts_as_a_change():
    prefs = _prefs(backend='pyte', model='pyte')
    prefs._get_open_terminals = list
    row = mock.Mock()
    row.get_selected.return_value = 2

    preferences.PreferencesWindow._on_backend_row_changed(prefs, row, None)

    assert prefs._settings['terminal.builtin_model'] == 'vterm'


def test_unavailable_libvterm_reverts_the_selection():
    prefs = _prefs(backend='pyte', model='pyte')
    prefs._backend_choice_data[2]['available'] = False
    prefs._backend_last_valid_index = 1
    row = mock.Mock()
    row.get_selected.return_value = 2

    preferences.PreferencesWindow._on_backend_row_changed(prefs, row, None)

    row.set_selected.assert_called_once_with(1)
    assert prefs._settings['terminal.builtin_model'] == 'pyte'


def test_builtin_model_defaults_to_libvterm_only_on_windows(monkeypatch):
    from sshpilot.terminal_model_common import default_builtin_model

    monkeypatch.setattr("sys.platform", "win32")
    assert default_builtin_model() == "vterm"
    monkeypatch.setattr("sys.platform", "linux")
    assert default_builtin_model() == "pyte"


def test_missing_model_setting_uses_the_platform_default(monkeypatch):
    monkeypatch.setattr("sys.platform", "win32")
    prefs = _prefs(backend='pyte')
    prefs._settings.pop('terminal.builtin_model')
    assert preferences.PreferencesWindow._backend_choice_index(prefs, 'pyte') == 2


def _choices(monkeypatch, *, vte, pyte, ghostty, windows=False):
    import importlib.util

    from sshpilot import ghostty_terminal_model, terminal_backends

    monkeypatch.setattr(terminal_backends, "VTE_AVAILABLE", vte)
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        importlib.util, "find_spec",
        lambda name, *a: (object() if pyte else None) if name == "pyte" else real_find_spec(name, *a),
    )

    def load():
        if not ghostty:
            raise ghostty_terminal_model.GhosttyUnavailable("not built")

    monkeypatch.setattr(ghostty_terminal_model, "load_libghostty", load)
    monkeypatch.setattr(preferences, "is_windows", lambda: windows)
    monkeypatch.setattr(preferences, "is_macos", lambda: False)
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs._detect_pyxterm_backend = lambda: (False, "no webkit")
    return [(c["id"], c.get("model"), c["available"]) for c in prefs._build_backend_choices()]


def test_linux_lists_libghostty_once_it_loads_even_without_pyte(monkeypatch):
    builtin = [c for c in _choices(monkeypatch, vte=True, pyte=False, ghostty=True) if c[0] == "pyte"]
    assert builtin == [("pyte", "ghostty", True)]


def test_linux_without_any_builtin_engine_is_unchanged(monkeypatch):
    choices = _choices(monkeypatch, vte=True, pyte=False, ghostty=False)
    assert [c for c in choices if c[0] == "pyte"] == []


def test_windows_lists_libghostty_even_when_missing(monkeypatch):
    builtin = [c[1:] for c in _choices(monkeypatch, vte=False, pyte=True, ghostty=False, windows=True) if c[0] == "pyte"]
    assert ("ghostty", False) in builtin
    assert {model for model, _available in builtin} == {"pyte", "vterm", "ghostty"}
