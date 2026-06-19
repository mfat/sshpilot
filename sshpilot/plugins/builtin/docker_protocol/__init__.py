"""Docker / Podman exec protocol plugin.

Opens an interactive shell inside a running container — ``docker exec -it
<container> <shell>`` (or ``podman``) — in the VTE. Optionally targets a remote
daemon via ``-H`` (e.g. ``ssh://user@host``). Pure terminal seam, no in-app auth:
the chosen runtime handles its own context/credentials.
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


class DockerProtocolBackend(ProtocolBackend):
    protocol_id = "docker"
    display_name = "Docker/Podman"
    default_port = None

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="container", label="Container", kind="text", required=True,
                      placeholder="name or id"),
            FieldSpec(key="command", label="Command", kind="text", default="sh",
                      placeholder="sh"),
            FieldSpec(key="runtime", label="Runtime", kind="choice", default="docker",
                      choices=[("docker", "Docker"), ("podman", "Podman")]),
            FieldSpec(key="docker_host", label="Daemon host", kind="text",
                      placeholder="ssh://user@host or tcp://host:2375", group="advanced"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("container") or "").strip():
            errors.append("A container name or id is required.")
        runtime = (data.get("runtime") or "docker")
        if runtime not in ("docker", "podman"):
            errors.append("Runtime must be docker or podman.")
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        container = (data.get("container") or "").strip()
        if not container:
            raise ProtocolError("No container configured for this connection.")
        runtime = (data.get("runtime") or "docker")
        if runtime not in ("docker", "podman"):
            runtime = "docker"
        binary = shutil.which(runtime)
        if not binary:
            raise ProtocolError(
                f"The '{runtime}' program is not installed. Install it to use "
                f"container connections.")

        command = (data.get("command") or "sh").strip() or "sh"
        host = (data.get("docker_host") or "").strip()
        argv = [binary]
        if host:
            argv += ["-H", host]
        argv += ["exec", "-it", container]
        argv += shlex.split(command)
        return SpawnSpec(argv=argv, env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(DockerProtocolBackend())
