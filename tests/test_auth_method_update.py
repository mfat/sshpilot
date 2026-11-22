"""Authentication settings are plain Host-block lines.

The login order (PreferredAuthentications) and the per-method switches
(PubkeyAuthentication, ...) are kept exactly as the block writes them; the
writer never generates them. The editor's older Key/Password choice and its
"Disable public key authentication" switch only edit those lines, and only
when a save changes them.
"""

import pytest

from sshpilot.core.connections.auth_preferences import (
    OPENSSH_DEFAULT_PREFERRED_AUTH,
    PASSWORD_MODE_PREFERRED_AUTH,
    apply_legacy_auth_choice,
)
from sshpilot.core.connections.ssh_config_loader import load_ssh_configuration
from sshpilot.ssh_config_formatter import format_ssh_config_entry

PASSWORD_LINE = f"PreferredAuthentications {PASSWORD_MODE_PREFERRED_AUTH}"


# -- the writer --------------------------------------------------------------

@pytest.mark.parametrize("data", [
    {"auth_method": 1},
    {"auth_method": 1, "pubkey_auth_no": True},
    {"auth_method": 0, "preferred_authentications": ["publickey", "password"]},
])
def test_the_writer_generates_no_login_lines(data):
    entry = format_ssh_config_entry({"nickname": "h", "hostname": "example.com", **data})
    assert "PreferredAuthentications" not in entry
    assert "PubkeyAuthentication" not in entry


def test_login_lines_are_written_as_given():
    extra = (
        "PreferredAuthentications publickey,keyboard-interactive\n"
        "PubkeyAuthentication yes\n"
        "GSSAPIAuthentication no"
    )
    entry = format_ssh_config_entry({
        "nickname": "h", "hostname": "example.com", "auth_method": 1,
        "extra_ssh_config": extra,
    })
    for line in extra.splitlines():
        assert f"    {line}" in entry


def test_password_first_is_openssh_order_with_the_password_methods_first():
    default = OPENSSH_DEFAULT_PREFERRED_AUTH.split(",")
    first = ["keyboard-interactive", "password"]
    assert PASSWORD_MODE_PREFERRED_AUTH == ",".join(
        first + [m for m in default if m not in first]
    )


# -- the older Key/Password choice ---------------------------------------------

@pytest.mark.parametrize("extra, change, expected", [
    # Choosing password login puts the password methods first ...
    ("", {"auth_method": 1}, [PASSWORD_LINE]),
    ("PreferredAuthentications gssapi-with-mic,hostbased,publickey",
     {"auth_method": 1}, [PASSWORD_LINE]),
    # ... unless the order already does.
    ("PreferredAuthentications password,publickey", {"auth_method": 1},
     ["PreferredAuthentications password,publickey"]),
    # Choosing key login removes what made the block read as password login.
    (f"{PASSWORD_LINE}\nPubkeyAuthentication no", {"auth_method": 0}, []),
    # A key-first order is the user's own and stays.
    ("PreferredAuthentications publickey,keyboard-interactive", {"auth_method": 0},
     ["PreferredAuthentications publickey,keyboard-interactive"]),
    # The switch sets or removes "no"; an authored "yes" stays.
    ("", {"pubkey_auth_no": True}, ["PubkeyAuthentication no"]),
    ("PubkeyAuthentication yes", {"pubkey_auth_no": True}, ["PubkeyAuthentication no"]),
    ("PubkeyAuthentication no", {"pubkey_auth_no": False}, []),
    ("PubkeyAuthentication yes", {"pubkey_auth_no": False}, ["PubkeyAuthentication yes"]),
    # Both in one save: key login, but keys switched off.
    (PASSWORD_LINE, {"auth_method": 0, "pubkey_auth_no": True},
     ["PubkeyAuthentication no"]),
    # Other lines are untouched.
    ("Compression yes", {"auth_method": 1}, ["Compression yes", PASSWORD_LINE]),
])
def test_the_older_choice_edits_the_lines(extra, change, expected):
    assert apply_legacy_auth_choice(extra, **change).splitlines() == expected


# -- through the daemon's connection service ----------------------------------

def _service(tmp_path, block):
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from sshpilot.core.connections.repository import ConnectionRepository
    from sshpilot.core.connections.ssh_config_store import SshConfigStore

    root = tmp_path / "ssh_config"
    root.write_text("Host h\n    HostName 192.0.2.1\n" + block, encoding="utf-8")
    repository = ConnectionRepository(
        ssh_store=SshConfigStore(root, isolated=True),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=True,
    )
    return ConnectionApplicationService(repository, client_name="test"), repository, root


