from types import SimpleNamespace

import pytest

from sshpilot.api.models.connection_store import ConnectionMetadataSummary
from sshpilot.omni_search import (
    CommandSpec,
    _match_score,
    search_omni,
)
from sshpilot.plugins.api import Capability
from sshpilot.plugins.registry import ProtocolRegistry

_CAPS = {
    "ssh": frozenset(Capability),
    "rdp": frozenset({Capability.AUTH_PASSWORD}),
    "mosh": frozenset({Capability.REMOTE_COMMAND}),
}


class _Backend:
    def __init__(self, protocol_id, display_name):
        self.protocol_id = protocol_id
        self.display_name = display_name

    def capabilities(self):
        return _CAPS.get(self.protocol_id, frozenset())


@pytest.fixture(autouse=True)
def _protocols(monkeypatch):
    """SSH, RDP and Mosh backends, as the built-in plugins register them."""
    registry = ProtocolRegistry()
    for pid, name in (("ssh", "SSH"), ("rdp", "RDP"), ("mosh", "Mosh")):
        registry.register(_Backend(pid, name))
    monkeypatch.setattr(
        "sshpilot.omni_search.protocol_registry", lambda: registry
    )
    monkeypatch.setattr(
        "sshpilot.omni_search.capabilities_for",
        lambda c: _CAPS.get(getattr(c, "protocol", "ssh"), frozenset()),
    )


class _Connections:
    """Connection-manager projection double owning pinning/recent metadata.

    Production reads pinning and recent-use data from
    ``window.connection_manager.metadata`` and ``get_metadata(connection_id)``,
    so the fake owns that state instead of stashing it on window.config.
    """

    def __init__(self, connections, *, pinned=(), recent=None):
        self._connections = list(connections)
        recent = dict(recent or {})
        pinned = set(pinned)

        names = {
            *(c.nickname for c in self._connections),
            *pinned,
            *recent,
        }
        self.metadata = tuple(
            ConnectionMetadataSummary(
                connection_id=name,
                values={
                    "pinned": name in pinned,
                    "last_used": recent.get(name, 0),
                },
            )
            for name in sorted(names)
        )

    def get_connections(self):
        return list(self._connections)

    def get_metadata(self, connection_id):
        for item in self.metadata:
            if item.connection_id == connection_id:
                return item.values
        return {}


def _window(connections=(), pinned=(), recent=None):
    return SimpleNamespace(
        connection_manager=_Connections(connections, pinned=pinned, recent=recent),
    )


def _connection(name, host="example.com", user="alice", protocol="ssh"):
    return SimpleNamespace(
        nickname=name,
        display_name=name,
        hostname=host,
        host=name,
        username=user,
        tags=[],
        protocol=protocol,
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


def test_transfer_intent_suggests_hosts(monkeypatch):
    web = _connection("web")
    db = _connection("database", host="db.internal")
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: [],
    )

    # Partial name after the tool fuzzy-matches hosts.
    partial = search_omni(_window([web, db]), "sftp we")
    assert partial[0].kind == "transfer"
    assert partial[0].payload == ("sftp", web)

    # Bare tool offers the chooser plus recent/pinned hosts.
    bare = search_omni(_window([web, db], recent={"web": 5}), "sftp")
    assert bare[0].payload == ("sftp", None)
    assert any(result.payload == ("sftp", web) for result in bare)


def test_explicit_ssh_suggests_matching_saved_hosts(monkeypatch):
    router = _connection("GoogleRouter", host="192.168.8.1", user="root")
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: [],
    )

    for query in ("ssh g", "ssh root@goo", "root@goo"):
        results = search_omni(_window([router]), query)
        assert any(
            result.kind == "connection" and result.payload is router
            for result in results
        ), query

    # Bare "ssh" offers recent hosts.
    bare = search_omni(_window([router], recent={"GoogleRouter": 5}), "ssh")
    assert any(
        result.kind == "connection" and result.payload is router
        for result in bare
    )


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


def test_ssh_results_share_field_validation_without_rejecting_aliases(
    monkeypatch,
):
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands",
        lambda _window: [],
    )

    invalid_host = search_omni(_window(), "ssh alice@999.1.1.1")[0]
    invalid_port = search_omni(
        _window(), "ssh -p 70000 alice@example.com"
    )[0]
    alias = search_omni(_window(), "ssh alice@team_alias")[0]

    assert invalid_host.kind == "validation"
    assert invalid_host.enabled is False
    assert invalid_port.kind == "validation"
    assert invalid_port.enabled is False
    assert alias.kind == "ssh"


def test_dashboard_for_any_host_in_either_order(monkeypatch):
    monkeypatch.setattr("sshpilot.omni_search.collect_commands", lambda _w: [])
    web = _connection("web")
    nas = _connection("nas", host="nas.lan")
    for query in ("dashboard nas", "nas dashboard", "stats na"):
        result = search_omni(_window([web, nas]), query)[0]
        assert (result.kind, result.payload) == ("dashboard", nas), query

    bare = search_omni(_window([web, nas]), "dashboard")
    assert {r.payload.nickname for r in bare if r.kind == "dashboard"} == {
        "web", "nas",
    }


def test_dashboard_skips_hosts_that_cannot_run_commands(monkeypatch):
    monkeypatch.setattr("sshpilot.omni_search.collect_commands", lambda _w: [])
    office = _connection("office", protocol="rdp")
    results = search_omni(_window([office]), "dashboard office")
    assert not [r for r in results if r.kind == "dashboard"]


def test_transfer_skips_hosts_without_file_transfer(monkeypatch):
    monkeypatch.setattr("sshpilot.omni_search.collect_commands", lambda _w: [])
    office = _connection("office", protocol="rdp")
    results = search_omni(_window([office]), "sftp office")
    assert [r.payload for r in results if r.kind == "transfer"] == [
        ("sftp", None)
    ]


def test_protocol_word_lists_saved_connections_of_that_protocol(monkeypatch):
    monkeypatch.setattr("sshpilot.omni_search.collect_commands", lambda _w: [])
    office = _connection("office", protocol="rdp")
    lab = _connection("lab", protocol="rdp")
    web = _connection("web")
    for query in ("rdp", "remote desktop", "RDP"):
        results = search_omni(_window([office, lab, web]), query)
        assert {r.payload.nickname for r in results} == {"office", "lab"}
    narrowed = search_omni(_window([office, lab, web]), "rdp off")
    assert narrowed[0].payload is office


def test_protocol_without_connections_offers_new_connection(monkeypatch):
    new = CommandSpec("New Connection", "app.new-connection")
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands", lambda _w: [new]
    )
    result = search_omni(_window([_connection("web")]), "mosh")[0]
    assert result.kind == "command"
    assert result.payload.action == "app.new-connection"


def test_ssh_with_a_bare_word_ranks_below_a_command_match(monkeypatch):
    keys = CommandSpec(
        "Copy Key to Server", "app.new-key", aliases=("ssh key",),
    )
    monkeypatch.setattr(
        "sshpilot.omni_search.collect_commands", lambda _w: [keys]
    )
    nas = _connection("nas")
    assert search_omni(_window(), "ssh key")[0].payload is keys
    # A saved host or a real destination still connects first.
    assert search_omni(_window([nas]), "ssh nas")[0].payload is nas
    assert search_omni(_window(), "ssh root@key")[0].kind == "ssh"
    # An unknown word with nothing better on offer still connects.
    assert search_omni(_window(), "ssh myalias")[0].kind == "ssh"
