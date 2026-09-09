"""Fresh-install defaults: default operation mode, no chooser, accent-bar groups."""

from sshpilot.config import Config


def test_group_color_display_defaults_to_accent_bar():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['group_color_display'] == 'bar'


def test_operation_mode_defaults_to_shared_ssh_config():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ssh']['use_isolated_config'] is False


def test_sidebar_row_action_button_defaults():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['sidebar_show_file_manager_button'] is True
    assert defaults['ui']['sidebar_show_split_view_button'] is False


def test_sidebar_secondary_label_defaults_are_off():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['sidebar_show_user_hostname'] is False
    assert defaults['ui']['sidebar_show_group_count'] is False
