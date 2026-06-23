"""Example sshPilot plugin (template).

A minimal protocol backend that runs a configurable local command in the
terminal — just enough to show the shape of a plugin end to end (manifest,
declarative fields, validation, spawn, registration). Copy this directory,
rename it and the ``id`` in ``plugin.json``, then replace the backend with your
own protocol or UI page.

See ../writing-plugins.md for the full API.
"""

from __future__ import annotations

import os
import shlex
import shutil
from typing import Any, Dict, List

from sshpilot.plugins.api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
    SpawnSpec,
    SshPilotPlugin,
)


class ExampleProtocolBackend(ProtocolBackend):
    protocol_id = "example"
    display_name = "Example"
    default_port = None

    def capabilities(self) -> frozenset:
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="command", label="Command", kind="text",
                      default="sh", placeholder="sh", required=True),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        if not (data.get("command") or "").strip():
            return ["A command is required."]
        return []

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        parts = shlex.split((data.get("command") or "sh").strip() or "sh")
        binary = shutil.which(parts[0]) if parts else None
        if not binary:
            raise ProtocolError(
                f"Command not found: {parts[0] if parts else '(empty)'}")
        return SpawnSpec(argv=[binary, *parts[1:]], env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(ExampleProtocolBackend())
