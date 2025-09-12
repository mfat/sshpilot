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
                "",
                "Host multijump",
                "    HostName example.com",
                "    ProxyJump bast1,bast2",
                "    ForwardAgent yes",
            ]
        )
    )

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(cfg_path)
    cm.load_ssh_config()

    assert len(cm.connections) == 3
    proxy_cmd_conn = next(c for c in cm.connections if c.nickname == "proxycmd")
    proxy_jump_conn = next(c for c in cm.connections if c.nickname == "proxyjump")
    multi_conn = next(c for c in cm.connections if c.nickname == "multijump")

    assert proxy_cmd_conn.proxy_command == "ssh -W %h:%p bastion"
    assert proxy_jump_conn.proxy_jump == ["bastion"]
    assert multi_conn.proxy_jump == ["bast1", "bast2"]
    assert multi_conn.forward_agent is True

async def _connect(conn: Connection):
    await conn.connect()

def test_connection_passes_proxy_options():
    loop = asyncio.get_event_loop()
    conn1 = Connection({"host": "example.com", "proxy_command": "ssh -W %h:%p bastion"})
    conn2 = Connection({"host": "example.com", "proxy_jump": ["bastion"]})
    conn3 = Connection({"host": "example.com", "proxy_jump": ["b1", "b2"], "forward_agent": True})
    loop.run_until_complete(_connect(conn1))
    loop.run_until_complete(_connect(conn2))
    loop.run_until_complete(_connect(conn3))
    assert "ProxyCommand=ssh -W %h:%p bastion" in conn1.ssh_cmd
    assert "ProxyJump=bastion" in conn2.ssh_cmd
    assert "ProxyJump=b1,b2" in conn3.ssh_cmd
    assert "-A" in conn3.ssh_cmd
    assert "ForwardAgent=yes" in conn3.ssh_cmd
