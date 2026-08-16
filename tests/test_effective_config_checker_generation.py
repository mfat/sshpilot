"""Generation guards for the daemon-backed GTK effective-config checker."""

from types import SimpleNamespace

import pytest

pytest.importorskip("gi")

from sshpilot.effective_config_check import EffectiveConfigChecker


def test_checker_rejects_daemon_generation_older_than_cached_result():
    checker = EffectiveConfigChecker(None)
    assert checker._accept_result("web", 0, (True, 8)) is True
    assert checker._accept_result("web", 0, (False, 7)) is False
    assert checker.status("web") is True


def test_checker_consumes_result_generation_and_publishes_newer_snapshot():
    result = SimpleNamespace(available=True, has_diff=True, generation=11)

    class Client:
        def get_effective_config(self, _connection_id):
            return result

    checker = EffectiveConfigChecker(
        None,
        client_provider=lambda: Client(),
    )
    assert checker._compute(SimpleNamespace(nickname="web")) == (True, 11)
    assert checker._accept_result("web", 0, (True, 11)) is True


def test_checker_does_not_query_an_already_closed_daemon_client():
    class ClosedClient:
        is_closed = True
        calls = 0

        def get_effective_config(self, _connection_id):
            self.calls += 1
            raise AssertionError("closed client must not receive an RPC")

    client = ClosedClient()
    checker = EffectiveConfigChecker(None, client_provider=lambda: client)

    assert checker._compute(SimpleNamespace(nickname="web")) is None
    assert client.calls == 0


def test_stale_completion_does_not_clear_new_generation_queue_marker(monkeypatch):
    checker = EffectiveConfigChecker(None)
    monkeypatch.setattr(checker, "_ensure_worker", lambda: None)
    connection = SimpleNamespace(nickname="web")

    checker.schedule(connection)
    first, nickname, first_generation = checker._queue.get_nowait()
    assert first is connection
    assert nickname == "web"
    assert first_generation == 0

    checker.invalidate("web")
    checker.schedule(connection)
    _second, _nickname, second_generation = checker._queue.get_nowait()
    assert second_generation == 1

    # Completion from A is stale. It must not remove B's pending marker.
    assert checker._accept_result("web", first_generation, (True, 1)) is False
    checker.schedule(connection)
    assert checker._queued == {"web": 1}
    assert checker._queue.empty()
