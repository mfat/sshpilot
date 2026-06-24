"""The copy-on-select / paste-on-right-click terminal options must default off."""

from sshpilot.config import Config


def test_terminal_mouse_defaults_present_and_off():
    # get_default_config() returns a literal dict and does not touch instance
    # state, so a bare instance is enough to read the defaults.
    defaults = Config.get_default_config(Config.__new__(Config))
    terminal = defaults['terminal']
    assert terminal['copy_on_select'] is False
    assert terminal['paste_on_right_click'] is False
