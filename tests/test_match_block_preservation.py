import os
import sys
import asyncio

# gi.repository / Secret stubs come from tests/conftest.py; redefining them
# here would clobber other tests' expectations (see #985).

# Ensure the project package is importable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.connection_manager import ConnectionManager


def test_match_block_preserved_on_update(tmp_path):
    asyncio.set_event_loop(asyncio.new_event_loop())

    cfg_path = tmp_path / 'config'
    cfg_path.write_text(
        '\n'.join([
            'Match host example.com user root',
            '    IdentityFile /match/id_rsa',
            '',
            'Host existing',
            '    HostName old.example.com',
            '    User user1',
        ])
    )

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(cfg_path)

    cm.load_ssh_config()

    # Match block captured as rule
    assert len(cm.rules) == 1
    expected_block = 'Match host example.com user root\n    IdentityFile /match/id_rsa'
    assert cm.rules[0]['raw'] == expected_block

    # Update existing host (rewrite same data)
    conn = cm.connections[0]
    cm.update_ssh_config_file(conn, conn.data)

    # Config should still contain the Match block exactly
    assert expected_block in cfg_path.read_text()
