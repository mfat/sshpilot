"""``sshpilot-mcp-runtime``: privileged daemon/API integration server.

Consumes ``DaemonClient`` (from ``sshpilot.api``) exclusively: it never calls
daemon internals, never spawns SSH binaries, and never bypasses daemon
capabilities or lifecycle. Missing daemon behavior is an API gap, not something
to work around here.

Explicitly opt-in: READ may be enabled broadly, OPERATE requires explicit
opt-in, and MUTATE is disabled unless explicitly allowed (and every MUTATE tool
additionally requires a human ``confirm=True``).
"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from contextlib import contextmanager
from typing import Any, Iterator, Optional

from .access import AssistantAccessError, AssistantAccessGate
from .jsonable import to_jsonable
from .policy import PermissionLevel, PolicyError, RuntimePolicy

SERVER_NAME = "sshpilot-mcp-runtime"
SERVER_VERSION = "0.2.0"

# Tools that remain available when the master assistant-access switch is off.
ALWAYS_AVAILABLE_TOOLS = frozenset({"assistant_status", "capabilities"})


try:
    from mcp.server.mcpserver.exceptions import ToolError as _ToolErrorBase
except ImportError:  # optional dependency; the handle stays usable without it
    _ToolErrorBase = RuntimeError


class RuntimeToolError(_ToolErrorBase):
    """Tool invocation failed; message is safe to surface to the model.

    Derives from MCP's ``ToolError`` when available: since mcp 2.2 any other
    exception reaches the model only as ``Error executing tool <name>``.
    """


class RuntimeHandle:
    """Typed adapter mapping MCP tools onto a (Daemon)Client.

    The handle never exposes secrets and routes every mutation through the
    policy's MUTATE gate plus an explicit ``confirm`` argument. Daemon
    assistant-access settings are the ceiling over the environment policy.
    """

    def __init__(self, client: Any, policy: RuntimePolicy):
        self.client = client
        self.env_policy = policy
        self.access = AssistantAccessGate(client, policy)

    def _policy(self) -> RuntimePolicy:
        """Effective policy (env ∩ daemon ceiling)."""
        return self.access.effective_policy()

    # ---- policy plumbing -------------------------------------------------
    def _require(self, level: PermissionLevel, *, connection_id: str | None = None) -> None:
        try:
            self.access.require_enabled()
            if connection_id is not None:
                self.access.require_connection(connection_id)
            self._policy().require(level)
        except (PolicyError, AssistantAccessError) as error:
            raise RuntimeToolError(str(error)) from error

    def _record_activity(
        self,
        *,
        tool: str,
        state: str,
        summary: str,
        activity_id: Optional[str] = None,
        connection_id: Optional[str] = None,
        detail: str = "",
        error: str = "",
        level: str = "operate",
    ) -> Optional[str]:
        recorder = getattr(self.client, "record_assistant_activity", None)
        if not callable(recorder):
            return activity_id
        try:
            from sshpilot.api.models.assistant_activity import (
                AssistantActivityState,
                RecordAssistantActivityRequest,
            )

            entry = recorder(
                RecordAssistantActivityRequest(
                    tool=tool,
                    state=AssistantActivityState(state),
                    summary=summary[:500],
                    activity_id=activity_id,
                    connection_id=connection_id,
                    detail=detail[:2000],
                    error=error[:1000],
                    level=level,
                )
            )
            return str(getattr(entry, "activity_id", activity_id) or activity_id or "")
        except Exception:  # noqa: BLE001 - UI activity must never break tools
            return activity_id

    @contextmanager
    def _track(
        self,
        tool: str,
        summary: str,
        *,
        connection_id: Optional[str] = None,
        detail: str = "",
        level: str = "operate",
    ) -> Iterator[None]:
        activity_id = self._record_activity(
            tool=tool,
            state="started",
            summary=summary,
            activity_id=f"asst-{uuid.uuid4().hex[:12]}",
            connection_id=connection_id,
            detail=detail,
            level=level,
        )
        try:
            yield
        except Exception as error:
            self._record_activity(
                tool=tool,
                state="failed",
                summary=summary,
                activity_id=activity_id,
                connection_id=connection_id,
                detail=detail,
                error=str(error),
                level=level,
            )
            raise
        else:
            self._record_activity(
                tool=tool,
                state="succeeded",
                summary=summary,
                activity_id=activity_id,
                connection_id=connection_id,
                detail=detail,
                level=level,
            )

    def _run(self, method: str, *args, **kwargs) -> dict:
        handler = getattr(self.client, method, None)
        if not callable(handler):
            raise RuntimeToolError(f"daemon client has no method: {method}")
        return to_jsonable(
            handler(*args, **kwargs),
            redact_sensitive=not self._policy().allows_content,
        )

    def _mutate(self, method: str, confirm: bool, *args, **kwargs) -> dict:
        self._require(PermissionLevel.MUTATE)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: "
                "pass confirm=True"
            )
        connection_id = None
        if args and isinstance(args[0], str):
            connection_id = args[0]
        with self._track(
            method,
            f"{method}",
            connection_id=connection_id,
            level="mutate",
        ):
            return self._run(method, *args, **kwargs)

    def _connection_id_for_session(self, session_id: str) -> str:
        summary = self.client.get_session(session_id)
        connection_id = str(getattr(summary, "connection_id", "") or "")
        if not connection_id:
            raise RuntimeToolError(f"session {session_id!r} has no connection id")
        return connection_id

    def _connection_id_for_sftp(self, service_id: str) -> str:
        summary = self.client.get_sftp_service(service_id)
        connection_id = str(getattr(summary, "connection_id", "") or "")
        if not connection_id:
            raise RuntimeToolError(f"SFTP service {service_id!r} has no connection id")
        return connection_id

    def _connection_id_for_transfer(self, transfer_id: str) -> str:
        summary = self.client.get_transfer(transfer_id)
        connection_id = str(getattr(summary, "connection_id", "") or "")
        if not connection_id:
            raise RuntimeToolError(f"transfer {transfer_id!r} has no connection id")
        return connection_id

    def _connection_id_for_forward(self, forward_id: str) -> str:
        summary = self.client.get_forward(forward_id)
        connection_id = str(getattr(summary, "connection_id", "") or "")
        if not connection_id:
            raise RuntimeToolError(f"forward {forward_id!r} has no connection id")
        return connection_id

    def _filter_by_connection(self, items: Any) -> list:
        allowed = self.access.allowed_ids()
        if not isinstance(items, (list, tuple)):
            return []
        if allowed is None:
            return list(items)
        return [
            item
            for item in items
            if str(getattr(item, "connection_id", "")) in allowed
            or str(getattr(item, "id", "")) in allowed
        ]

    # ---- status (always available) --------------------------------------
    def assistant_status(self) -> dict:
        """Return the effective assistant-access policy (no master switch required)."""
        return self.access.status_dict()

    # ---- READ -----------------------------------------------------------
    def capabilities(self) -> dict:
        # Capability discovery stays available so clients can see the surface
        # even before the master switch is enabled.
        try:
            return self._run("get_capabilities")
        except Exception as error:  # noqa: BLE001
            raise RuntimeToolError(str(error)) from error

    def daemon_status(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_daemon_status")

    def list_connections(self) -> dict:
        self._require(PermissionLevel.READ)
        raw = self.client.list_connections()
        filtered = self.access.filter_connection_summaries(raw)
        return to_jsonable(
            list(filtered),
            redact_sensitive=not self._policy().allows_content,
        )

    def get_connection(self, connection_id: str) -> dict:
        self._require(PermissionLevel.READ, connection_id=connection_id)
        return self._run("get_connection", connection_id)

    def get_ssh_config_text(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_ssh_config_text")

    def list_sessions(self) -> dict:
        self._require(PermissionLevel.READ)
        raw = self.client.list_sessions()
        return to_jsonable(
            self._filter_by_connection(raw),
            redact_sensitive=not self._policy().allows_content,
        )

    def get_session(self, session_id: str) -> dict:
        connection_id = self._connection_id_for_session(session_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        return self._run("get_session", session_id)

    def list_sftp_services(self) -> dict:
        self._require(PermissionLevel.READ)
        raw = self.client.list_sftp_services()
        return to_jsonable(
            self._filter_by_connection(raw),
            redact_sensitive=not self._policy().allows_content,
        )

    def get_sftp_service(self, service_id: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
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
        raw = self.client.list_transfers()
        return to_jsonable(
            self._filter_by_connection(raw),
            redact_sensitive=not self._policy().allows_content,
        )

    def get_transfer(self, transfer_id: str) -> dict:
        connection_id = self._connection_id_for_transfer(transfer_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        return self._run("get_transfer", transfer_id)

    def list_forwards(self) -> dict:
        self._require(PermissionLevel.READ)
        raw = self.client.list_forwards()
        return to_jsonable(
            self._filter_by_connection(raw),
            redact_sensitive=not self._policy().allows_content,
        )

    def get_forward(self, forward_id: str) -> dict:
        connection_id = self._connection_id_for_forward(forward_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        return self._run("get_forward", forward_id)

    def sftp_list_directory(
        self,
        connection_id: str,
        path: str,
        service_id: str | None = None,
        limit: int | None = None,
    ) -> dict:
        self._require(PermissionLevel.READ, connection_id=connection_id)
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
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        from sshpilot.api.models.operations import SftpPathRequest

        return self._run(
            "sftp_stat",
            SftpPathRequest(service_id=service_id, path=path, recursive=recursive),
        )

    def sftp_read_file(self, service_id: str, path: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        from sshpilot.api.models.operations import SftpFileTarget, SftpReadFileRequest

        return self._run(
            "sftp_read_file",
            SftpReadFileRequest(target=SftpFileTarget.REMOTE, path=path, service_id=service_id),
        )

    def sftp_lstat(self, service_id: str, path: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        from sshpilot.api.models.operations import SftpPathRequest

        return self._run("sftp_lstat", SftpPathRequest(service_id=service_id, path=path))

    def sftp_realpath(self, service_id: str, path: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        from sshpilot.api.models.operations import SftpPathRequest

        return {"path": self.client.sftp_realpath(SftpPathRequest(service_id=service_id, path=path))}

    def sftp_filesystem_usage(self, service_id: str, path: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        from sshpilot.api.models.operations import SftpPathRequest

        return self._run(
            "sftp_filesystem_usage",
            SftpPathRequest(service_id=service_id, path=path),
        )

    def sftp_readlink(self, service_id: str, path: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.READ, connection_id=connection_id)
        from sshpilot.api.models.operations import SftpPathRequest

        return {"target": self.client.sftp_readlink(SftpPathRequest(service_id=service_id, path=path))}

    def list_keys(self) -> dict:
        self._require(PermissionLevel.READ)
        from sshpilot.api.models.keys import ListKeysRequest

        return self._run("list_keys", ListKeysRequest())

    def read_public_key(self, key_id: str) -> dict:
        self._require(PermissionLevel.READ)
        from sshpilot.api.models.keys import ReadPublicKeyRequest

        return self._run("read_public_key", ReadPublicKeyRequest(key_id=key_id))

    def list_known_hosts(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("list_known_hosts")

    def get_identity_state(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_identity_state")

    def get_secret_state(self) -> dict:
        self._require(PermissionLevel.READ)
        return self._run("get_secret_state")

    def has_connection_password(self, connection_id: str) -> dict:
        self._require(PermissionLevel.READ, connection_id=connection_id)
        return {"has_password": bool(self.client.has_connection_password(connection_id))}

    # ---- OPERATE ---------------------------------------------------------
    def open_session(self, connection_id: str) -> dict:
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.sessions import OpenSessionRequest

        return self._run("open_session", OpenSessionRequest(connection_id=connection_id))

    def close_session(self, session_id: str) -> dict:
        connection_id = self._connection_id_for_session(session_id)
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.sessions import CloseSessionRequest

        return self._run("close_session", CloseSessionRequest(session_id=session_id))

    def open_sftp(self, connection_id: str) -> dict:
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.operations import OpenSftpRequest

        return self._run("open_sftp", OpenSftpRequest(connection_id=connection_id))

    def attach_sftp(self, service_id: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.operations import AttachSftpRequest

        return self._run("attach_sftp", AttachSftpRequest(service_id=service_id))

    def detach_sftp(self, service_id: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        self._run("detach_sftp", service_id)
        return {"detached": service_id}

    def close_sftp(self, service_id: str) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
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

    def run_command(
        self,
        connection_id: str,
        command: str,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Run one remote command via broadcast and wait for completion."""
        import time

        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.broadcast import (
            BroadcastCommandRequest,
            BroadcastExecutionPolicy,
            HostCommandState,
        )

        if timeout_seconds is None or float(timeout_seconds) <= 0:
            raise RuntimeToolError("timeout_seconds must be positive")
        with self._track(
            "run_command",
            command.strip()[:200],
            connection_id=connection_id,
            detail=command,
            level="operate",
        ):
            policy = BroadcastExecutionPolicy(
                timeout_seconds=float(timeout_seconds),
                capture_stdout=True,
                capture_stderr=True,
            )
            summary = self.client.start_broadcast_command(
                BroadcastCommandRequest(
                    connection_ids=(connection_id,),
                    command=command,
                    policy=policy,
                )
            )
            deadline = time.monotonic() + float(timeout_seconds) + 5.0
            current = summary
            while time.monotonic() < deadline:
                op = getattr(current, "operation", None)
                state = getattr(op, "state", None)
                state_value = getattr(state, "value", state)
                if state_value in {"succeeded", "failed", "cancelled", "completed"}:
                    break
                targets = getattr(current, "targets", ()) or ()
                if targets and all(
                    getattr(getattr(t, "state", None), "value", getattr(t, "state", None))
                    in {
                        HostCommandState.SUCCEEDED.value,
                        HostCommandState.FAILED.value,
                        HostCommandState.CANCELLED.value,
                        "succeeded",
                        "failed",
                        "cancelled",
                    }
                    for t in targets
                ):
                    break
                time.sleep(0.2)
                op_id = getattr(op, "operation_id", None)
                if op_id is None:
                    break
                current = self.client.get_broadcast_command(op_id)
            return to_jsonable(
                current, redact_sensitive=not self._policy().allows_content
            )

    def host_info(self, connection_id: str, timeout_seconds: float = 60.0) -> dict:
        """Run a host-info probe and wait for the parsed dashboard snapshot."""
        import time

        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.host_info import HostInfoRequest

        if timeout_seconds is None or float(timeout_seconds) <= 0:
            raise RuntimeToolError("timeout_seconds must be positive")
        with self._track(
            "host_info",
            "host info probe",
            connection_id=connection_id,
            level="operate",
        ):
            summary = self.client.start_host_info(
                HostInfoRequest(connection_id=connection_id)
            )
            deadline = time.monotonic() + float(timeout_seconds) + 5.0
            current = summary
            while time.monotonic() < deadline:
                op = getattr(current, "operation", None)
                state = getattr(op, "state", None)
                state_value = getattr(state, "value", state)
                if state_value in {"succeeded", "failed", "cancelled", "completed"}:
                    break
                if getattr(current, "snapshot", None) is not None and state_value in {
                    "succeeded",
                    "completed",
                }:
                    break
                time.sleep(0.2)
                op_id = getattr(op, "operation_id", None)
                if op_id is None:
                    break
                current = self.client.get_host_info(op_id)
            return to_jsonable(
                current, redact_sensitive=not self._policy().allows_content
            )

    def replay_terminal(
        self,
        session_id: str,
        after_sequence: int | None = None,
        max_bytes: int = 65536,
    ) -> dict:
        connection_id = self._connection_id_for_session(session_id)
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.terminal import ReplayRequest

        return self._run(
            "replay_terminal",
            ReplayRequest(
                session_id=session_id,
                after_sequence=after_sequence,
                max_bytes=max_bytes,
            ),
        )

    def open_forward(
        self,
        connection_id: str,
        forward_type: str,
        bind_host: str,
        bind_port: int,
        destination_host: str | None = None,
        destination_port: int | None = None,
    ) -> dict:
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.operations import ForwardType, OpenForwardRequest

        return self._run(
            "open_forward",
            OpenForwardRequest(
                connection_id=connection_id,
                type=ForwardType(forward_type),
                bind_host=bind_host,
                bind_port=bind_port,
                destination_host=destination_host,
                destination_port=destination_port,
            ),
        )

    def close_forward(self, forward_id: str) -> dict:
        connection_id = self._connection_id_for_forward(forward_id)
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.operations import CloseForwardRequest

        self._run("close_forward", CloseForwardRequest(forward_id=forward_id))
        return {"closed_forward": forward_id}

    def start_transfer(
        self,
        connection_id: str,
        sftp_service_id: str,
        direction: str,
        remote_path: str,
        local_path: str,
        recursive: bool = False,
    ) -> dict:
        self._require(PermissionLevel.OPERATE, connection_id=connection_id)
        from sshpilot.api.models.transfers import StartTransferRequest, TransferDirection

        return self._run(
            "start_transfer",
            StartTransferRequest(
                connection_id=connection_id,
                sftp_service_id=sftp_service_id,
                direction=TransferDirection(direction),
                remote_path=remote_path,
                local_path=local_path,
                recursive=recursive,
            ),
        )

    # ---- MUTATE (confirm=True required at the tool layer) ----------------
    def sftp_create_file(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpCreateFileRequest

        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run(
            "sftp_create_file",
            SftpCreateFileRequest(service_id=service_id, path=path),
        )
        return {"created": path}

    def sftp_mkdir(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpPathRequest

        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run("sftp_mkdir", SftpPathRequest(service_id=service_id, path=path))
        return {"created_directory": path}

    def sftp_rmdir(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpPathRequest

        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run("sftp_rmdir", SftpPathRequest(service_id=service_id, path=path))
        return {"removed_directory": path}

    def sftp_remove(self, service_id: str, path: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.operations import SftpPathRequest

        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run("sftp_remove", SftpPathRequest(service_id=service_id, path=path))
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

        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run(
            "sftp_rename",
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

        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run(
            "sftp_chmod",
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

        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run(
            "sftp_symlink",
            SftpSymlinkRequest(service_id=service_id, target_path=target_path, link_path=link_path),
        )
        return {"symlink": [target_path, link_path]}

    def sftp_replace_file(
        self,
        service_id: str,
        path: str,
        content: str,
        expected_revision: str,
        confirm: bool = False,
    ) -> dict:
        connection_id = self._connection_id_for_sftp(service_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        from sshpilot.api.models.operations import SftpFileTarget, SftpReplaceFileRequest

        return self._run(
            "sftp_replace_file",
            SftpReplaceFileRequest(
                target=SftpFileTarget.REMOTE,
                path=path,
                content=content,
                expected_revision=expected_revision,
                service_id=service_id,
            ),
        )

    def cancel_transfer(self, transfer_id: str, confirm: bool = False) -> dict:
        from sshpilot.api.models.transfers import CancelTransferRequest

        connection_id = self._connection_id_for_transfer(transfer_id)
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        self._run(
            "cancel_transfer",
            CancelTransferRequest(transfer_id=transfer_id),
        )
        return {"cancelled_transfer": transfer_id}

    def create_connection(
        self,
        nickname: str,
        hostname: str,
        username: str,
        port: int = 22,
        confirm: bool = False,
    ) -> dict:
        self._require(PermissionLevel.MUTATE)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        from sshpilot.api.models.connections import CreateConnectionRequest

        return self._run(
            "create_connection",
            CreateConnectionRequest(
                nickname=nickname,
                hostname=hostname,
                username=username,
                port=port,
            ),
        )

    def delete_connection(self, connection_id: str, confirm: bool = False) -> dict:
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        from sshpilot.api.models.connections import DeleteConnectionRequest

        return self._run(
            "delete_connection",
            DeleteConnectionRequest(connection_id=connection_id),
        )

    def generate_key(
        self,
        name: str,
        key_type: str = "ed25519",
        comment: str = "",
        confirm: bool = False,
    ) -> dict:
        self._require(PermissionLevel.MUTATE)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        from sshpilot.api.models.keys import GenerateKeyRequest

        return self._run(
            "generate_key",
            GenerateKeyRequest(name=name, key_type=key_type, comment=comment),
        )

    def deploy_key(self, connection_id: str, key_id: str, confirm: bool = False) -> dict:
        self._require(PermissionLevel.MUTATE, connection_id=connection_id)
        if not confirm:
            raise RuntimeToolError(
                "MUTATE operations require explicit human confirmation: pass confirm=True"
            )
        from sshpilot.api.models.identity import DeployKeyRequest

        return self._run(
            "deploy_key",
            DeployKeyRequest(connection_id=connection_id, key_id=key_id),
        )


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
    "sftp_lstat": "sftp_lstat",
    "sftp_realpath": "sftp_realpath",
    "sftp_filesystem_usage": "sftp_filesystem_usage",
    "sftp_readlink": "sftp_readlink",
    "list_keys": "list_keys",
    "read_public_key": "read_public_key",
    "list_known_hosts": "list_known_hosts",
    "get_identity_state": "get_identity_state",
    "get_secret_state": "get_secret_state",
    "has_connection_password": "has_connection_password",
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
    "run_command": "start_broadcast_command",
    "host_info": "start_host_info",
    "replay_terminal": "replay_terminal",
    "open_forward": "open_forward",
    "close_forward": "close_forward",
    "start_transfer": "start_transfer",
    "sftp_create_file": "sftp_create_file",
    "sftp_mkdir": "sftp_mkdir",
    "sftp_rmdir": "sftp_rmdir",
    "sftp_remove": "sftp_remove",
    "sftp_rename": "sftp_rename",
    "sftp_chmod": "sftp_chmod",
    "sftp_symlink": "sftp_symlink",
    "sftp_replace_file": "sftp_replace_file",
    "cancel_transfer": "cancel_transfer",
    "create_connection": "create_connection",
    "delete_connection": "delete_connection",
    "generate_key": "generate_key",
    "deploy_key": "deploy_key",
}

#: Client methods that must never appear as MCP tools (secret / auth surface).
FORBIDDEN_CLIENT_METHODS = frozenset(
    {
        "reveal_connection_password",
        "reveal_key_passphrase",
        "send_interaction_secret",
        "get_plugin_secret",
        "store_connection_password",
        "delete_connection_password",
        "store_key_passphrase",
        "delete_key_passphrase",
        "respond_to_interaction",
        "stop_daemon",
        "restart_daemon",
        "unlock_secrets",
        "bitwarden_login",
        "bitwarden_unlock",
        "keepassxc_unlock",
    }
)


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
    def assistant_status() -> dict:
        """Return the AI assistant access policy and effective permissions."""
        return handle.assistant_status()

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
        """List allowlisted connections (read-only)."""
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
    def sftp_lstat(service_id: str, path: str) -> dict:
        """lstat a remote path over an SFTP service (read-only)."""
        return handle.sftp_lstat(service_id, path)

    @server.tool()
    def sftp_realpath(service_id: str, path: str) -> dict:
        """Resolve a remote path over an SFTP service (read-only)."""
        return handle.sftp_realpath(service_id, path)

    @server.tool()
    def sftp_filesystem_usage(service_id: str, path: str) -> dict:
        """Return remote filesystem usage for a path (read-only)."""
        return handle.sftp_filesystem_usage(service_id, path)

    @server.tool()
    def sftp_readlink(service_id: str, path: str) -> dict:
        """Read a remote symlink target (read-only)."""
        return handle.sftp_readlink(service_id, path)

    @server.tool()
    def list_keys() -> dict:
        """List daemon-known SSH keys (public metadata only)."""
        return handle.list_keys()

    @server.tool()
    def read_public_key(key_id: str) -> dict:
        """Read one public key by id (never private key material)."""
        return handle.read_public_key(key_id)

    @server.tool()
    def list_known_hosts() -> dict:
        """List known_hosts entries (read-only)."""
        return handle.list_known_hosts()

    @server.tool()
    def get_identity_state() -> dict:
        """Return identity/agent state (read-only)."""
        return handle.get_identity_state()

    @server.tool()
    def get_secret_state() -> dict:
        """Return secret-backend lock state (never secret values)."""
        return handle.get_secret_state()

    @server.tool()
    def has_connection_password(connection_id: str) -> dict:
        """Whether a password is stored for a connection (never the password)."""
        return handle.has_connection_password(connection_id)

    @server.tool()
    def open_session(connection_id: str) -> dict:
        """Open a terminal/session on a connection (SSH/RDP/mosh/serial)."""
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
    def run_command(
        connection_id: str,
        command: str,
        timeout_seconds: float = 60.0,
    ) -> dict:
        """Run one remote command and wait for stdout/stderr (OPERATE)."""
        return handle.run_command(connection_id, command, timeout_seconds)

    @server.tool()
    def host_info(connection_id: str, timeout_seconds: float = 60.0) -> dict:
        """Probe host CPU/memory/disk/network (OPERATE)."""
        return handle.host_info(connection_id, timeout_seconds)

    @server.tool()
    def replay_terminal(
        session_id: str,
        after_sequence: int | None = None,
        max_bytes: int = 65536,
    ) -> dict:
        """Replay buffered terminal output for a session (OPERATE)."""
        return handle.replay_terminal(session_id, after_sequence, max_bytes)

    @server.tool()
    def open_forward(
        connection_id: str,
        forward_type: str,
        bind_host: str,
        bind_port: int,
        destination_host: str | None = None,
        destination_port: int | None = None,
    ) -> dict:
        """Open a local/remote/dynamic port forward (OPERATE)."""
        return handle.open_forward(
            connection_id,
            forward_type,
            bind_host,
            bind_port,
            destination_host,
            destination_port,
        )

    @server.tool()
    def close_forward(forward_id: str) -> dict:
        """Close a port forward (OPERATE)."""
        return handle.close_forward(forward_id)

    @server.tool()
    def start_transfer(
        connection_id: str,
        sftp_service_id: str,
        direction: str,
        remote_path: str,
        local_path: str,
        recursive: bool = False,
    ) -> dict:
        """Start an SFTP upload or download (OPERATE)."""
        return handle.start_transfer(
            connection_id,
            sftp_service_id,
            direction,
            remote_path,
            local_path,
            recursive,
        )

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

    @server.tool()
    def sftp_replace_file(
        service_id: str,
        path: str,
        content: str,
        expected_revision: str,
        confirm: bool = False,
    ) -> dict:
        """Replace a remote file (MUTATE; requires confirm=True)."""
        return handle.sftp_replace_file(
            service_id,
            path,
            content,
            expected_revision,
            confirm=confirm,
        )

    @server.tool()
    def create_connection(
        nickname: str,
        hostname: str,
        username: str,
        port: int = 22,
        confirm: bool = False,
    ) -> dict:
        """Create a saved connection (MUTATE; requires confirm=True)."""
        return handle.create_connection(
            nickname, hostname, username, port, confirm=confirm
        )

    @server.tool()
    def delete_connection(connection_id: str, confirm: bool = False) -> dict:
        """Delete a saved connection (MUTATE; requires confirm=True)."""
        return handle.delete_connection(connection_id, confirm=confirm)

    @server.tool()
    def generate_key(
        name: str,
        key_type: str = "ed25519",
        comment: str = "",
        confirm: bool = False,
    ) -> dict:
        """Generate a new SSH key (MUTATE; requires confirm=True)."""
        return handle.generate_key(name, key_type, comment, confirm=confirm)

    @server.tool()
    def deploy_key(connection_id: str, key_id: str, confirm: bool = False) -> dict:
        """Deploy a public key to a host (MUTATE; requires confirm=True)."""
        return handle.deploy_key(connection_id, key_id, confirm=confirm)

    # Refuse to advertise forbidden secret/auth tools if they ever appear.
    for forbidden in FORBIDDEN_CLIENT_METHODS:
        if forbidden in TOOL_CLIENT_METHOD:
            raise RuntimeError(f"forbidden client method mapped as MCP tool: {forbidden}")
        try:
            server.remove_tool(forbidden)
        except Exception:  # noqa: BLE001 - tool may not exist
            pass

    _remove_unsupported_tools(server, handle, supported)
    return server


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="sshpilot-mcp-runtime")
    parser.add_argument(
        "--transport",
        choices=("stdio", "streamable-http", "sse"),
        default=os.environ.get("SSHPILOT_MCP_TRANSPORT", "stdio"),
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SSHPILOT_MCP_HTTP_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SSHPILOT_MCP_HTTP_PORT", "8000")),
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("SSHPILOT_MCP_HTTP_TOKEN", ""),
        help="Bearer token required for Streamable HTTP (localhost sidecar).",
    )
    return parser.parse_args(argv)


def _bearer_middleware(app: Any, token: str) -> Any:
    """ASGI middleware that requires ``Authorization: Bearer <token>``."""

    async def application(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope.get("type") == "http":
            headers = {
                key.decode("latin1").lower(): value.decode("latin1")
                for key, value in scope.get("headers") or ()
            }
            expected = f"Bearer {token}"
            if headers.get("authorization") != expected:
                body = b'{"error":"unauthorized"}'
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"content-type", b"application/json"),
                            (b"content-length", str(len(body)).encode("ascii")),
                            (b"www-authenticate", b"Bearer"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return application


def _run_streamable_http(server: Any, *, host: str, port: int, token: str) -> None:
    # Import via importlib so the MCP boundary scan stays on stdlib + mcp + api
    # (uvicorn/anyio are transitive deps of the optional mcp extra).
    import importlib

    anyio = importlib.import_module("anyio")
    uvicorn = importlib.import_module("uvicorn")

    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("MCP HTTP listener may only bind to localhost")
    if not token:
        raise RuntimeError("MCP HTTP listener requires a bearer token")

    async def _serve() -> None:
        starlette_app = server.streamable_http_app(streamable_http_path="/mcp")
        app = _bearer_middleware(starlette_app, token)
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",
        )
        http_server = uvicorn.Server(config)
        await http_server.serve()

    anyio.run(_serve)


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``sshpilot-mcp-runtime`` console script.

    Default transport is stdio (contributor/dev). The daemon-managed sidecar
    uses ``--transport streamable-http`` on localhost with a bearer token.
    """
    from sshpilot.api import DaemonClient

    args = _parse_args(argv)
    socket_path = os.environ.get("SSHPILOT_MCP_SOCKET")
    policy = RuntimePolicy.from_environment(os.environ)
    try:
        if socket_path:
            client = DaemonClient(socket_path=socket_path)
        else:
            client = DaemonClient()
    except Exception as error:  # noqa: BLE001 - connection failure surfaced verbatim
        print(f"sshpilot-mcp-runtime: could not connect to daemon: {error}", file=sys.stderr)
        return 1
    server = create_server(RuntimeHandle(client, policy))
    try:
        if args.transport == "stdio":
            server.run()
        elif args.transport == "streamable-http":
            _run_streamable_http(
                server,
                host=args.host,
                port=int(args.port),
                token=str(args.token or ""),
            )
        else:
            server.run(transport=args.transport)
    except Exception as error:  # noqa: BLE001
        print(f"sshpilot-mcp-runtime: {error}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
