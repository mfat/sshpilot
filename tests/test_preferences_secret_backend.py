"""Preferences must not probe secret backends or prompt unlock on window open."""

from unittest import mock


from sshpilot import preferences


def test_set_secret_backend_model_suppresses_change_handler(monkeypatch):
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs._secret_backend_labels = {'bitwarden': 'Bitwarden'}
    prefs._secret_backend_ordered = ['bitwarden']
    prefs._secret_backend_selection_sync = False

    row = mock.Mock()
    row.get_selected.return_value = 1
    prefs.secret_backend_row = row

    calls = []
    prefs.on_secret_backend_changed = lambda *a, **k: calls.append(1)

    fake_model = mock.Mock()
    monkeypatch.setattr(
        preferences.Gtk, 'StringList', lambda: fake_model,
    )

    preferences.PreferencesWindow._set_secret_backend_model(
        prefs, {'bitwarden'})

    assert prefs._secret_backend_selection_sync is False
    assert calls == []
    row.set_model.assert_called_once_with(fake_model)


def test_on_secret_backend_changed_ignored_during_model_sync():
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs._secret_backend_selection_sync = True
    prefs.config = mock.Mock()

    with mock.patch('sshpilot.secret_storage.get_secret_manager') as gsm:
        preferences.PreferencesWindow.on_secret_backend_changed(prefs, mock.Mock(), None)
        gsm.assert_not_called()


def test_ensure_secrets_page_probes_runs_once(monkeypatch):
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs._secrets_page_probes_done = False
    availability = []
    visibility = []

    monkeypatch.setattr(
        prefs, '_refresh_secret_backend_availability',
        lambda: availability.append(1),
    )
    monkeypatch.setattr(
        prefs, '_current_secret_backend_name', lambda: 'auto',
    )
    monkeypatch.setattr(
        prefs, '_update_secret_rows_visibility',
        lambda name, **kw: visibility.append((name, kw)),
    )

    preferences.PreferencesWindow._ensure_secrets_page_probes(prefs)
    preferences.PreferencesWindow._ensure_secrets_page_probes(prefs)

    assert availability == [1]
    assert visibility == [('auto', {'defer_status_probe': False})]
