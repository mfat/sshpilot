def test_force_internal_toggle_hidden_on_macos(monkeypatch):
    from sshpilot import preferences

    monkeypatch.setattr(preferences, "is_macos", lambda: True)
    monkeypatch.setattr(preferences, "has_native_gvfs_support", lambda: True)

    assert preferences.should_show_force_internal_file_manager_toggle() is False


def test_force_internal_toggle_requires_gvfs_support(monkeypatch):
    from sshpilot import preferences

    monkeypatch.setattr(preferences, "is_macos", lambda: False)
    monkeypatch.setattr(preferences, "has_native_gvfs_support", lambda: True)

    assert preferences.should_show_force_internal_file_manager_toggle() is True

    monkeypatch.setattr(preferences, "has_native_gvfs_support", lambda: False)
    assert preferences.should_show_force_internal_file_manager_toggle() is False
