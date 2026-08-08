"""Owner/group display for the SFTP properties dialog."""

from sshpilot.file_manager.properties_dialog import PropertiesDialog


def test_format_owner_resolves_local_names():
    text = PropertiesDialog._format_owner(0, 0)
    assert " : " in text and text != "—"


def test_format_remote_owner_shows_numeric_ids():
    assert PropertiesDialog._format_remote_owner(1000, 1000) == "1000 : 1000"
    assert PropertiesDialog._format_remote_owner(0, 0) == "0 : 0"


def test_format_remote_owner_missing_ids_is_placeholder():
    assert PropertiesDialog._format_remote_owner(None, None) == "—"
    assert PropertiesDialog._format_remote_owner(1000, None) == "—"
