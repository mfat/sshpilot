"""The hide-hostnames toolbar button only appears when hostnames are shown."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from sshpilot.sidebar import update_hide_hosts_button_visibility


def _window(mode, show_hostname):
    prefs = {
        'ui.sidebar_mode': mode,
        'ui.sidebar_show_user_hostname': show_hostname,
    }
    config = MagicMock()
    config.get_setting.side_effect = lambda key, default=None: prefs.get(key, default)
    button = SimpleNamespace(get_parent=lambda: None)
    return SimpleNamespace(config=config, _hide_hosts_button=button), button, prefs


@pytest.mark.parametrize(
    'mode, show_hostname, hidden',
    [
        ('full', True, False),
        ('full', False, True),
        ('compact', True, True),
        ('compact', False, True),
    ],
)
def test_button_is_excluded_unless_hostnames_are_shown(mode, show_hostname, hidden):
    win, button, _prefs = _window(mode, show_hostname)
    update_hide_hosts_button_visibility(win)
    assert button._overflow_force_hidden is hidden


def test_button_returns_when_hostnames_are_shown_again():
    win, button, prefs = _window('compact', True)
    update_hide_hosts_button_visibility(win)
    assert button._overflow_force_hidden is True

    prefs['ui.sidebar_mode'] = 'full'
    update_hide_hosts_button_visibility(win)
    assert button._overflow_force_hidden is False


def test_missing_button_is_a_no_op():
    update_hide_hosts_button_visibility(SimpleNamespace(config=MagicMock()))
