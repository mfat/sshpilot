"""GTK's F10 menu-bar accelerator must not steal htop's Quit key."""


def test_terminal_menu_bar_accel_is_cleared():
    from sshpilot.main import apply_terminal_menu_bar_accel_setting

    class _Window:
        def __init__(self):
            self.handle_menubar_accel = True

        def set_handle_menubar_accel(self, value):
            self.handle_menubar_accel = value

    window = _Window()
    apply_terminal_menu_bar_accel_setting(window)
    assert window.handle_menubar_accel is False


def test_missing_window_is_ignored():
    from sshpilot.main import apply_terminal_menu_bar_accel_setting

    apply_terminal_menu_bar_accel_setting(None)


def test_gtk4_settings_without_menubar_property_are_ignored():
    """GTK 4 GtkSettings has no gtk-menu-bar-accel; do not raise or set it."""
    from sshpilot.main import apply_terminal_menu_bar_accel_setting

    class _Gtk4Settings:
        def set_property(self, name, value):
            raise AssertionError(f"unexpected set_property({name!r})")

    apply_terminal_menu_bar_accel_setting(_Gtk4Settings())
