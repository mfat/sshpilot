"""Docker / Podman exec protocol plugin.

Opens an interactive shell inside a running container — ``docker exec -it
<container> <shell>`` (or ``podman``) — in the VTE. Optionally targets a remote
daemon via ``-H`` (e.g. ``ssh://user@host``). Pure terminal seam, no in-app auth:
the chosen runtime handles its own context/credentials.
"""

from __future__ import annotations

import os
import shutil  # noqa: F401  # kept: tests patch this module's `shutil.which`
from gettext import gettext as _
from typing import Any, Dict, List, Tuple

from .._shell import command_split_diagnostic, split_command
from .._session_failure import BuiltinProtocolError
from ....api.models.sessions import PluginSessionFailureCode
from .._summary import run_lines
from ...api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    SpawnSpec,
    SshPilotPlugin,
)


_RUNTIME_NAMES = {"docker": "Docker", "podman": "Podman"}


def _runtime(values: Dict[str, Any]) -> str:
    runtime = str(values.get("runtime") or "docker")
    return runtime if runtime in _RUNTIME_NAMES else "docker"


def _target_args(values: Dict[str, Any]) -> List[str]:
    """Which daemon to talk to: a named context, or a raw daemon URL.

    Docker calls a saved endpoint a context (``--context``); Podman calls it
    a connection (``--connection``). ``-H`` works for both (Podman keeps it
    as an alias of ``--url``).
    """
    context = str(values.get("docker_context") or "").strip()
    if context:
        return ["--connection" if _runtime(values) == "podman" else "--context", context]
    host = str(values.get("docker_host") or "").strip()
    return ["-H", host] if host else []


def _containers(values: Dict[str, Any], ctx: Any) -> List[Tuple[str, str]]:
    """Containers with their state, running ones first."""
    runtime = _runtime(values)
    lines = run_lines(ctx, [runtime, *_target_args(values), "ps", "-a",
                            "--format", "{{.Names}}\t{{.Status}}"])
    rows = []
    for line in lines:
        name, _sep, status = line.partition("\t")
        rows.append((not status.startswith("Up"), name, status))
    return [(name, f"{name} — {status}" if status else name)
            for _stopped, name, status in sorted(rows)]


def _contexts(values: Dict[str, Any], ctx: Any) -> List[Tuple[str, str]]:
    if _runtime(values) == "podman":
        argv = ["podman", "system", "connection", "list", "--format", "{{.Name}}"]
    else:
        argv = ["docker", "context", "ls", "--format", "{{.Name}}"]
    return [(name, name) for name in run_lines(ctx, argv)]


class DockerProtocolBackend(ProtocolBackend):
    protocol_id = "docker"
    display_name = "Docker/Podman"
    default_port = None

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="container", label=_("Container"), kind="text", required=True,
                      placeholder=_("name or id"), suggest=_containers),
            FieldSpec(key="command", label=_("Command"), kind="text", default="sh",
                      placeholder="sh"),
            FieldSpec(key="runtime", label=_("Runtime"), kind="choice", default="docker",
                      choices=[("docker", "Docker"), ("podman", "Podman")]),
            FieldSpec(key="docker_context", label=_("Context"), kind="text",
                      placeholder=_("(default)"), group="advanced", suggest=_contexts),
            FieldSpec(key="docker_host", label=_("Daemon host"), kind="text",
                      placeholder="ssh://user@host or tcp://host:2375", group="advanced"),
            FieldSpec(key="user", label=_("User"), kind="text",
                      placeholder=_("user or UID"), group="advanced"),
            FieldSpec(key="workdir", label=_("Working directory"), kind="text",
                      placeholder="/path/in/container", group="advanced"),
        ]

    def summary(self, data: Dict[str, Any]) -> str:
        container = str(data.get("container") or "").strip()
        if not container:
            return ""
        where = (str(data.get("docker_context") or "").strip()
                 or str(data.get("docker_host") or "").strip())
        text = f"{container} · {_RUNTIME_NAMES[_runtime(data)]}"
        return f"{text} · {where}" if where else text

    def validate(self, data: Dict[str, Any]) -> List[str]:
        errors: List[str] = []
        if not (data.get("container") or "").strip():
            errors.append(_("A container name or id is required."))
        runtime = (data.get("runtime") or "docker")
        if runtime not in ("docker", "podman"):
            errors.append(_("Runtime must be docker or podman."))
        if (str(data.get("docker_context") or "").strip()
                and str(data.get("docker_host") or "").strip()):
            errors.append(_("Set either a context or a daemon host, not both."))
        diagnostic = command_split_diagnostic(data.get("command"))
        if diagnostic:
            errors.append(
                _("{field} could not be parsed: {diagnostic}.").format(
                    field=_("Command"), diagnostic=diagnostic
                )
            )
        return errors

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        container = (data.get("container") or "").strip()
        if not container:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.CONTAINER_REQUIRED,
                "No container configured for this connection.",
            )
        runtime = (data.get("runtime") or "docker")
        if runtime not in ("docker", "podman"):
            runtime = "docker"
        from .._flatpak import resolve_host_binary  # noqa: PLC0415
        binary_argv = resolve_host_binary(runtime)
        if binary_argv is None:
            raise BuiltinProtocolError(
                PluginSessionFailureCode.CONTAINER_RUNTIME_UNAVAILABLE,
                f"The '{runtime}' program is not installed. Install it to use "
                "container connections.",
                parameters={"runtime": runtime},
            )

        command = (data.get("command") or "sh").strip() or "sh"
        argv = list(binary_argv)
        argv += _target_args(data)
        argv += ["exec", "-it"]
        user = (data.get("user") or "").strip()
        if user:
            argv += ["-u", user]
        workdir = (data.get("workdir") or "").strip()
        if workdir:
            argv += ["-w", workdir]
        argv.append(container)
        argv += split_command(command, "command")
        return SpawnSpec(argv=argv, env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(DockerProtocolBackend())
