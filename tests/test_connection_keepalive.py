"""Keepalive options reach the command via the app-level ``ssh_overrides``.

Native mode composes app-level SSH options from ``ssh_overrides`` (the flat
list produced by Preferences ▸ SSH Settings) and appends them verbatim to the
command, so keepalive is provided that way rather than via the old
``keepalive_interval`` / ``keepalive_count_max`` keys. The plain native
command builder is the contract surface — no event loop or removed
``Connection.connect()`` involved.
"""

from sshpilot.api.models import ConnectionDetails
from sshpilot.ssh_connection_builder import build_native_command


class Config:
    def get_ssh_config(self):
        return {
            "ssh_overrides": [
                "-o", "ServerAliveInterval=45",
                "-o", "ServerAliveCountMax=6",
            ],
        }


def test_connection_appends_keepalive_options():
    connection = ConnectionDetails(
        id="example",
        nickname="example",
        host="example",
        hostname="example.com",
        username="alice",
        port=22,
    )
    cmd = build_native_command(connection, Config())

    assert "ServerAliveInterval=45" in cmd
    assert "ServerAliveCountMax=6" in cmd

    interval_index = cmd.index("ServerAliveInterval=45")
    count_index = cmd.index("ServerAliveCountMax=6")

    # Each option is preceded by -o and lands before the host token.
    assert cmd[interval_index - 1] == "-o"
    assert cmd[count_index - 1] == "-o"

    # The connection alias is the final host token.
    assert cmd[-1] == "example"
    assert interval_index < cmd.index("example")
    assert count_index < cmd.index("example")


# Note: per-connection port forwarding is now expressed as LocalForward/
# RemoteForward/DynamicForward directives in ~/.ssh/config (handled by ssh
# itself in the single native command), not by spawning a separate ssh process
# per rule. The former ``Connection.start_local_forwarding`` /
# ``start_remote_forwarding`` subprocess methods were removed; config-based
# forwarding output is covered by tests/test_connection_forwarding.py.
