import asyncio
import importlib


def test_connection_batchmode_removed_when_identity_agent_none(monkeypatch):
    manager_mod = importlib.import_module("sshpilot.connection_manager")

    class DummyConfig:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_ssh_config(self):
            return {"batch_mode": True}

    monkeypatch.setattr("sshpilot.config.Config", DummyConfig, raising=False)

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


def test_native_connect_strips_batchmode_from_overrides(monkeypatch):
    manager_mod = importlib.import_module("sshpilot.connection_manager")

    class DummyConfig:
        def __init__(self, *_args, **_kwargs):
            pass

        def get_ssh_config(self):
            return {
                "ssh_overrides": [
                    '-o',
                    'BatchMode=yes',
                    '-o',
                    'ConnectTimeout=20',
                ]
            }

    monkeypatch.setattr("sshpilot.config.Config", DummyConfig, raising=False)

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

        connection._resolve_config_override_path = lambda: None

        result = loop.run_until_complete(connection.native_connect())
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
    assert '-o' in connection.ssh_cmd
    assert any(opt.startswith('ConnectTimeout=') for opt in connection.ssh_cmd)
