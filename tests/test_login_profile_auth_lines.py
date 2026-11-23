"""Login profiles hold authentication as plain lines, like a Host block.

A profile's PreferredAuthentications and per-method switches are options of
the profile: those it sets replace a linked connection's own, what it leaves
unset stays the connection's. Profiles saved with the older Key/Password
choice load as the lines that choice stood for, and links recorded before
keep matching their fingerprint.
"""

import pytest

from sshpilot.core.connections.auth_preferences import PASSWORD_MODE_PREFERRED_AUTH
from sshpilot.core.login_profiles.models import (
    LoginProfile,
    auth_fingerprint,
    is_legacy_auth_link,
    profile_owned_fields,
    profile_owned_keywords,
)

PASSWORD_LINE = f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}"
PID = "lp-0000000000000001"


def test_an_older_password_profile_loads_as_its_lines():
    profile = LoginProfile.from_dict({
        "id": PID, "name": "ops", "auth_method": 1, "pubkey_auth_no": True,
        "extra_ssh_config": "Compression yes",
    })
    assert profile.extra_ssh_config.splitlines() == [
        "Compression yes", PASSWORD_LINE, "PubkeyAuthentication no",
    ]
    assert profile.auth_method == 0 and profile.pubkey_auth_no is False
    assert profile.revision == 1  # loading is not an edit: links do not re-apply
    assert {"preferredauthentications", "pubkeyauthentication"} <= set(
        profile_owned_keywords(profile)
    )


def test_an_older_key_profile_sets_nothing():
    profile = LoginProfile.from_dict({"id": PID, "name": "ops", "auth_method": 0})
    assert profile.extra_ssh_config == ""


def test_authentication_lines_are_accepted_in_a_profile():
    lines = "PreferredAuthentications publickey,password\nGSSAPIAuthentication no"
    assert LoginProfile(id=PID, name="ops", extra_ssh_config=lines).extra_ssh_config == lines


def test_new_links_hash_only_what_the_profile_owns():
    data = {"username": "alice", "auth_method": 1, "pubkey_auth_no": False,
            "extra_ssh_config": "PreferredAuthentications password,publickey"}
    fingerprint = auth_fingerprint(data, ())
    assert not is_legacy_auth_link(fingerprint)
    # The connection's own order is not the profile's business ...
    changed = {**data, "auth_method": 0,
               "extra_ssh_config": "PreferredAuthentications publickey"}
    assert auth_fingerprint(changed, ()) == fingerprint
    # ... unless the profile sets it.
    owned = ("preferredauthentications",)
    assert auth_fingerprint(changed, owned) != auth_fingerprint(data, owned)


def test_a_link_recorded_before_keeps_its_fingerprint():
    data = {"username": "alice", "auth_method": 1, "pubkey_auth_no": True}
    legacy = auth_fingerprint(data, (), legacy_auth=True)
    assert is_legacy_auth_link(legacy)
    assert auth_fingerprint(data, (), legacy_auth=is_legacy_auth_link(legacy)) == legacy


def test_a_profile_that_lists_keys_writes_them():
    # With IdentitiesOnly unset, the keys must still reach OpenSSH.
    from sshpilot.core.login_profiles.models import apply_profile_to_data
    from sshpilot.ssh_config_formatter import format_ssh_config_entry

    profile = LoginProfile(id=PID, name="k", identity_files=("/keys/deploy",))
    assert profile_owned_fields(profile) == ("identity_files",)
    out = apply_profile_to_data({"nickname": "test", "key_select_mode": 0}, profile)
    assert "IdentityFile /keys/deploy" in format_ssh_config_entry(out)


@pytest.mark.parametrize("own_mode, explicit_no, line", [
    (1, False, "IdentitiesOnly yes"),
    (2, True, "IdentitiesOnly no"),
])
def test_a_profile_that_lists_keys_keeps_the_hosts_identities_only(own_mode, explicit_no, line):
    # Only what the profile sets: its keys, not the host's IdentitiesOnly.
    from sshpilot.core.login_profiles.models import apply_profile_to_data
    from sshpilot.ssh_config_formatter import format_ssh_config_entry

    profile = LoginProfile(id=PID, name="k", identity_files=("/keys/deploy",))
    out = apply_profile_to_data({
        "nickname": "test", "key_select_mode": own_mode,
        "identity_files": ["/keys/own"], "identities_only_explicit_no": explicit_no,
    }, profile)
    entry = format_ssh_config_entry(out)
    assert "IdentityFile /keys/deploy" in entry and "/keys/own" not in entry
    assert line in entry


@pytest.mark.parametrize("value, line", [(True, "IdentitiesOnly yes"), (False, "IdentitiesOnly no")])
def test_a_profile_that_sets_identities_only_keeps_the_hosts_keys(value, line):
    from sshpilot.core.login_profiles.models import apply_profile_to_data
    from sshpilot.ssh_config_formatter import format_ssh_config_entry

    profile = LoginProfile(id=PID, name="k", identities_only=value)
    assert profile_owned_fields(profile) == ("identities_only",)
    out = apply_profile_to_data({
        "nickname": "test", "key_select_mode": 2, "identity_files": ["/keys/own"],
    }, profile)
    entry = format_ssh_config_entry(out)
    assert "IdentityFile /keys/own" in entry and line in entry


def test_an_old_automatic_profile_drops_its_stale_keys():
    profile = LoginProfile.from_dict({
        "id": PID, "name": "k", "key_select_mode": 0, "identity_files": ["/k"],
    })
    assert profile.identity_files == () and profile.identities_only is None


def test_links_that_own_the_old_key_mode_keep_their_fingerprint():
    # Recorded before IdentitiesOnly was its own profile setting: such a
    # link lists key_select_mode, and its hash must not move or the
    # connection would read as edited (drift) and be detached.
    data = {"username": "deploy", "key_select_mode": 1, "identity_files": ["/k/a"]}
    assert auth_fingerprint(
        data, (), owned_fields=("username", "key_select_mode", "identity_files")
    ) == "v4:69f76523fb06945c8c713c87b79baad2"
