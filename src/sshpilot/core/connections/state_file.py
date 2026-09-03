"""Dedicated daemon-owned connection-state storage (GTK-free).

The daemon owns a dedicated ``<config-dir>/connections.json`` file that stores
only non-SSH connections, connection groups/ordering, and safe per-connection
metadata. Legacy values are migrated once from ``config.json`` into the new
file without altering ``config.json``; once the dedicated file exists, the
legacy sections are never read again.

The write path refuses symlink destinations, creates missing parents as
``0700``, preserves an existing safe file mode (``0600`` for new files), writes
through a same-directory temporary file with ``fsync``, atomically replaces
the target, and ``fsync``s the parent directory. Reads refuse symlinks, enforce
strict UTF-8, a JSON object root, a 16 MiB size limit, and safe metadata.
The same hardened primitives also store the accepted UUID-owned v2 sidecar
and its pending transaction intent; v1 callers remain compatible. File
contents and filesystem paths are never logged or surfaced in errors.
"""

from __future__ import annotations

import copy
import json
import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple, TYPE_CHECKING

from ...api.models.connection_store import thaw_safe_metadata, validate_safe_metadata
from ..errors import CoreError, ErrorCode

_MAX_STATE_BYTES = 16 * 1024 * 1024
_MAX_IDENTITY_INTENT_BYTES = 32 * 1024 * 1024
_STATE_VERSION = 1

if TYPE_CHECKING:
    from .identity_reconciliation import ConnectionIdentityProjection
    from .identity_state_v2 import (
        IdentityRecoveryDecision,
        IdentityStateV2,
        IdentityTransactionIntent,
        MigrationReport,
    )


class ConnectionStateFileKind(str, Enum):
    """Safe classification of the JSON state file on disk."""

    ABSENT = "absent"
    V1 = "v1"
    V2 = "v2"
    UNSUPPORTED = "unsupported"
    CORRUPT = "corrupt"


def _core_error(exc: BaseException) -> CoreError:
    # Deliberately generic: the original OS error text can embed the full
    # target or temporary path, which callers must never surface.
    return CoreError(
        ErrorCode.CONNECTION_STATE_IO_ERROR,
        "Connection-state file operation failed",
        diagnostic_category="io_error",
        diagnostic_reason="state file I/O failed",
    )


def _state_rejected(category: str, reason: str) -> CoreError:
    return CoreError(
        ErrorCode.CONNECTION_STATE_IO_ERROR,
        "Connection-state file rejected",
        diagnostic_category=category,
        diagnostic_reason=reason,
    )


def _cleanup_temp(fd: Optional[int], tmp_name: Optional[str]) -> None:
    """Close and unlink a half-written temporary file after a failure."""
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    if tmp_name is not None:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def _refuse_symlink(path: os.PathLike) -> None:
    """Raise ``CoreError`` if *path* is a symbolic link (non-following)."""
    if os.path.islink(path):
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Refusing to operate through a symbolic link",
            diagnostic_category="unsafe_file",
            diagnostic_reason="unsafe state-file target",
        )


def _sanitize_legacy_metadata(meta: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """Validate legacy metadata without silently removing unsafe content."""
    if not isinstance(meta, Mapping):
        return None
    return validate_safe_metadata(meta)


# ---------------------------------------------------------------------------
# Frozen internal model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GroupFileState:
    """One connection group persisted by the daemon."""

    id: str
    name: str
    parent_id: Optional[str] = None
    order: int = 0
    color: str = ""
    connection_ids: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if type(self.id) is not str or not self.id.strip():
            raise ValueError("group id must be a non-empty string")
        if type(self.name) is not str or not self.name.strip():
            raise ValueError("group name must be a non-empty string")
        if self.parent_id is not None and (
            type(self.parent_id) is not str or not self.parent_id.strip()
        ):
            raise ValueError("group parent id must be a non-empty string or null")
        if type(self.order) is not int or self.order < 0:
            raise ValueError("group order must be a non-negative integer")
        if type(self.color) is not str:
            raise ValueError("group color must be a string")
        if type(self.connection_ids) is not tuple:
            raise TypeError("group connection ids must be a tuple")
        for cid in self.connection_ids:
            if type(cid) is not str or not cid.strip():
                raise ValueError("group connection ids must be non-empty strings")

    def to_dict(self) -> Mapping[str, Any]:
        payload: dict = {
            "id": self.id,
            "name": self.name,
            "order": self.order,
            "color": self.color,
            "connection_ids": list(self.connection_ids),
        }
        if self.parent_id is not None:
            payload["parent_id"] = self.parent_id
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GroupFileState":
        raw = dict(data or {})
        connection_ids = raw.get("connection_ids", ())
        if not isinstance(connection_ids, (list, tuple)):
            raise ValueError("group connection ids must be an array")
        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            parent_id=(
                str(raw["parent_id"]) if raw.get("parent_id") else None
            ),
            order=int(raw.get("order") or 0),
            color=str(raw.get("color") or ""),
            connection_ids=tuple(
                str(item) for item in connection_ids if str(item)
            ),
        )


