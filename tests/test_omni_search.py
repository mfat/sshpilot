from types import SimpleNamespace

from sshpilot.omni_search import (
    CommandSpec,
    _match_score,
    search_omni,
)


class _Connections:
    def __init__(self, connections):
        self._connections = connections

    def get_connections(self):
        return list(self._connections)


class _Config:
    def __init__(self, pinned=(), recent=None):
        self._pinned = list(pinned)
        self._recent = recent or {}

    def get_pinned_nicknames(self):
        return list(self._pinned)

    def get_connection_meta(self, nickname):
        return {"last_used": self._recent.get(nickname, 0)}


def _window(connections=(), pinned=(), recent=None):
    return SimpleNamespace(
        connection_manager=_Connections(connections),
        config=_Config(pinned, recent),
    )


def _connection(name, host="example.com", user="alice"):
    return SimpleNamespace(
        nickname=name,
        display_name=name,
        hostname=host,
        host=name,
        username=user,
        tags=[],
    )


def test_match_score_prefers_exact_and_accepts_conservative_typo():
    exact = _match_score("preferences", ("Preferences",))
    typo = _match_score("preferenes", ("Preferences",))
    unrelated = _match_score("banana", ("Preferences",))

    assert exact > typo > 0
    assert unrelated == 0


def test_common_wording_maps_to_command(monkeypatch):
    commands = [
        CommandSpec(
            "Preferences",
            "app.preferences",
            aliases=("settings", "options"),
        )
    ]
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: commands,
    )

    results = search_omni(_window(), "settings")

    assert results[0].kind == "command"
    assert results[0].payload.action == "app.preferences"


def test_transfer_intent_uses_single_saved_alias(monkeypatch):
    web = _connection("web")
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: [],
    )

    result = search_omni(_window([web]), "scp web")[0]

    assert result.kind == "transfer"
    assert result.payload == ("scp", web)


def test_transfer_intents_do_not_become_terminal_commands(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: [],
    )

    for query, expected in (
        ("sftp", "sftp"),
        ("scp", "scp"),
        ("ssh-copy-id", "ssh-copy-id"),
    ):
        result = search_omni(_window(), query)[0]
        assert result.kind == "transfer"
        assert result.payload == (expected, None)


def test_explicit_ssh_is_executable_but_arbitrary_shell_is_not(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: [],
    )

    ssh_results = search_omni(_window(), "ssh alice@example.com")
    shell_results = search_omni(_window(), "rm -rf /tmp/example")

    assert ssh_results[0].kind == "ssh"
    assert ssh_results[0].payload == ("ssh", "alice@example.com")
    assert shell_results == []


def test_empty_query_suggests_pinned_then_common_tools(monkeypatch):
    web = _connection("web")
    commands = [
        CommandSpec("New Connection", "app.new-connection"),
        CommandSpec("Preferences", "app.preferences"),
    ]
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: commands,
    )

    results = search_omni(_window([web], pinned=["web"]), "")

    assert results[0].kind == "connection"
    assert results[0].payload is web
    assert [result.payload.action for result in results[1:]] == [
        "app.new-connection",
        "app.preferences",
    ]


def test_malformed_explicit_ssh_is_disabled(monkeypatch):
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: [],
    )

    result = search_omni(_window(), 'ssh "unfinished')[0]

    assert result.kind == "validation"
    assert result.enabled is False
