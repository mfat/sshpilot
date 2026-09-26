"""GTK-free login profile controller and presentation helpers.

Wraps the daemon-backed ``SshPilotClient`` ``login_profiles.*`` methods with
a cached :class:`LoginProfileSnapshot`, and holds the pure helpers the dialogs
use (picker choices, group-profile resolution, delete plans, preview text) so
they are testable without a display.

Threading: client RPCs block; call them from a worker (the window's client
bridge or a thread) and deliver results on the GTK thread. The lock guards
only the cached snapshot.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from sshpilot.api.models.login_profiles import (
    AssignLoginProfileRequest,
    CreateLoginProfileRequest,
    DeleteLoginProfileRequest,
    DeleteLoginProfileResult,
    LoginProfileAssignmentPreview,
    LoginProfileLinkMode,
    LoginProfileReassignment,
    LoginProfileSecretKind,
    LoginProfileSettings,
    LoginProfileSnapshot,
    LoginProfileSummary,
    PreviewLoginProfileAssignmentRequest,
    SetGroupLoginProfileRequest,
    SetLoginProfileSecretRequest,
    UpdateLoginProfileRequest,
)

# Secret edits from the editor: ``None`` keeps the stored value, ``""`` clears
# it, anything else replaces it.
SecretEdit = Optional[str]

CHOICE_CUSTOM = "custom"
CHOICE_INHERIT = "inherit"
CHOICE_PROFILE = "profile"


@dataclass(frozen=True)
class ProfileChoice:
    """One entry of a login profile picker."""

    kind: str  # CHOICE_CUSTOM | CHOICE_INHERIT | CHOICE_PROFILE
    label: str
    profile_id: Optional[str] = None

    @property
    def mode(self) -> Optional[LoginProfileLinkMode]:
        if self.kind == CHOICE_PROFILE:
            return LoginProfileLinkMode.EXPLICIT
        if self.kind == CHOICE_INHERIT:
            return LoginProfileLinkMode.INHERIT
        return None


def resolve_group_profile(
    snapshot: LoginProfileSnapshot,
    group_id: Optional[str],
    parents: Mapping[str, Optional[str]],
) -> Optional[LoginProfileSummary]:
    """The profile a connection in *group_id* inherits (nearest ancestor)."""
    seen = set()
    current = group_id
    while current and current not in seen:
        seen.add(current)
        profile = snapshot.profile(snapshot.group_profile_id(current))
        if profile is not None:
            return profile
        current = parents.get(current)
    return None


def profile_choices(
    snapshot: LoginProfileSnapshot,
    *,
    inherited: Optional[LoginProfileSummary] = None,
    custom_label: str = "Custom (no profile)",
    inherit_label: str = "Inherit from group ({name})",
    include_inherit: bool = True,
) -> Tuple[ProfileChoice, ...]:
    """Picker entries: custom, inherit (when a group supplies a profile), profiles."""
    choices: List[ProfileChoice] = [ProfileChoice(CHOICE_CUSTOM, custom_label)]
    if include_inherit and inherited is not None:
        choices.append(
            ProfileChoice(CHOICE_INHERIT, inherit_label.format(name=inherited.name))
        )
    for profile in snapshot.profiles:
        choices.append(ProfileChoice(CHOICE_PROFILE, profile.name, profile.id))
    return tuple(choices)


def current_choice_index(
    choices: Sequence[ProfileChoice],
    snapshot: LoginProfileSnapshot,
    connection_id: Optional[str],
) -> int:
    """Index of the picker entry matching the connection's current link."""
    link = snapshot.link_for(connection_id) if connection_id else None
    if link is None:
        return 0
    for index, choice in enumerate(choices):
        if link.mode is LoginProfileLinkMode.EXPLICIT and (
            choice.kind == CHOICE_PROFILE and choice.profile_id == link.profile_id
        ):
            return index
        if link.mode is LoginProfileLinkMode.INHERIT and choice.kind == CHOICE_INHERIT:
            return index
    return 0


