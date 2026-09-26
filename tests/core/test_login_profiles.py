"""Login profiles: model, resolution into Host blocks, inheritance, drift, delete."""

import json

import pytest

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.errors import CoreError
from sshpilot.core.login_profiles.models import (
    LINK_EXPLICIT,
    LINK_INHERIT,
    LoginProfile,
    LoginProfileState,
    apply_profile_to_data,
    parse_link,
)
from sshpilot.core.login_profiles.service import (
    DETACH_DELETED,
    DETACH_DRIFT,
    LoginProfileService,
)
from sshpilot.core.login_profiles.store import read_login_profiles
from sshpilot.daemon.connection_secret_provider import DaemonLoginProfileSecretStore


SSH_CONFIG = (
    "Host web1\n"
    "    HostName web1.example.com\n"
    "    User alice\n"
    "\n"
    "Host web2\n"
    "    HostName web2.example.com\n"
    "    User bob\n"
    "\n"
    "Host db1\n"
    "    HostName db1.example.com\n"
    "    User carol\n"
)


class FakeManager:
    """Stands in for the secret-storage manager (keyed like a real backend)."""

    def __init__(self):
        self.values = {}

    def store(self, spec, value):
        self.values[spec.keyring_account, spec.attributes.get("host")] = value
        return True

    def lookup(self, spec):
        return self.values.get((spec.keyring_account, spec.attributes.get("host")))

    def delete(self, spec):
        return self.values.pop((spec.keyring_account, spec.attributes.get("host")), None) is not None


@pytest.fixture
def env(tmp_path):
    root = tmp_path / "config"
    root.write_text(SSH_CONFIG)
    repo = ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )
    secrets = FakeManager()
    events = []
    service = LoginProfileService(
        repo,
        tmp_path / "login_profiles.json",
        secret_store=DaemonLoginProfileSecretStore(lambda: secrets),
        on_event=lambda kind, payload: events.append((kind, dict(payload))),
    )
    return repo, service, root, secrets, events


