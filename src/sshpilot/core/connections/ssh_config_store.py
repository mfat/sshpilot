"""Headless atomic SSH config mutation storage (GTK-free).

``SshConfigStore`` is the daemon-owned write path for the selected SSH
configuration: it decides the owning source file from *loaded daemon state*,
never from client-supplied paths, and persists every mutation through one
hardened atomic writer (same-directory temporary file, ``fsync``, atomic
replace, parent-directory ``fsync``, one-shot ``.bak`` backup, symlink
refusal, mode preservation).

Optimistic concurrency: every mutation compares the loaded configuration
revision immediately before commit and raises a dedicated stale-state
``CoreError`` when any participating file changed in the meantime. Mutations
are never retried automatically, and failed writes leave the previous file
bytes untouched.

All public errors are generic — they never embed filesystem paths, OS
exception text, or file contents.
"""

from __future__ import annotations

import copy
import os
import shlex
import shutil
import stat
import tempfile
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from ...api.models.connections import SshConfigText
from ...ssh_config_document import (
    HostBlock,
    RawSpan,
    SSHConfigDocument,
    _split_keyword,
    is_concrete_host_block,
)
from ...ssh_config_formatter import merged_block_lines
from ..errors import CoreError, ErrorCode
from .models import ConnectionRecord, generate_duplicate_nickname
from .ssh_config_loader import (
    LoadedSshConfiguration,
    _compute_revision,
    _resolve_config_files,
    load_ssh_configuration,
)

_SSH_CONFIG_HEADER = "# SSH configuration file\n\n"


def _store_error(message: str) -> CoreError:
    return CoreError(ErrorCode.CONNECTION_STATE_IO_ERROR, message)


def _stale_error() -> CoreError:
    return CoreError(
        ErrorCode.STALE_CONNECTION_STATE,
        "The connection configuration changed since it was last read",
    )


def _not_found_error() -> CoreError:
    return CoreError(
        ErrorCode.CONNECTION_NOT_FOUND,
        "The connection does not exist",
    )


def _already_exists_error() -> CoreError:
    return CoreError(
        ErrorCode.CONNECTION_ALREADY_EXISTS,
        "A connection with this name already exists",
    )


# ---------------------------------------------------------------------------
# Hardened atomic writer
# ---------------------------------------------------------------------------

def _atomic_write_text(
    path: Path,
    text: str,
    *,
    expected_bytes: Optional[bytes] = None,
) -> None:
    """Atomically replace *path* with *text* using a same-directory temp file.

    - Rejects exact ``str`` input only.
    - Refuses symbolic-link targets (checked before and immediately before
      replacement, so a target swapped into a symlink mid-write aborts).
    - Existing-file metadata failure is an error; mode 0600 is used only for
      genuinely new files; an existing safe mode is preserved.
    - Creates one ``.bak`` of the first prior good file (never overwrites an
      existing backup).
    - Flushes and ``fsync``s the temporary file, then the parent directory.
    - When *expected_bytes* is supplied, the target's current bytes must
      equal it immediately before replacement or a stale-state error is
      raised. This narrows and detects most read→write races; it cannot close
      the final interval before ``os.replace()`` without stronger locking or
      kernel primitives.
    - All public errors are generic (no paths, no OS text, no contents).
    """
    if type(text) is not str:
        raise TypeError("SSH configuration content must be exact text")

    target = Path(path)
    directory = target.parent

    # Existing-file metadata must be readable; a failure is an error.
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise _store_error("The connection configuration could not be saved") from exc
    if st is not None and stat.S_ISLNK(st.st_mode):
        raise _store_error("Refusing to write through a symbolic link")

    try:
        os.makedirs(directory, mode=0o700, exist_ok=True)
    except OSError as exc:
        raise _store_error("The connection configuration could not be saved") from exc

    # One-shot backup of the first prior good file.
    backup = target.with_name(target.name + ".bak")
    if st is not None and not backup.exists():
        try:
            shutil.copy2(target, backup)
            os.chmod(backup, 0o600)
        except OSError as exc:
            raise _store_error("The connection configuration could not be backed up") from exc

    fd: Optional[int] = None
    tmp_name: Optional[str] = None
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(directory), prefix=".sshpilot-", suffix=".tmp"
        )
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            fd = None  # os.fdopen took ownership
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())

        # Re-check the target immediately before replacement: it must still
        # be the same bytes we loaded (no concurrent writer) and must not
        # have become a symbolic link.
        try:
            st_now = os.lstat(target)
        except FileNotFoundError:
            st_now = None
        except OSError as exc:
            raise _store_error("The connection configuration could not be saved") from exc
        if st_now is not None and stat.S_ISLNK(st_now.st_mode):
            raise _store_error("Refusing to write through a symbolic link")
        if expected_bytes is not None:
            try:
                current = target.read_bytes() if st_now is not None else None
            except OSError as exc:
                raise _store_error(
                    "The connection configuration could not be saved"
                ) from exc
            if current != expected_bytes:
                raise _stale_error()

        os.replace(tmp_name, target)
        tmp_name = None

        if st is not None:
            os.chmod(target, stat.S_IMODE(st.st_mode))
        else:
            os.chmod(target, 0o600)

        # Parent-directory durability (the rename itself must be durable).
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            dir_fd = os.open(str(directory), flags)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            # The replacement may already have succeeded; report generically.
            raise _store_error("The connection configuration could not be saved") from exc
    except CoreError:
        raise
    except OSError as exc:
        raise _store_error("The connection configuration could not be saved") from exc
    finally:
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