def _update(tmp_path, block, patch):
    from sshpilot.api.models.common import ConnectionId
    from sshpilot.api.models.connections import UpdateConnectionRequest

    service, repository, root = _service(tmp_path, block)
    record = repository.get_record("h")
    service.update_connection(
        ConnectionId("h"),
        UpdateConnectionRequest(config_patch=patch, expected_generation=record.generation),
    )
    lines = [line.strip() for line in root.read_text().splitlines()[1:] if line.strip()]
    reloaded = next(
        item for item in load_ssh_configuration(root, isolated=True).connections
        if item.id == "h"
    )
    return lines, reloaded.data


def test_choosing_password_login_writes_the_order_and_reads_back(tmp_path):
    lines, data = _update(tmp_path, "    IdentityFile ~/.ssh/id_work\n", {"auth_method": 1})
    assert PASSWORD_LINE in lines
    assert "IdentityFile ~/.ssh/id_work" in lines
    assert data["auth_method"] == 1


def test_choosing_key_login_reads_back_as_key_login(tmp_path):
    lines, data = _update(
        tmp_path, f"    {PASSWORD_LINE}\n    PubkeyAuthentication no\n",
        {"auth_method": 0},
    )
    assert lines == ["HostName 192.0.2.1"]
    assert data["auth_method"] == 0
    assert data["pubkey_auth_no"] is False


def test_an_unrelated_edit_leaves_an_older_app_order_as_written(tmp_path):
    # Earlier versions wrote this; it is now simply the block's own order.
    lines, data = _update(
        tmp_path, "    PreferredAuthentications keyboard-interactive,password\n",
        {"hostname": "192.0.2.2"},
    )
    assert "PreferredAuthentications keyboard-interactive,password" in lines
    assert data["auth_method"] == 1


def test_a_saved_password_does_not_add_an_order(tmp_path):
    lines, _data = _update(tmp_path, "", {"hostname": "192.0.2.2", "auth_method": 0})
    assert not any(line.startswith("PreferredAuthentications") for line in lines)


# -- login profiles -------------------------------------------------------------

def test_a_profile_that_sets_an_order_replaces_the_hosts_own():
    from sshpilot.core.login_profiles.models import LoginProfile, apply_profile_to_data

    profile = LoginProfile(
        id="lp-0000000000000001", name="ops",
        extra_ssh_config=f"{PASSWORD_LINE}\nPubkeyAuthentication no",
    )
    out = apply_profile_to_data(
        {"extra_ssh_config": "PreferredAuthentications publickey\nCompression yes"},
        profile,
    )
    assert out["extra_ssh_config"].splitlines() == [
        "Compression yes", PASSWORD_LINE, "PubkeyAuthentication no",
    ]
    assert out["auth_method"] == 1 and out["pubkey_auth_no"] is True


def test_a_profile_that_sets_no_order_leaves_the_hosts_own():
    from sshpilot.core.login_profiles.models import LoginProfile, apply_profile_to_data

    profile = LoginProfile(id="lp-0000000000000001", name="ops")
    out = apply_profile_to_data({"extra_ssh_config": PASSWORD_LINE}, profile)
    assert out["extra_ssh_config"] == PASSWORD_LINE


# -- what a host inherits without its own authentication lines ----------------

def test_inherited_auth_ignores_the_hosts_own_lines(tmp_path):
    service, _repository, root = _service(
        tmp_path,
        "    PreferredAuthentications password\n"
        "    GSSAPIAuthentication yes\n"
        "\nHost h* *.corp\n"
        "    PreferredAuthentications publickey,keyboard-interactive\n"
        "\nHost *\n"
        "    GSSAPIAuthentication no\n",
    )
    found = {d.key: d.value for d in service.get_inherited_auth("h")}
    assert found == {
        "preferredauthentications": "publickey,keyboard-interactive",
        "gssapiauthentication": "no",
    }
    # A host not saved yet inherits the same way.
    assert {d.key: d.value for d in service.get_inherited_auth("new.corp")} == {
        "preferredauthentications": "publickey,keyboard-interactive",
        "gssapiauthentication": "no",
    }
    assert service.get_inherited_auth("bad host") == ()