def profile_summary_line(profile: LoginProfileSummary) -> str:
    """Short one-line description for rows and subtitles."""
    settings = profile.settings
    parts = []
    if settings.username:
        parts.append(settings.username)
    if settings.auth_method == 1:
        parts.append("password")
    elif settings.identity_files:
        names = [path.rsplit("/", 1)[-1] for path in settings.identity_files]
        parts.append(", ".join(names))
    else:
        parts.append("automatic keys")
    return " · ".join(parts)


def format_preview(previews: Iterable[LoginProfileAssignmentPreview]) -> str:
    """Plain-text diff for a confirmation dialog (empty when nothing changes)."""
    lines: List[str] = []
    for preview in previews:
        if not preview.changes:
            continue
        lines.append(f"{preview.connection_id}:")
        for change in preview.changes:
            before = change.before.replace("\n", ", ") or "—"
            after = change.after.replace("\n", ", ") or "—"
            lines.append(f"  {change.label}: {before} → {after}")
    return "\n".join(lines)


@dataclass(frozen=True)
class AffectedItem:
    """One connection or group that uses a profile being deleted."""

    key: str  # connection id or "group:<id>"
    label: str
    is_group: bool


def affected_items(
    snapshot: LoginProfileSnapshot,
    profile_id: str,
    group_names: Mapping[str, str],
) -> Tuple[AffectedItem, ...]:
    """Explicitly linked connections and assigned groups (inheriting
    connections follow their group's decision)."""
    items: List[AffectedItem] = []
    for link in snapshot.group_links:
        if link.profile_id == profile_id:
            items.append(
                AffectedItem(
                    f"group:{link.group_id}",
                    group_names.get(link.group_id, link.group_id),
                    True,
                )
            )
    for link in snapshot.connection_links:
        if link.mode is LoginProfileLinkMode.EXPLICIT and link.profile_id == profile_id:
            items.append(AffectedItem(link.connection_id, link.connection_id, False))
    return tuple(items)


def build_delete_request(
    profile_id: str,
    targets: Mapping[str, Optional[str]],
) -> DeleteLoginProfileRequest:
    """``targets`` maps each affected key to a profile id or ``None`` (detach)."""
    return DeleteLoginProfileRequest(
        profile_id=profile_id,
        replacement_profile_id=None,
        overrides=tuple(
            LoginProfileReassignment(key=key, target_profile_id=target)
            for key, target in sorted(targets.items())
        ),
    )


def default_group_members_to_link(
    snapshot: LoginProfileSnapshot, member_ids: Iterable[str]
) -> Tuple[str, ...]:
    """Members pre-checked to inherit a group profile: those without an explicit profile."""
    selected = []
    for cid in member_ids:
        link = snapshot.link_for(cid)
        if link is None or link.mode is LoginProfileLinkMode.INHERIT:
            selected.append(cid)
    return tuple(selected)


class LoginProfileSecretError(RuntimeError):
    """The profile was saved but a secret could not be stored or cleared."""

    def __init__(self, summary: LoginProfileSummary, error: BaseException) -> None:
        super().__init__(getattr(error, "message", None) or str(error))
        self.summary = summary
        self.message = (
            "The profile was saved, but its password could not be stored: "
            + (getattr(error, "message", None) or str(error))
        )