# ---------------------------------------------------------------------------
# Pure mutation helpers
# ---------------------------------------------------------------------------

def _preserve_multivalue_on_update(
    record: ConnectionRecord,
    new_data: Dict[str, Any],
) -> None:
    """Fold unedited multi-value/extra directives forward on update.

    Mirrors the legacy behavior: when the save payload omits the full
    ``identity_files``/``certificate_files`` lists, the unedited extras are
    kept (with the edited primary replacing the first entry); and directives
    the dialog does not surface (proxy command, request tty, forward-agent
    target) are carried forward unless the payload re-authors or clears them.
    """
    data = record.data or {}

    def _reconcile(list_key: str, primary_key: str) -> None:
        if list_key in new_data:
            return
        existing = data.get(list_key) or []
        if not isinstance(existing, list) or len(existing) <= 1:
            return
        new_primary = str(new_data.get(primary_key, "") or "").strip()
        merged = ([new_primary] if new_primary else []) + [
            entry for entry in existing[1:] if entry
        ]
        deduped = list(dict.fromkeys(entry for entry in merged if entry))
        if deduped:
            new_data[list_key] = deduped

    _reconcile("identity_files", "keyfile")
    _reconcile("certificate_files", "certificate")

    extra = str(new_data.get("extra_ssh_config") or "")
    extra_keys = {
        _split_keyword(line.strip())[0].lower()
        for line in extra.splitlines()
        if line.strip()
    }
    for attr, directive in (
        ("proxy_command", "proxycommand"),
        ("request_tty", "requesttty"),
        ("forward_agent_target", "forwardagent"),
    ):
        if attr in new_data or directive in extra_keys:
            continue
        existing = data.get(attr)
        if existing not in (None, "", False):
            new_data[attr] = existing


def _source_of(record: ConnectionRecord) -> str:
    """Daemon-recorded source file for a connection (never client data)."""
    return record.source or str((record.data or {}).get("source") or "")


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SshConfigMutationResult:
    """Result of a store mutation: the fresh configuration plus the changed id."""

    config: LoadedSshConfiguration
    connection_id: str
    touched_path: Optional[str] = None


@dataclass(frozen=True)
class PreparedSshMutation:
    """A validated SSH replacement that has not changed durable state."""

    operation: str
    base_revision: str
    target_revision: str
    target_path: Path
    expected_bytes: Optional[bytes]
    target_text: str
    connection_id: str = ""
    remove_connection_id: Optional[str] = None

    def commit(self, store: "SshConfigStore") -> SshConfigText:
        """Commit after rechecking the base revision and expected bytes."""
        if not isinstance(store, SshConfigStore):
            raise TypeError("prepared mutation needs its owning SshConfigStore")
        store._refuse_symlinked_root()
        if not store._revision_matches(self.base_revision):
            raise _stale_error()
        # Re-evaluate the exact target source tree immediately before the
        # replacement.  This catches dependencies introduced only by the
        # prepared Include graph (which are absent from the base revision).
        if store.preview_prepared(self).root_revision != self.target_revision:
            raise _stale_error()
        _atomic_write_text(
            self.target_path,
            self.target_text,
            expected_bytes=self.expected_bytes,
        )
        return SshConfigText(
            text=self.target_text,
            revision=store._current_revision(),
            display_name=store._display_name(),
            writable=store._is_writable(),
        )


