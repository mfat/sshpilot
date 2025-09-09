import asyncio
from sshpilot.connection_manager import Connection, ConnectionManager

# Ensure an event loop for Connection objects
asyncio.set_event_loop(asyncio.new_event_loop())

def test_parse_and_load_proxy_directives(tmp_path):
    cfg_path = tmp_path / "config"
    cfg_path.write_text(
        "\n".join(
            [
                "Host proxycmd",
                "    HostName example.com",
                "    ProxyCommand ssh -W %h:%p bastion",
                "",
                "Host proxyjump",
                "    HostName example.com",
                "    ProxyJump bastion",
            ]
        )
    )

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(cfg_path)
    cm.load_ssh_config()

    assert len(cm.connections) == 2
    proxy_cmd_conn = next(c for c in cm.connections if c.nickname == "proxycmd")
    proxy_jump_conn = next(c for c in cm.connections if c.nickname == "proxyjump")

    assert proxy_cmd_conn.proxy_command == "ssh -W %h:%p bastion"
    assert proxy_jump_conn.proxy_jump == "bastion"

async def _connect(conn: Connection):
    await conn.connect()

def test_connection_passes_proxy_options():
    loop = asyncio.get_event_loop()
    conn1 = Connection({"host": "example.com", "proxy_command": "ssh -W %h:%p bastion"})
    conn2 = Connection({"host": "example.com", "proxy_jump": "bastion"})
    loop.run_until_complete(_connect(conn1))
    loop.run_until_complete(_connect(conn2))
    assert "ProxyCommand=ssh -W %h:%p bastion" in conn1.ssh_cmd
    assert "ProxyJump=bastion" in conn2.ssh_cmd
