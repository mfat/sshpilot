"""Login profiles hold authentication as plain lines, like a Host block.

A profile's PreferredAuthentications and per-method switches are options of
the profile: those it sets replace a linked connection's own, what it leaves
unset stays the connection's. Profiles saved with the older Key/Password
choice load as the lines that choice stood for, and links recorded before
keep matching their fingerprint.
"""

from sshpilot.core.connections.auth_preferences import PASSWORD_MODE_PREFERRED_AUTH
from sshpilot.core.login_profiles.models import (
    LoginProfile,
    auth_fingerprint,
    is_legacy_auth_link,
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
