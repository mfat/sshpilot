"""One-time split of the shared identity sidecar into one file per SSH root.

Default and Isolated mode are independent SSH configuration documents, but
historically both were backed by a single ``connections.json``. Everything a
connection carries beyond its ``Host`` block -- its UUID, display name, folder,
tags, ordering, and the non-SSH connections alongside it -- therefore lived in
one namespace spanning two roots, and a mode switch reconciled that namespace
against whichever root had just become active.

This module partitions an existing shared sidecar into the per-root files the
daemon now expects. It runs once, before the repository is constructed, so no
reader ever observes a half-split pair.

Partitioning is by *which root's include graph an identity was declared in*.
``projection.source`` names the declaring file, not the root, so an entry
pulled in through an ``Include`` (``sshpilot-imported.conf``, a ``conf.d``
fragment) carries the fragment's path -- comparing it against the two root
paths would misfile every such entry.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .identity_state_v2 import (
    ConnectionReference,
    IdentityStateV2,
    ReferenceKind,
    UuidGroupState,
)
from .ssh_config_store import SshConfigStore
from .state_file import (
    ConnectionStateFileKind,
    identity_transaction_intent_path,
    probe_connection_state_file,
    read_identity_state_v2,
    write_identity_state_v2,
)

logger = logging.getLogger(__name__)

#: Bumped only if the partitioning rules themselves change.
WORKSPACE_SPLIT_VERSION = 1

#: Environment escape hatch for support: skip the migration entirely.
SKIP_ENV = "SSHPILOT_SKIP_WORKSPACE_SPLIT"

#: Sentinel recording that the split already ran.
#:
#: Deliberately its own file rather than a key in ``config.json``: the daemon
#: must not rewrite the user's settings file as a side effect of starting up
#: (doing so normalizes it, which silently rewrites legacy connection state
#: that has not been migrated yet). It is also what makes the migration run
#: exactly once -- keying off "the isolated sidecar is missing" would re-split
#: an already-partitioned default workspace if the user ever deleted it.
MARKER_NAME = ".connections-workspace-split"


def marker_path(config_dir) -> Path:
    return Path(config_dir) / MARKER_NAME


def already_split(config_dir) -> bool:
    marker = marker_path(config_dir)
    try:
        return int(marker.read_text(encoding="utf-8").strip()) >= WORKSPACE_SPLIT_VERSION
    except (OSError, ValueError):
        return False


def _record_split(config_dir) -> None:
    marker = marker_path(config_dir)
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(f"{WORKSPACE_SPLIT_VERSION}\n", encoding="utf-8")
    os.replace(tmp, marker)
    _fsync_dir(marker.parent)


class WorkspaceSplitSkipped(Exception):
    """The split did not run. Carries a machine-readable reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _realpath(path) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(str(path))))


def _is_under(path: str, root_dir: str) -> bool:
    return path == root_dir or path.startswith(root_dir + os.sep)


def _source_paths(root: Path, *, isolated: bool) -> frozenset:
    """Resolved include graph of one root, or empty when it cannot be read."""
    try:
        loaded = SshConfigStore(Path(root), isolated=isolated).load()
    except Exception:
        # A root that will not parse cannot contribute evidence; containment
        # still classifies its identities correctly.
        logger.debug("Workspace split could not load root %s", root, exc_info=True)
        return frozenset()
    return frozenset(_realpath(item) for item in (loaded.source_paths or ()))


class _Classifier:
    """Decide which root an identity was declared in."""

    def __init__(
        self,
        *,
        default_root: Path,
        isolated_root: Path,
        config_dir: Path,
    ) -> None:
        self._default_sources = _source_paths(default_root, isolated=False)
        self._isolated_sources = _source_paths(isolated_root, isolated=True)
        self._config_dir = _realpath(config_dir)
        self._isolated_root = _realpath(isolated_root)

    def is_isolated(self, source: str) -> bool:
        """True when *source* belongs to the isolated root's document."""
        if not source:
            # No declaring file recorded (a v1 migration artefact). The default
            # root owned the shared sidecar, so that is the safe home.
            return False
        resolved = _realpath(source)
        in_isolated = resolved in self._isolated_sources
        in_default = resolved in self._default_sources
        if in_isolated and not in_default:
            return True
        if in_default and not in_isolated:
            return False
        # Either both roots include the file, or -- far more common for the
        # tombstones that dominate a real sidecar -- neither still does,
        # because the fragment was edited away. Fall back to containment:
        # anything inside sshPilot's own config directory is the isolated
        # document, everything else is the user's.
        return _is_under(resolved, self._config_dir) or resolved == self._isolated_root


