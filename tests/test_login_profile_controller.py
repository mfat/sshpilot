"""GTK-free login profile controller and presentation helpers."""

import pytest

from sshpilot.api.models.login_profiles import (
    ConnectionProfileLink,
    GroupProfileLink,
    LoginProfileAssignmentPreview,
    LoginProfileFieldChange,
    LoginProfileLinkMode,
    LoginProfileSecretKind,
    LoginProfileSettings,
    LoginProfileSnapshot,
    LoginProfileSource,
    LoginProfileSummary,
)
from sshpilot.gtk.login_profile_controller import (
    CHOICE_CUSTOM,
    CHOICE_INHERIT,
    CHOICE_PROFILE,
    LoginProfileController,
    affected_items,
    build_delete_request,
    current_choice_index,
    default_group_members_to_link,
    format_preview,
    profile_choices,
    profile_summary_line,
    resolve_group_profile,
    settings_from_values,
)

A = "lp-aaaaaaaaaaaaaaaa"
B = "lp-bbbbbbbbbbbbbbbb"


def _summary(pid, name, **settings):
    return LoginProfileSummary(
        id=pid, settings=LoginProfileSettings(name=name, **settings), revision=1
    )


SNAPSHOT = LoginProfileSnapshot(
    available=True,
    profiles=(
        _summary(A, "Deploy", username="deploy", key_select_mode=1, identity_files=("/k/deploy",)),
        _summary(B, "Admin", username="root", auth_method=1),
    ),
    group_links=(GroupProfileLink("prod", A),),
    connection_links=(
        ConnectionProfileLink("web1", LoginProfileLinkMode.EXPLICIT, A, A, LoginProfileSource.CONNECTION),
        ConnectionProfileLink("web2", LoginProfileLinkMode.INHERIT, None, A, LoginProfileSource.GROUP, "prod"),
    ),
)


def test_group_resolution_walks_parents():
    parents = {"web": "prod", "prod": None, "other": None}
    assert resolve_group_profile(SNAPSHOT, "web", parents).id == A
    assert resolve_group_profile(SNAPSHOT, "other", parents) is None
    assert resolve_group_profile(SNAPSHOT, None, parents) is None
    # A parent cycle must not hang.
    assert resolve_group_profile(SNAPSHOT, "x", {"x": "y", "y": "x"}) is None


def test_choices_and_current_index():
    inherited = SNAPSHOT.profile(A)
    choices = profile_choices(SNAPSHOT, inherited=inherited)
    assert [c.kind for c in choices] == [CHOICE_CUSTOM, CHOICE_INHERIT, CHOICE_PROFILE, CHOICE_PROFILE]
    assert "Deploy" in choices[1].label
    assert choices[1].mode is LoginProfileLinkMode.INHERIT
    assert choices[2].mode is LoginProfileLinkMode.EXPLICIT and choices[0].mode is None
    assert current_choice_index(choices, SNAPSHOT, "web1") == 2
    assert current_choice_index(choices, SNAPSHOT, "web2") == 1
    assert current_choice_index(choices, SNAPSHOT, "db1") == 0
    assert len(profile_choices(SNAPSHOT)) == 3  # no inherit without a group profile


def test_summary_line_and_preview_text():
    assert profile_summary_line(SNAPSHOT.profile(A)) == "deploy · deploy"
    assert profile_summary_line(SNAPSHOT.profile(B)) == "root · password"
    text = format_preview((
        LoginProfileAssignmentPreview("web1", "Deploy", (
            LoginProfileFieldChange("username", "User", "alice", "deploy"),
            LoginProfileFieldChange("identity_files", "IdentityFile", "", "/k/a\n/k/b"),
        )),
        LoginProfileAssignmentPreview("web2", "Deploy", ()),
    ))
    assert text == "web1:\n  User: alice → deploy\n  IdentityFile: — → /k/a, /k/b"


def test_delete_plan_helpers():
    items = affected_items(SNAPSHOT, A, {"prod": "Production"})
    assert [(i.key, i.label, i.is_group) for i in items] == [
        ("group:prod", "Production", True),
        ("web1", "web1", False),
    ]
    request = build_delete_request(A, {"web1": B, "group:prod": None})
    assert [(o.key, o.target_profile_id) for o in request.overrides] == [
        ("group:prod", None), ("web1", B),
    ]
    assert default_group_members_to_link(SNAPSHOT, ["web1", "web2", "db1"]) == ("web2", "db1")


def test_settings_from_values_normalizes():
    settings = settings_from_values({
        "name": " Ops ",
        "username": "ops",
        "auth_method": 0,
        "key_select_mode": 1,
        "identity_files": ["/k/a", "/k/a", " ", "/k/b"],
        "extra_ssh_config": "ServerAliveInterval 30\n",
    })
    assert settings.name == "Ops"
    assert settings.identity_files == ("/k/a", "/k/b")
    assert settings.extra_ssh_config == "ServerAliveInterval 30"
    with pytest.raises(ValueError):
        settings_from_values({"name": ""})


class _Client:
    def __init__(self):
        self.calls = []

    def get_login_profiles(self):
        self.calls.append(("get",))
        return SNAPSHOT

    def create_login_profile(self, request):
        self.calls.append(("create", request.settings.name))
        return _summary(A, request.settings.name)

    def update_login_profile(self, request):
        self.calls.append(("update", request.profile_id, request.expected_revision))
        return _summary(request.profile_id, request.settings.name)

    def set_login_profile_secret(self, request, secret=None):
        self.calls.append(("secret", request.kind, request.clear, bytes(secret) if secret else None))
        return True

    def assign_login_profile(self, request):
        self.calls.append(("assign", request.connection_ids, request.mode, request.profile_id))
        return True


def test_controller_create_update_and_secret_edits():
    client = _Client()
    controller = LoginProfileController(lambda: client)
    controller.create(LoginProfileSettings(name="New"), password="pw", sudo_password=None)
    controller.update(A, LoginProfileSettings(name="New"), expected_revision=3, password="", sudo_password="s")
    assert client.calls == [
        ("create", "New"),
        ("secret", LoginProfileSecretKind.LOGIN, False, b"pw"),
        ("get",),
        ("update", A, 3),
        ("secret", LoginProfileSecretKind.LOGIN, True, None),
        ("secret", LoginProfileSecretKind.SUDO, False, b"s"),
        ("get",),
    ]
    assert controller.snapshot is SNAPSHOT


def test_controller_follows_client_replacement_and_requires_one():
    holder = {"client": None}
    controller = LoginProfileController(lambda: holder["client"])
    with pytest.raises(RuntimeError):
        controller.refresh()
    holder["client"] = _Client()
    controller.assign(["web1"], LoginProfileLinkMode.EXPLICIT, A)
    assert holder["client"].calls[0] == ("assign", ("web1",), LoginProfileLinkMode.EXPLICIT, A)


def test_secret_failure_after_create_reports_the_saved_profile():
    from sshpilot.gtk.login_profile_controller import LoginProfileSecretError

    class _Failing(_Client):
        def set_login_profile_secret(self, request, secret=None):
            raise RuntimeError("no secret backend")

    client = _Failing()
    controller = LoginProfileController(lambda: client)
    with pytest.raises(LoginProfileSecretError) as info:
        controller.create(LoginProfileSettings(name="New"), password="pw")
    assert info.value.summary.id == A
    assert "could not be stored" in info.value.message
    assert controller.snapshot is SNAPSHOT  # refreshed even on failure