@dataclass(frozen=True)
class ConnectionFileState:
    """Frozen daemon-owned connection-state snapshot for ``connections.json``."""

    version: int = _STATE_VERSION
    non_ssh_connections: Tuple[Mapping[str, Any], ...] = ()
    groups: Tuple[GroupFileState, ...] = ()
    root_connections: Tuple[str, ...] = ()
    metadata: Mapping[str, Mapping[str, Any]] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != _STATE_VERSION:
            raise ValueError("connection-state file version must be 1")
        if type(self.non_ssh_connections) is not tuple:
            raise TypeError("non-SSH connections must be a tuple")
        for item in self.non_ssh_connections:
            if not isinstance(item, Mapping):
                raise TypeError("non-SSH connections must be mappings")
        if type(self.groups) is not tuple:
            raise TypeError("groups must be a tuple")
        for group in self.groups:
            if type(group) is not GroupFileState:
                raise TypeError("groups must contain GroupFileState members")
        group_ids = [group.id for group in self.groups]
        if len(set(group_ids)) != len(group_ids):
            raise ValueError("group ids must not contain duplicates")
        if type(self.root_connections) is not tuple:
            raise TypeError("root connections must be a tuple")
        for cid in self.root_connections:
            if type(cid) is not str or not cid.strip():
                raise ValueError("root connection ids must be non-empty strings")
        if len(set(self.root_connections)) != len(self.root_connections):
            raise ValueError("root connection ids must not contain duplicates")
        sanitized: dict = {}
        for cid, values in self.metadata.items():
            if type(cid) is not str or not cid.strip():
                raise ValueError("metadata connection ids must be non-empty strings")
            sanitized[cid] = validate_safe_metadata(values)
        object.__setattr__(self, "metadata", sanitized)

    def to_dict(self) -> Mapping[str, Any]:
        return {
            "version": self.version,
            "non_ssh_connections": [
                copy.deepcopy(dict(item)) for item in self.non_ssh_connections
            ],
            "groups": {
                "groups": {
                    group.id: group.to_dict() for group in self.groups
                },
                "root_connections": list(self.root_connections),
            },
            "metadata": thaw_safe_metadata(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ConnectionFileState":
        raw = dict(data or {})
        non_ssh = raw.get("non_ssh_connections", [])
        if not isinstance(non_ssh, list):
            raise ValueError("non-SSH connections must be an array")
        groups_blob = raw.get("groups")
        if not isinstance(groups_blob, dict):
            raise ValueError("groups must be an object")
        raw_groups = groups_blob.get("groups", {})
        if not isinstance(raw_groups, dict):
            raise ValueError("connection groups must be an object")
        root_raw = groups_blob.get("root_connections", [])
        if not isinstance(root_raw, list):
            raise ValueError("root connections must be an array")
        metadata_raw = raw.get("metadata", {})
        if not isinstance(metadata_raw, dict):
            raise ValueError("metadata must be an object")
        return cls(
            version=raw.get("version", _STATE_VERSION),
            non_ssh_connections=tuple(
                dict(item) for item in non_ssh if isinstance(item, dict)
            ),
            groups=tuple(
                GroupFileState.from_dict(item)
                for item in raw_groups.values()
                if isinstance(item, dict)
            ),
            root_connections=tuple(str(item) for item in root_raw if str(item)),
            metadata={
                str(cid): dict(values)
                for cid, values in metadata_raw.items()
                if isinstance(values, dict)
            },
        )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _read_state_bytes(
    path: Path,
    *,
    max_bytes: Optional[int] = None,
) -> Optional[bytes]:
    """Read exact state bytes without following symlinks.

    Returns ``None`` when the file is absent; raises ``CoreError`` for any
    symlink, metadata, size, or read failure.
    """
    limit = _MAX_STATE_BYTES if max_bytes is None else max_bytes
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise _core_error(exc) from exc
    if stat.S_ISLNK(st.st_mode):
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Refusing to read through a symbolic link",
            diagnostic_category="unsafe_file",
            diagnostic_reason="unsafe state-file target",
        )
    if st.st_size > limit:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Connection-state file exceeds the size limit",
            diagnostic_category="unsafe_file",
            diagnostic_reason="state file exceeds size limit",
        )

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd: Optional[int] = None
    try:
        fd = os.open(path, flags)
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode):
            raise CoreError(
                ErrorCode.CONNECTION_STATE_IO_ERROR,
                "Connection-state file is not a regular file",
                diagnostic_category="unsafe_file",
                diagnostic_reason="state file is not a regular file",
            )
        chunks: list = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise CoreError(
                    ErrorCode.CONNECTION_STATE_IO_ERROR,
                    "Connection-state file exceeds the size limit",
                    diagnostic_category="unsafe_file",
                    diagnostic_reason="state file exceeds size limit",
                )
            chunks.append(chunk)
    except CoreError:
        raise
    except OSError as exc:
        raise _core_error(exc) from exc
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
    return b"".join(chunks)


