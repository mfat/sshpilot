"""Fresh-install defaults: default operation mode, no chooser, colored-grip groups."""

from sshpilot.config import Config


def test_group_color_display_defaults_to_colored_grips():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['group_color_display'] == 'grip'
    assert defaults['ui']['sidebar_show_drag_handles'] is True


def test_configs_from_before_the_key_keep_color_dots():
    """Upgraders without the key keep the dots they always saw."""
    from sshpilot.core.settings.migration import ensure_config_defaults

    config, _updated = ensure_config_defaults({'ui': {}})
    assert config['ui']['group_color_display'] == 'dot'


def test_operation_mode_defaults_to_shared_ssh_config():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ssh']['use_isolated_config'] is False


def test_sidebar_row_action_button_defaults():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['sidebar_show_file_manager_button'] is True
    assert defaults['ui']['sidebar_show_split_view_button'] is False
    assert defaults['ui']['sidebar_show_dashboard_button'] is True
    assert defaults['ui']['sidebar_show_local_terminal'] is False


def test_headerbar_tab_button_defaults_on():
    defaults = Config.get_default_config(Config.__new__(Config))
    # The sidebar toggle has no setting: it is always shown.
    assert 'headerbar_show_sidebar_toggle' not in defaults['ui']
    assert defaults['ui']['headerbar_show_tab_button'] is True


def test_sidebar_connection_row_display_defaults():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['sidebar_show_user_hostname'] is False
    assert defaults['ui']['sidebar_show_connection_icon'] is True
    assert defaults['ui']['sidebar_show_group_count'] is False


def test_group_color_child_rows_defaults_on():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['group_color_child_rows'] is True
