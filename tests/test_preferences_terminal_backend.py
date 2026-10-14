"""The Terminal Backend picker lists pyte and libvterm as separate entries."""

from unittest import mock

from sshpilot import preferences

CHOICES = [
    {'id': 'vte', 'available': True},
    {'id': 'pyte', 'model': 'pyte', 'available': True},
    {'id': 'pyte', 'model': 'vterm', 'available': True},
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


def test_switching_between_models_counts_as_a_change():
    prefs = _prefs(backend='pyte', model='pyte')
    prefs._get_open_terminals = lambda: []
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