def _decode_json_object(raw: bytes) -> Mapping[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Connection-state file is not valid UTF-8",
            diagnostic_category="invalid_utf8",
            diagnostic_reason="state file is not valid UTF-8",
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Connection-state file is malformed",
            diagnostic_category="invalid_json",
            diagnostic_reason="state file contains invalid JSON",
        ) from exc
    if type(data) is not dict:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Connection-state file must contain a JSON object",
            diagnostic_category="wrong_root_type",
            diagnostic_reason="state file root is not an object",
        )
    return data


def _parse_state_bytes(raw: bytes) -> ConnectionFileState:
    data = _decode_json_object(raw)
    try:
        return ConnectionFileState.from_dict(data)
    except (TypeError, ValueError) as exc:
        category = "invalid_metadata"
        reason = "state metadata is invalid"
        if data.get("version") != _STATE_VERSION:
            category, reason = "unsupported_version", "state-file version is unsupported"
        elif type(data.get("non_ssh_connections")) is not list:
            category, reason = "invalid_non_ssh", "non-SSH connection data is invalid"
        elif type(data.get("groups")) is not dict:
            category, reason = "invalid_groups", "group data is invalid"
        elif type(data.get("groups", {}).get("root_connections")) is not list:
            category, reason = "invalid_root_order", "root connection order is invalid"
        elif type(data.get("metadata")) is not dict:
            category, reason = "invalid_metadata", "metadata container is invalid"
        raise _state_rejected(category, reason) from exc


def read_connection_state(path: Path) -> ConnectionFileState:
    """Read the dedicated connection-state file.

    A missing file initializes default state; a malformed, oversized, or
    unsafe file is an error and is never replaced with defaults.
    """
    path = Path(path)
    raw = _read_state_bytes(path)
    if raw is None:
        return ConnectionFileState()
    return _parse_state_bytes(raw)


def probe_connection_state_file(path: Path) -> ConnectionStateFileKind:
    """Classify an on-disk ``connections.json`` without repairing it.

    Unsafe filesystem conditions still raise ``CoreError``. Malformed bytes,
    JSON, roots, or version fields are classified as ``CORRUPT`` so callers
    can distinguish corruption from an absent file without treating either as
    an empty identity registry.
    """
    raw = _read_state_bytes(Path(path))
    if raw is None:
        return ConnectionStateFileKind.ABSENT
    try:
        data = _decode_json_object(raw)
    except CoreError as exc:
        if exc.diagnostic_category in {
            "invalid_utf8",
            "invalid_json",
            "wrong_root_type",
        }:
            return ConnectionStateFileKind.CORRUPT
        raise
    version = data.get("version")
    if type(version) is not int:
        return ConnectionStateFileKind.CORRUPT
    if version == 1:
        try:
            _parse_state_bytes(raw)
        except CoreError:
            return ConnectionStateFileKind.CORRUPT
        return ConnectionStateFileKind.V1
    if version == 2:
        try:
            from .identity_state_v2 import IdentityStateV2

            IdentityStateV2.from_dict(data)
        except (TypeError, ValueError, KeyError):
            return ConnectionStateFileKind.CORRUPT
        return ConnectionStateFileKind.V2
    return ConnectionStateFileKind.UNSUPPORTED


