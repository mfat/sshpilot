from types import SimpleNamespace

from sshpilot.tui.editor import ConnectionEditSession


class ScriptedInput:
    """Helper to feed deterministic answers into ConnectionEditSession."""

    def __init__(self, responses):
        self._responses = list(responses)

    def __call__(self, prompt: str) -> str:
        if not self._responses:
            raise AssertionError(f"No scripted response left for prompt: {prompt}")
        return self._responses.pop(0)


def _make_connection(**overrides):
    defaults = {
        "nickname": "web",
        "hostname": "web.internal",
        "username": "deploy",
        "port": 22,
        "local_command": "",
        "remote_command": "",
        "forwarding_rules": [],
        "source": "/tmp/ssh_config",
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_editor_updates_remote_command_without_touching_other_fields():
    conn = _make_connection()
    scripted = ScriptedInput(
        [
            "",  # nickname
            "",  # hostname
            "",  # username
            "",  # port
            "",  # local command
            "htop",  # remote command
            "n",  # skip forwarding edits
        ]
    )
    session = ConnectionEditSession(conn, input_func=scripted, print_func=lambda *args, **kwargs: None)

    result = session.run()

    assert result["nickname"] == conn.nickname
    assert result["hostname"] == conn.hostname
    assert result["remote_command"] == "htop"
    assert result["forwarding_rules"] == []


def test_editor_can_add_local_forwarding_rule():
    conn = _make_connection()
    scripted = ScriptedInput(
        [
            "",  # nickname
            "",  # hostname
            "",  # username
            "",  # port
            "",  # local command
            "",  # remote command
            "y",  # edit forwarding rules
            "a",  # add rule
            "",  # rule type -> default local
            "",  # bind address -> default localhost
            "9000",  # listen port
            "",  # enable rule -> default yes
            "db.internal",  # remote host
            "5432",  # remote port
            "",  # finish menu
        ]
    )
    session = ConnectionEditSession(conn, input_func=scripted, print_func=lambda *args, **kwargs: None)

    result = session.run()

    assert len(result["forwarding_rules"]) == 1
    rule = result["forwarding_rules"][0]
    assert rule["type"] == "local"
    assert rule["listen_port"] == 9000
    assert rule["remote_host"] == "db.internal"
    assert rule["remote_port"] == 5432
