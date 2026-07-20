"""Plugin zero: the SSH protocol, expressed through the plugin API.

This deliberately does NOT move any logic out of ssh_connection_builder —
it adapts the existing, battle-tested builder to the ProtocolBackend
interface (strangler pattern). Auth orchestration that terminal.py still
owns at runtime (sshpass FIFO, askpass log forwarding) is signalled via
SpawnSpec.extras during the migration; once the second real protocol
lands, that code can migrate behind this interface too.

Marked ``"required": true`` in plugin.json: it loads even if listed in
plugins.disabled, and loader.load_plugins() raises if it fails.
"""

from __future__ import annotations

from typing import Any, List

from ...api import (
    Capability,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
    SpawnSpec,
    SshPilotPlugin,
)

_CAPS = frozenset({
    Capability.FILE_TRANSFER,
    Capability.PORT_FORWARDING,
    Capability.REMOTE_COMMAND,
    Capability.AUTH_PASSWORD,
    Capability.AUTH_KEY,
    Capability.KEY_DEPLOYMENT,
    Capability.AGENT,
    Capability.JUMP_HOST,
})


class SshProtocolBackend(ProtocolBackend):
    protocol_id = "ssh"
    display_name = "SSH"
    default_port = 22

    def capabilities(self) -> frozenset:
        return _CAPS

    def connection_fields(self) -> List:
        # The existing hand-built connection dialog remains authoritative
        # for SSH; declarative fields are for new protocols.
        return []

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        # Late imports keep plugin loading cheap and avoid import cycles
        # (ssh_connection_builder pulls in askpass/keyring machinery).
        from ....ssh_connection_builder import (  # noqa: PLC0415
            ConnectionContext,
            build_ssh_connection,
        )

        # Prefer a command the Connection already prepared during
        # connect()/native_connect() — identical to today's terminal.py
        # behaviour — and build fresh otherwise (reconnects, providers
        # injecting connections that never went through connect()).
        prepared = getattr(connection, "ssh_connection_cmd", None)
        if prepared is None:
            try:
                prepared = build_ssh_connection(ConnectionContext(
                    connection=connection,
                    connection_manager=ctx.connection_manager,
                    config=ctx.config,
                    command_type="ssh",
                    native_mode=True,
                ))
            except Exception as exc:
                raise ProtocolError(str(exc)) from exc

        return SpawnSpec(
            argv=list(prepared.command),
            env=dict(prepared.env),
            extras={
                # Auth delivery is askpass in prepared.env; these flags are for
                # terminal messaging / legacy extras only (use_sshpass is always False).
                "use_sshpass": bool(getattr(prepared, "use_sshpass", False)),
                "password": getattr(prepared, "password", None),
                "use_askpass": bool(getattr(prepared, "use_askpass", False)),
            },
        )

    def validate(self, data) -> List[str]:
        errors: List[str] = []
        if not (data.get("nickname") or data.get("host")
                or data.get("hostname")):
            errors.append("A host, hostname, or nickname is required.")
        return errors


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(SshProtocolBackend())
