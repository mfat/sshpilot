"""Fresh-install defaults: default operation mode, no chooser, accent-bar groups."""

from sshpilot.config import Config


def test_group_color_display_defaults_to_accent_bar():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['group_color_display'] == 'bar'


def test_operation_mode_defaults_to_shared_ssh_config():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ssh']['use_isolated_config'] is False
