"""Regressions found reviewing the authentication redesign after 6.4.4."""
from __future__ import annotations

import pytest

from sshpilot.core.connections.auth_preferences import (
    PASSWORD_MODE_PREFERRED_AUTH,
    pubkey_disabled,
    split_option,
)
from sshpilot.core.login_profiles.models import (
    PROFILE_FIELDS,
    LoginProfile,
    apply_profile_to_data,
    profile_owned_fields,
)

PROFILE_ID = "lp-0123456789abcdef"


# -- one option splitter ------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    ("PubkeyAuthentication no", ("pubkeyauthentication", "no")),
    ("PubkeyAuthentication=no", ("pubkeyauthentication", "no")),
    ("PubkeyAuthentication = no", ("pubkeyauthentication", "no")),
    ('IdentityAgent "/run/my agent"', ("identityagent", "/run/my agent")),
    ("SetEnv A=b", ("setenv", "A=b")),
    ("# PubkeyAuthentication no", ("", "")),
    ("", ("", "")),
])
def test_split_option_reads_both_separators(line, expected):
    assert split_option(line) == expected


def test_pubkey_disabled_reads_the_first_line_only():
    assert pubkey_disabled("PubkeyAuthentication=no")
    assert not pubkey_disabled("PubkeyAuthentication yes\nPubkeyAuthentication no")
    assert not pubkey_disabled("PasswordAuthentication no")


def test_profile_with_equals_form_reads_as_public_keys_off():
    profile = LoginProfile(
        id=PROFILE_ID, name="p", extra_ssh_config="PubkeyAuthentication=no"
    )
    out = apply_profile_to_data({}, profile)
    assert out["pubkey_auth_no"] is True
    assert out["auth_method"] == 1


# -- profiles stored by 6.4.4 -------------------------------------------------

def _stored_644(**overrides):
    data = {
        "id": PROFILE_ID, "name": "Old", "username": "alice", "auth_method": 0,
        "key_select_mode": 0, "identity_files": [], "certificate_files": [],
        "identity_agent": "", "add_keys_to_agent": "", "pkcs11_provider": "",
        "security_key_provider": "", "pubkey_auth_no": False,
        "forward_agent": False, "forward_agent_target": "", "proxy_command": "",
        "extra_ssh_config": "", "has_password": False,
        "has_sudo_password": False, "revision": 3,
    }
    data.update(overrides)
    return data


def test_password_profile_does_not_gain_the_keys_its_old_form_hid():
    profile = LoginProfile.from_dict(_stored_644(
        auth_method=1, key_select_mode=1, identity_files=["~/.ssh/old"],
        certificate_files=["~/.ssh/old-cert.pub"], identity_agent="~/agent.sock",
    ))
    assert profile_owned_fields(profile) == ("username",)
    assert profile.identity_files == ()
    assert profile.identities_only is None
    assert f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}" in (
        profile.extra_ssh_config
    )
    out = apply_profile_to_data(
        {"identity_files": ["~/.ssh/host"], "key_select_mode": 2}, profile
    )
    assert out["identity_files"] == ["~/.ssh/host"]
    assert out["key_select_mode"] == 2


def test_key_profile_keeps_its_keys():
    profile = LoginProfile.from_dict(_stored_644(
        key_select_mode=1, identity_files=["~/.ssh/work"],
    ))
    assert profile.identity_files == ("~/.ssh/work",)
    assert profile.identities_only is True


# -- a store an older version can still read ----------------------------------

@pytest.mark.parametrize("profile", [
    LoginProfile(id=PROFILE_ID, name="empty"),
    LoginProfile(id=PROFILE_ID, name="no forward", forward_agent=False),
    LoginProfile(id=PROFILE_ID, name="forward", forward_agent=True),
    LoginProfile(id=PROFILE_ID, name="keys", identity_files=("~/.ssh/a",)),
    LoginProfile(
        id=PROFILE_ID, name="only", identity_files=("~/.ssh/a",), identities_only=True
    ),
])
def test_stored_profile_is_readable_by_644_and_round_trips(profile):
    stored = profile.to_dict()
    # What 6.4.4's LoginProfile.__post_init__ insists on.
    assert stored["key_select_mode"] in (0, 1, 2)
    assert type(stored["forward_agent"]) is bool
    assert type(stored["pubkey_auth_no"]) is bool
    assert LoginProfile.from_dict(stored) == profile
    assert profile.settings()["forward_agent"] is profile.forward_agent


def test_store_written_before_the_forwarding_flag_still_reads():
    stored = LoginProfile(id=PROFILE_ID, name="p").to_dict()
    del stored["forward_agent_explicit_no"]
    stored["forward_agent"] = None
    assert LoginProfile.from_dict(stored).forward_agent is None
    stored["forward_agent"] = False
    assert LoginProfile.from_dict(stored).forward_agent is False


# -- clients from before fields could be left unset ---------------------------

