"""``sshpilot-runtime-mcp``: privileged daemon/API integration server.

Consumes ``DaemonClient`` (from ``sshpilot.api``) exclusively: it never calls
daemon internals, never spawns SSH binaries, and never bypasses daemon
capabilities or lifecycle. Missing daemon behavior is an API gap, not something
to work around here.

Explicitly opt-in: READ may be enabled broadly, OPERATE requires explicit
opt-in, and MUTATE is disabled unless explicitly allowed (and every MUTATE tool
additionally requires a human ``confirm=True``).
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .jsonable import to_jsonable
from .policy import PermissionLevel, PolicyError, RuntimePolicy

SERVER_NAME = "sshpilot-runtime-mcp"
SERVER_VERSION = "0.1.0"


class RuntimeToolError(RuntimeError):
    """Tool invocation failed; message is safe to surface to the model."""


class RuntimeHandle:
    """Typed adapter mapping MCP tools onto a (Daemon)Client.

    The handle never exposes secrets and routes every mutation through the
    policy's MUTATE gate plus an explicit ``confirm`` argument.
    """

    def __init__(self, client: Any, policy: RuntimePolicy):
        self.client = client
        self.policy = policy

    # ---- policy plumbing -------------------------------------------------
    def _require(self, level: PermissionLevel) -> None:
        try:
            self.policy.require(level)
        except PolicyError as error:
            raise RuntimeToolError(str(error)) from error

    def _run(self, method: str, *args, **kwargs) -> dict:
        handler = getattr(self.client, method, None)
        if not callable(handler):
            raise RuntimeToolError(f"daemon client has no method: {method}")
        return to_jsonable(
            handler(*args, **kwargs),
            redact_sensitive=not self.policy.allows_content,
        )

    def _mutate(self, method: str, confirm: bool, *args, **kwargs) -> dict:
        self._require(PermissionLevel.MUTATE)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: "
                "pass confirm=True"
            )
        return self._run(method, *args, **kwargs)

    # ---- READ -----------------------------------------------------------
    def capabilities(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_capabilities")

    def daemon_status(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_daemon_status")

    def list_connections(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("list_connections")

    def get_connection(self, connection_id: str) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_connection", connection_id)

    def get_ssh_config_text(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_ssh_config_text")

    def list_sessions(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("list_sessions")

    def get_session(self, session_id: str) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_session", session_id)

    def list_sftp_services(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("list_sftp_services")

    def get_sftp_service(self, service_id: str) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_sftp_service", service_id)

    def list_interactions(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("list_interactions")

    def get_interaction(self, interaction_id: str) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_interaction", interaction_id)

    def get_operation(self, operation_id: str) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_operation", operation_id)

    def list_transfers(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("list_transfers")

    def get_transfer(self, transfer_id: str) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_transfer", transfer_id)

    def list_forwards(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("list_forwards")

    def get_forward(self, forward_id: str) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_forward", forward_id)

    def sftp_list_directory(
        self,
        connection_id: str,
        path: str,
        service_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        self._require(PermissionLevel.READ)
        from sshpilot.api.models.operations import ListDirectoryRequest

        return self._run(
            "sftp_list_directory",
            ListDirectoryRequest(
                connection_id=connection_id,
                service_id=service_id,
                path=path,
                limit=limit,
            ),
        )

    def sftp_stat(self, service_id: str, path: str, recursive: bool = False) -> dict:
        self._require(PermissionLevel.READ)
        from sshpilot.api.models.operations import SftpPathRequest

        return self._run(
            "sftp_stat",
            SftpPathRequest(service_id=service_id, path=path, recursive=recursive),
        )

    def sftp_read_file(self, service_id: str, path: str) -> dict:
        self._require(PermissionLevel.READ)
        from sshpilot.api.models.operations import SftpFileTarget, SftpReadFileRequest

        return self._run(
            "sftp_read_file",
            SftpReadFileRequest(target=SftpFileTarget.REMOTE, path=path, service_id=service_id),
        )

    # ---- OPERATE ---------------------------------------------------------
    def open_session(self, connection_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        from sshpilot.api.models.sessions import OpenSessionRequest

        return self._run("open_session", OpenSessionRequest(connection_id=connection_id))

    def close_session(self, session_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        from sshpilot.api.models.sessions import CloseSessionRequest

        return self._run("close_session", CloseSessionRequest(session_id=session_id))

    def open_sftp(self, connection_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        from sshpilot.api.models.operations import OpenSftpRequest

        return self._run("open_sftp", OpenSftpRequest(connection_id=connection_id))

    def attach_sftp(self, service_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        from sshpilot.api.models.operations import AttachSftpRequest

        return self._run("attach_sftp", AttachSftpRequest(service_id=service_id))

    def detach_sftp(self, service_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        self._run("detach_sftp", service_id)
        return {"detached": service_id}

    def close_sftp(self, service_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        from sshpilot.api.models.operations import CloseSftpRequest

        return self._run("close_sftp", CloseSftpRequest(service_id=service_id))

    def cancel_operation(self, operation_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        return self._run("cancel_operation", operation_id)

    def claim_interaction(self, interaction_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        return self._run("claim_interaction", interaction_id)

    def release_interaction(self, interaction_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        self._run("release_interaction", interaction_id)
        return {"released": interaction_id}

    def cancel_interaction(self, interaction_id: str) -> dict:
        self._require(PermissionLevel.OPERATE)
        self._run("cancel_interaction", interaction_id)
        return {"cancelled": interaction_id}

    # ---- MUTATE (confirm=True required at the tool layer) ----------------
    def sftp_create_file(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpCreateFileRequest

        self._mutate("sftp_create_file", confirm, SftpCreateFileRequest(service_id=service_id, path=path))
        return {"created": path}

    def sftp_mkdir(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpPathRequest

        self._mutate("sftp_mkdir", confirm, SftpPathRequest(service_id=service_id, path=path))
        return {"created_directory": path}

    def sftp_rmdir(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpPathRequest

        self._mutate("sftp_rmdir", confirm, SftpPathRequest(service_id=service_id, path=path))
        return {"removed_directory": path}

    def sftp_remove(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpPathRequest

        self._mutate("sftp_remove", confirm, SftpPathRequest(service_id=service_id, path=path))
        return {"removed": path}

    def sftp_rename(
        self,
        service_id: str,
        source_path: str,
        destination_path: str,
        overwrite: bool = False,
        confirm: bool = False,
    ) -> dict:
        from sshpilot.api.models.operations import SftpRenameRequest

        self._mutate(
            "sftp_rename",
            confirm,
            SftpRenameRequest(
                service_id=service_id,
                source_path=source_path,
                destination_path=destination_path,
                overwrite=overwrite,
            ),
        )
        return {"renamed": [source_path, destination_path]}

    def sftp_chmod(self, service_id: str, path: str, mode: int, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpChmodRequest

        self._mutate(
            "sftp_chmod",
            confirm,
            SftpChmodRequest(service_id=service_id, path=path, mode=mode),
        )
        return {"chmod": [path, mode]}

    def sftp_symlink(
        self,
        service_id: str,
        target_path: str,
        link_path: str,
        confirm: bool = False,
    ) -> dict:
        from sshpilot.api.models.operations import SftpSymlinkRequest

        self._mutate(
            "sftp_symlink",
            confirm,
            SftpSymlinkRequest(service_id=service_id, target_path=target_path, link_path=link_path),
        )
        return {"symlink": [target_path, link_path]}

    def cancel_transfer(self, transfer_id: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.transfers import CancelTransferRequest

        self._mutate(
            "cancel_transfer",
            confirm,
            CancelTransferRequest(transfer_id=transfer_id),
        )
        return {"cancelled_transfer": transfer_id}


#: MCP tool name -> daemon-client method it dispatches to. Used to derive the
#: daemon capability each tool needs (via the authoritative method-capability
#: map), so the server only advertises tools the connected daemon supports.
TOOL_CLIENT_METHOD = {
    "daemon_status": "get_daemon_status",
    "list_connections": "list_connections",
    "get_connection": "get_connection",
    "get_ssh_config_text": "get_ssh_config_text",
    "list_sessions": "list_sessions",
    "get_session": "get_session",
    "list_sftp_services": "list_sftp_services",
    "get_sftp_service": "get_sftp_service",
    "list_interactions": "list_interactions",
    "get_interaction": "get_interaction",
    "get_operation": "get_operation",
    "list_transfers": "list_transfers",
    "get_transfer": "get_transfer",
    "list_forwards": "list_forwards",
    "get_forward": "get_forward",
    "sftp_list_directory": "sftp_list_directory",
    "sftp_stat": "sftp_stat",
    "sftp_read_file": "sftp_read_file",
    "open_session": "open_session",
    "close_session": "close_session",
    "open_sftp": "open_sftp",
    "attach_sftp": "attach_sftp",
    "detach_sftp": "detach_sftp",
    "close_sftp": "close_sftp",
    "cancel_operation": "cancel_operation",
    "claim_interaction": "claim_interaction",
    "release_interaction": "release_interaction",
    "cancel_interaction": "cancel_interaction",
    "sftp_create_file": "sftp_create_file",
    "sftp_mkdir": "sftp_mkdir",
    "sftp_rmdir": "sftp_rmdir",
    "sftp_remove": "sftp_remove",
    "sftp_rename": "sftp_rename",
    "sftp_chmod": "sftp_chmod",
    "sftp_symlink": "sftp_symlink",
    "cancel_transfer": "cancel_transfer",
}


def _queried_capabilities(handle: RuntimeHandle) -> object:
    """Best-effort snapshot of the daemon's supported capabilities.

    Returns ``None`` when they cannot be queried (in which case every tool
    stays visible; capability is still enforced at call time by the client).
    """
    try:
        capabilities = handle.client.get_capabilities()
    except Exception:  # noqa: BLE001 - unknown daemon state is not fatal here
        return None
    return getattr(capabilities, "supported", None)


def _remove_unsupported_tools(server: Any, handle: RuntimeHandle, supported: object) -> None:
    """Hide tools whose daemon capability the connected daemon lacks."""
    if supported is None:
        return
    from sshpilot.api.method_capabilities import UNSUPPORTED_CLIENT_METHOD_CAPABILITIES

    supported_names = {
        getattr(item, "value", str(item))
        for item in supported
    }
    for tool_name, method_name in TOOL_CLIENT_METHOD.items():
        capability = UNSUPPORTED_CLIENT_METHOD_CAPABILITIES.get(method_name)
        if capability is None:
            continue
        if getattr(capability, "value", str(capability)) not in supported_names:
            server.remove_tool(tool_name)


def create_server(handle: RuntimeHandle) -> Any:
    """Build the runtime MCP server around a :class:`RuntimeHandle`.

    Tools whose required daemon capability is missing are removed, so the MCP
    surface reflects what the connected daemon can actually do (capability is
    still rechecked at call time by ``DaemonClient``).
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(
        name=SERVER_NAME,
        version=SERVER_VERSION,
        description=(
            "Privileged, opt-in SSH Pilot daemon integration. Consumes "
            "DaemonClient only; never spawns SSH binaries or reads secrets. "
            "READ is broadly enabled; OPERATE and MUTATE are explicit opt-ins. "
            "MUTATE tools require confirm=True."
        ),
    )

    supported = _queried_capabilities(handle)

    @server.tool()
    def capabilities() -> dict:
        """Return the daemon's capabilities (read-only)."""
        return handle.capabilities()

    @server.tool()
    def daemon_status() -> dict:
        """Return the daemon status (read-only)."""
        return handle.daemon_status()

    @server.tool()
    def list_connections() -> dict:
        """List saved connections (read-only)."""
        return handle.list_connections()

    @server.tool()
    def get_connection(connection_id: str) -> dict:
        """Return one saved connection by id (read-only)."""
        return handle.get_connection(connection_id)

    @server.tool()
    def get_ssh_config_text() -> dict:
        """Return the SSH config text managed by the daemon (read-only)."""
        return handle.get_ssh_config_text()

    @server.tool()
    def list_sessions() -> dict:
        """List terminal sessions (read-only)."""
        return handle.list_sessions()

    @server.tool()
    def get_session(session_id: str) -> dict:
        """Return one terminal session by id (read-only)."""
        return handle.get_session(session_id)

    @server.tool()
    def list_sftp_services() -> dict:
        """List SFTP services (read-only)."""
        return handle.list_sftp_services()

    @server.tool()
    def get_sftp_service(service_id: str) -> dict:
        """Return one SFTP service by id (read-only)."""
        return handle.get_sftp_service(service_id)

    @server.tool()
    def list_interactions() -> dict:
        """List pending daemon interactions (read-only)."""
        return handle.list_interactions()

    @server.tool()
    def get_interaction(interaction_id: str) -> dict:
        """Return one interaction by id (read-only)."""
        return handle.get_interaction(interaction_id)

    @server.tool()
    def get_operation(operation_id: str) -> dict:
        """Return one operation snapshot by id (read-only)."""
        return handle.get_operation(operation_id)

    @server.tool()
    def list_transfers() -> dict:
        """List data transfers (read-only)."""
        return handle.list_transfers()

    @server.tool()
    def get_transfer(transfer_id: str) -> dict:
        """Return one transfer by id (read-only)."""
        return handle.get_transfer(transfer_id)

    @server.tool()
    def list_forwards() -> dict:
        """List port forwards (read-only)."""
        return handle.list_forwards()

    @server.tool()
    def get_forward(forward_id: str) -> dict:
        """Return one forward by id (read-only)."""
        return handle.get_forward(forward_id)

    @server.tool()
    def sftp_list_directory(
        connection_id: str,
        path: str,
        service_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        """List a remote directory (read-only). ``path`` is the directory to list;
        ``connection_id`` is required; ``service_id`` may pin an SFTP service."""
        return handle.sftp_list_directory(connection_id, path, service_id, limit)

    @server.tool()
    def sftp_stat(service_id: str, path: str, recursive: bool = False) -> dict:
        """Stat a remote path over an SFTP service (read-only)."""
        return handle.sftp_stat(service_id, path, recursive)

    @server.tool()
    def sftp_read_file(service_id: str, path: str) -> dict:
        """Read a remote file over an SFTP service (read-only)."""
        return handle.sftp_read_file(service_id, path)

    @server.tool()
    def open_session(connection_id: str) -> dict:
        """Open a terminal session on a connection (OPERATE)."""
        return handle.open_session(connection_id)

    @server.tool()
    def close_session(session_id: str) -> dict:
        """Close a terminal session (OPERATE)."""
        return handle.close_session(session_id)

    @server.tool()
    def open_sftp(connection_id: str) -> dict:
        """Open an SFTP service on a connection (OPERATE)."""
        return handle.open_sftp(connection_id)

    @server.tool()
    def attach_sftp(service_id: str) -> dict:
        """Attach to an existing SFTP service (OPERATE)."""
        return handle.attach_sftp(service_id)

    @server.tool()
    def detach_sftp(service_id: str) -> dict:
        """Detach from an SFTP service (OPERATE)."""
        return handle.detach_sftp(service_id)

    @server.tool()
    def close_sftp(service_id: str) -> dict:
        """Close an SFTP service (OPERATE)."""
        return handle.close_sftp(service_id)

    @server.tool()
    def cancel_operation(operation_id: str) -> dict:
        """Cancel an in-flight daemon operation (OPERATE)."""
        return handle.cancel_operation(operation_id)

    @server.tool()
    def claim_interaction(interaction_id: str) -> dict:
        """Claim ownership of a pending interaction (OPERATE)."""
        return handle.claim_interaction(interaction_id)

    @server.tool()
    def release_interaction(interaction_id: str) -> dict:
        """Release a claimed interaction back to the daemon (OPERATE)."""
        return handle.release_interaction(interaction_id)

    @server.tool()
    def cancel_interaction(interaction_id: str) -> dict:
        """Cancel a pending interaction (OPERATE)."""
        return handle.cancel_interaction(interaction_id)

    @server.tool()
    def sftp_create_file(service_id: str, path: str, confirm: bool = False) -> dict:
        """Create a remote file over SFTP (MUTATE; requires confirm=True)."""
        return handle.sftp_create_file(service_id, path, confirm=confirm)

    @server.tool()
    def sftp_mkdir(service_id: str, path: str, confirm: bool = False) -> dict:
        """Create a remote directory over SFTP (MUTATE; requires confirm=True)."""
        return handle.sftp_mkdir(service_id, path, confirm=confirm)

    @server.tool()
    def sftp_rmdir(service_id: str, path: str, confirm: bool = False) -> dict:
        """Remove a remote empty directory over SFTP (MUTATE; requires confirm=True)."""
        return handle.sftp_rmdir(service_id, path, confirm=confirm)

    @server.tool()
    def sftp_remove(service_id: str, path: str, confirm: bool = False) -> dict:
        """Remove a remote file over SFTP (MUTATE; requires confirm=True)."""
        return handle.sftp_remove(service_id, path, confirm=confirm)

    @server.tool()
    def sftp_rename(
        service_id: str,
        source_path: str,
        destination_path: str,
        overwrite: bool = False,
        confirm: bool = False,
    ) -> dict:
        """Rename a remote path over SFTP (MUTATE; requires confirm=True)."""
        return handle.sftp_rename(
            service_id,
            source_path,
            destination_path,
            overwrite=overwrite,
            confirm=confirm,
        )

    @server.tool()
    def sftp_chmod(service_id: str, path: str, mode: int, confirm: bool = False) -> dict:
        """Change remote file mode over SFTP (MUTATE; requires confirm=True)."""
        return handle.sftp_chmod(service_id, path, mode, confirm=confirm)

    @server.tool()
    def sftp_symlink(
        service_id: str,
        target_path: str,
        link_path: str,
        confirm: bool = False,
    ) -> dict:
        """Create a remote symlink over SFTP (MUTATE; requires confirm=True)."""
        return handle.sftp_symlink(service_id, target_path, link_path, confirm=confirm)

    @server.tool()
    def cancel_transfer(transfer_id: str, confirm: bool = False) -> dict:
        """Cancel an in-flight data transfer (MUTATE; requires confirm=True)."""
        return handle.cancel_transfer(transfer_id, confirm=confirm)

    _remove_unsupported_tools(server, handle, supported)
    return server


def main() -> int:
    """Entry point for the ``sshpilot-runtime-mcp`` console script.

    Connects via :class:`DaemonClient` (socket path from ``SSHPILOT_MCP_SOCKET``
    or the daemon default), reads the policy from environment opt-ins, and runs
    the server over stdio.
    """
    from sshpilot.api import DaemonClient

    socket_path = os.environ.get("SSHPILOT_MCP_SOCKET")
    policy = RuntimePolicy.from_environment(os.environ)
    try:
        if socket_path:
            client = DaemonClient(socket_path=socket_path)
        else:
            client = DaemonClient()
    except Exception as error:  # noqa: BLE001 - connection failure surfaced verbatim
        print(f"sshpilot-runtime-mcp: could not connect to daemon: {error}", file=sys.stderr)
        return 1
    server = create_server(RuntimeHandle(client, policy))
    try:
        server.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())