def read_identity_state_v2(path: Path) -> Optional["IdentityStateV2"]:
    """Read and strictly validate a production v2 identity sidecar.

    ``None`` means the target is absent. Existing malformed, unsupported, or
    invalid state is always rejected and is never replaced with defaults.
    """
    raw = _read_state_bytes(Path(path))
    if raw is None:
        return None
    data = _decode_json_object(raw)
    if data.get("version") != 2:
        raise _state_rejected(
            "unsupported_version", "identity state version is unsupported"
        )
    try:
        from .identity_state_v2 import IdentityStateV2

        return IdentityStateV2.from_dict(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise _state_rejected(
            "invalid_identity_state", "identity state schema is invalid"
        ) from exc


def read_legacy_connection_state(config_path: Path) -> ConnectionFileState:
    """Read and normalize legacy values from ``config.json``.

    Migrates ``connections.non_ssh``, ``connection_groups``, and
    ``connections_meta`` into the new schema. ``config.json`` is never
    modified. A missing config file yields default state.
    """
    path = Path(config_path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ConnectionFileState()
    except OSError as exc:
        raise _core_error(exc) from exc
    if stat.S_ISLNK(st.st_mode):
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Refusing to read through a symbolic link",
        )
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise _core_error(exc) from exc
    if len(raw) > _MAX_STATE_BYTES:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Configuration file exceeds the size limit",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Configuration file is not valid UTF-8",
        ) from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Configuration file is malformed",
        ) from exc
    if type(data) is not dict:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            "Configuration file must contain a JSON object",
        )

    non_ssh: list = []
    connections_blob = data.get("connections")
    if isinstance(connections_blob, dict):
        stored = connections_blob.get("non_ssh")
        if isinstance(stored, list):
            non_ssh = [dict(item) for item in stored if isinstance(item, dict)]

    group_records: list = []
    root: list = []
    groups_blob = data.get("connection_groups")
    if isinstance(groups_blob, dict):
        raw_groups = groups_blob.get("groups")
        membership = groups_blob.get("connections")
        if not isinstance(membership, dict):
            membership = {}
        root_raw = groups_blob.get("root_connections")
        if isinstance(root_raw, list):
            seen_root = set()
            root = []
            for item in root_raw:
                value = str(item)
                if value and value not in seen_root:
                    seen_root.add(value)
                    root.append(value)
        if isinstance(raw_groups, dict):
            for gid, info in raw_groups.items():
                if not isinstance(info, dict):
                    continue
                conns = info.get("connections")
                conn_ids = (
                    [str(c) for c in conns if str(c)]
                    if isinstance(conns, list)
                    else []
                )
                for nickname, primary_gid in membership.items():
                    if str(primary_gid) == str(gid) and nickname not in conn_ids:
                        conn_ids.append(str(nickname))
                try:
                    group_records.append(
                        GroupFileState(
                            id=str(gid),
                            name=str(info.get("name") or gid),
                            parent_id=(
                                str(info["parent_id"])
                                if info.get("parent_id")
                                else None
                            ),
                            order=int(info.get("order") or 0),
                            color=str(info.get("color") or ""),
                            connection_ids=tuple(conn_ids),
                        )
                    )
                except (TypeError, ValueError):
                    continue

    metadata: dict = {}
    meta_blob = data.get("connections_meta")
    if isinstance(meta_blob, dict):
        for cid, meta in meta_blob.items():
            if not isinstance(meta, dict):
                continue
            try:
                sanitized = _sanitize_legacy_metadata(meta)
            except (TypeError, ValueError):
                # Legacy metadata is auxiliary and entries are independent.
                # Keep valid decorations migratable without weakening the
                # shared safe-metadata validator or copying unsafe values.
                continue
            if sanitized is not None:
                metadata[str(cid)] = sanitized

    return ConnectionFileState(
        version=_STATE_VERSION,
        non_ssh_connections=tuple(non_ssh),
        groups=tuple(group_records),
        root_connections=tuple(root),
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------

def _serialize_json(payload: Mapping[str, Any], label: str) -> bytes:
    try:
        return json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CoreError(
            ErrorCode.CONNECTION_STATE_IO_ERROR,
            f"{label} data is not serializable",
        ) from exc


def _fsync_parent(path: Path) -> None:
    """Make a completed same-directory rename or unlink durable."""
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    dir_fd: Optional[int] = None
    try:
        dir_fd = os.open(path.parent, flags)
        os.fsync(dir_fd)
    except OSError as exc:
        raise _core_error(exc) from exc
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except OSError:
                pass


def _atomic_write_bytes(
    path: Path,
    content: bytes,
    *,
    prefix: str,
    max_bytes: Optional[int] = None,
) -> None:
    """Atomically write bytes using the hardened v1 storage policy."""
    path = Path(path)
    limit = _MAX_STATE_BYTES if max_bytes is None else max_bytes
    if len(content) > limit:
        raise _state_rejected(
            "unsafe_file", "serialized state exceeds the size limit"
        )
    try:
        _refuse_symlink(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except CoreError:
        raise
    except OSError as exc:
        raise _core_error(exc) from exc

    # Read existing metadata WITHOUT following the target. If the target
    # exists but its metadata cannot be read, that is an error, not a silent
    # fallback to 0600. Only a truly absent target gets the default mode.
    existing_mode: Optional[int] = None
    try:
        st = os.lstat(path)
        existing_mode = stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        existing_mode = None
    except OSError as exc:
        raise _core_error(exc) from exc
    mode = existing_mode if existing_mode is not None else 0o600

    fd: Optional[int] = None
    tmp_name: Optional[str] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=prefix, suffix=".tmp"
        )
        # os.fdopen takes ownership of the descriptor; do not close it twice.
        with os.fdopen(fd, "wb") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)

        # Re-check the destination non-following right before the swap.
        _refuse_symlink(path)

        os.replace(tmp_name, path)
        tmp_name = None  # consumed by the replace
    except CoreError:
        _cleanup_temp(fd, tmp_name)
        raise
    except Exception as exc:
        _cleanup_temp(fd, tmp_name)
        raise _core_error(exc) from exc

    # Make the rename durable. The replacement may already have succeeded when
    # this fails, so callers must treat a raised CoreError here as
    # "durability unknown".
    _fsync_parent(path)