class SshConfigStore:
    """Daemon-owned, headless, atomic SSH config read/mutation store."""

    def __init__(self, root_path: Path, *, isolated: bool = False) -> None:
        self._root_path = Path(root_path)
        self._isolated = bool(isolated)
        # Per-connection mutation generations (stale-editor detection). These
        # are in-memory only; the daemon process owns them for its lifetime.
        self._generations: Dict[str, int] = {}

    @property
    def root_path(self) -> Path:
        return self._root_path

    # -- loading -----------------------------------------------------------

    def load(self) -> LoadedSshConfiguration:
        config = load_ssh_configuration(self._root_path, isolated=self._isolated)
        return self._overlay_generations(config)

    def _overlay_generations(
        self, config: LoadedSshConfiguration
    ) -> LoadedSshConfiguration:
        for record in config.connections:
            record.generation = self._generations.get(record.id, 0)
        return config

    def _reload_after_write(self) -> LoadedSshConfiguration:
        return self._overlay_generations(
            load_ssh_configuration(self._root_path, isolated=self._isolated)
        )

    def _revision_matches(self, expected: str) -> bool:
        """True when the on-disk revision still equals *expected*."""
        try:
            files = _resolve_config_files(self._root_path)
            return _compute_revision(files) == expected
        except (CoreError, OSError):
            return False

    # -- raw text editor (daemon-selected document) -----------------------

    def _display_name(self) -> str:
        """Home-collapsed display label for the editor title (never a path
        chosen by the client — the daemon resolved the root)."""
        path = str(self._root_path)
        try:
            home = os.path.expanduser("~")
        except Exception:
            home = ""
        if home and path.startswith(home + os.sep):
            return "~" + path[len(home):]
        if home and path == home:
            return "~"
        return path

    def _is_writable(self) -> bool:
        """True when the daemon's hardened write path can replace the root.

        Mirrors ``_atomic_write_text``: same-directory temp file + atomic
        rename, so directory write+execute access is required and symlinked
        roots are never mutated.
        """
        try:
            root = Path(self._root_path)
            if os.path.islink(root):
                return False
            if not os.access(root.parent, os.W_OK | os.X_OK):
                return False
            if root.exists():
                return os.access(root, os.W_OK)
            return True
        except OSError:
            return False

    def _read_root_text(self) -> str:
        try:
            return self._root_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
        except (OSError, UnicodeError) as exc:
            raise _store_error(
                "The connection configuration could not be read"
            ) from exc

    def _current_revision(self) -> str:
        try:
            return _compute_revision(_resolve_config_files(self._root_path))
        except (CoreError, OSError) as exc:
            raise _store_error(
                "The connection configuration could not be read"
            ) from exc

    def get_text(self) -> SshConfigText:
        """Return the daemon-selected root config text for the raw editor.

        Reading never requires the document to *parse* (the editor is the tool
        that fixes a malformed config); only readability of the root and its
        recursively included files is enforced, matching the strict-load
        policy of the daemon.
        """
        return SshConfigText(
            text=self._read_root_text(),
            revision=self._current_revision(),
            display_name=self._display_name(),
            writable=self._is_writable(),
        )

    def replace_text(self, text: str, expected_revision: str) -> SshConfigText:
        """Replace the daemon-selected root config text atomically.

        Reuses the hardened writer (same-directory temp, fsync, atomic
        replace, one-shot ``.bak`` backup, mode preservation, symlink
        refusal) and rejects stale saves when any participating file changed
        since the editor loaded it. The text is written exactly as provided.
        """
        if type(text) is not str:
            raise TypeError("SSH configuration content must be exact text")
        if type(expected_revision) is not str or not expected_revision.strip():
            raise TypeError("an expected SSH config revision is required")
        self._refuse_symlinked_root()
        current_bytes = (
            self._root_path.read_bytes() if self._root_path.exists() else None
        )
        if not self._revision_matches(expected_revision):
            raise _stale_error()
        _atomic_write_text(
            self._root_path,
            text,
            expected_bytes=current_bytes,
        )
        return SshConfigText(
            text=text,
            revision=self._current_revision(),
            display_name=self._display_name(),
            writable=self._is_writable(),
        )

    def prepare_replace_text(
        self, text: str, expected_revision: str
    ) -> PreparedSshMutation:
        """Prepare a raw-config replacement without writing any file."""
        if type(text) is not str:
            raise TypeError("SSH configuration content must be exact text")
        if type(expected_revision) is not str or not expected_revision.strip():
            raise TypeError("an expected SSH config revision is required")
        self._refuse_symlinked_root()
        expected_bytes = (
            self._root_path.read_bytes() if self._root_path.exists() else None
        )
        if not self._revision_matches(expected_revision):
            raise _stale_error()
        prepared = PreparedSshMutation(
            operation="replace_text",
            base_revision=expected_revision,
            target_revision="",
            target_path=self._root_path,
            expected_bytes=expected_bytes,
            target_text=text,
        )
        return self._with_target_revision(prepared)

    def commit_prepared(self, prepared: PreparedSshMutation) -> SshConfigMutationResult:
        """Commit a prepared mutation and reload authoritative projections."""
        if type(prepared) is not PreparedSshMutation:
            raise TypeError("a PreparedSshMutation is required")
        prepared.commit(self)
        if prepared.remove_connection_id is not None:
            self._generations.pop(prepared.remove_connection_id, None)
        if prepared.connection_id:
            self._generations[prepared.connection_id] = (
                self._generations.get(prepared.connection_id, 0) + 1
            )
        fresh = self._reload_after_write()
        return SshConfigMutationResult(
            config=fresh,
            connection_id=prepared.connection_id,
            touched_path=str(prepared.target_path),
        )

    def preview_prepared(self, prepared: PreparedSshMutation) -> LoadedSshConfiguration:
        """Load a prepared config through the authoritative loader overlay.

        The overlay keeps the real source paths and resolves the target
        Include graph against the real filesystem.  This is important for
        absolute/parent-relative includes and for detecting dependencies that
        exist only in the proposed target document.
        """
        if type(prepared) is not PreparedSshMutation:
            raise TypeError("a PreparedSshMutation is required")
        return self._overlay_generations(
            load_ssh_configuration(
                self._root_path,
                isolated=self._isolated,
                _content_overrides={
                    self._root_path.resolve(): prepared.target_text.encode("utf-8")
                    if prepared.target_path.resolve() == self._root_path.resolve()
                    else self._root_path.read_bytes(),
                    prepared.target_path.resolve(): prepared.target_text.encode("utf-8"),
                },
            )
        )

    def _with_target_revision(self, prepared: PreparedSshMutation) -> PreparedSshMutation:
        """Fill a prepared mutation's revision from its target Include graph."""
        preview = self.preview_prepared(prepared)
        return PreparedSshMutation(
            operation=prepared.operation,
            base_revision=prepared.base_revision,
            target_revision=preview.root_revision,
            target_path=prepared.target_path,
            expected_bytes=prepared.expected_bytes,
            target_text=prepared.target_text,
            connection_id=prepared.connection_id,
            remove_connection_id=prepared.remove_connection_id,
        )

    def _prepare_document_replacement(
        self,
        *,
        operation: str,
        target: Path,
        expected_revision: str,
        expected_bytes: Optional[bytes],
        target_text: str,
        connection_id: str = "",
        remove_connection_id: Optional[str] = None,
    ) -> PreparedSshMutation:
        if not self._revision_matches(expected_revision):
            raise _stale_error()
        provisional = PreparedSshMutation(
            operation=operation,
            base_revision=expected_revision,
            target_revision="",
            target_path=target,
            expected_bytes=expected_bytes,
            target_text=target_text,
            connection_id=connection_id,
            remove_connection_id=remove_connection_id,
        )
        return self._with_target_revision(provisional)

    def prepare_create(self, data: Mapping[str, Any]) -> PreparedSshMutation:
        """Prepare a new Host block without touching the SSH config."""
        self._refuse_symlinked_root()
        config = self.load()
        payload = dict(data)
        payload.pop("uuid", None)
        nickname = str(payload.get("nickname") or "").strip()
        if not nickname:
            raise CoreError(ErrorCode.VALIDATION_ERROR, "A nickname is required")
        if any(record.id == nickname for record in config.connections):
            raise _already_exists_error()
        self._ensure_authored_alias_available(nickname)
        expected = self._root_path.read_bytes() if self._root_path.exists() else None
        if self._root_path.exists():
            doc = SSHConfigDocument.parse_file(str(self._root_path))
            doc.nodes.append(
                RawSpan(lines=doc.render_lines(["\n"] + merged_block_lines(None, payload)))
            )
            text = doc.text()
        else:
            text = _SSH_CONFIG_HEADER + "".join(merged_block_lines(None, payload))
        return self._prepare_document_replacement(
            operation="create", target=self._root_path,
            expected_revision=config.root_revision, expected_bytes=expected,
            target_text=text, connection_id=nickname,
        )

    def prepare_update(
        self,
        connection_id: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int],
    ) -> PreparedSshMutation:
        """Prepare an existing Host-block update without writing it."""
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        self._check_stale(connection_id, expected_generation)
        self._ensure_unique_managed_declaration(connection_id, allow_shared=False)
        payload = dict(data)
        payload.pop("uuid", None)
        payload.pop("source", None)
        payload.pop("source_config_path", None)
        payload["id"] = payload.get("nickname") or record.nickname
        payload["nickname"] = payload.get("nickname") or record.nickname
        _preserve_multivalue_on_update(record, payload)
        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")
        target = Path(source)
        new_name = str(payload.get("nickname") or "").strip() or connection_id
        if new_name != connection_id and any(item.id == new_name for item in config.connections):
            raise _already_exists_error()
        if new_name != connection_id:
            self._ensure_authored_alias_available(new_name)
        expected = target.read_bytes()
        doc = SSHConfigDocument.parse_file(str(target))
        found = False
        nodes: List[Any] = []
        for node in doc.nodes:
            if (
                isinstance(node, HostBlock)
                and is_concrete_host_block(node)
                and connection_id in node.tokens
            ):
                if not found:
                    nodes.append(RawSpan(lines=doc.render_lines(merged_block_lines(node, payload))))
                found = True
                continue
            nodes.append(node)
        if not found:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The connection declaration changed before it could be edited",
            )
        doc.nodes = nodes
        return self._prepare_document_replacement(
            operation="update", target=target,
            expected_revision=config.root_revision, expected_bytes=expected,
            target_text=doc.text(), connection_id=new_name,
            remove_connection_id=connection_id if new_name != connection_id else None,
        )

    def prepare_delete(self, connection_id: str) -> PreparedSshMutation:
        """Prepare deletion of one Host token without writing it."""
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        self._ensure_unique_managed_declaration(connection_id, allow_shared=True)
        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")
        target = Path(source)
        expected = target.read_bytes()
        doc = SSHConfigDocument.parse_file(str(target))
        modified = False
        nodes: List[Any] = []
        for node in doc.nodes:
            if (
                isinstance(node, HostBlock)
                and is_concrete_host_block(node)
                and connection_id in node.tokens
            ):
                modified = True
                remaining = [token for token in node.tokens if token != connection_id]
                if remaining:
                    node.tokens = remaining
                    node.lines[0] = doc.render_lines([f"Host {shlex.join(remaining)}\n"])[0]
                    nodes.append(node)
                continue
            nodes.append(node)
        if not modified:
            raise _not_found_error()
        doc.nodes = nodes
        return self._prepare_document_replacement(
            operation="delete", target=target,
            expected_revision=config.root_revision, expected_bytes=expected,
            target_text=doc.text(), remove_connection_id=connection_id,
        )

    def prepare_duplicate(self, connection_id: str) -> PreparedSshMutation:
        """Prepare a duplicate Host block without writing it."""
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")
        base = dict(copy.deepcopy(record.data or {}))
        for key in list(base):
            if key.startswith("__") or key in {"aliases", "password_changed"}:
                base.pop(key, None)
        base.pop("uuid", None)
        existing_ids = {
            item.id for item in config.connections
        } | self._authored_concrete_aliases()
        new_name = generate_duplicate_nickname(record.nickname, existing_ids)
        base["nickname"] = new_name
        base.setdefault("hostname", record.hostname or new_name)
        target = Path(source)
        expected = target.read_bytes()
        doc = SSHConfigDocument.parse_file(str(target))
        doc.nodes.append(RawSpan(lines=doc.render_lines(["\n"] + merged_block_lines(None, base))))
        return self._prepare_document_replacement(
            operation="duplicate", target=target,
            expected_revision=config.root_revision, expected_bytes=expected,
            target_text=doc.text(), connection_id=new_name,
        )

    def prepare_split(
        self,
        connection_id: str,
        original_host_token: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int],
    ) -> PreparedSshMutation:
        """Prepare a split without changing the real SSH tree."""
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        self._check_stale(connection_id, expected_generation)
        self._ensure_unique_managed_declaration(
            original_host_token, allow_shared=True
        )
        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")
        payload = dict(data)
        payload.pop("uuid", None)
        payload.pop("source", None)
        payload.pop("source_config_path", None)
        new_name = str(payload.get("nickname") or "").strip()
        if not original_host_token or not new_name:
            raise CoreError(ErrorCode.VALIDATION_ERROR, "A split host token and destination name are required")
        if new_name != connection_id and any(item.id == new_name for item in config.connections):
            raise _already_exists_error()
        self._ensure_split_destination_available(new_name, original_host_token)
        target = Path(source)
        expected = target.read_bytes()
        doc = SSHConfigDocument.parse_file(str(target))
        found = False
        nodes: List[Any] = []
        for node in doc.nodes:
            if (
                isinstance(node, HostBlock)
                and is_concrete_host_block(node)
                and original_host_token in node.tokens
            ):
                found = True
                remaining = [token for token in node.tokens if token != original_host_token]
                if remaining:
                    node.tokens = remaining
                    node.lines[0] = doc.render_lines([f"Host {shlex.join(remaining)}\n"])[0]
                    nodes.append(node)
                continue
            nodes.append(node)
        if not found:
            raise CoreError(ErrorCode.VALIDATION_ERROR, "The original host token was not found")
        nodes.append(
            RawSpan(lines=doc.render_lines(["\n"] + merged_block_lines(None, payload)))
        )
        doc.nodes = nodes
        return self._prepare_document_replacement(
            operation="split", target=target, expected_revision=config.root_revision,
            expected_bytes=expected, target_text=doc.text(), connection_id=new_name,
            remove_connection_id=connection_id if new_name != connection_id else None,
        )

    def _revision_for_root_bytes(self, root_bytes: bytes) -> str:
        """Compute the source-tree revision with the root bytes substituted."""
        hasher = hashlib.sha256()
        files = _resolve_config_files(self._root_path)
        for path in files:
            data = root_bytes if Path(path) == self._root_path.resolve() else Path(path).read_bytes()
            rel = os.path.relpath(path)
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(data)
            hasher.update(b"\x00")
        if not files and not self._root_path.exists():
            rel = os.path.relpath(self._root_path)
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(root_bytes)
            hasher.update(b"\x00")
        return hasher.hexdigest()

    def _revision_for_path_bytes(self, target: Path, target_bytes: bytes) -> str:
        """Compute a source-tree revision with one participating file replaced."""
        hasher = hashlib.sha256()
        files = _resolve_config_files(self._root_path)
        target_resolved = target.resolve()
        for path in files:
            data = target_bytes if Path(path).resolve() == target_resolved else Path(path).read_bytes()
            rel = os.path.relpath(path)
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(data)
            hasher.update(b"\x00")
        if target_resolved not in {Path(path).resolve() for path in files}:
            rel = os.path.relpath(target)
            hasher.update(rel.encode("utf-8"))
            hasher.update(b"\x00")
            hasher.update(target_bytes)
            hasher.update(b"\x00")
        return hasher.hexdigest()

    # -- lookups -----------------------------------------------------------

    def _refuse_symlinked_root(self) -> None:
        """Refuse mutations when the selected root config is a symlink.

        The loader resolves symbolic links while reading, so a symlinked root
        would otherwise silently redirect writes to its target; the daemon
        never mutates a configuration through a symlink.
        """
        try:
            if os.path.islink(self._root_path):
                raise _store_error("Refusing to write through a symbolic link")
        except OSError as exc:
            raise _store_error(
                "The connection configuration could not be saved"
            ) from exc

    def _find(self, config: LoadedSshConfiguration, connection_id: str) -> ConnectionRecord:
        for record in config.connections:
            if record.id == connection_id:
                return record
        raise _not_found_error()

    def _ensure_unique_managed_declaration(
        self, connection_id: str, *, allow_shared: bool
    ) -> None:
        """Reject structured edits without unique Host declaration ownership."""
        occurrences = self._concrete_host_blocks_for_alias(connection_id)
        if len(occurrences) > 1:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The connection is defined by multiple Host declarations",
            )
        if occurrences and not allow_shared and len(occurrences[0].tokens) > 1:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The connection shares a Host declaration with other aliases",
            )

    def _concrete_host_blocks_for_alias(self, alias: str) -> List[HostBlock]:
        """Find authored concrete declarations for *alias* in the Include graph."""
        return [
            block
            for block in self._authored_concrete_host_blocks()
            if alias in block.tokens
        ]

    def _authored_concrete_host_blocks(self) -> List[HostBlock]:
        """Enumerate literal Host declarations across the Include graph."""
        occurrences: List[HostBlock] = []
        for cfg_file in _resolve_config_files(self._root_path):
            doc = SSHConfigDocument.parse_file(str(cfg_file))
            for node in doc.nodes:
                if (
                    isinstance(node, HostBlock)
                    and is_concrete_host_block(node)
                ):
                    occurrences.append(node)
        return occurrences

    def _authored_concrete_aliases(self) -> set[str]:
        """Return every literal alias authored in the reachable config tree."""
        return {
            alias
            for block in self._authored_concrete_host_blocks()
            for alias in block.tokens
        }

    def _ensure_authored_alias_available(self, alias: str) -> None:
        """Reject aliases colliding with any authored concrete declaration."""
        if alias in self._authored_concrete_aliases():
            raise _already_exists_error()

    def _ensure_split_destination_available(
        self, destination: str, removed_token: str
    ) -> None:
        """Reject a split destination owned after removing its source token."""
        aliases = self._authored_concrete_aliases()
        if destination == removed_token:
            aliases.discard(removed_token)
        if destination in aliases:
            raise _already_exists_error()

    # -- writes ------------------------------------------------------------

    def _append_new_block(
        self,
        path: str,
        data: Mapping[str, Any],
        *,
        expected_bytes: Optional[bytes] = None,
    ) -> None:
        """Append a new standalone Host block to *path* (create/duplicate)."""
        target = Path(path)
        if target.exists():
            doc = SSHConfigDocument.parse_file(str(target))
            block_lines = merged_block_lines(None, dict(data))
            doc.nodes.append(
                RawSpan(lines=doc.render_lines(["\n"] + block_lines))
            )
            _atomic_write_text(
                target, doc.text(), expected_bytes=expected_bytes
            )
        else:
            block_lines = merged_block_lines(None, dict(data))
            text = _SSH_CONFIG_HEADER + "".join(block_lines)
            _atomic_write_text(target, text)

    def _check_stale(self, connection_id: str, expected_generation: Optional[int]) -> None:
        if expected_generation is None:
            return
        current = self._generations.get(connection_id, 0)
        if expected_generation != current:
            raise _stale_error()

    # -- mutations ---------------------------------------------------------

    def create(self, data: Mapping[str, Any]) -> SshConfigMutationResult:
        self._refuse_symlinked_root()
        config = self.load()
        payload = dict(data)
        payload.pop("uuid", None)
        nickname = str(payload.get("nickname") or "").strip()
        if not nickname:
            raise CoreError(
                ErrorCode.VALIDATION_ERROR,
                "A nickname is required",
            )
        if any(record.id == nickname for record in config.connections):
            raise _already_exists_error()
        self._ensure_authored_alias_available(nickname)

        expected_revision = config.root_revision
        expected_bytes = (
            self._root_path.read_bytes() if self._root_path.exists() else None
        )
        if not self._revision_matches(expected_revision):
            raise _stale_error()
        self._append_new_block(
            str(self._root_path), payload, expected_bytes=expected_bytes
        )

        self._generations[nickname] = 1
        fresh = self._reload_after_write()
        return SshConfigMutationResult(config=fresh, connection_id=nickname, touched_path=str(self._root_path))

    def update(
        self,
        connection_id: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int],
    ) -> SshConfigMutationResult:
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        self._check_stale(connection_id, expected_generation)
        self._ensure_unique_managed_declaration(connection_id, allow_shared=False)

        payload = dict(data)
        payload.pop("uuid", None)
        payload.pop("source", None)
        payload.pop("source_config_path", None)
        payload["id"] = payload.get("nickname") or record.nickname
        payload["nickname"] = payload.get("nickname") or record.nickname
        _preserve_multivalue_on_update(record, payload)

        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")
        target = Path(source)
        new_name = str(payload.get("nickname") or "").strip() or connection_id
        if new_name != connection_id:
            self._ensure_authored_alias_available(new_name)

        expected_revision = config.root_revision
        expected_bytes = target.read_bytes()
        doc = SSHConfigDocument.parse_file(str(target))
        host_found = False
        new_nodes: List[Any] = []
        for node in doc.nodes:
            if (
                isinstance(node, HostBlock)
                and is_concrete_host_block(node)
                and connection_id in node.tokens
            ):
                if not host_found:
                    new_nodes.append(
                        RawSpan(
                            lines=doc.render_lines(
                                merged_block_lines(node, payload)
                            )
                        )
                    )
                host_found = True
                continue
            new_nodes.append(node)
        if not host_found:
            raise CoreError(
                ErrorCode.MUTATION_AMBIGUOUS,
                "The connection declaration changed before it could be edited",
            )
        doc.nodes = new_nodes

        if not self._revision_matches(expected_revision):
            raise _stale_error()
        _atomic_write_text(target, doc.text(), expected_bytes=expected_bytes)

        old_generation = self._generations.get(connection_id, 0)
        if new_name != connection_id:
            self._generations.pop(connection_id, None)
        self._generations[new_name] = old_generation + 1
        fresh = self._reload_after_write()
        return SshConfigMutationResult(config=fresh, connection_id=new_name, touched_path=source)

    def delete(self, connection_id: str) -> SshConfigMutationResult:
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        self._ensure_unique_managed_declaration(connection_id, allow_shared=True)
        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")
        target = Path(source)

        expected_revision = config.root_revision
        expected_bytes = target.read_bytes()
        doc = SSHConfigDocument.parse_file(str(target))
        modified = False
        new_nodes: List[Any] = []
        for node in doc.nodes:
            if (
                isinstance(node, HostBlock)
                and is_concrete_host_block(node)
                and connection_id in node.tokens
            ):
                modified = True
                remaining = [t for t in node.tokens if t != connection_id]
                if remaining:
                    header = node.lines[0]
                    indent = header[: len(header) - len(header.lstrip())]
                    node.tokens = remaining
                    node.lines[0] = doc.render_lines(
                        [f"{indent}Host {shlex.join(remaining)}\n"]
                    )[0]
                    new_nodes.append(node)
                # else: drop the whole block
                continue
            new_nodes.append(node)
        doc.nodes = new_nodes

        if not modified:
            raise _not_found_error()
        if not self._revision_matches(expected_revision):
            raise _stale_error()
        _atomic_write_text(target, doc.text(), expected_bytes=expected_bytes)

        self._generations.pop(connection_id, None)
        fresh = self._reload_after_write()
        return SshConfigMutationResult(config=fresh, connection_id=connection_id, touched_path=source)

    def duplicate(self, connection_id: str) -> SshConfigMutationResult:
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")

        base = dict(copy.deepcopy(record.data or {}))
        for key in list(base.keys()):
            if key.startswith("__") or key in {"aliases", "password_changed"}:
                base.pop(key, None)
        base.pop("uuid", None)

        existing_ids = {
            rec.id for rec in config.connections
        } | self._authored_concrete_aliases()
        new_nickname = generate_duplicate_nickname(record.nickname, existing_ids)
        base["nickname"] = new_nickname
        if "hostname" not in base or not str(base.get("hostname") or "").strip():
            base["hostname"] = record.hostname or new_nickname

        expected_revision = config.root_revision
        expected_bytes = (
            Path(source).read_bytes() if Path(source).exists() else None
        )
        if not self._revision_matches(expected_revision):
            raise _stale_error()
        self._append_new_block(
            source, base, expected_bytes=expected_bytes
        )

        self._generations[new_nickname] = 1
        fresh = self._reload_after_write()
        return SshConfigMutationResult(config=fresh, connection_id=new_nickname, touched_path=source)

    def split(
        self,
        connection_id: str,
        original_host_token: str,
        data: Mapping[str, Any],
        *,
        expected_generation: Optional[int],
    ) -> SshConfigMutationResult:
        self._refuse_symlinked_root()
        config = self.load()
        record = self._find(config, connection_id)
        self._check_stale(connection_id, expected_generation)
        self._ensure_unique_managed_declaration(
            original_host_token, allow_shared=True
        )
        source = _source_of(record)
        if not source:
            raise _store_error("The connection has no recorded source file")

        # ``source_config_path`` / ``source`` is compatibility input only: it
        # must match the daemon-recorded source when nonempty, and is never
        # used to select a different file.
        client_source = str(
            data.get("source") or data.get("source_config_path") or ""
        ).strip()
        if client_source and os.path.normpath(client_source) != os.path.normpath(source):
            raise _store_error("The requested source file does not match the recorded one")

        payload = dict(data)
        payload.pop("uuid", None)
        payload.pop("source", None)
        payload.pop("source_config_path", None)
        payload["nickname"] = str(payload.get("nickname") or "").strip()
        new_name = str(payload.get("nickname") or "").strip()
        if not original_host_token or not new_name:
            raise CoreError(
                ErrorCode.VALIDATION_ERROR,
                "A split host token and destination name are required",
            )
        if new_name != connection_id and any(
            item.id == new_name for item in config.connections
        ):
            raise _already_exists_error()
        self._ensure_split_destination_available(new_name, original_host_token)

        target = Path(source)
        expected_revision = config.root_revision
        expected_bytes = target.read_bytes()

        doc = SSHConfigDocument.parse_file(str(target))
        matching_blocks = [
            node
            for node in doc.nodes
            if (
                isinstance(node, HostBlock)
                and is_concrete_host_block(node)
                and original_host_token in node.tokens
            )
        ]
        if not matching_blocks:
            raise CoreError(
                ErrorCode.VALIDATION_ERROR,
                "The original host token was not found",
            )
        new_nodes: List[Any] = []
        for node in doc.nodes:
            if (
                isinstance(node, HostBlock)
                and is_concrete_host_block(node)
                and original_host_token in node.tokens
            ):
                remaining = [t for t in node.tokens if t != original_host_token]
                if remaining:
                    header = node.lines[0]
                    indent = header[: len(header) - len(header.lstrip())]
                    node.tokens = remaining
                    node.lines[0] = doc.render_lines(
                        [f"{indent}Host {shlex.join(remaining)}\n"]
                    )[0]
                    new_nodes.append(node)
                continue
            new_nodes.append(node)
        block_lines = merged_block_lines(None, payload)
        new_nodes.append(RawSpan(lines=doc.render_lines(["\n"] + block_lines)))
        doc.nodes = new_nodes

        if not self._revision_matches(expected_revision):
            raise _stale_error()
        _atomic_write_text(target, doc.text(), expected_bytes=expected_bytes)

        old_generation = self._generations.get(connection_id, 0)
        self._generations.pop(connection_id, None)
        self._generations[new_name] = old_generation + 1
        fresh = self._reload_after_write()
        if connection_id in {c.id for c in fresh.connections}:
            # The original connection survived the split (a different alias
            # was split out): keep its generation counter intact.
            self._generations[connection_id] = old_generation
        return SshConfigMutationResult(config=fresh, connection_id=new_name, touched_path=source)
