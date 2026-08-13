"""Contributor-safe repository intelligence server.

Policy: repository-only, read-only, no SSH, no daemon access, no credentials,
no network, no arbitrary shell, no arbitrary filesystem access, no Git
mutation. Access is confined to a single repository root through
:class:`sshpilot.mcp._scope.RepoScope`.
"""

from .server import SERVER_NAME, SERVER_VERSION, create_server, main

__all__ = ["SERVER_NAME", "SERVER_VERSION", "create_server", "main"]
