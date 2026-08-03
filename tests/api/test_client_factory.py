from types import SimpleNamespace
import pytest
from sshpilot.api.client_factory import ClientSelection, select_client


def test_select_client_returns_daemon_result():
    client = object()
    launcher = SimpleNamespace(connect_or_start=lambda: SimpleNamespace(client=client, process=None))
    result = select_client(launcher=launcher)
    assert result == ClientSelection(client=client, daemon_process=None)


def test_select_client_propagates_daemon_failure():
    class Launcher:
        def connect_or_start(self):
            raise RuntimeError("daemon unavailable")
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        select_client(launcher=Launcher())