class LoginProfileController:
    """Blocking client wrapper with a cached snapshot."""

    def __init__(self, client_getter: Callable[[], object]) -> None:
        # A getter, not a client: the window replaces its client on daemon
        # reconnect, and a controller must follow it.
        self._client_getter = client_getter
        self._lock = threading.RLock()
        self._snapshot: Optional[LoginProfileSnapshot] = None

    def _client(self):
        client = self._client_getter()
        if client is None:
            raise RuntimeError("the SSH Pilot daemon is not connected")
        return client

    @property
    def snapshot(self) -> Optional[LoginProfileSnapshot]:
        with self._lock:
            return self._snapshot

    def refresh(self) -> LoginProfileSnapshot:
        snapshot = self._client().get_login_profiles()
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    # -- profile CRUD ------------------------------------------------------

    def _apply_secrets(self, profile_id: str, password: SecretEdit, sudo: SecretEdit) -> None:
        for kind, value in (
            (LoginProfileSecretKind.LOGIN, password),
            (LoginProfileSecretKind.SUDO, sudo),
        ):
            if value is None:
                continue
            client = self._client()
            if value == "":
                client.set_login_profile_secret(
                    SetLoginProfileSecretRequest(profile_id, kind, clear=True)
                )
            else:
                secret = bytearray(value.encode("utf-8"))
                try:
                    client.set_login_profile_secret(
                        SetLoginProfileSecretRequest(profile_id, kind), secret
                    )
                finally:
                    secret[:] = b"\0" * len(secret)

    def create(
        self,
        settings: LoginProfileSettings,
        *,
        password: SecretEdit = None,
        sudo_password: SecretEdit = None,
    ) -> LoginProfileSummary:
        summary = self._client().create_login_profile(CreateLoginProfileRequest(settings))
        self._apply_secrets_or_raise(summary, password, sudo_password)
        return summary

    def update(
        self,
        profile_id: str,
        settings: LoginProfileSettings,
        *,
        expected_revision: Optional[int] = None,
        password: SecretEdit = None,
        sudo_password: SecretEdit = None,
    ) -> LoginProfileSummary:
        summary = self._client().update_login_profile(
            UpdateLoginProfileRequest(profile_id, settings, expected_revision)
        )
        self._apply_secrets_or_raise(summary, password, sudo_password)
        return summary

    def _apply_secrets_or_raise(
        self, summary: LoginProfileSummary, password: SecretEdit, sudo: SecretEdit
    ) -> None:
        try:
            self._apply_secrets(summary.id, password, sudo)
        except Exception as error:
            self.refresh()
            raise LoginProfileSecretError(summary, error) from error
        self.refresh()

    def delete(
        self, profile_id: str, targets: Mapping[str, Optional[str]]
    ) -> DeleteLoginProfileResult:
        result = self._client().delete_login_profile(build_delete_request(profile_id, targets))
        self.refresh()
        return result

    # -- assignment --------------------------------------------------------

    def preview(
        self,
        connection_ids: Sequence[str],
        mode: Optional[LoginProfileLinkMode],
        profile_id: Optional[str] = None,
    ) -> Tuple[LoginProfileAssignmentPreview, ...]:
        return self._client().preview_login_profile_assignment(
            PreviewLoginProfileAssignmentRequest(tuple(connection_ids), mode, profile_id)
        )

    def assign(
        self,
        connection_ids: Sequence[str],
        mode: Optional[LoginProfileLinkMode],
        profile_id: Optional[str] = None,
    ) -> bool:
        result = self._client().assign_login_profile(
            AssignLoginProfileRequest(tuple(connection_ids), mode, profile_id)
        )
        self.refresh()
        return result

    def set_group_profile(
        self,
        group_id: str,
        profile_id: Optional[str],
        link_members: Sequence[str] = (),
    ) -> bool:
        result = self._client().set_group_login_profile(
            SetGroupLoginProfileRequest(group_id, profile_id, tuple(link_members))
        )
        self.refresh()
        return result


def settings_from_values(values: Mapping[str, object]) -> LoginProfileSettings:
    """Build settings from an editor form mapping (strings/bools/ints/lists)."""

    def text(key: str) -> str:
        return str(values.get(key) or "").strip()

    def paths(key: str) -> Tuple[str, ...]:
        raw = values.get(key) or ()
        if isinstance(raw, str):
            raw = raw.splitlines()
        seen: Dict[str, None] = {}
        for item in raw:
            item = str(item).strip()
            if item:
                seen[item] = None
        return tuple(seen)

    return LoginProfileSettings(
        name=text("name"),
        username=text("username"),
        auth_method=int(values.get("auth_method") or 0),
        key_select_mode=int(values.get("key_select_mode") or 0),
        identity_files=paths("identity_files"),
        certificate_files=paths("certificate_files"),
        identity_agent=text("identity_agent"),
        add_keys_to_agent=text("add_keys_to_agent"),
        pkcs11_provider=text("pkcs11_provider"),
        security_key_provider=text("security_key_provider"),
        pubkey_auth_no=bool(values.get("pubkey_auth_no")),
        forward_agent=bool(values.get("forward_agent")),
        forward_agent_target=text("forward_agent_target"),
        extra_ssh_config=str(values.get("extra_ssh_config") or "").strip(),
    )