def _partition_groups(
    groups: Tuple[UuidGroupState, ...], keep: set
) -> Tuple[UuidGroupState, ...]:
    """Copy every group, keeping only the members this workspace owns.

    Folders are part of both workspaces: a folder is a thing the user made,
    not a property of one root's hosts. Dropping a group that ends up empty
    would silently delete it from one mode, so empty groups are kept -- an
    empty folder is recoverable, a deleted one is not. Parents are copied
    alongside children, so the acyclic-parent validation still holds.
    """
    result = []
    for group in groups:
        members = tuple(
            reference
            for reference in group.members
            if reference.kind is not ReferenceKind.SSH_UUID
            or reference.value in keep
        )
        result.append(
            UuidGroupState(
                id=group.id,
                name=group.name,
                members=members,
                parent_id=group.parent_id,
                order=group.order,
                color=group.color,
            )
        )
    return tuple(result)


def _partition_roots(
    references: Tuple[ConnectionReference, ...], keep: set
) -> Tuple[ConnectionReference, ...]:
    return tuple(
        reference
        for reference in references
        if reference.kind is not ReferenceKind.SSH_UUID or reference.value in keep
    )


def partition_state(
    state: IdentityStateV2,
    classifier: _Classifier,
    *,
    non_ssh_to_isolated: bool,
) -> Tuple[IdentityStateV2, IdentityStateV2]:
    """Split one shared sidecar into ``(default_state, isolated_state)``."""

    default_identities = []
    isolated_identities = []
    for identity in state.identities:
        if classifier.is_isolated(identity.projection.source):
            isolated_identities.append(identity)
        else:
            default_identities.append(identity)

    default_uuids = {identity.uuid for identity in default_identities}
    isolated_uuids = {identity.uuid for identity in isolated_identities}

    def _metadata(keep: set) -> Dict[str, Mapping[str, Any]]:
        return {
            uuid: dict(values)
            for uuid, values in state.metadata.items()
            if uuid in keep
        }

    # Non-SSH connections are not declared in any ssh_config, so nothing says
    # which root they belong to. They go to the workspace the user is looking
    # at right now, so nothing appears to vanish on upgrade; the other mode
    # starts empty and fills up from there.
    non_ssh = tuple(dict(item) for item in state.non_ssh_connections)
    non_ssh_meta = {
        key: dict(values) for key, values in state.non_ssh_metadata.items()
    }

    def _build(
        identities, keep: set, *, owns_non_ssh: bool, orphans
    ) -> IdentityStateV2:
        references = _partition_roots(state.root_connections, keep)
        if not owns_non_ssh:
            references = tuple(
                reference
                for reference in references
                if reference.kind is not ReferenceKind.NON_SSH_ID
            )
        groups = _partition_groups(state.groups, keep)
        if not owns_non_ssh:
            groups = tuple(
                UuidGroupState(
                    id=group.id,
                    name=group.name,
                    members=tuple(
                        reference
                        for reference in group.members
                        if reference.kind is not ReferenceKind.NON_SSH_ID
                    ),
                    parent_id=group.parent_id,
                    order=group.order,
                    color=group.color,
                )
                for group in groups
            )
        return IdentityStateV2(
            identities=tuple(identities),
            groups=groups,
            root_connections=references,
            metadata=_metadata(keep),
            non_ssh_connections=non_ssh if owns_non_ssh else (),
            non_ssh_metadata=non_ssh_meta if owns_non_ssh else {},
            legacy_orphans=orphans,
            # A pending ambiguity is recorded against one root's revision and
            # can only be resolved by the aliases that root still offers. The
            # two roots have different revisions, so carrying one across the
            # split is meaningless; a still-real ambiguity is re-derived on
            # the first reconcile pass.
            pending_ambiguities=(),
            sidecar_generation=state.sidecar_generation,
            # Force an honest reconcile of each workspace against its own
            # root the first time it is loaded.
            last_reconciled_ssh_revision=None,
            observed_ssh_revision=None,
        )

    default_state = _build(
        default_identities,
        default_uuids,
        owns_non_ssh=not non_ssh_to_isolated,
        orphans=state.legacy_orphans,
    )
    isolated_state = _build(
        isolated_identities,
        isolated_uuids,
        owns_non_ssh=non_ssh_to_isolated,
        orphans=(),
    )
    return default_state, isolated_state


