"""Regenerate this directory from the v6.4.4 sources.

    git worktree add --detach /tmp/v644 v6.4.4
    PYTHONPATH=/tmp/v644/src python3 tests/fixtures/release_6_4_4/make_fixture.py /tmp/out
    cp /tmp/out/config /tmp/out/login_profiles.json tests/fixtures/release_6_4_4/
    sed 's#/tmp/out#@ROOT@#g' /tmp/out/connections.json \
        > tests/fixtures/release_6_4_4/connections.json

It must run under 6.4.4, never the current tree: the point of the files is
that the release wrote them. tests/test_upgrade_from_6_4_4.py reads them.
"""
import sys
from pathlib import Path

from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.core.login_profiles.models import LINK_EXPLICIT
from sshpilot.core.login_profiles.service import LoginProfileService

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
root = out / "config"
# Hand-written blocks the app did not author, ahead of anything it saves.
root.write_text(
    "# my servers\n"
    "Host otp\n"
    "    HostName otp.example.com\n"
    "    User alice\n"
    "    PreferredAuthentications publickey,keyboard-interactive\n"
    "    IdentityFile ~/.ssh/id_work\n"
    "\n"
    "Host handpw\n"
    "    HostName handpw.example.com\n"
    "    PreferredAuthentications password,publickey\n"
    "    PubkeyAuthentication no\n"
    "    KbdInteractiveAuthentication no\n"
    "\n"
    "Host plain\n"
    "    HostName plain.example.com\n"
)
repo = ConnectionRepository(
    ssh_store=SshConfigStore(root),
    state_path=out / "connections.json",
    legacy_config_path=out / "config.json",
    isolated=False,
)


class Secrets:
    def store(self, *a): return True
    def lookup(self, *a): return None
    def delete(self, *a): return True


service = LoginProfileService(repo, out / "login_profiles.json", secret_store=Secrets())

BASE = {"username": "alice", "port": 22}
SAVED = {
    "key-auto": {},
    "key-own": {"key_select_mode": 1, "keyfile": "~/.ssh/id_own",
                "identity_files": ["~/.ssh/id_own"]},
    "key-pref": {"key_select_mode": 2, "keyfile": "~/.ssh/id_pref",
                 "identity_files": ["~/.ssh/id_pref"]},
    "key-agent": {"identity_agent": "~/.1password/agent.sock",
                  "add_keys_to_agent": "yes"},
    "pw": {"auth_method": 1},
    "pw-nokey": {"auth_method": 1, "pubkey_auth_no": True},
    "pw-extra": {"auth_method": 1,
                 "extra_ssh_config": "Compression yes\nServerAliveInterval 30"},
    "linked-key": {},
    "linked-pw": {},
    "linked-extra": {},
    "linked-jump": {},
    "group-a": {},
    "group-b": {},
}
for name, extra in SAVED.items():
    repo.create_connection({
        **BASE, "nickname": name, "hostname": f"{name}.example.com", **extra,
    })

deploy = service.create_profile({
    "name": "Deploy", "username": "deploy", "key_select_mode": 1,
    "identity_files": ["/keys/deploy_ed25519"],
})
ops = service.create_profile({
    "name": "Ops password", "username": "ops", "auth_method": 1,
    "pubkey_auth_no": True,
})
tuned = service.create_profile({
    "name": "Tuned", "username": "tuned",
    "extra_ssh_config": "ServerAliveInterval 15\nCompression yes",
})
jump = service.create_profile({
    "name": "Through jump", "username": "hop",
    "proxy_command": "ssh -W %h:%p  bastion.example.com",
})
service.assign_connections(["linked-jump"], LINK_EXPLICIT, jump.id)
service.assign_connections(["linked-key"], LINK_EXPLICIT, deploy.id)
service.assign_connections(["linked-pw"], LINK_EXPLICIT, ops.id)
service.assign_connections(["linked-extra"], LINK_EXPLICIT, tuned.id)
group = repo.create_group("Prod").id
repo.assign_connection_to_group("group-a", group)
repo.assign_connection_to_group("group-b", group)
service.set_group_profile(group, ops.id, link_members=["group-a", "group-b"])
assert service.reconcile() == ()
