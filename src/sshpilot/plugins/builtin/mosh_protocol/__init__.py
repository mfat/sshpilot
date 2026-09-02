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
from gettext import gettext as _
from typing import Any, Dict, List

from .._shell import command_split_diagnostic, split_command
from .._session_failure import BuiltinProtocolError
from ....api.models.sessions import PluginSessionFailureCode
from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
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
            FieldSpec(key="host", label=_("Host"), kind="text", required=True,
                      placeholder=_("hostname or IP address")),
            FieldSpec(key="username", label=_("Username"), kind="text",
                      placeholder=_("(from ~/.ssh/config)")),
            FieldSpec(key="port", label=_("SSH port"), kind="int", default=22),
            FieldSpec(key="keyfile", label=_("Key file"), kind="file", group="advanced"),
            FieldSpec(key="extra_ssh_opts", label=_("Extra SSH options"), kind="text",
                      placeholder="-o Compression=yes", group="advanced"),
            FieldSpec(key="predict", label=_("Local echo (predict)"), kind="choice",
                      default="adaptive", group="advanced",
                      choices=[("adaptive", _("Adaptive (default)")),
                               ("always", _("Always")),
                               ("never", _("Never")),
                               ("experimental", _("Experimental"))]),
            FieldSpec(key="mosh_port", label=_("UDP port / range"), kind="text",
                      placeholder="60000:60010", group="advanced"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("host") or data.get("hostname")):
            errors.append(_("A host is required."))
        raw_port = data.get("port", self.default_port)
        if raw_port not in (None, ""):
            try:
                if not 0 < int(raw_port) < 65536:
                    errors.append(_("Port must be between 1 and 65535."))
            except (TypeError, ValueError):
                errors.append(_("Port must be a number."))
        diagnostic = command_split_diagnostic(data.get("extra_ssh_opts"))
        if diagnostic:
            errors.append(
                _("{field} could not be parsed: {diagnostic}.").format(
                    field=_("Extra SSH options"), diagnostic=diagnostic
                )
            )
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        # NOTE: unlike telnet/docker/kubernetes/serial, mosh is intentionally
        # NOT routed through flatpak-spawn --host. It carries the resolved SSH
        # auth env (askpass/sshpass helpers from resolve_native_auth) whose paths
        # live inside the sandbox and can't be forwarded to a host process
        # unchanged — host-spawning mosh would break the single auth path. A
        # Flatpak that needs mosh should include it in the runtime.
        mosh = shutil.which("mosh")
        if not mosh:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.MOSH_UNAVAILABLE,
                "The 'mosh' program is not installed. Install it (and "
                "mosh-server on the host) to use Mosh connections.",
                parameters={
                    "client_program": "mosh",
                    "server_program": "mosh-server",
                },
            )

        data = getattr(connection, "data", None) or {}
        host = (data.get("host") or data.get("hostname")
                or getattr(connection, "hostname", "")
                or getattr(connection, "host", "")).strip()
        if not host:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.HOST_REQUIRED,
                "No host configured for this connection.",
            )

        # Reuse the single SSH command/auth path (docs/architecture.md): never hand-roll ssh.
        from ....ssh_connection_builder import (  # noqa: PLC0415
            build_native_command,
            resolve_native_auth,
        )
        try:
            auth = resolve_native_auth(connection, ctx.connection_manager, ctx.config)
        except Exception as exc:
            diagnostic = str(exc)
            raise BuiltinProtocolError(
                PluginSessionFailureCode.MOSH_PREPARATION_FAILED,
                diagnostic,
                diagnostic=diagnostic,
            ) from exc

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
            extra += split_command(extra_opts, "extra_ssh_opts")
        extra += list(auth.extra_opts or [])

        try:
            ssh_argv = build_native_command(
                connection, ctx.config, command_type="ssh", extra_args=extra)
        except Exception as exc:
            diagnostic = str(exc)
            raise BuiltinProtocolError(
                PluginSessionFailureCode.MOSH_PREPARATION_FAILED,
                diagnostic,
                diagnostic=diagnostic,
            ) from exc
        # build_native_command appends the target host as the last token; mosh
        # supplies the host itself, so the --ssh value is the ssh prefix only.
        ssh_prefix = ssh_argv[:-1] if len(ssh_argv) > 1 else ssh_argv

        env = dict(os.environ)
        env.update(auth.env or {})
        argv = [mosh]
        predict = (data.get("predict") or "adaptive").strip()
        if predict and predict != "adaptive":
            argv.append("--predict=" + predict)
        mosh_port = (data.get("mosh_port") or "").strip()
        if mosh_port:
            argv += ["--port", mosh_port]
        argv += ["--ssh=" + shlex.join(ssh_prefix), host]
        return SpawnSpec(argv=argv, env=env)


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(MoshProtocolBackend())
