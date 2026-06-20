"""AWS SSM — open AWS Session Manager shells as a sshPilot protocol.

Many AWS environments disable inbound SSH and use
``aws ssm start-session`` instead. This plugin adds an "AWS SSM" protocol so an
SSM session opens in a normal sshPilot terminal tab. Authentication is delegated
entirely to the AWS CLI (profiles / SSO / environment) — no credentials are
stored by the plugin.

Protocol plugin: it registers a ``ProtocolBackend`` and returns a ``SpawnSpec``
whose argv the terminal runs directly (same contract as the built-in docker /
kubernetes backends). No GTK is imported.

Requires the AWS CLI v2 with the Session Manager plugin
(``session-manager-plugin``) installed and configured.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Any, Dict, List, Optional

from sshpilot.plugins.api import (
    FieldSpec,
    PluginContext,
    ProtocolBackend,
    ProtocolError,
    SpawnSpec,
    SshPilotPlugin,
)


# --- Flatpak helpers (stdlib only; plugins can't import platform_utils) ------

def _is_flatpak() -> bool:
    return bool(os.environ.get("FLATPAK_ID")) or os.path.exists("/.flatpak-info")


def _host_has(binary: str) -> bool:
    """Whether ``binary`` exists on the Flatpak host (outside the sandbox)."""
    spawn = shutil.which("flatpak-spawn")
    if not spawn:
        return False
    try:
        result = subprocess.run(
            [spawn, "--host", "which", binary],
            capture_output=True, text=True, timeout=10, check=False)
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


# --- pure logic (no GTK) ----------------------------------------------------

def build_ssm_argv(data: Dict[str, Any], aws_bin: str, *,
                   flatpak_prefix: bool = False) -> List[str]:
    """Assemble the ``aws ssm start-session`` argv from connection data.

    ``flatpak_prefix`` prepends ``flatpak-spawn --host`` so the host's AWS CLI
    (with the user's profiles/SSO) is used from inside the sandbox."""
    instance = (data.get("instance_id") or "").strip()
    if not instance:
        raise ProtocolError("An EC2 instance id (i-…) is required.")

    argv = [aws_bin, "ssm", "start-session", "--target", instance]
    profile = (data.get("profile") or "").strip()
    if profile:
        argv += ["--profile", profile]
    region = (data.get("region") or "").strip()
    if region:
        argv += ["--region", region]
    document = (data.get("document_name") or "").strip()
    if document:
        argv += ["--document-name", document]

    if flatpak_prefix:
        argv = ["flatpak-spawn", "--host", *argv]
    return argv


# --- protocol backend -------------------------------------------------------

class AwsSsmBackend(ProtocolBackend):
    protocol_id = "ssm"
    display_name = "AWS SSM"
    default_port = None

    def capabilities(self) -> frozenset:
        # Not an SSH transport — hide SFTP, port-forwarding, ssh-copy-id, etc.
        return frozenset()

    def connection_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec(key="instance_id", label="Instance ID", kind="text",
                      required=True, placeholder="i-0123456789abcdef0"),
            FieldSpec(key="profile", label="AWS profile", kind="text",
                      placeholder="default", group="advanced"),
            FieldSpec(key="region", label="Region", kind="text",
                      placeholder="eu-central-1", group="advanced"),
            FieldSpec(key="document_name", label="SSM document", kind="text",
                      placeholder="AWS-StartInteractiveCommand", group="advanced"),
        ]

    def validate(self, data: Dict[str, Any]) -> List[str]:
        if not (data.get("instance_id") or "").strip():
            return ["An instance id is required."]
        return []

    def build_spawn(self, connection: Any, ctx: PluginContext) -> SpawnSpec:
        data = getattr(connection, "data", None) or {}
        aws = shutil.which("aws")
        flatpak_prefix = False
        if not aws and _is_flatpak() and _host_has("aws"):
            aws, flatpak_prefix = "aws", True
        if not aws:
            raise ProtocolError(
                "The AWS CLI ('aws') is not installed. Install AWS CLI v2 and "
                "the Session Manager plugin to use SSM connections.")
        argv = build_ssm_argv(data, aws, flatpak_prefix=flatpak_prefix)
        return SpawnSpec(argv=argv, env=dict(os.environ))


class Plugin(SshPilotPlugin):
    def activate(self, ctx: PluginContext) -> None:
        ctx.register_protocol(AwsSsmBackend())
