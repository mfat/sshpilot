"""Fresh-install defaults: default operation mode, no chooser, color-dot groups."""

from sshpilot.config import Config


def test_group_color_display_defaults_to_color_dots():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['group_color_display'] == 'dot'


def test_operation_mode_defaults_to_shared_ssh_config():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ssh']['use_isolated_config'] is False


def test_sidebar_row_action_button_defaults():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['sidebar_show_file_manager_button'] is True
    assert defaults['ui']['sidebar_show_split_view_button'] is False


def test_sidebar_connection_row_display_defaults():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['sidebar_show_user_hostname'] is True
    assert defaults['ui']['sidebar_show_connection_icon'] is True
    assert defaults['ui']['sidebar_show_group_count'] is False


def test_group_color_child_rows_defaults_on():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['group_color_child_rows'] is True
