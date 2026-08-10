"""Focused coverage for daemon-owned remote-history autocomplete."""

from types import SimpleNamespace

from sshpilot.terminal_backends import _fetch_remote_history_via_daemon


def test_remote_history_uses_single_target_broadcast_command():
    connection = SimpleNamespace(id="conn-1", nickname="host1")
    target = SimpleNamespace(exit_code=0, stdout="git status\n")
    summary = SimpleNamespace(
        operation=SimpleNamespace(id="op-1", state=SimpleNamespace(value="succeeded")),
        targets=(target,),
    )

    class Client:
        def __init__(self):
            self.request = None

        def list_connections(self):
            return [connection]

        def start_broadcast_command(self, request):
            self.request = request
            return summary

    client = Client()
    result = _fetch_remote_history_via_daemon(
        SimpleNamespace(client=client), SimpleNamespace(nickname="host1")
    )

    assert result == "git status\n"
    assert client.request.connection_ids == ("conn-1",)
    assert ".bash_history" in client.request.command
    assert client.request.policy.concurrency_limit == 1


def test_remote_history_returns_none_without_daemon():
    assert _fetch_remote_history_via_daemon(
        SimpleNamespace(client=None), SimpleNamespace(nickname="host1")
    ) is None
