"""Daemon-owned login profile service (GTK-free).

Owns ``login_profiles.json`` (profiles and group links) and keeps every linked
SSH connection's ``Host`` block resolved to its effective profile:

* profile edits, assignments, and group-profile changes re-render the affected
  Host blocks through ``ConnectionRepository.update_connection``;
* the connection's link (``login_profile`` connection metadata) records a
  fingerprint of the block as written, and the profile revision it came from;
* :meth:`reconcile` — also run on every repository change not caused by this
  service — detaches links whose block was edited outside the profile (drift),
  detaches explicit links to missing profiles, and re-applies links whose
  effective profile or revision changed (e.g. after a group move).

Profile secrets go through an injected :class:`ProfileSecretStore` (the daemon
backs it with the active secret backend, keyed under
:func:`~.models.profile_secret_host`); only ``has_*`` flags are persisted here.
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Tuple

from ..errors import CoreError, ErrorCode
from .models import (
    LINK_EXPLICIT,
    LINK_INHERIT,
    LINK_METADATA_KEY,
    PROFILE_PASSWORD_ACCOUNT,
    PROFILE_SUDO_ACCOUNT,
    EDITABLE_FIELDS,
    FieldChange,
    LoginProfile,
    LoginProfileState,
    ProfileLink,
    apply_profile_to_data,
    auth_fingerprint,
    auth_projection,
    diff_projections,
    extra_keywords,
    new_profile_id,
    parse_link,
    revision_token,
    sorted_profiles,
)
from .resolver import (
    EffectiveProfile,
    effective_profile,
    group_profile,
    primary_group_id,
)
from .store import read_login_profiles, write_login_profiles

logger = logging.getLogger(__name__)

SCOPE_DEFAULT = "default"
SCOPE_ISOLATED = "isolated"

DETACH_DRIFT = "drift"
DETACH_MISSING = "profile_missing"
DETACH_DELETED = "profile_deleted"

EVENT_DETACHED = "detached"
EVENT_CHANGED = "changed"


def _not_found() -> CoreError:
    return CoreError(ErrorCode.VALIDATION_ERROR, "The login profile does not exist")


def _validation(message: str) -> CoreError:
    return CoreError(ErrorCode.VALIDATION_ERROR, message)


class ProfileSecretStore(Protocol):
    """Secret persistence for one profile secret (``account`` = login/sudo)."""

    def store(self, profile_id: str, account: str, value: str) -> bool: ...

    def lookup(self, profile_id: str, account: str) -> Optional[str]: ...

    def delete(self, profile_id: str, account: str) -> bool: ...


@dataclass(frozen=True)
class ProfileUsage:
    profile_id: str
    connections: Tuple[str, ...]  # explicitly linked
    inherited_connections: Tuple[str, ...]  # linked through a group
    groups: Tuple[str, ...]  # groups (in the active scope) assigned this profile


@dataclass(frozen=True)
class AssignmentPreview:
    connection_id: str
    profile_name: str  # "" when the target resolves to no profile
    changes: Tuple[FieldChange, ...]


@dataclass(frozen=True)
class DetachedConnection:
    connection_id: str
    profile_name: str
    reason: str


class LoginProfileService:
    """Authoritative login profile state and its resolution into Host blocks."""

    def __init__(
        self,
        repository: Any,
        path: Path,
        *,
        secret_store: Optional[ProfileSecretStore] = None,
        on_event: Optional[Callable[[str, Mapping[str, Any]], None]] = None,
        listen: bool = True,
    ) -> None:
        self._repository = repository
        self._path = Path(path)
        self._lock = threading.RLock()
        self._busy = 0
        self._secret_store = secret_store
        self._on_event = on_event
        self._load_error: Optional[CoreError] = None
        try:
            self._state = read_login_profiles(self._path)
        except CoreError as exc:
            # Never replace an unreadable file with defaults, and never
            # reconcile against an empty state (that would detach every link).
            logger.error(
                "Login profiles are unavailable: %s could not be read (%s)",
                self._path,
                exc.diagnostic_reason or exc,
            )
            self._state = LoginProfileState()
            self._load_error = exc
        if listen and hasattr(repository, "add_listener"):
            repository.add_listener(self._on_repository_change)

    # ------------------------------------------------------------------
    # infrastructure
    # ------------------------------------------------------------------

    @contextmanager
    def _operation(self):
        with self._lock:
            self._busy += 1
            try:
                yield
            finally:
                self._busy -= 1

    def _scope(self) -> str:
        isolated = bool(getattr(self._repository, "ssh_config_isolated", False))
        return SCOPE_ISOLATED if isolated else SCOPE_DEFAULT

    @property
    def available(self) -> bool:
        return self._load_error is None

    def _require_available(self) -> None:
        if self._load_error is not None:
            raise CoreError(
                ErrorCode.CONNECTION_STATE_IO_ERROR,
                "Login profiles are unavailable because their file could not be read",
            )

    def _save(self, state: LoginProfileState) -> None:
        self._require_available()
        write_login_profiles(self._path, state)
        self._state = state

    def _emit(self, kind: str, payload: Mapping[str, Any]) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(kind, payload)
        except Exception:  # an observer must never break a mutation
            logger.exception("Login profile event observer failed")

    def _secrets(self) -> ProfileSecretStore:
        if self._secret_store is None:
            raise CoreError(
                ErrorCode.CONNECTION_STATE_IO_ERROR,
                "No secret storage is configured for login profiles",
            )
        return self._secret_store

    def _snapshot(self):
        return self._repository.snapshot()

    def _metadata_map(self, snapshot) -> Dict[str, Mapping[str, Any]]:
        return {m.connection_id: m.values for m in snapshot.metadata}

    def _ssh_ids(self, snapshot) -> List[str]:
        return [c.id for c in snapshot.connections if (c.protocol or "ssh") == "ssh"]

    def _group_links(self) -> Dict[str, str]:
        return self._state.links_for(self._scope())

    def _link_for(self, connection_id: str, snapshot=None) -> Optional[ProfileLink]:
        snapshot = snapshot or self._snapshot()
        return parse_link(self._metadata_map(snapshot).get(connection_id))

    def _require_ssh(self, connection_id: str):
        record = self._repository.get_record(connection_id)
        if record is None:
            raise CoreError(ErrorCode.CONNECTION_NOT_FOUND, "The connection does not exist")
        if (record.protocol or "ssh") != "ssh":
            raise _validation("Login profiles apply to SSH connections only")
        return record

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------

    def list_profiles(self) -> Tuple[LoginProfile, ...]:
        with self._lock:
            return self._state.profiles

    def get_profile(self, profile_id: str) -> Optional[LoginProfile]:
        with self._lock:
            return self._state.get(profile_id)

    def set_event_observer(
        self, callback: Optional[Callable[[str, Mapping[str, Any]], None]]
    ) -> None:
        self._on_event = callback

    def group_links(self) -> Dict[str, str]:
        """Group id -> profile id for the active SSH configuration."""
        with self._lock:
            return self._group_links()

    def link_states(
        self,
    ) -> Tuple[Tuple[str, ProfileLink, Optional[EffectiveProfile]], ...]:
        """Every linked SSH connection with its link and effective profile."""
        with self._lock:
            snapshot = self._snapshot()
            metadata = self._metadata_map(snapshot)
            links = self._group_links()
            states = []
            for cid in self._ssh_ids(snapshot):
                link = parse_link(metadata.get(cid))
                if link is None:
                    continue
                states.append(
                    (cid, link, effective_profile(cid, link, snapshot.groups, links, self._state))
                )
            return tuple(states)

    def group_profile_id(self, group_id: str) -> Optional[str]:
        with self._lock:
            return self._group_links().get(group_id)

    def effective_for(self, connection_id: str) -> Optional[EffectiveProfile]:
        with self._lock:
            snapshot = self._snapshot()
            return effective_profile(
                connection_id,
                self._link_for(connection_id, snapshot),
                snapshot.groups,
                self._group_links(),
                self._state,
            )

    def link_for(self, connection_id: str) -> Optional[ProfileLink]:
        with self._lock:
            return self._link_for(connection_id)

    def inherited_profile_for_group(self, group_id: Optional[str]) -> Optional[EffectiveProfile]:
        """Profile a connection in *group_id* would inherit (for editors)."""
        with self._lock:
            snapshot = self._snapshot()
            return group_profile(group_id, snapshot.groups, self._group_links(), self._state)

    def usage(self, profile_id: str) -> ProfileUsage:
        with self._lock:
            if self._state.get(profile_id) is None:
                raise _not_found()
            snapshot = self._snapshot()
            metadata = self._metadata_map(snapshot)
            links = self._group_links()
            explicit: List[str] = []
            inherited: List[str] = []
            for cid in self._ssh_ids(snapshot):
                link = parse_link(metadata.get(cid))
                if link is None:
                    continue
                if link.mode == LINK_EXPLICIT and link.profile_id == profile_id:
                    explicit.append(cid)
                elif link.mode == LINK_INHERIT:
                    eff = effective_profile(cid, link, snapshot.groups, links, self._state)
                    if eff is not None and eff.profile.id == profile_id:
                        inherited.append(cid)
            groups = tuple(
                g.id for g in snapshot.groups if links.get(g.id) == profile_id
            )
            return ProfileUsage(profile_id, tuple(explicit), tuple(inherited), groups)

    # ------------------------------------------------------------------
    # profile CRUD
    # ------------------------------------------------------------------

    def _check_name(self, name: str, *, exclude: Optional[str] = None) -> None:
        folded = str(name or "").strip().casefold()
        for profile in self._state.profiles:
            if profile.id != exclude and profile.name.casefold() == folded:
                raise _validation("A login profile with this name already exists")

    @staticmethod
    def _settings(values: Mapping[str, Any]) -> Dict[str, Any]:
        unknown = set(values) - EDITABLE_FIELDS
        if unknown:
            raise _validation(f"Unknown login profile fields: {sorted(unknown)}")
        return dict(values)

    def create_profile(self, values: Mapping[str, Any]) -> LoginProfile:
        with self._operation():
            settings = self._settings(values)
            self._check_name(settings.get("name", ""))
            try:
                profile = LoginProfile(id=new_profile_id(), **settings)
            except (TypeError, ValueError) as exc:
                raise _validation(str(exc)) from exc
            state = LoginProfileState(
                profiles=sorted_profiles(self._state.profiles + (profile,)),
                group_links=self._state.group_links,
            )
            self._save(state)
            self._emit(EVENT_CHANGED, {"profile_id": profile.id})
            return profile

    def update_profile(
        self,
        profile_id: str,
        changes: Mapping[str, Any],
        *,
        expected_revision: Optional[int] = None,
    ) -> LoginProfile:
        """Update a profile and re-render every connection that uses it."""
        with self._operation():
            current = self._state.get(profile_id)
            if current is None:
                raise _not_found()
            if expected_revision is not None and expected_revision != current.revision:
                raise CoreError(
                    ErrorCode.STALE_CONNECTION_STATE,
                    "The login profile has been modified since it was last read",
                )
            settings = self._settings(changes)
            if "name" in settings:
                self._check_name(settings["name"], exclude=profile_id)
            try:
                updated = current.with_changes(revision=current.revision + 1, **settings)
            except (TypeError, ValueError) as exc:
                raise _validation(str(exc)) from exc
            self._replace_profile(updated)
            self._reconcile_locked()
            self._emit(EVENT_CHANGED, {"profile_id": profile_id})
            return updated

    def _replace_profile(self, profile: LoginProfile) -> None:
        profiles = tuple(
            profile if p.id == profile.id else p for p in self._state.profiles
        )
        self._save(
            LoginProfileState(
                profiles=sorted_profiles(profiles), group_links=self._state.group_links
            )
        )

    def delete_profile(
        self,
        profile_id: str,
        *,
        replacement_id: Optional[str] = None,
        overrides: Optional[Mapping[str, Optional[str]]] = None,
    ) -> Tuple[DetachedConnection, ...]:
        """Delete a profile, reassigning or detaching everything that used it.

        Every affected connection id and ``group:<id>`` key resolves to
        ``overrides.get(key, replacement_id)``: a profile id reassigns it, and
        ``None`` detaches it. A detached connection keeps its current Host
        block values; connections inheriting through a detached group become
        custom (unlinked) with their current values.
        """
        with self._operation():
            self._require_available()
            profile = self._state.get(profile_id)
            if profile is None:
                raise _not_found()
            overrides = dict(overrides or {})
            targets = [replacement_id, *overrides.values()]
            for target in targets:
                if target is not None and (
                    target == profile_id or self._state.get(target) is None
                ):
                    raise _validation("Replacement login profile is invalid")

            usage = self.usage(profile_id)
            detached: List[DetachedConnection] = []

            # Group links (in every scope) first, so inheriting connections
            # resolve against the post-delete assignment.
            scope = self._scope()
            group_links: Dict[str, Dict[str, str]] = {}
            for link_scope, links in self._state.group_links.items():
                updated_links: Dict[str, str] = {}
                for gid, pid in links.items():
                    if pid != profile_id:
                        updated_links[gid] = pid
                        continue
                    key = f"group:{gid}"
                    target = overrides.get(key, replacement_id) if link_scope == scope else replacement_id
                    if target is not None:
                        updated_links[gid] = target
                group_links[link_scope] = updated_links

            # Connections inheriting the deleted profile whose group is being
            # detached become custom *before* the profile disappears.
            snapshot = self._snapshot()
            new_links = group_links.get(scope, {})
            for cid in usage.inherited_connections:
                gid = primary_group_id(cid, snapshot.groups)
                eff_after = group_profile(
                    gid,
                    snapshot.groups,
                    new_links,
                    LoginProfileState(
                        profiles=tuple(p for p in self._state.profiles if p.id != profile_id),
                    ),
                )
                if eff_after is None:
                    self._detach_locked(cid)
                    detached.append(DetachedConnection(cid, profile.name, DETACH_DELETED))

            for cid in usage.connections:
                target = overrides.get(cid, replacement_id)
                if target is None:
                    self._detach_locked(cid)
                    detached.append(DetachedConnection(cid, profile.name, DETACH_DELETED))
                else:
                    self._set_link_locked(cid, ProfileLink(LINK_EXPLICIT, target))

            self._save(
                LoginProfileState(
                    profiles=tuple(p for p in self._state.profiles if p.id != profile_id),
                    group_links=group_links,
                )
            )
            self._delete_secrets(profile_id)
            self._reconcile_locked()
            self._emit(EVENT_CHANGED, {"profile_id": profile_id, "deleted": True})
            return tuple(detached)

    # ------------------------------------------------------------------
    # secrets
    # ------------------------------------------------------------------

    def _set_secret(self, profile_id: str, account: str, flag: str, value: Optional[str]) -> bool:
        with self._lock:
            self._require_available()
            if self._state.get(profile_id) is None:
                raise _not_found()
        if value and "\x00" in value:
            raise _validation("Secrets must not contain NUL")
        # The backend call may wait on an unlock prompt: never hold the
        # service lock across it.
        store = self._secrets()
        if value:
            ok = bool(store.store(profile_id, account, value))
        else:
            # Deleting an absent secret already satisfies the request.
            store.delete(profile_id, account)
            ok = True
        if ok:
            with self._operation():
                profile = self._state.get(profile_id)
                if profile is None:  # deleted meanwhile
                    store.delete(profile_id, account)
                    raise _not_found()
                self._replace_profile(profile.with_changes(**{flag: bool(value)}))
        return ok

    def set_password(self, profile_id: str, password: Optional[str]) -> bool:
        return self._set_secret(profile_id, PROFILE_PASSWORD_ACCOUNT, "has_password", password)

    def set_sudo_password(self, profile_id: str, password: Optional[str]) -> bool:
        return self._set_secret(profile_id, PROFILE_SUDO_ACCOUNT, "has_sudo_password", password)

    def lookup_password(self, profile_id: str) -> Optional[str]:
        return self._secrets().lookup(profile_id, PROFILE_PASSWORD_ACCOUNT) or None

    def lookup_sudo_password(self, profile_id: str) -> Optional[str]:
        return self._secrets().lookup(profile_id, PROFILE_SUDO_ACCOUNT) or None

    def password_for_connection(self, connection_id: str) -> Optional[str]:
        eff = self.effective_for(connection_id)
        if eff is None or not eff.profile.has_password:
            return None
        return self.lookup_password(eff.profile.id)

    def sudo_password_for_connection(self, connection_id: str) -> Optional[str]:
        eff = self.effective_for(connection_id)
        if eff is None or not eff.profile.has_sudo_password:
            return None
        return self.lookup_sudo_password(eff.profile.id)

    def _delete_secrets(self, profile_id: str) -> None:
        try:
            store = self._secrets()
            for account in (PROFILE_PASSWORD_ACCOUNT, PROFILE_SUDO_ACCOUNT):
                store.delete(profile_id, account)
        except Exception:
            logger.warning("Could not delete secrets of login profile %s", profile_id)

    # ------------------------------------------------------------------
    # assignment
    # ------------------------------------------------------------------

    def _target_link(self, mode: Optional[str], profile_id: Optional[str]) -> Optional[ProfileLink]:
        if mode is None:
            return None
        if mode == LINK_EXPLICIT:
            if self._state.get(profile_id) is None:
                raise _not_found()
            return ProfileLink(LINK_EXPLICIT, profile_id)
        if mode == LINK_INHERIT:
            return ProfileLink(LINK_INHERIT)
        raise _validation("Unknown login profile link mode")

    def preview_assignment(
        self,
        connection_ids: Iterable[str],
        mode: Optional[str],
        profile_id: Optional[str] = None,
    ) -> Tuple[AssignmentPreview, ...]:
        """What linking each connection would change in its Host block."""
        with self._lock:
            link = self._target_link(mode, profile_id)
            snapshot = self._snapshot()
            previews: List[AssignmentPreview] = []
            for cid in connection_ids:
                record = self._require_ssh(cid)
                eff = effective_profile(
                    cid, link, snapshot.groups, self._group_links(), self._state
                )
                if eff is None:
                    previews.append(AssignmentPreview(cid, "", ()))
                    continue
                keys = extra_keywords(eff.profile.extra_ssh_config)
                before = auth_projection(record.data, keys, expand_paths=True)
                after = auth_projection(
                    apply_profile_to_data(record.data, eff.profile), keys, expand_paths=True
                )
                previews.append(
                    AssignmentPreview(cid, eff.profile.name, diff_projections(before, after))
                )
            return tuple(previews)

    def assign_connections(
        self,
        connection_ids: Iterable[str],
        mode: Optional[str],
        profile_id: Optional[str] = None,
    ) -> None:
        """Link connections explicitly, to their group (inherit), or unlink (``None``).

        Unlinking keeps the connection's current Host block values.
        """
        with self._operation():
            self._require_available()
            link = self._target_link(mode, profile_id)
            ids = list(connection_ids)
            for cid in ids:
                self._require_ssh(cid)
            for cid in ids:
                if link is None:
                    self._detach_locked(cid)
                else:
                    self._set_link_locked(cid, link)
            self._reconcile_locked()

    def set_group_profile(
        self,
        group_id: str,
        profile_id: Optional[str],
        *,
        link_members: Iterable[str] = (),
    ) -> None:
        """Assign (or clear) a group's profile.

        ``link_members`` are connections switched to ``inherit`` as part of
        the change (the UI preselects members with no explicit profile).
        Connections already inheriting follow the new assignment.
        """
        with self._operation():
            snapshot = self._snapshot()
            if group_id not in {g.id for g in snapshot.groups}:
                raise _validation("The group does not exist")
            if profile_id is not None and self._state.get(profile_id) is None:
                raise _not_found()
            scope = self._scope()
            group_links = {s: dict(l) for s, l in self._state.group_links.items()}
            links = group_links.setdefault(scope, {})
            if profile_id is None:
                links.pop(group_id, None)
            else:
                links[group_id] = profile_id
            self._save(
                LoginProfileState(profiles=self._state.profiles, group_links=group_links)
            )
            for cid in link_members:
                self._require_ssh(cid)
                self._set_link_locked(cid, ProfileLink(LINK_INHERIT))
            self._reconcile_locked()
            self._emit(EVENT_CHANGED, {"group_id": group_id})

    def _set_link_locked(self, connection_id: str, link: ProfileLink) -> None:
        # A fresh link carries no fingerprint/revision, so reconcile applies it.
        self._repository.update_connection_metadata(
            connection_id, {LINK_METADATA_KEY: link.to_metadata()}
        )

    def _detach_locked(self, connection_id: str) -> None:
        self._repository.update_connection_metadata(connection_id, {LINK_METADATA_KEY: None})

    def _apply_locked(self, connection_id: str, link: ProfileLink, profile: LoginProfile) -> None:
        record = self._require_ssh(connection_id)
        payload = apply_profile_to_data(record.data, profile)
        updated = self._repository.update_connection(connection_id, payload)
        new_id = getattr(updated, "id", None) or connection_id
        written = self._repository.get_record(new_id) or updated
        keys = extra_keywords(profile.extra_ssh_config)
        applied = ProfileLink(
            link.mode,
            link.profile_id,
            auth_fingerprint(written.data, keys),
            revision_token(profile),
            keys,
        )
        self._repository.update_connection_metadata(
            new_id, {LINK_METADATA_KEY: applied.to_metadata()}
        )

    # ------------------------------------------------------------------
    # reconciliation
    # ------------------------------------------------------------------

    def reconcile(self) -> Tuple[DetachedConnection, ...]:
        with self._operation():
            return self._reconcile_locked()

    def _reconcile_locked(self) -> Tuple[DetachedConnection, ...]:
        if self._load_error is not None:
            return ()
        snapshot = self._snapshot()
        metadata = self._metadata_map(snapshot)
        links = self._group_links()
        detached: List[DetachedConnection] = []
        for cid in self._ssh_ids(snapshot):
            link = parse_link(metadata.get(cid))
            if link is None:
                continue
            record = self._repository.get_record(cid)
            if record is None:
                continue
            previous = self._state.get((link.rev or "").split(":", 1)[0])
            previous_name = previous.name if previous else ""
            if link.applied and auth_fingerprint(record.data, link.extra_keys) != link.applied:
                self._detach_locked(cid)
                detached.append(DetachedConnection(cid, previous_name, DETACH_DRIFT))
                continue
            eff = effective_profile(cid, link, snapshot.groups, links, self._state)
            if eff is None:
                if link.mode == LINK_EXPLICIT:
                    self._detach_locked(cid)
                    detached.append(DetachedConnection(cid, previous_name, DETACH_MISSING))
                continue
            if link.rev != revision_token(eff.profile) or not link.applied:
                try:
                    self._apply_locked(cid, link, eff.profile)
                except CoreError:
                    logger.warning(
                        "Could not apply login profile %s to %s", eff.profile.id, cid
                    )
        for item in detached:
            logger.info(
                "Connection %s detached from login profile %r (%s)",
                item.connection_id,
                item.profile_name,
                item.reason,
            )
            self._emit(
                EVENT_DETACHED,
                {
                    "connection_id": item.connection_id,
                    "profile_name": item.profile_name,
                    "reason": item.reason,
                },
            )
        return tuple(detached)

    def _on_repository_change(self, _change: Any) -> None:
        with self._lock:
            if self._busy:
                return
            try:
                with self._operation():
                    self._reconcile_locked()
            except Exception:
                logger.exception("Login profile reconciliation failed")

    # ------------------------------------------------------------------
    # backup
    # ------------------------------------------------------------------

    def snapshot_for_backup(self) -> Dict[str, Any]:
        """Profiles and group links, without secrets or secret flags."""
        with self._lock:
            data = self._state.to_dict()
            for item in data["profiles"].values():
                item["has_password"] = False
                item["has_sudo_password"] = False
            return data

    def restore_from_backup(self, section: Mapping[str, Any], *, mode: str = "merge") -> int:
        """Restore profiles; ``merge`` keeps existing ones and suffixes clashing names."""
        with self._operation():
            try:
                incoming = LoginProfileState.from_dict(section)
            except (TypeError, ValueError) as exc:
                raise _validation("The login profile backup is invalid") from exc
            if mode == "replace":
                self._save(incoming)
                self._reconcile_locked()
                return len(incoming.profiles)
            profiles = list(self._state.profiles)
            names = {p.name.casefold() for p in profiles}
            ids = {p.id for p in profiles}
            added = 0
            for profile in incoming.profiles:
                if profile.id in ids:
                    continue
                name = profile.name
                n = 2
                while name.casefold() in names:
                    name = f"{profile.name} ({n})"
                    n += 1
                profiles.append(profile.with_changes(name=name))
                names.add(name.casefold())
                added += 1
            group_links = {s: dict(l) for s, l in self._state.group_links.items()}
            for scope, links in incoming.group_links.items():
                target = group_links.setdefault(scope, {})
                for gid, pid in links.items():
                    target.setdefault(gid, pid)
            self._save(
                LoginProfileState(profiles=sorted_profiles(profiles), group_links=group_links)
            )
            self._reconcile_locked()
            return added
