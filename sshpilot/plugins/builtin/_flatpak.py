"""Shared helper for built-in protocol backends: resolve an external binary,
falling back to the Flatpak host when it isn't available inside the sandbox.

Built-ins ship in-app, so they may import app modules — but this keeps the
Flatpak detection in one place so telnet/docker/kubernetes/serial stay tidy.
"""

from __future__ import annotations

from sshpilot.platform_utils import is_flatpak, resolve_host_binary

__all__ = ["is_flatpak", "resolve_host_binary"]
