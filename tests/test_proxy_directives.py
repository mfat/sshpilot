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
                "Host forwardagent",
                "    HostName example.com",
                "    ForwardAgent yes",
                "",
                "Host multijump",
                "    HostName example.com",
                "    ProxyJump bastion1,bastion2",
            ]
        )
    )

    cm = ConnectionManager.__new__(ConnectionManager)
    cm.connections = []
    cm.ssh_config_path = str(cfg_path)
    cm.load_ssh_config()

    assert len(cm.connections) == 4
    proxy_cmd_conn = next(c for c in cm.connections if c.nickname == "proxycmd")
    proxy_jump_conn = next(c for c in cm.connections if c.nickname == "proxyjump")
    forward_agent_conn = next(c for c in cm.connections if c.nickname == "forwardagent")
    multijump_conn = next(c for c in cm.connections if c.nickname == "multijump")

    assert proxy_cmd_conn.proxy_command == "ssh -W %h:%p bastion"
    assert proxy_jump_conn.proxy_jump == "bastion"
    assert forward_agent_conn.forward_agent is True
    assert multijump_conn.proxy_jump == "bastion1,bastion2"
    # Ensure advanced config does not duplicate handled directives
    assert 'proxyjump' not in (proxy_jump_conn.extra_ssh_config or '').lower()
    assert 'forwardagent' not in (forward_agent_conn.extra_ssh_config or '').lower()

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


def test_connection_multiple_jump_hosts_and_agent_forwarding():
    loop = asyncio.get_event_loop()
    conn = Connection(
        {"host": "example.com", "proxy_jump": ["b1", "b2"], "forward_agent": True}
    )
    loop.run_until_complete(_connect(conn))
    assert "ProxyJump=b1,b2" in conn.ssh_cmd
    assert "-A" in conn.ssh_cmd


def test_format_writes_jump_hosts_and_forward_agent():
    cm = ConnectionManager.__new__(ConnectionManager)
    data = {
        "nickname": "test",
        "host": "example.com",
        "username": "user",
        "jump_hosts": ["b1", "b2"],
        "forward_agent": True,
    }
    entry = cm.format_ssh_config_entry(data)
    assert "ProxyJump b1,b2" in entry
    assert "ForwardAgent yes" in entry


def test_update_config_persists_jump_and_agent(tmp_path):
    cm = ConnectionManager.__new__(ConnectionManager)
    cm.ssh_config_path = str(tmp_path / "cfg")
    data = {
        "nickname": "test",
        "host": "example.com",
        "username": "user",
        "jump_hosts": ["b1", "b2"],
        "forward_agent": True,
    }
    conn = Connection(data)
    cm.update_ssh_config_file(conn, data)
    content = (tmp_path / "cfg").read_text()
    assert "ProxyJump b1,b2" in content
    assert "ForwardAgent yes" in content


def test_connection_update_data_preserves_jump_and_agent():
    conn = Connection({"host": "example.com"})
    conn.update_data({"jump_hosts": ["b1", "b2"], "forward_agent": True})
    assert conn.jump_hosts == ["b1", "b2"]
    assert conn.forward_agent is True
