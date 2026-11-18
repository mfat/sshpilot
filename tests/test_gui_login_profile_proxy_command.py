"""The login profile editor's Proxy command row, against real GTK widgets."""

import pytest

from sshpilot.api.models.login_profiles import LoginProfileSettings

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
    return Adw, Gtk


def _editor(initial: LoginProfileSettings):
    from sshpilot.gtk.login_profile_controller import settings_from_values
    from sshpilot.login_profile_dialogs import LoginProfileEditor

    editor = LoginProfileEditor(initial=initial, on_submit=lambda *_args: None)
    return editor, settings_from_values


def test_proxy_command_row_loads_and_saves_on_its_own(gtk):
    editor, settings_from_values = _editor(
        LoginProfileSettings(
            name="Proxy",
            proxy_command="ssh -W %h:%p bastion",
            extra_ssh_config="Compression yes",
        )
    )
    assert editor.proxy_command_row.get_title() == "Proxy command"
    assert editor.proxy_command_row.get_text() == "ssh -W %h:%p bastion"
    buffer = editor.extra_view.get_buffer()
    extra = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
    assert extra == "Compression yes"
    assert "ProxyCommand" not in extra

    editor.proxy_command_row.set_text("ssh -W %h:%p other")
    settings = settings_from_values(editor.values())
    assert settings.proxy_command == "ssh -W %h:%p other"
    assert settings.extra_ssh_config == "Compression yes"

    editor.proxy_command_row.set_text("   ")
    assert settings_from_values(editor.values()).proxy_command == ""


def test_new_profile_proxy_command_row_starts_empty(gtk):
    editor, settings_from_values = _editor(LoginProfileSettings(name="New profile"))
    assert editor.proxy_command_row.get_text() == ""
    editor.proxy_command_row.set_text("none")
    assert settings_from_values(editor.values()).proxy_command == "none"
