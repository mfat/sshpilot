"""Fresh-install defaults: default operation mode, no chooser, accent-bar groups."""

from types import SimpleNamespace

from sshpilot.config import Config
from sshpilot.window_file_manager import WindowFileManagerMixin


def test_group_color_display_defaults_to_accent_bar():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ui']['group_color_display'] == 'bar'


def test_operation_mode_defaults_to_shared_ssh_config():
    defaults = Config.get_default_config(Config.__new__(Config))
    assert defaults['ssh']['use_isolated_config'] is False


def test_operation_mode_first_run_prompt_disabled():
    assert WindowFileManagerMixin._OPERATION_MODE_FIRST_RUN_PROMPT_ENABLED is False

    recorded = {}

    class FakeWindow(WindowFileManagerMixin):
        def __init__(self):
            self.config = SimpleNamespace(
                get_setting=lambda key, default=None: recorded.get(key, default),
                set_setting=lambda key, value: recorded.__setitem__(key, value),
            )
            self.isolated_mode = False

    win = FakeWindow()
    assert win._should_prompt_operation_mode() is False
    assert recorded.get('ssh.operation_mode_prompt_shown') is True
