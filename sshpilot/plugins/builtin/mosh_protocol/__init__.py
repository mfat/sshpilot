"""Mosh protocol plugin — roaming, persistent SSH.

Mosh bootstraps over SSH (to start ``mosh-server``) and then keeps a UDP
connection alive across roaming/suspend. This backend does NOT hand-roll an ssh
command: it reuses sshPilot's single SSH path — ``build_native_command`` for the
``ssh -F <config> [overrides]`` shape and ``resolve_native_auth`` for the auth
environment (askpass + keyring autofill, agent) — then hands that to mosh via
``--ssh=…`` and runs ``mosh --ssh="ssh …" <host>`` inside the VTE.

Key/agent auth (askpass passphrase autofill) works through the merged env. For a
stored *password* connection, the inner ssh prompts interactively in the
terminal during the mosh bootstrap (sshpass FIFO wiring is owned by terminal.py
and isn't applied to the wrapping mosh process yet).
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Any, Dict, List

from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
    SpawnSpec,
    SshPilotPlugin,
)


class MoshProtocolBackend(ProtocolBackend):
    protocol_id = "mosh"
    display_name = "Mosh"
    default_port = 22

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="host", label="Host", kind="text", required=True,
                      placeholder="hostname or IP address"),
            FieldSpec(key="username", label="Username", kind="text",
                      placeholder="(from ~/.ssh/config)"),
            FieldSpec(key="port", label="SSH port", kind="int", default=22),
            FieldSpec(key="keyfile", label="Key file", kind="file", group="advanced"),
            FieldSpec(key="extra_ssh_opts", label="Extra SSH options", kind="text",
                      placeholder="-o Compression=yes", group="advanced"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("host") or data.get("hostname")):
            errors.append("A host is required.")
        raw_port = data.get("port", self.default_port)
        if raw_port not in (None, ""):
            try:
                if not 0 < int(raw_port) < 65536:
                    errors.append("Port must be between 1 and 65535.")
            except (TypeError, ValueError):
                errors.append("Port must be a number.")
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        mosh = shutil.which("mosh")
        if not mosh:
            raise ProtocolError(
                "The 'mosh' program is not installed. Install it (and "
                "mosh-server on the host) to use Mosh connections.")

        data = getattr(connection, "data", None) or {}
        host = (data.get("host") or data.get("hostname")
                or getattr(connection, "hostname", "")
                or getattr(connection, "host", "")).strip()
        if not host:
            raise ProtocolError("No host configured for this connection.")

        # Reuse the single SSH command/auth path (CLAUDE.md): never hand-roll ssh.
        from ....ssh_connection_builder import (  # noqa: PLC0415
            build_native_command,
            resolve_native_auth,
        )
        try:
            auth = resolve_native_auth(connection, ctx.connection_manager, ctx.config)
        except Exception as exc:
            raise ProtocolError(str(exc)) from exc

        extra: List[str] = []
        try:
            port = int(data.get("port") or self.default_port)
        except (TypeError, ValueError):
            port = self.default_port
        if port and port != 22:
            extra += ["-p", str(port)]
        keyfile = (data.get("keyfile") or "").strip()
        if keyfile:
            extra += ["-i", os.path.expanduser(keyfile)]
        username = (data.get("username") or "").strip()
        if username:
            extra += ["-l", username]
        extra_opts = (data.get("extra_ssh_opts") or "").strip()
        if extra_opts:
            extra += shlex.split(extra_opts)
        extra += list(auth.extra_opts or [])

        try:
            ssh_argv = build_native_command(
                connection, ctx.config, command_type="ssh", extra_args=extra)
        except Exception as exc:
            raise ProtocolError(str(exc)) from exc
        # build_native_command appends the target host as the last token; mosh
        # supplies the host itself, so the --ssh value is the ssh prefix only.
        ssh_prefix = ssh_argv[:-1] if len(ssh_argv) > 1 else ssh_argv

        env = dict(os.environ)
        env.update(auth.env or {})
        argv = [mosh, "--ssh=" + shlex.join(ssh_prefix), host]
        return SpawnSpec(argv=argv, env=env)


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(MoshProtocolBackend())
