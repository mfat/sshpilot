# These capability helpers live in file_manager_integration (re-exported from
# preferences), so patch their dependencies where the functions resolve them.
def test_force_internal_toggle_hidden_on_macos(monkeypatch):
    from sshpilot import file_manager_integration as fmi

    monkeypatch.setattr(fmi, "is_macos", lambda: True)
    monkeypatch.setattr(fmi, "has_native_gvfs_support", lambda: True)

    assert fmi.should_show_force_internal_file_manager_toggle() is False


def test_force_internal_toggle_requires_gvfs_support(monkeypatch):
    from sshpilot import file_manager_integration as fmi

    monkeypatch.setattr(fmi, "is_macos", lambda: False)
    monkeypatch.setattr(fmi, "has_native_gvfs_support", lambda: True)

    assert fmi.should_show_force_internal_file_manager_toggle() is True

    monkeypatch.setattr(fmi, "has_native_gvfs_support", lambda: False)
    assert fmi.should_show_force_internal_file_manager_toggle() is False