def write_connection_state(path: Path, state: ConnectionFileState) -> None:
    """Atomically persist *state* to *path* with full durability hardening."""
    if type(state) is not ConnectionFileState:
        raise TypeError("connection state must be a ConnectionFileState")
    content = _serialize_json(state.to_dict(), "Connection-state")
    _atomic_write_bytes(Path(path), content, prefix=".connections-")


def write_identity_state_v2(path: Path, state: "IdentityStateV2") -> None:
    """Atomically persist an already-validated v2 identity state."""
    from .identity_state_v2 import IdentityStateV2

    if type(state) is not IdentityStateV2:
        raise TypeError("identity state must be an IdentityStateV2")
    content = _serialize_json(state.to_dict(), "Identity-state")
    _atomic_write_bytes(Path(path), content, prefix=".connections-")


def migrate_connection_state_v1_to_v2(
    path: Path,
    projections: Sequence["ConnectionIdentityProjection"],
    *,
    ssh_config_revision: str,
    uuid_factory: Optional[Callable[[], str]] = None,
) -> Tuple["IdentityStateV2", "MigrationReport"]:
    """Safely migrate one real v1 sidecar to v2.

    The caller supplies authoritative loader projections. This function never
    derives evidence from serialized ``record.data`` and never removes or
    truncates the v1 file before the atomic v2 replacement commits.
    """
    path = Path(path)
    if type(ssh_config_revision) is not str or not ssh_config_revision.strip():
        raise ValueError("SSH config revision must be a non-empty string")
    kind = probe_connection_state_file(path)
    if kind is ConnectionStateFileKind.ABSENT:
        raise _state_rejected("missing_state", "v1 state file is absent")
    if kind is not ConnectionStateFileKind.V1:
        category = (
            "unsupported_version"
            if kind is ConnectionStateFileKind.UNSUPPORTED
            else "invalid_state"
        )
        raise _state_rejected(category, "state file is not a valid v1 file")
    legacy = read_connection_state(path)
    from .identity_state_v2 import migrate_v1_state

    kwargs: dict[str, Any] = {"ssh_config_revision": ssh_config_revision}
    if uuid_factory is not None:
        kwargs["uuid_factory"] = uuid_factory
    state, report = migrate_v1_state(legacy, projections, **kwargs)
    write_identity_state_v2(path, state)
    return state, report


def read_state_bytes_for_salvage(path: Path) -> Optional[bytes]:
    """Read a damaged state file's exact bytes under the normal guards.

    Salvage needs the raw document the strict reader rejected.  The same
    symlink, metadata and size refusals still apply -- a file that is unsafe
    to read is unsafe to salvage.
    """
    return _read_state_bytes(Path(path))


