"""Login profile display text belongs to the frontend catalogue."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "src" / "sshpilot"


def test_login_profile_gettext_sources_are_in_potfiles():
    potfiles = set((ROOT / "po" / "POTFILES").read_text().splitlines())
    for relative in (
        "gtk/login_profile_messages.py",
        "login_profile_dialogs.py",
        "connection_dialog_login_profile.py",
    ):
        assert f"src/sshpilot/{relative}" in potfiles


def test_login_profile_backend_sends_no_display_text():
    backend = [
        SOURCE / "daemon" / "login_profile_api.py",
        *(SOURCE / "core" / "login_profiles").glob("*.py"),
    ]
    for path in backend:
        text = path.read_text()
        assert "gettext" not in text, path
        # The preview diff once carried English labels and value wording.
        for display in ('"Key selection"', '"Specific keys only"', '"Extra directives"'):
            assert display not in text, (path, display)


def test_controller_holds_no_display_text():
    controller = (SOURCE / "gtk" / "login_profile_controller.py").read_text()
    for display in ('"password"', '"automatic keys"', "The profile was saved, but", "Don't use"):
        assert display not in controller


def test_dialogs_present_errors_and_previews_through_the_presenter():
    dialogs = (SOURCE / "login_profile_dialogs.py").read_text()
    assert "from sshpilot.gtk.login_profile_messages import" in dialogs
    assert "format_login_profile_error(error)" in dialogs
    assert ".label}" not in dialogs  # no daemon-supplied preview labels
    assert 'getattr(error, "message"' not in dialogs
