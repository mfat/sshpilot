"""The login profile editor shows authentication like the connection dialog.

Order and per-method switches are lines of the profile's options: the
section edits them as one order line, and the extra-options text keeps
the rest.
"""

import pytest

from sshpilot.api.models.login_profiles import LoginProfileSettings

PASSWORD_ORDER = "keyboard-interactive,password,gssapi-with-mic,hostbased,publickey"


pytestmark = pytest.mark.gui


@pytest.fixture
def gtk():
    try:
        import gi

        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Adw, Gtk
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"GTK unavailable: {exc}")
    if not isinstance(getattr(Gtk, "MAJOR_VERSION", None), int):
        pytest.skip("gi is stubbed in this environment (no real GTK)")
    if not Gtk.init_check():
        pytest.skip("GTK cannot initialise (no display)")
    Adw.init()
    return Gtk


def _editor(initial):
    from sshpilot.gtk.login_profile_controller import settings_from_values
    from sshpilot.login_profile_dialogs import LoginProfileEditor

    editor = LoginProfileEditor(initial=initial, on_submit=lambda *_args: None)
    return editor, settings_from_values


def test_the_section_edits_the_profiles_lines_and_keeps_the_rest(gtk):
    editor, settings_from_values = _editor(LoginProfileSettings(
        name="Ops",
        extra_ssh_config=f"Compression yes\nPreferredAuthentications {PASSWORD_ORDER}",
    ))
    section = editor.auth_policy
    assert section.policy.order == PASSWORD_ORDER.split(",")
    # The profile's order lists keys, so they are checked.
    check = section.rows["publickey"]._check
    assert check.get_active() and not check.get_inconsistent()
    assert check.get_sensitive()

    # The user turns keys off for linked connections: the order line loses
    # them; no per-method line is written.
    check.activate()
    assert not check.get_active()
    settings = settings_from_values(editor.values())
    assert settings.extra_ssh_config.splitlines() == [
        "Compression yes",
        "PreferredAuthentications "
        "keyboard-interactive,password,gssapi-with-mic,hostbased",
    ]
    section.reset_button.emit("clicked")  # leave the order to each connection
    assert "PreferredAuthentications" not in settings_from_values(
        editor.values()
    ).extra_ssh_config