def _wire(**overrides):
    from sshpilot.api.models.login_profiles import LoginProfileSettings
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_to_wire,
    )

    wire = login_profile_settings_to_wire(LoginProfileSettings(name="Old"))
    wire.update(overrides)
    return wire


def test_older_client_defaults_are_not_settings():
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_from_wire,
    )

    wire = _wire(forward_agent=False, key_select_mode=0)
    del wire["identities_only"]
    settings = login_profile_settings_from_wire(wire)
    assert settings.forward_agent is None
    assert settings.key_select_mode is None


def test_older_client_password_profile_sets_no_keys():
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_from_wire,
    )

    wire = _wire(
        auth_method=1, key_select_mode=1, identity_files=["~/.ssh/old"],
        pkcs11_provider="/usr/lib/opensc-pkcs11.so",
    )
    del wire["identities_only"]
    settings = login_profile_settings_from_wire(wire)
    assert settings.identity_files == ()
    assert settings.key_select_mode is None
    assert settings.pkcs11_provider == ""
    assert settings.auth_method == 1


def test_current_client_values_are_taken_as_sent():
    from sshpilot.api.transport.login_profile_codec import (
        login_profile_settings_from_wire,
    )

    settings = login_profile_settings_from_wire(_wire(forward_agent=False))
    assert settings.forward_agent is False


# -- the legacy Key/Password choice only acts on a change ---------------------

def _legacy(data, patch, before=(None, None)):
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )

    ConnectionApplicationService._apply_legacy_auth_choice(data, patch, before)
    return data.get("extra_ssh_config", "")


def test_echoing_back_what_was_read_leaves_the_block_alone():
    extra = "PubkeyAuthentication no"
    patch = {"auth_method": 1, "pubkey_auth_no": True, "extra_ssh_config": extra}
    assert _legacy({"extra_ssh_config": extra}, patch, before=(1, True)) == extra


def test_creating_with_the_default_choice_keeps_authored_lines():
    extra = "PreferredAuthentications password,publickey\nPubkeyAuthentication no"
    patch = {"auth_method": 0, "pubkey_auth_no": False, "extra_ssh_config": extra}
    assert _legacy({"extra_ssh_config": extra}, patch) == extra


def test_a_changed_choice_still_edits_the_lines():
    assert _legacy({}, {"auth_method": 1}) == (
        f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}"
    )
    assert _legacy(
        {"extra_ssh_config": "PubkeyAuthentication no"},
        {"pubkey_auth_no": False},
        before=(1, True),
    ) == ""


# -- the section is editable without an answer from the configuration ---------

def test_empty_fallback_makes_the_order_known():
    from sshpilot.connection_dialog_auth import DEFAULT_ORDER, AuthPolicy

    policy = AuthPolicy()
    assert not policy.can_edit()
    policy.set_fallback({})
    assert policy.can_edit()
    assert policy.shown_order() == list(DEFAULT_ORDER)
    assert policy.set_tried("hostbased", False)


class _Dialog:
    is_editing = False
    connection = None

    def __init__(self, parent):
        self.parent_window = parent
        self.fallbacks = []
        self.earlier = []

    def _auth_policy_set_fallback(self, inherited):
        self.fallbacks.append(inherited)

    def _set_earlier_directives(self, earlier):
        self.earlier.append(earlier)

    def _watch_alias_for_earlier_directives(self):
        pass


def _load_earlier(dialog):
    pytest.importorskip("gi")
    from sshpilot.connection_dialog_overrides import ConnectionDialogOverridesMixin

    ConnectionDialogOverridesMixin._load_earlier_directives_async(dialog)


def test_new_connection_without_alias_gets_the_builtin_order():
    class Client:
        def get_earlier_directives(self, alias):
            raise AssertionError("no alias to ask about")

    class Parent:
        client = Client()

        def _daemon_ready(self):
            return True

    dialog = _Dialog(Parent())
    _load_earlier(dialog)
    assert dialog.fallbacks == [{}]


def test_daemon_not_ready_gets_the_builtin_order():
    class Parent:
        client = None

        def _daemon_ready(self):
            return False

    dialog = _Dialog(Parent())
    _load_earlier(dialog)
    assert dialog.fallbacks == [{}]


# -- the frontend's copy of the profile fields --------------------------------

def test_frontend_profile_fields_match_the_model():
    from sshpilot.api.models.login_profiles import LoginProfileSettings
    from sshpilot.gtk import login_profile_controller as controller

    assert controller.PROFILE_FIELDS == PROFILE_FIELDS
    samples = [
        dict(name="a"),
        dict(name="b", username="u", forward_agent=False),
        dict(name="c", identity_files=("~/.ssh/k",), identities_only=True),
        dict(name="d", proxy_command="nc %h %p", add_keys_to_agent="yes"),
    ]
    for sample in samples:
        settings = LoginProfileSettings(**sample)
        profile = LoginProfile(id=PROFILE_ID, **sample)
        assert controller.settings_owned_fields(settings) == profile_owned_fields(profile)
