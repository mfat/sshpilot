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