def _backup_path(shared_path: Path) -> Optional[Path]:
    """Newest existing pre-split backup, if the split already made one."""
    candidates = sorted(
        shared_path.parent.glob(shared_path.name + ".pre-workspace-split-*")
    )
    return candidates[-1] if candidates else None


def split_workspaces(
    *,
    shared_path: Path,
    isolated_path: Path,
    default_root: Path,
    isolated_root: Path,
    config_dir: Path,
    non_ssh_to_isolated: bool,
) -> Tuple[IdentityStateV2, IdentityStateV2]:
    """Partition the shared sidecar in place. Raises ``WorkspaceSplitSkipped``.

    Always partitions from the backup rather than from the live file, so a
    crash part-way through leaves a re-run producing identical bytes instead
    of re-splitting an already-pruned file.
    """
    shared_path = Path(shared_path)
    isolated_path = Path(isolated_path)

    if os.environ.get(SKIP_ENV):
        raise WorkspaceSplitSkipped("disabled by environment")

    intent = identity_transaction_intent_path(shared_path)
    if intent.exists() or intent.is_symlink():
        # Crash recovery owns this file for the current boot.
        raise WorkspaceSplitSkipped("pending identity transaction")

    backup = _backup_path(shared_path)
    if backup is None:
        if not shared_path.exists():
            raise WorkspaceSplitSkipped("no shared sidecar")
        kind = probe_connection_state_file(shared_path)
        if kind is not ConnectionStateFileKind.V2:
            # v1 is migrated by the repository itself on first load, and a
            # corrupt file must never be silently replaced.
            raise WorkspaceSplitSkipped(f"shared sidecar is {kind.value}")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        backup = shared_path.with_name(
            f"{shared_path.name}.pre-workspace-split-{stamp}"
        )
        shutil.copy2(shared_path, backup)
        _fsync_dir(shared_path.parent)

    state = read_identity_state_v2(backup)
    if state is None:
        raise WorkspaceSplitSkipped("backup disappeared")

    classifier = _Classifier(
        default_root=Path(default_root),
        isolated_root=Path(isolated_root),
        config_dir=Path(config_dir),
    )
    # Built and validated in memory before a single byte is written: an
    # invalid partition raises here rather than landing on disk.
    default_state, isolated_state = partition_state(
        state, classifier, non_ssh_to_isolated=non_ssh_to_isolated
    )

    write_identity_state_v2(isolated_path, isolated_state)
    write_identity_state_v2(shared_path, default_state)
    logger.info(
        "Split the shared connection sidecar into per-root workspaces "
        "(default=%d isolated=%d identities, backup=%s)",
        len(default_state.identities),
        len(isolated_state.identities),
        backup.name,
    )
    return default_state, isolated_state


def _fsync_dir(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def ensure_workspaces_split(
    *,
    config_dir: Path,
    shared_path: Path,
    isolated_path: Path,
    default_root: Path,
    isolated_root: Path,
    non_ssh_to_isolated: bool,
) -> bool:
    """Run the split once per install. Never raises.

    Returns True when the workspaces are known to be split (now or earlier).
    A failure here must not stop the daemon: the shared sidecar is still
    intact and the app keeps working exactly as it did before.
    """
    config_dir = Path(config_dir)
    if already_split(config_dir):
        return True
    try:
        split_workspaces(
            shared_path=shared_path,
            isolated_path=isolated_path,
            default_root=default_root,
            isolated_root=isolated_root,
            config_dir=config_dir,
            non_ssh_to_isolated=non_ssh_to_isolated,
        )
    except WorkspaceSplitSkipped as skipped:
        if skipped.reason == "pending identity transaction":
            # Crash recovery owns the sidecar this boot; retry next start
            # rather than recording a split that never happened.
            logger.info(
                "Deferring the connection workspace split: %s", skipped.reason
            )
            return False
        logger.debug("Connection workspace split not needed: %s", skipped.reason)
    except Exception:
        logger.warning(
            "Could not split the shared connection sidecar into per-root "
            "workspaces; continuing with the existing state file",
            exc_info=True,
        )
        return False
    try:
        _record_split(config_dir)
    except OSError:
        logger.warning(
            "Could not record that the connection workspace split completed",
            exc_info=True,
        )
        return False
    return True