def quarantine_connection_state_file(path: Path, raw: bytes) -> Path:
    """Copy damaged state aside before anything overwrites it.

    Salvage keeps what it can parse; the copy keeps everything, including
    whatever salvage had to drop, so a damaged file is never the end of the
    story.  Written with the ordinary hardened policy and a timestamped
    sibling name, so repeated attempts never clobber an earlier copy.
    """
    path = Path(path)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    suffix = 0
    while target.exists() or target.is_symlink():
        suffix += 1
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{suffix}")
    _atomic_write_bytes(target, raw, prefix=".connections-corrupt-")
    return target


def identity_transaction_intent_path(path: Path) -> Path:
    """Return the deterministic same-directory pending-intent companion."""
    path = Path(path)
    return path.with_name(path.name + ".pending")


def read_pending_identity_transaction(
    path: Path,
) -> Optional["IdentityTransactionIntent"]:
    """Read a strict pending target snapshot; malformed data is an error."""
    raw = _read_state_bytes(
        Path(path), max_bytes=_MAX_IDENTITY_INTENT_BYTES
    )
    if raw is None:
        return None
    data = _decode_json_object(raw)
    try:
        from .identity_state_v2 import IdentityTransactionIntent

        intent = IdentityTransactionIntent.from_dict(data)
    except (TypeError, ValueError, KeyError) as exc:
        raise _state_rejected(
            "invalid_transaction_intent", "pending identity transaction is invalid"
        ) from exc
    target_content = _serialize_json(intent.target_state.to_dict(), "Identity-state")
    if len(target_content) > _MAX_STATE_BYTES:
        raise _state_rejected(
            "invalid_transaction_intent",
            "serialized transaction target exceeds the state limit",
        )
    return intent


def write_pending_identity_transaction(
    path: Path,
    intent: "IdentityTransactionIntent",
) -> None:
    """Atomically persist a complete, validated target snapshot intent."""
    from .identity_state_v2 import IdentityTransactionIntent

    if type(intent) is not IdentityTransactionIntent:
        raise TypeError("identity transaction must be an IdentityTransactionIntent")
    target_content = _serialize_json(intent.target_state.to_dict(), "Identity-state")
    if len(target_content) > _MAX_STATE_BYTES:
        raise _state_rejected(
            "unsafe_file", "serialized transaction target exceeds the state limit"
        )
    content = _serialize_json(intent.to_dict(), "Identity transaction")
    _atomic_write_bytes(
        Path(path),
        content,
        prefix=".connections-pending-",
        max_bytes=_MAX_IDENTITY_INTENT_BYTES,
    )


def clear_pending_identity_transaction(path: Path) -> None:
    """Durably remove a pending intent, refusing symlink targets."""
    path = Path(path)
    try:
        _refuse_symlink(path)
        os.unlink(path)
    except FileNotFoundError:
        return
    except CoreError:
        raise
    except OSError as exc:
        raise _core_error(exc) from exc
    _fsync_parent(path)


def recover_pending_identity_transaction(
    sidecar_path: Path,
    *,
    actual_ssh_revision: Optional[str],
    intent_path: Optional[Path] = None,
) -> "IdentityRecoveryDecision":
    """Apply only unambiguous pending-intent recovery decisions.

    TARGET and BASE revisions are finalized/aborted respectively. Unrelated,
    incomplete, stale, or corrupt state is left for a higher-level adapter and
    is never resolved by guessing.
    """
    from .identity_state_v2 import (
        IdentityRecoveryAction,
        IdentityRecoveryDecision,
        classify_identity_transaction_recovery,
    )

    sidecar_path = Path(sidecar_path)
    intent_path = (
        Path(intent_path)
        if intent_path is not None
        else identity_transaction_intent_path(sidecar_path)
    )
    intent = read_pending_identity_transaction(intent_path)
    if intent is None:
        return IdentityRecoveryDecision(
            IdentityRecoveryAction.NO_PENDING,
            "no pending identity transaction",
        )
    current = read_identity_state_v2(sidecar_path)
    if current is None:
        raise _state_rejected(
            "missing_identity_state", "pending transaction has no sidecar state"
        )
    decision = classify_identity_transaction_recovery(
        current, intent, actual_ssh_revision
    )
    if decision.action is IdentityRecoveryAction.FINALIZE_TARGET:
        if current != intent.target_state:
            write_identity_state_v2(sidecar_path, intent.target_state)
        clear_pending_identity_transaction(intent_path)
    elif decision.action is IdentityRecoveryAction.ABORT_BASE:
        clear_pending_identity_transaction(intent_path)
    return decision
