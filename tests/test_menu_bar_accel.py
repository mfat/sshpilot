"""GTK's F10 menu-bar accelerator must not steal htop's Quit key."""


def test_terminal_menu_bar_accel_is_cleared():
    from sshpilot.main import apply_terminal_menu_bar_accel_setting

    class _Settings:
        def __init__(self):
            self.props = {}

        def set_property(self, name, value):
            self.props[name] = value

    settings = _Settings()
    apply_terminal_menu_bar_accel_setting(settings)
    assert settings.props["gtk-menu-bar-accel"] == ""


def test_missing_settings_are_ignored():
    from sshpilot.main import apply_terminal_menu_bar_accel_setting

    apply_terminal_menu_bar_accel_setting(None)
