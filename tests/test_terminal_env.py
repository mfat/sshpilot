import pytest

from sshpilot import terminal as terminal_mod


class DummyConfig:
    def __init__(self, term=None):
        self._term = term

    def get_setting(self, key, default=None):
        if key in {'terminal.term', 'terminal.term_override'}:
            return self._term if self._term is not None else default
        return default


@pytest.fixture
def widget():
    term_widget = terminal_mod.TerminalWidget.__new__(terminal_mod.TerminalWidget)
    term_widget.config = DummyConfig()
    return term_widget


def test_normalize_spawn_env_defaults_to_xterm256(widget):
    env = {}
    widget._normalize_spawn_env(env)
    assert env['TERM'] == 'xterm-256color'


def test_normalize_spawn_env_preserves_known_alias(widget):
    env = {'TERM': 'xterm'}
    widget._normalize_spawn_env(env)
    assert env['TERM'] == 'xterm'


def test_normalize_spawn_env_honors_user_override(monkeypatch):
    term_widget = terminal_mod.TerminalWidget.__new__(terminal_mod.TerminalWidget)
    term_widget.config = DummyConfig('ansi')
    env = {'TERM': 'dumb'}
    term_widget._normalize_spawn_env(env)
    assert env['TERM'] == 'ansi'
