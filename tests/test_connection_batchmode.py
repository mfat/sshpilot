import asyncio
import importlib


def test_connection_batchmode_removed_when_identity_agent_none(monkeypatch):
    manager_mod = importlib.import_module("sshpilot.connection_manager")

    class DummyConfig:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_ssh_config(self):
            return {"batch_mode": True}

    monkeypatch.setattr(manager_mod, "Config", DummyConfig, raising=False)

    def fake_effective_config(_alias, config_file=None):
        return {"identityagent": "none"}

    monkeypatch.setattr(
        manager_mod,
        "get_effective_ssh_config",
        fake_effective_config,
        raising=False,
    )

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)

        connection = manager_mod.Connection(
            {
                "hostname": "example.com",
                "username": "demo",
                "port": 22,
            }
        )

        # Avoid file lookups for config overrides during the test
        connection._resolve_config_override_path = lambda: None

        result = loop.run_until_complete(connection.connect())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()

    assert result is True
    assert connection.identity_agent_disabled is True
    assert 'BatchMode=yes' not in connection.ssh_cmd


def test_connection_effective_config_lookup_uses_hostname(monkeypatch):
    manager_mod = importlib.import_module("sshpilot.connection_manager")

    class DummyConfig:
        def get_ssh_config(self):
            return {"batch_mode": True}

    monkeypatch.setattr(manager_mod, "Config", DummyConfig, raising=False)

    lookup_calls = []

    def fake_effective_config(host, config_file=None):
        lookup_calls.append(host)
        if host == "Alias Name":
            return {}
        if host == "example.com":
            return {"identityagent": "none"}
        return {}

    monkeypatch.setattr(
        manager_mod,
        "get_effective_ssh_config",
        fake_effective_config,
        raising=False,
    )

    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)

        connection = manager_mod.Connection(
            {
                "nickname": "Alias Name",
                "hostname": "example.com",
                "host": "example.com",
                "username": "demo",
                "port": 22,
            }
        )

        connection._resolve_config_override_path = lambda: None

        result = loop.run_until_complete(connection.connect())
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        asyncio.set_event_loop(None)
        loop.close()

    assert result is True
    assert connection.identity_agent_disabled is True
    assert 'BatchMode=yes' not in connection.ssh_cmd
    assert lookup_calls[0] == "Alias Name"
    assert "example.com" in lookup_calls
