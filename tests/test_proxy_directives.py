import asyncio

from sshpilot.connection_manager import ConnectionManager


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


# Removed: test_connection_passes_proxy_options,
#          test_terminal_widget_uses_prepared_proxy_command,
#          test_terminal_widget_prepares_key_in_default_mode,
#          test_terminal_manager_prepares_connection_before_spawn.
#
# Those asserted ProxyCommand/ProxyJump/IdentityFile appeared as `-o` flags on the
# spawned ssh command. In the native architecture the command is the minimal
# `ssh -F <config> <host>` and those per-host directives live in ~/.ssh/config
# (ssh applies them). Parsing into Connection attributes is covered above; the
# generated config and native command are covered by tests/test_ssh_connection_builder*.py
# and the config round-trip tests.
