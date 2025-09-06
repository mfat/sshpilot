import os
import sys
import types
import asyncio

# Stub external dependencies required by ConnectionManager
sys.modules['secretstorage'] = types.ModuleType('secretstorage')

gi_repo = types.ModuleType('gi.repository')
gi_repo.GObject = types.SimpleNamespace(
    SignalFlags=types.SimpleNamespace(RUN_FIRST=0),
    Object=type('GObject', (object,), {})
)
gi_repo.GLib = types.SimpleNamespace(idle_add=lambda *args, **kwargs: None)
gi_repo.Gtk = types.SimpleNamespace()

gi_mod = types.ModuleType('gi')
gi_mod.require_version = lambda *args, **kwargs: None
gi_mod.repository = gi_repo
sys.modules['gi'] = gi_mod
sys.modules['gi.repository'] = gi_repo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from sshpilot.connection_manager import ConnectionManager

# Ensure an event loop for Connection objects
asyncio.set_event_loop(asyncio.new_event_loop())

def test_complex_config_parsing_and_ssh_command(tmp_path, monkeypatch):
    # Write included config file
    extra_cfg = tmp_path / 'extra.cfg'
    extra_cfg.write_text('\n'.join([
        'Host extraserver',
        '    HostName extra.internal',
        '    User extra',
    ]))

    # Write main config with various rules and a connectable host
    config = tmp_path / 'config'
    config.write_text('\n'.join([
        'Include extra.cfg',
        '',
        'Host my-app webapp',
        '    HostName 192.168.1.50',
        '    User admin',
        '    Port 2222',
        '    ProxyJump bastion',
        '',
        'Host pattern*',
        '    User patternuser',
        '',
        'Match user root',
        '    IdentityFile ~/.ssh/id_root',
    ]))

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(config)
    cm.load_ssh_config()

    # Only hosts with HostName should be connectable
    assert sorted(c.nickname for c in cm.connections) == ['extraserver', 'my-app']
    conn = next(c for c in cm.connections if c.nickname == 'my-app')
    assert conn.aliases == ['webapp']

    # Wildcard host and Match block stored as rules
    assert len(cm.rules) == 2
    rule_hosts = {r['host'] for r in cm.rules if 'host' in r}
    assert rule_hosts == {'pattern*'}
    assert any('Match user root' in r['raw'] for r in cm.rules if 'raw' in r)

    # Avoid running actual ssh -G in tests
    monkeypatch.setattr('sshpilot.connection_manager.get_effective_ssh_config', lambda host: {})

    # Build SSH command for the connectable host
    loop = asyncio.get_event_loop()
    loop.run_until_complete(conn.connect())
    cmd = conn.ssh_cmd
    assert 'ProxyJump=bastion' in cmd
    assert '-p' in cmd and cmd[cmd.index('-p') + 1] == '2222'
    assert cmd[-1] == 'admin@192.168.1.50'
