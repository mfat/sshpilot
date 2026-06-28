"""Telnet protocol plugin: the first non-SSH protocol.

Deliberately minimal — it proves the plugin API end to end (declarative
connection fields, capability gating, non-ssh_config persistence, spawn
through the terminal seam) with nothing beyond the system ``telnet``
binary. No capabilities: SFTP, port forwarding, key deployment, and the
other SSH-only UI stay hidden for telnet connections.
"""

from __future__ import annotations

import os
import shutil  # noqa: F401  # kept: tests patch this module's `shutil.which`
from typing import Any, Dict, List

from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
    SpawnSpec,
    SshPilotPlugin,
)


class TelnetProtocolBackend(ProtocolBackend):
    protocol_id = "telnet"
    display_name = "Telnet"
    default_port = 23

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="host", label="Host", kind="text", required=True,
                      placeholder="hostname or IP address"),
            FieldSpec(key="port", label="Port", kind="int",
                      default=self.default_port),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("host") or data.get("hostname")):
            errors.append("A host is required.")
        raw_port = data.get("port", self.default_port)
        if raw_port is None:
            raw_port = self.default_port
        try:
            if not 0 < int(raw_port) < 65536:
                errors.append("Port must be between 1 and 65535.")
        except (TypeError, ValueError):
            errors.append("Port must be a number.")
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        from .._flatpak import resolve_host_binary  # noqa: PLC0415
        telnet_argv = resolve_host_binary("telnet")
        if telnet_argv is None:
            raise ProtocolError(
                "The 'telnet' program is not installed. Install it to use "
                "telnet connections.")

        data = getattr(connection, "data", None) or {}
        host = (data.get("host") or data.get("hostname")
                or getattr(connection, "hostname", "")
                or getattr(connection, "host", ""))
        if not host:
            raise ProtocolError("No host configured for this connection.")
        # Port comes from the connection's data dict only: the Connection
        # attribute defaults to the SSH port (22), which is wrong here.
        try:
            port = int(data.get("port") or self.default_port)
        except (TypeError, ValueError):
            port = self.default_port

        argv = [*telnet_argv, str(host)]
        if port != self.default_port:
            argv.append(str(port))
        return SpawnSpec(argv=argv, env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(TelnetProtocolBackend())
