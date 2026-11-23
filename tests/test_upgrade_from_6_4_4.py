"""Data written by 6.4.4 must survive being opened by this build.

``tests/fixtures/release_6_4_4`` holds the three files that release leaves
behind (ssh config, connection state, login profiles), produced by running
the 6.4.4 sources -- see ``make_fixture.py`` there. Since then authentication
moved from a Key/Password choice to plain Host-block lines, and profile links
are fingerprinted differently. An upgrade is the one moment every user hits
both changes at once, so:

* opening the data writes nothing to the ssh config and detaches no profile;
* an unrelated edit changes only the line that was edited;
* what ``ssh -G`` resolves for each host is the same before and after;
* editing an older profile keeps the logins of the hosts that use it.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.login_profiles.models import parse_link
from sshpilot.core.login_profiles.service import LoginProfileService

FIXTURE = Path(__file__).parent / "fixtures" / "release_6_4_4"

HAND_WRITTEN = ["otp", "handpw", "plain"]
SAVED = ["key-auto", "key-own", "key-pref", "key-agent", "pw", "pw-nokey", "pw-extra"]
LINKED = {
    "linked-key": "Deploy",
    "linked-pw": "Ops password",
    "linked-extra": "Tuned",
    "group-a": "Ops password",
    "group-b": "Ops password",
}
ALL = HAND_WRITTEN + SAVED + list(LINKED)

# What decides how a host logs in, as ``ssh -G`` names it.
AUTH_KEYS = (
    "user",
    "preferredauthentications",
    "pubkeyauthentication",
    "passwordauthentication",
    "kbdinteractiveauthentication",
    "gssapiauthentication",
    "hostbasedauthentication",
    "identityfile",
    "identitiesonly",
    "identityagent",
    "addkeystoagent",
    "certificatefile",
)


# Password login as this build writes it: the default order, password first.
NEW_ORDER = "keyboard-interactive,password,gssapi-with-mic,hostbased,publickey"


class _Secrets:
    def store(self, *_args):
        return True

    def lookup(self, *_args):
        return None

    def delete(self, *_args):
        return True


@pytest.fixture
def upgraded(tmp_path):
    """The 6.4.4 files in a fresh directory, opened by the current code."""
    for name in ("config", "login_profiles.json"):
        shutil.copy(FIXTURE / name, tmp_path / name)
    state = (FIXTURE / "connections.json").read_text(encoding="utf-8")
    (tmp_path / "connections.json").write_text(
        state.replace("@ROOT@", str(tmp_path)), encoding="utf-8"
    )
    root = tmp_path / "config"
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    service = LoginProfileService(
        repo, tmp_path / "login_profiles.json", secret_store=_Secrets()
    )
    return repo, service, root


def _blocks(root):
    """Alias -> the stripped option lines of its Host block."""
    blocks, current = {}, None
    for line in root.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("host "):
            current = blocks.setdefault(stripped.split(None, 1)[1], [])
        elif stripped and not stripped.startswith("#") and current is not None:
            current.append(stripped)
    return blocks


def _resolved(root, alias):
    """The login-deciding part of ``ssh -G`` for one alias."""
    out = subprocess.run(
        ["ssh", "-G", "-F", str(root), alias],
        capture_output=True, text=True, check=True, timeout=20,
    ).stdout
    found = {}
    for line in out.splitlines():
        key, _, value = line.partition(" ")
        if key in AUTH_KEYS:
            found.setdefault(key, []).append(value)
    return found


def _links(repo, service):
    """Alias -> the name of the profile it is linked to, for linked hosts."""
    metadata = {m.connection_id: m.values for m in repo.snapshot().metadata}
    names = {}
    for record in repo.list_records():
        if parse_link(metadata.get(record.id)) is None:
            continue
        effective = service.effective_for(record.id)
        names[repo.ssh_alias_for(record.id)] = (
            effective.profile.name if effective else None
        )
    return names


def test_the_fixture_is_what_this_test_thinks_it_is():
    assert sorted(_blocks(FIXTURE / "config")) == sorted(ALL)


def test_opening_writes_nothing_and_detaches_nothing(upgraded):
    repo, service, root = upgraded
    assert service.available
    assert service.reconcile() == (), "opening 6.4.4 data detached a login profile"
    assert root.read_text(encoding="utf-8") == (FIXTURE / "config").read_text(
        encoding="utf-8"
    ), "opening 6.4.4 data rewrote the ssh config"
    assert _links(repo, service) == LINKED
    # A second start must be as quiet as the first.
    assert service.reconcile() == ()
    assert _links(repo, service) == LINKED


def test_older_profiles_load_as_the_lines_their_choice_stood_for(upgraded):
    _repo, service, _root = upgraded
    profiles = {p.name: p for p in service.list_profiles()}
    assert sorted(profiles) == ["Deploy", "Ops password", "Tuned"]
    assert profiles["Ops password"].extra_ssh_config.splitlines() == [
        f"PreferredAuthentications {NEW_ORDER}",
        "PubkeyAuthentication no",
    ]
    assert profiles["Deploy"].extra_ssh_config == ""
    assert profiles["Deploy"].identity_files == ("/keys/deploy_ed25519",)
    assert profiles["Tuned"].extra_ssh_config.splitlines() == [
        "ServerAliveInterval 15", "Compression yes",
    ]
    # Loading is not an edit; a bumped revision would re-apply to every host.
    assert {p.revision for p in profiles.values()} == {1}


@pytest.mark.parametrize("alias", ALL)
def test_an_unrelated_edit_changes_only_the_edited_line(upgraded, alias):
    repo, service, root = upgraded
    service.reconcile()
    before = _blocks(root)
    record = next(r for r in repo.list_records() if repo.ssh_alias_for(r.id) == alias)
    editable = repo.get_editor_record(record.id)
    repo.update_connection(
        record.id, {**editable.data, "hostname": "192.0.2.77"}
    )
    after = _blocks(root)

    expected = [
        "HostName 192.0.2.77" if line.startswith("HostName ") else line
        for line in before[alias]
    ]
    assert sorted(after[alias]) == sorted(expected)
    for other in ALL:
        if other != alias:
            assert after[other] == before[other], f"editing {alias} changed {other}"
    assert service.reconcile() == ()
    assert _links(repo, service) == LINKED


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs the ssh client")
def test_ssh_resolves_every_host_the_same_after_opening_and_editing(upgraded):
    repo, service, root = upgraded
    before = {alias: _resolved(FIXTURE / "config", alias) for alias in ALL}
    service.reconcile()
    for record in repo.list_records():
        editable = repo.get_editor_record(record.id)
        repo.update_connection(
            record.id, {**editable.data, "hostname": "192.0.2.77"}
        )
    service.reconcile()
    for alias in ALL:
        assert _resolved(root, alias) == before[alias], alias


def test_editing_an_older_profile_keeps_its_hosts_logins(upgraded):
    """Password stays first and keys stay off; only the tail of the order grows.

    6.4.4 wrote ``keyboard-interactive,password`` for a password profile. The
    profile now holds the full order with those two moved to the front, so
    its first edit rewrites the hosts' line to that. The methods added behind
    the password are ones 6.4.4 never tried for these hosts.
    """
    repo, service, root = upgraded
    service.reconcile()
    before = _blocks(root)
    ops = next(p for p in service.list_profiles() if p.name == "Ops password")
    service.update_profile(ops.id, {"username": "ops2"})
    after = _blocks(root)

    old_order = "PreferredAuthentications keyboard-interactive,password"
    replaced = {"User ops": "User ops2", old_order: f"PreferredAuthentications {NEW_ORDER}"}
    for alias in ("linked-pw", "group-a", "group-b"):
        assert old_order in before[alias]
        expected = [replaced.get(line, line) for line in before[alias]]
        assert sorted(after[alias]) == sorted(expected), alias
        assert "PubkeyAuthentication no" in after[alias]
    for alias in ALL:
        if alias not in ("linked-pw", "group-a", "group-b"):
            assert after[alias] == before[alias], alias
    assert service.reconcile() == ()
    assert _links(repo, service) == LINKED