def _block(root, alias):
    text = root.read_text()
    start = text.index(f"Host {alias}\n")
    end = text.find("\nHost ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _link(repo, cid):
    metadata = {m.connection_id: m.values for m in repo.snapshot().metadata}
    return parse_link(metadata.get(cid))


def _deploy(service, **extra):
    values = {
        "name": "Deploy",
        "username": "deploy",
        "key_select_mode": 1,
        "identity_files": ["/keys/deploy_ed25519"],
    }
    values.update(extra)
    return service.create_profile(values)


# -- model -------------------------------------------------------------------

def test_profile_validation_and_roundtrip():
    profile = LoginProfile(
        id="lp-0123456789abcdef",
        name=" Ops ",
        username="ops",
        identity_files=("/k/a", "/k/a", "/k/b"),
        extra_ssh_config="ServerAliveInterval 30\n# comment\n",
    )
    assert profile.name == "Ops"
    assert profile.identity_files == ("/k/a", "/k/b")
    assert profile.extra_ssh_config == "ServerAliveInterval 30"
    assert LoginProfile.from_dict(profile.to_dict()) == profile
    with pytest.raises(ValueError):
        LoginProfile(id="lp-0123456789abcdef", name="x", extra_ssh_config="User root")
    with pytest.raises(ValueError):
        LoginProfile(id="lp-0123456789abcdef", name="x", username="a b")
    with pytest.raises(ValueError):
        LoginProfile(id="bad", name="x")


def test_apply_profile_merges_extra_and_marks_user_authored():
    profile = LoginProfile(
        id="lp-0123456789abcdef",
        name="Ops",
        username="ops",
        extra_ssh_config="ServerAliveInterval 30",
    )
    out = apply_profile_to_data(
        {
            "username": "alice",
            "extra_ssh_config": "ServerAliveInterval 5\nCompression yes",
            "preferred_authentications": ["password"],
        },
        profile,
    )
    assert out["username"] == "ops"
    assert "user" in out["__authored_directives"]
    assert out["extra_ssh_config"] == "Compression yes\nServerAliveInterval 30"
    assert out["preferred_authentications"] == []


# -- CRUD & persistence ------------------------------------------------------

def test_create_persists_without_secrets(env, tmp_path):
    _repo, service, _root, secrets, _events = env
    profile = _deploy(service)
    assert service.set_password(profile.id, "hunter2")
    raw = (tmp_path / "login_profiles.json").read_text()
    assert "hunter2" not in raw
    state = read_login_profiles(tmp_path / "login_profiles.json")
    assert state.get(profile.id).has_password is True
    assert service.lookup_password(profile.id) == "hunter2"
    with pytest.raises(CoreError):
        service.create_profile({"name": "deploy"})  # case-insensitive clash


def test_unknown_fields_rejected(env):
    _repo, service, *_ = env
    with pytest.raises(CoreError):
        service.create_profile({"name": "x", "password": "nope"})


# -- explicit assignment -----------------------------------------------------

def test_assign_renders_host_blocks(env):
    repo, service, root, *_ = env
    profile = _deploy(service)
    service.assign_connections(["web1", "web2"], LINK_EXPLICIT, profile.id)
    for alias in ("web1", "web2"):
        block = _block(root, alias)
        assert "User deploy" in block
        assert "IdentityFile /keys/deploy_ed25519" in block
        assert "IdentitiesOnly yes" in block
        link = _link(repo, alias)
        assert link.mode == LINK_EXPLICIT and link.profile_id == profile.id
        assert link.applied and link.rev == f"{profile.id}:1"
    assert "User carol" in _block(root, "db1")


def test_preview_lists_changes(env):
    _repo, service, *_ = env
    profile = _deploy(service)
    (preview,) = service.preview_assignment(["web1"], LINK_EXPLICIT, profile.id)
    fields = {c.field: (c.before, c.after) for c in preview.changes}
    assert fields["username"] == ("alice", "deploy")
    assert fields["identity_files"] == ([], ["/keys/deploy_ed25519"])
    assert preview.profile_name == "Deploy"


def test_profile_edit_updates_every_linked_block(env):
    repo, service, root, *_ = env
    profile = _deploy(service)
    service.assign_connections(["web1", "web2"], LINK_EXPLICIT, profile.id)
    service.update_profile(profile.id, {"username": "release"}, expected_revision=1)
    assert "User release" in _block(root, "web1")
    assert "User release" in _block(root, "web2")
    assert _link(repo, "web1").rev == f"{profile.id}:2"
    with pytest.raises(CoreError):
        service.update_profile(profile.id, {"username": "x"}, expected_revision=1)


def test_unassign_keeps_values(env):
    repo, service, root, *_ = env
    profile = _deploy(service)
    service.assign_connections(["web1"], LINK_EXPLICIT, profile.id)
    service.assign_connections(["web1"], None)
    assert _link(repo, "web1") is None
    assert "User deploy" in _block(root, "web1")


# -- groups ------------------------------------------------------------------

def test_group_inheritance_nested_and_explicit_wins(env):
    repo, service, root, *_ = env
    deploy = _deploy(service)
    admin = service.create_profile({"name": "Admin", "username": "admin"})
    parent = repo.create_group("Prod").id
    child = repo.create_group("Web", parent_id=parent).id
    repo.assign_connection_to_group("web1", child)
    repo.assign_connection_to_group("web2", child)

    service.set_group_profile(parent, deploy.id, link_members=["web1", "web2"])
    assert "User deploy" in _block(root, "web1")
    assert service.effective_for("web1").group_id == parent

    service.assign_connections(["web2"], LINK_EXPLICIT, admin.id)
    assert "User admin" in _block(root, "web2")

    # Nearest ancestor wins.
    service.set_group_profile(child, admin.id)
    assert "User admin" in _block(root, "web1")
    assert service.usage(admin.id).inherited_connections == ("web1",)
    assert service.usage(admin.id).connections == ("web2",)


def test_group_move_reapplies_inherited_profile(env):
    repo, service, root, *_ = env
    deploy = _deploy(service)
    admin = service.create_profile({"name": "Admin", "username": "admin"})
    a = repo.create_group("A").id
    b = repo.create_group("B").id
    repo.assign_connection_to_group("web1", a)
    service.set_group_profile(a, deploy.id, link_members=["web1"])
    service.set_group_profile(b, admin.id)
    assert "User deploy" in _block(root, "web1")

    repo.assign_connection_to_group("web1", b)  # listener reconciles
    assert "User admin" in _block(root, "web1")
    assert _link(repo, "web1").mode == LINK_INHERIT


# -- drift -------------------------------------------------------------------

def test_external_edit_auto_detaches(env):
    repo, service, root, _secrets, events = env
    profile = _deploy(service)
    service.assign_connections(["web1", "web2"], LINK_EXPLICIT, profile.id)
    root.write_text(root.read_text().replace(
        "Host web1\n    HostName web1.example.com\n    User deploy",
        "Host web1\n    HostName web1.example.com\n    User root",
    ))
    repo.reload()
    assert _link(repo, "web1") is None
    assert "User root" in _block(root, "web1")
    assert _link(repo, "web2") is not None
    assert ("detached", {
        "connection_id": "web1", "profile_name": "Deploy", "reason": DETACH_DRIFT,
    }) in events


def test_unrelated_edit_does_not_detach(env):
    repo, service, root, *_ = env
    profile = _deploy(service)
    service.assign_connections(["web1"], LINK_EXPLICIT, profile.id)
    root.write_text(root.read_text().replace("web1.example.com", "web1.example.org"))
    repo.reload()
    assert _link(repo, "web1") is not None


# -- delete ------------------------------------------------------------------

def test_delete_with_reassign_and_detach(env):
    repo, service, root, secrets, _events = env
    deploy = _deploy(service)
    admin = service.create_profile({"name": "Admin", "username": "admin"})
    service.set_password(deploy.id, "pw")
    group = repo.create_group("G").id
    repo.assign_connection_to_group("db1", group)
    service.set_group_profile(group, deploy.id, link_members=["db1"])
    service.assign_connections(["web1", "web2"], LINK_EXPLICIT, deploy.id)

    detached = service.delete_profile(
        deploy.id,
        replacement_id=admin.id,
        overrides={"web2": None, f"group:{group}": None},
    )
    assert service.get_profile(deploy.id) is None
    assert "User admin" in _block(root, "web1")
    assert _link(repo, "web2") is None and "User deploy" in _block(root, "web2")
    assert _link(repo, "db1") is None and "User deploy" in _block(root, "db1")
    assert service.group_profile_id(group) is None
    assert {(d.connection_id, d.reason) for d in detached} == {
        ("web2", DETACH_DELETED), ("db1", DETACH_DELETED),
    }
    assert secrets.values == {}


# -- secrets -----------------------------------------------------------------

def test_password_resolves_through_group(env):
    repo, service, *_ = env
    deploy = _deploy(service)
    service.set_password(deploy.id, "s3cret")
    service.set_sudo_password(deploy.id, "sud0")
    group = repo.create_group("G").id
    repo.assign_connection_to_group("web1", group)
    service.set_group_profile(group, deploy.id, link_members=["web1"])
    assert service.password_for_connection("web1") == "s3cret"
    assert service.sudo_password_for_connection("web1") == "sud0"
    assert service.password_for_connection("web2") is None
    service.set_password(deploy.id, None)
    assert service.password_for_connection("web1") is None


# -- backup ------------------------------------------------------------------

def test_backup_roundtrip_merges_with_suffix(env):
    _repo, service, *_ = env
    profile = _deploy(service)
    service.set_password(profile.id, "pw")
    section = service.snapshot_for_backup()
    assert "pw" not in json.dumps(section)
    assert section["profiles"][profile.id]["has_password"] is False

    other = LoginProfileState.from_dict(section)
    renamed = other.profiles[0].with_changes(id="lp-aaaaaaaaaaaaaaaa")
    added = service.restore_from_backup(
        LoginProfileState(profiles=(renamed,)).to_dict()
    )
    assert added == 1
    assert sorted(p.name for p in service.list_profiles()) == ["Deploy", "Deploy (2)"]


def test_unreadable_file_disables_mutations_and_keeps_links(env, tmp_path):
    repo, service, root, *_ = env
    profile = _deploy(service)
    service.assign_connections(["web1"], LINK_EXPLICIT, profile.id)
    path = tmp_path / "login_profiles.json"
    path.write_text("{not json")
    broken = LoginProfileService(repo, path, listen=False)
    assert broken.available is False
    assert broken.reconcile() == ()
    assert _link(repo, "web1") is not None
    with pytest.raises(CoreError):
        broken.create_profile({"name": "x"})
    assert path.read_text() == "{not json"


def test_daemon_secret_provider_prefers_profile_password(env):
    from sshpilot.daemon.connection_secret_provider import DaemonConnectionSecretProvider

    repo, service, _root, secrets, _events = env
    profile = _deploy(service)
    service.set_password(profile.id, "from-profile")
    service.assign_connections(["web1"], LINK_EXPLICIT, profile.id)
    provider = DaemonConnectionSecretProvider(
        repo.get_record,
        secret_manager_factory=lambda: secrets,
        profile_password_lookup=service.password_for_connection,
    )
    assert provider.lookup_connection_password("web1") == "from-profile"
    assert provider.lookup_connection_password("web2") is None
