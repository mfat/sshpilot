"""Login profile API models and strict wire codecs."""

import pytest

from sshpilot.api.events import CoreEvent, EventType
from sshpilot.api.models.login_profiles import (
    AssignLoginProfileRequest,
    ConnectionProfileLink,
    CreateLoginProfileRequest,
    DeleteLoginProfileRequest,
    DeleteLoginProfileResult,
    DetachedConnectionInfo,
    GroupProfileLink,
    LoginProfileAssignmentPreview,
    LoginProfileDetachReason,
    LoginProfileFieldChange,
    LoginProfileLinkMode,
    LoginProfileReassignment,
    LoginProfileSecretKind,
    LoginProfileSettings,
    LoginProfileSnapshot,
    LoginProfilesChangedEvent,
    LoginProfileSource,
    LoginProfileSummary,
    PreviewLoginProfileAssignmentRequest,
    SetGroupLoginProfileRequest,
    SetLoginProfileSecretRequest,
    UpdateLoginProfileRequest,
)
from sshpilot.api.transport import login_profile_codec as codec
from sshpilot.api.transport.codec import public_event_from_envelope, public_event_to_envelope

SETTINGS = LoginProfileSettings(
    name="Deploy",
    username="deploy",
    key_select_mode=1,
    identity_files=("/k/a", "/k/b"),
    extra_ssh_config="ServerAliveInterval 30",
    forward_agent=True,
)
PID = "lp-0123456789abcdef"


def _roundtrip(to_wire, from_wire, value):
    assert from_wire(to_wire(value)) == value


def test_settings_and_summary_roundtrip():
    _roundtrip(codec.login_profile_settings_to_wire, codec.login_profile_settings_from_wire, SETTINGS)
    summary = LoginProfileSummary(
        id=PID, settings=SETTINGS, revision=3, has_password=True, connection_count=2, group_count=1
    )
    _roundtrip(codec.login_profile_summary_to_wire, codec.login_profile_summary_from_wire, summary)


def test_snapshot_roundtrip_and_lookups():
    snapshot = LoginProfileSnapshot(
        available=True,
        profiles=(LoginProfileSummary(id=PID, settings=SETTINGS, revision=1),),
        group_links=(GroupProfileLink(group_id="prod", profile_id=PID),),
        connection_links=(
            ConnectionProfileLink(
                connection_id="web1",
                mode=LoginProfileLinkMode.INHERIT,
                effective_profile_id=PID,
                source=LoginProfileSource.GROUP,
                group_id="prod",
            ),
            ConnectionProfileLink(
                connection_id="web2",
                mode=LoginProfileLinkMode.EXPLICIT,
                profile_id=PID,
                effective_profile_id=PID,
                source=LoginProfileSource.CONNECTION,
            ),
        ),
    )
    _roundtrip(codec.login_profile_snapshot_to_wire, codec.login_profile_snapshot_from_wire, snapshot)
    assert snapshot.link_for("web2").profile_id == PID
    assert snapshot.group_profile_id("prod") == PID
    assert snapshot.profile(PID).name == "Deploy"


@pytest.mark.parametrize(
    "value, to_wire, from_wire",
    [
        (CreateLoginProfileRequest(SETTINGS), codec.create_login_profile_request_to_wire,
         codec.create_login_profile_request_from_wire),
        (UpdateLoginProfileRequest(PID, SETTINGS, expected_revision=2),
         codec.update_login_profile_request_to_wire, codec.update_login_profile_request_from_wire),
        (DeleteLoginProfileRequest(PID, None, (LoginProfileReassignment("web1", PID),
                                               LoginProfileReassignment("group:g", None))),
         codec.delete_login_profile_request_to_wire, codec.delete_login_profile_request_from_wire),
        (DeleteLoginProfileResult((DetachedConnectionInfo("web1", "Deploy",
                                                          LoginProfileDetachReason.PROFILE_DELETED),)),
         codec.delete_login_profile_result_to_wire, codec.delete_login_profile_result_from_wire),
        (AssignLoginProfileRequest(("a", "b"), LoginProfileLinkMode.EXPLICIT, PID),
         codec.assign_login_profile_request_to_wire, codec.assign_login_profile_request_from_wire),
        (AssignLoginProfileRequest(("a",)),
         codec.assign_login_profile_request_to_wire, codec.assign_login_profile_request_from_wire),
        (PreviewLoginProfileAssignmentRequest(("a",), LoginProfileLinkMode.INHERIT),
         codec.preview_login_profile_assignment_request_to_wire,
         codec.preview_login_profile_assignment_request_from_wire),
        (SetGroupLoginProfileRequest("g", PID, ("a",)),
         codec.set_group_login_profile_request_to_wire, codec.set_group_login_profile_request_from_wire),
        (SetLoginProfileSecretRequest(PID, LoginProfileSecretKind.SUDO, clear=True),
         codec.set_login_profile_secret_request_to_wire, codec.set_login_profile_secret_request_from_wire),
    ],
)
def test_request_roundtrips(value, to_wire, from_wire):
    _roundtrip(to_wire, from_wire, value)


def test_previews_roundtrip():
    previews = (
        LoginProfileAssignmentPreview(
            "web1", "Deploy", (LoginProfileFieldChange("username", "User", "alice", "deploy"),)
        ),
    )
    assert codec.login_profile_assignment_previews_from_wire(
        codec.login_profile_assignment_previews_to_wire(previews)
    ) == previews


def test_strict_decoding_rejects_unknown_and_missing_fields():
    wire = codec.set_login_profile_secret_request_to_wire(SetLoginProfileSecretRequest(PID))
    with pytest.raises(ValueError):
        codec.set_login_profile_secret_request_from_wire({**wire, "password": "x"})
    with pytest.raises(ValueError):
        codec.set_login_profile_secret_request_from_wire({"profile_id": PID})


def test_model_validation():
    with pytest.raises(ValueError):
        AssignLoginProfileRequest(("a",), LoginProfileLinkMode.EXPLICIT)  # no profile id
    with pytest.raises(ValueError):
        AssignLoginProfileRequest((), None)
    with pytest.raises(ValueError):
        LoginProfileSettings(name=" ")
    with pytest.raises(ValueError):
        LoginProfileSettings(name="x", auth_method=3)
    with pytest.raises(ValueError):
        ConnectionProfileLink(connection_id="a", mode=LoginProfileLinkMode.EXPLICIT)
    assert "password" not in repr(SetLoginProfileSecretRequest(PID))


def test_changed_event_envelope_roundtrip():
    payload = LoginProfilesChangedEvent(
        detached=(DetachedConnectionInfo("web1", "Deploy", LoginProfileDetachReason.DRIFT),)
    )
    event = CoreEvent(type=EventType.LOGIN_PROFILES_CHANGED, payload=payload, sequence=4)
    decoded = public_event_from_envelope(
        public_event_to_envelope(event, sequence=4, protocol_version="1.0")
    )
    assert decoded.type is EventType.LOGIN_PROFILES_CHANGED
    assert decoded.payload == payload
