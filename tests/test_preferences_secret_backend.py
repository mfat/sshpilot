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


def test_backend_change_checks_unlock_need_before_refreshing_rows(monkeypatch):
    """Regression: _update_secret_rows_visibility() refreshes the "Forget"
    row on a background thread that also calls controller.load_state() —
    when the unlock-need check ran after that call, it raced the same
    guarded SecretBackendsController and its RuntimeError("a secret backend
    operation is already in progress") was silently swallowed as "no unlock
    needed", skipping the unlock prompt on nearly every backend switch. The
    unlock check must run before rows are refreshed."""
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs._secret_backend_selection_sync = False
    prefs._secret_backend_ids = ['auto', 'keepassxc']

    controller = mock.Mock()
    prefs._resolve_secrets_controller = lambda: controller

    combo = mock.Mock()
    combo.get_selected.return_value = 1  # 'keepassxc'

    order = []
    monkeypatch.setattr(
        preferences.PreferencesWindow, '_selected_needs_unlock_from_state',
        lambda self: (order.append('needs_unlock'), False)[1],
    )
    monkeypatch.setattr(
        preferences.PreferencesWindow, '_update_secret_rows_visibility',
        lambda self, name: order.append('update_rows'),
    )

    preferences.PreferencesWindow.on_secret_backend_changed(prefs, combo, None)

    assert order == ['needs_unlock', 'update_rows']


def test_selected_needs_unlock_retries_past_a_busy_controller(monkeypatch):
    """The controller rejects overlapping guarded operations with a plain
    RuntimeError — the old code treated that identically to "genuinely does
    not need unlock" and silently skipped the unlock prompt. A transient
    RuntimeError (an unrelated concurrent probe still holding the
    controller) must be retried, not swallowed."""
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    monkeypatch.setattr(preferences.time, 'sleep', lambda _seconds: None)

    controller = mock.Mock()
    calls = []

    def load_state():
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("a secret backend operation is already in progress")
        return mock.Mock(needs_unlock=True)

    controller.load_state.side_effect = load_state
    prefs._resolve_secrets_controller = lambda: controller

    result = preferences.PreferencesWindow._selected_needs_unlock_from_state(prefs)

    assert result is True
    assert len(calls) == 3


def test_selected_needs_unlock_gives_up_after_bounded_retries(monkeypatch):
    """A controller that never frees up must not hang the main thread
    forever — bounded retries, then a plain False (matches the old
    fail-safe default when the state genuinely can't be read)."""
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    monkeypatch.setattr(preferences.time, 'sleep', lambda _seconds: None)

    controller = mock.Mock()
    controller.load_state.side_effect = RuntimeError(
        "a secret backend operation is already in progress"
    )
    prefs._resolve_secrets_controller = lambda: controller

    result = preferences.PreferencesWindow._selected_needs_unlock_from_state(prefs)

    assert result is False
    assert controller.load_state.call_count == 8


def test_build_security_page_sets_secrets_page_id(monkeypatch):
    """Security page build must register _secrets_page_id for deferred probes."""
    prefs = preferences.PreferencesWindow.__new__(preferences.PreferencesWindow)
    prefs._secrets_page_id = None

    page = mock.Mock()
    monkeypatch.setattr(preferences.Adw, 'PreferencesPage', lambda: page)
    monkeypatch.setattr(
        preferences.PreferencesWindow,
        '_add_security_secrets_group',
        lambda self, p: None,
    )
    monkeypatch.setattr(
        preferences.PreferencesWindow,
        '_add_security_identity_group',
        lambda self, p: None,
    )

    result = preferences.PreferencesWindow._build_security_preferences_page(prefs)

    assert result is page
    assert prefs._secrets_page_id == preferences.PreferencesWindow._page_id(
        "Security & Credentials"
    )
    assert prefs._secrets_page_id == "security-&-credentials"
