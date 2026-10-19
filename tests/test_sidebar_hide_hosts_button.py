"""Show Hostnames is offered only when the sidebar shows hostnames."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sshpilot.sidebar import update_hide_hosts_button_visibility


class _Action:
    def __init__(self):
        self.enabled = None

    def set_enabled(self, value):
        self.enabled = value


def _window(mode, show_hostname):
    prefs = {
        'ui.sidebar_mode': mode,
        'ui.sidebar_show_user_hostname': show_hostname,
    }
    config = MagicMock()
    config.get_setting.side_effect = lambda key, default=None: prefs.get(key, default)
    action = _Action()
    actions = {'show-hostnames': action}
    return SimpleNamespace(config=config, lookup_action=actions.get), action, prefs


@pytest.mark.parametrize(
    'mode, show_hostname, enabled',
    [
        ('full', True, True),
        ('full', False, False),
        ('compact', True, False),
        ('compact', False, False),
    ],
)
def test_action_is_disabled_unless_hostnames_are_shown(mode, show_hostname, enabled):
    win, action, _prefs = _window(mode, show_hostname)
    update_hide_hosts_button_visibility(win)
    assert action.enabled is enabled


def test_action_returns_when_hostnames_are_shown_again():
    win, action, prefs = _window('compact', True)
    update_hide_hosts_button_visibility(win)
    assert action.enabled is False

    prefs['ui.sidebar_mode'] = 'full'
    update_hide_hosts_button_visibility(win)
    assert action.enabled is True


def test_missing_action_is_a_no_op():
    update_hide_hosts_button_visibility(
        SimpleNamespace(config=MagicMock(), lookup_action=lambda _name: None)
    )
