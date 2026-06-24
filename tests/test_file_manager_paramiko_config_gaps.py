"""Regression tests for Paramiko/file-manager SSH config fidelity gaps.

Each test maps to a divergence between native ``ssh -F config`` and the in-app
file manager's Paramiko connect path documented during the Oracle/USA ProxyJump
investigation.

Tests marked ``xfail`` assert the *desired* OpenSSH-compatible behaviour and
fail today until the corresponding gap is fixed. Unmarked tests document fixed
behaviour or lock in known limitations.
"""

from __future__ import annotations

import subprocess
import sys
import types
from typing import List, Optional
from unittest import mock

import pytest

from tests.test_file_manager_auth import (
    DummyClient,
    DummyPolicy,
    DummyProxyCommand,
    _load_file_manager_module,
)


@pytest.fixture(autouse=True)
def _reset_dummy_clients():
    DummyClient.instances.clear()
    DummyProxyCommand.instances.clear()
    yield
    DummyClient.instances.clear()
    DummyProxyCommand.instances.clear()


def _connection(**overrides):
    defaults = {
        "hostname": "target.internal",
        "host": "target.internal",
        "username": "alice",
        "nickname": "target",
        "auth_method": 0,
        "key_select_mode": 0,
        "keyfile": "",
        "pubkey_auth_no": False,
    }
    defaults.update(overrides)
    return types.SimpleNamespace(**defaults)


def _manager(module, connection, *, ssh_config=None, connection_manager=None, **kwargs):
    return module.AsyncSFTPManager(
        getattr(connection, "hostname", "target.internal"),
        getattr(connection, "username", "alice"),
        port=kwargs.get("port", 2222),
        connection=connection,
        connection_manager=connection_manager,
        ssh_config=ssh_config or {"auto_add_host_keys": True},
    )


def _target_connect_kwargs() -> dict:
    assert DummyClient.instances, "expected a Paramiko client"
    target = DummyClient.instances[0]
    assert target.connect_calls, "expected target connect()"
    return target.connect_calls[-1]


def _jump_connect_kwargs() -> dict:
    assert len(DummyClient.instances) >= 2, "expected jump + target clients"
    jump = DummyClient.instances[1]
    assert jump.connect_calls, "expected jump connect()"
    return jump.connect_calls[-1]


# ---------------------------------------------------------------------------
# Fixed behaviour (must stay green)
# ---------------------------------------------------------------------------


def test_hop_honors_jump_host_identity_from_effective_config(monkeypatch, tmp_path):
    """Jump hosts must offer their own IdentityFile / IdentitiesOnly (USA/kwp4 class)."""
    module = _load_file_manager_module(monkeypatch)

    jump_key = tmp_path / "bastion_ed25519"
    jump_key.write_text("dummy-key")

    module._fake_ssh_config.config_map.update(
        {
            "Bastion": {
                "hostname": "bastion.internal",
                "user": "root",
                "identityfile": [str(jump_key)],
                "identitiesonly": "yes",
            },
        }
    )

    connection = _connection(proxy_jump=["Bastion"])
    _manager(module, connection)._connect_impl()

    jump_kwargs = _jump_connect_kwargs()
    assert jump_kwargs["key_filename"] == [str(jump_key)]
    assert jump_kwargs["look_for_keys"] is False
    assert jump_kwargs["allow_agent"] is True


def test_proxy_jump_failure_does_not_fall_back_to_direct_target(monkeypatch):
    """When ProxyJump is required, a hop failure must not connect to the target directly."""
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map["Router"] = {
        "hostname": "router.internal",
        "user": "gateway",
    }

    original_connect = DummyClient.connect

    def failing_connect(self, **kwargs):
        if kwargs.get("hostname") == "router.internal":
            raise module.paramiko.SSHException("No existing session")
        return original_connect(self, **kwargs)

    monkeypatch.setattr(DummyClient, "connect", failing_connect)

    connection = _connection(proxy_jump=["Router"])
    with pytest.raises(module.paramiko.SSHException):
        _manager(module, connection)._connect_impl()

    target_calls = DummyClient.instances[0].connect_calls
    assert not any(call.get("hostname") == "target.internal" for call in target_calls)


def test_hop_handshake_uses_banner_and_auth_timeouts(monkeypatch):
    """Jump connects should bound banner/auth like the target (post-fix hardening)."""
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map["Router"] = {
        "hostname": "router.internal",
        "user": "gateway",
    }

    connection = _connection(proxy_jump=["Router"])
    _manager(
        module,
        connection,
        ssh_config={
            "auto_add_host_keys": True,
            "file_manager": {"sftp_connect_timeout": 17},
        },
    )._connect_impl()

    jump_kwargs = _jump_connect_kwargs()
    assert jump_kwargs.get("timeout") == 17
    assert jump_kwargs.get("banner_timeout") == 17
    assert jump_kwargs.get("auth_timeout") == 17


def test_connection_pubkey_auth_no_disables_agent_on_target(monkeypatch):
    """When the Connection object marks pubkey off, the target must not use the agent."""
    module = _load_file_manager_module(monkeypatch)

    connection = _connection(auth_method=0, pubkey_auth_no=True)
    _manager(module, connection)._connect_impl()

    target_kwargs = _target_connect_kwargs()
    assert target_kwargs["allow_agent"] is False
    assert target_kwargs["look_for_keys"] is False


# ---------------------------------------------------------------------------
# Known gaps — desired OpenSSH semantics (xfail until fixed)
# ---------------------------------------------------------------------------


def test_target_automatic_mode_should_use_effective_identityfile(
    monkeypatch, tmp_path
):
    """Oracle-class: ssh -G lists IdentityFile but key_mode=0 ignores them on the target."""
    module = _load_file_manager_module(monkeypatch)

    cfg_key = tmp_path / "oracle_only"
    cfg_key.write_text("dummy-key")

    module._fake_ssh_config.config_map["target"] = {
        "hostname": "target.internal",
        "user": "alice",
        "identityfile": [str(cfg_key)],
        "identitiesonly": "yes",
    }

    connection = _connection(
        nickname="target",
        hostname="target",
        key_select_mode=0,
        keyfile="",
    )
    monkeypatch.setattr(
        module.os.path,
        "isfile",
        lambda path: path == str(cfg_key),
    )
    _manager(module, connection)._connect_impl()

    target_kwargs = _target_connect_kwargs()
    assert target_kwargs.get("key_filename") == [str(cfg_key)]
    assert target_kwargs["look_for_keys"] is False
    assert target_kwargs["allow_agent"] is True


def test_target_with_identitiesonly_keeps_agent_when_identity_files_configured(
    monkeypatch, tmp_path,
):
    """IdentitiesOnly limits offered keys via IdentityFile; agent stays on like native ssh."""
    module = _load_file_manager_module(monkeypatch)

    cfg_key = tmp_path / "only_key"
    cfg_key.write_text("dummy-key")

    module._fake_ssh_config.config_map["target"] = {
        "hostname": "target.internal",
        "user": "alice",
        "identityfile": [str(cfg_key)],
        "identitiesonly": "yes",
    }

    class StubConnection:
        nickname = "target"
        hostname = "target"
        username = "alice"
        auth_method = 0
        key_select_mode = 0

        def collect_identity_file_candidates(self, effective_cfg=None):
            return [str(cfg_key)]

    monkeypatch.setattr(module.os.path, "isfile", lambda path: path == str(cfg_key))

    connection = StubConnection()
    _manager(module, connection)._connect_impl()

    target_kwargs = _target_connect_kwargs()
    assert target_kwargs["allow_agent"] is True
    assert target_kwargs["look_for_keys"] is False
    assert target_kwargs["key_filename"] == [str(cfg_key)]


def test_hop_without_identity_should_not_inherit_target_keyfile(
    monkeypatch, tmp_path
):
    """Wrong-key-on-bastion: target key A offered to hop that has no IdentityFile block."""
    module = _load_file_manager_module(monkeypatch)

    target_key = tmp_path / "target_key"
    target_key.write_text("target-key")

    module._fake_ssh_config.config_map["Router"] = {
        "hostname": "router.internal",
        "user": "gateway",
    }

    connection = _connection(
        proxy_jump=["Router"],
        key_select_mode=1,
        keyfile=str(target_key),
    )
    _manager(module, connection)._connect_impl()

    jump_kwargs = _jump_connect_kwargs()
    assert jump_kwargs.get("key_filename") != str(target_key)
    assert jump_kwargs.get("key_filename") != [str(target_key)]


def test_password_target_should_not_disable_agent_for_key_based_hop_without_hop_identity(
    monkeypatch,
):
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map["Router"] = {
        "hostname": "router.internal",
        "user": "gateway",
        "pubkeyauthentication": "true",
    }

    connection = _connection(proxy_jump=["Router"], auth_method=1, pubkey_auth_no=True)
    _manager(module, connection)._connect_impl()

    jump_kwargs = _jump_connect_kwargs()
    assert jump_kwargs["allow_agent"] is True or jump_kwargs.get("password")


def test_host_key_policy_should_use_per_host_stricthostkeychecking(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map["target"] = {
        "hostname": "target.internal",
        "user": "alice",
        "stricthostkeychecking": "yes",
    }

    connection = _connection(nickname="target", hostname="target")
    _manager(
        module,
        connection,
        ssh_config={"auto_add_host_keys": True, "strict_host_key_checking": "accept-new"},
    )._connect_impl()

    policy = DummyClient.instances[0].set_missing_host_key_policy_calls[-1]
    assert isinstance(policy, DummyPolicy)
    assert policy.name == "reject"


def test_known_hosts_should_use_per_host_userknownhostsfile(monkeypatch, tmp_path):
    module = _load_file_manager_module(monkeypatch)

    per_host_kh = tmp_path / "per_host_known_hosts"
    per_host_kh.write_text("# empty\n")
    app_kh = tmp_path / "app_known_hosts"
    app_kh.write_text("# empty\n")

    monkeypatch.setattr(
        module.os.path,
        "exists",
        lambda path: path in {str(per_host_kh), str(app_kh)},
    )

    module._fake_ssh_config.config_map["target"] = {
        "hostname": "target.internal",
        "user": "alice",
        "userknownhostsfile": str(per_host_kh),
    }

    class DummyConnMgr:
        known_hosts_path = str(app_kh)

        def get_password(self, host, username):
            return None

    connection = _connection(nickname="target", hostname="target")
    _manager(module, connection, connection_manager=DummyConnMgr())._connect_impl()

    loaded = DummyClient.instances[0].loaded_host_keys
    assert str(per_host_kh) in loaded
    assert str(app_kh) not in loaded


def test_proxy_command_failure_should_not_fall_back_to_direct_connect(monkeypatch):
    module = _load_file_manager_module(monkeypatch)

    def boom(_command):
        raise OSError("proxy setup failed")

    monkeypatch.setattr(
        module.paramiko.proxy,
        "ProxyCommand",
        boom,
    )

    connection = _connection(proxy_command="ssh -W %h:%p bastion")
    with pytest.raises((OSError, module.paramiko.SSHException)):
        _manager(module, connection)._connect_impl()

    # Fail-fast: the target must not be connected directly when its ProxyCommand
    # could not be set up.
    assert DummyClient.instances, "expected the target client to be created"
    assert DummyClient.instances[0].connect_calls == []


def test_hop_keeps_agent_even_with_identityagent_none(
    monkeypatch,
):
    """We never disable the agent, even on IdentityAgent none.

    sshpilot's askpass flow loads passphrase-protected keys into the ssh-agent,
    so turning the agent off (as native ssh would for ``IdentityAgent none``)
    would break auth for exactly those keys. The hop keeps ``allow_agent`` on.
    """
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map["Router"] = {
        "hostname": "router.internal",
        "user": "gateway",
        "identityagent": "none",
    }

    connection = _connection(proxy_jump=["Router"])
    _manager(module, connection)._connect_impl()

    jump_kwargs = _jump_connect_kwargs()
    assert jump_kwargs["allow_agent"] is True


# ---------------------------------------------------------------------------
# Documented limitations — assert current behaviour (stay green)
# ---------------------------------------------------------------------------


def test_proxy_command_leaves_unsupported_percent_tokens_unexpanded(monkeypatch):
    """Only %%, %h, %p, %r, %n are expanded today; %C/%i/%d/etc. are not."""
    module = _load_file_manager_module(monkeypatch)

    connection = _connection(
        proxy_command="ssh -W %h:%p cert-%C@%n",
        nickname="prod",
    )
    _manager(module, connection, port=2200)._connect_impl()

    proxy = DummyProxyCommand.instances[-1]
    assert proxy.command == "ssh -W target.internal:2200 cert-%C@prod"
    assert "%C" in proxy.command


def test_nested_proxyjump_on_hop_chains_through_inner_bastion(monkeypatch):
    """Nested ProxyJump on a hop is expanded into the Paramiko chain."""
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map.update(
        {
            "Inner": {"hostname": "inner.internal", "user": "inner"},
            "Router": {
                "hostname": "router.internal",
                "user": "gateway",
                "proxyjump": "Inner",
            },
        }
    )

    connection = _connection(proxy_jump=["Router"])
    _manager(module, connection)._connect_impl()

    assert len(DummyClient.instances) == 3
    hop_hostnames = [
        call["hostname"]
        for client in DummyClient.instances[1:]
        for call in client.connect_calls
    ]
    assert "inner.internal" in hop_hostnames
    assert "router.internal" in hop_hostnames
    assert ("Inner", None) in module._fake_ssh_config.queries


def test_external_verify_ssh_omits_config_file_alias_and_proxyjump(monkeypatch):
    """sftp_utils pre-mount verify uses bare ssh user@host, not ssh -F / ProxyJump."""
    from sshpilot import sftp_utils

    captured: List[List[str]] = []

    def fake_run(cmd, **kwargs):
        captured.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="READY\n", stderr="")

    monkeypatch.setattr(sftp_utils.subprocess, "run", fake_run)
    monkeypatch.delenv("SSH_ASKPASS", raising=False)

    assert sftp_utils._verify_ssh_connection("ubuntu", "150.230.27.23", 2222) is True

    assert captured, "expected subprocess.run to be invoked"
    cmd = captured[0]
    assert cmd[0] == "ssh"
    assert "-F" not in cmd
    assert "-J" not in cmd
    assert "-o" in cmd
    assert "ubuntu@150.230.27.23" in cmd
    assert "-p" in cmd
    port_index = cmd.index("-p")
    assert cmd[port_index + 1] == "2222"


def test_target_with_pinned_keyfile_does_not_merge_other_effective_identityfiles(
    monkeypatch, tmp_path,
):
    """NL-class: ssh -G lists multiple IdentityFile entries; app pins only one."""
    module = _load_file_manager_module(monkeypatch)

    pinned = tmp_path / "id_ed25519"
    other = tmp_path / "kwp9"
    pinned.write_text("pinned")
    other.write_text("other")

    module._fake_ssh_config.config_map["target"] = {
        "hostname": "target.internal",
        "user": "alice",
        "identityfile": [str(pinned), str(other)],
        "identitiesonly": "yes",
    }

    connection = _connection(
        nickname="target",
        hostname="target",
        key_select_mode=1,
        keyfile=str(pinned),
    )
    monkeypatch.setattr(
        module.os.path,
        "isfile",
        lambda path: path in {str(pinned), str(other)},
    )
    _manager(module, connection)._connect_impl()

    target_kwargs = _target_connect_kwargs()
    assert target_kwargs.get("key_filename") == str(pinned)
    assert str(other) not in (target_kwargs.get("key_filename") or [])


def test_automatic_target_still_enables_agent_spray(monkeypatch):
    """Documents MaxAuthTries risk on targets in automatic key mode (Oracle-class)."""
    module = _load_file_manager_module(monkeypatch)

    module._fake_ssh_config.config_map["target"] = {
        "hostname": "target.internal",
        "user": "alice",
        "identityfile": ["~/.ssh/id_ed25519"],
        "identitiesonly": "no",
    }

    connection = _connection(nickname="target", hostname="target", key_select_mode=0)
    _manager(module, connection)._connect_impl()

    target_kwargs = _target_connect_kwargs()
    assert target_kwargs["allow_agent"] is True
    assert target_kwargs["look_for_keys"] is True
    assert "key_filename" not in target_kwargs


# ---------------------------------------------------------------------------
# Passphrase-protected keys: keyring-stored passphrases are preloaded into the
# ssh-agent (via askpass) for the target AND every jump host before connecting,
# so paramiko authenticates through the agent. The agent is never disabled.
# ---------------------------------------------------------------------------


def _fake_askpass(monkeypatch, *, stored_keys):
    """Install a fake ``sshpilot.askpass_utils`` and record ssh-agent adds.

    ``stored_keys`` is the set of key paths whose passphrase is "in the keyring".
    Returns the list that records every key handed to ``ensure_key_in_agent``.
    """
    added: list = []
    fake = types.ModuleType("sshpilot.askpass_utils")
    fake.lookup_passphrase = lambda path: "stored-pp" if path in stored_keys else ""

    def _ensure(path, **kwargs):
        added.append(path)
        return True

    fake.ensure_key_in_agent = _ensure
    monkeypatch.setitem(sys.modules, "sshpilot.askpass_utils", fake)
    return added


def _record_emits(manager):
    """Capture GObject signal emissions from the manager."""
    emitted = []
    manager.emit = lambda *args: emitted.append(args)
    return emitted


def _fail_connect_for(module, monkeypatch, hostname):
    """Make a DummyClient.connect to *hostname* fail as an encrypted key would.

    Paramiko raises ``PasswordRequiredException`` (an ``AuthenticationException``
    subclass) when a key_filename is encrypted and no passphrase is available.
    """
    original_connect = DummyClient.connect

    def failing_connect(self, **kwargs):
        if kwargs.get("hostname") == hostname:
            raise module.paramiko.AuthenticationException(
                "Private key file is encrypted"
            )
        return original_connect(self, **kwargs)

    monkeypatch.setattr(DummyClient, "connect", failing_connect)


def test_target_passphrase_key_failure_triggers_in_app_auth_prompt(monkeypatch):
    """Target: an encrypted-key auth failure surfaces the password/passphrase dialog.

    paramiko reuses the typed value as the key passphrase, so the user can
    recover. This is the working fallback the jump-host path lacks.
    """
    module = _load_file_manager_module(monkeypatch)
    _fail_connect_for(module, monkeypatch, "target.internal")

    connection = _connection()  # no proxy
    manager = _manager(module, connection)
    emitted = _record_emits(manager)

    # Must NOT raise — the AuthenticationException is caught and turned into a
    # prompt request instead.
    manager._connect_impl()

    assert any(sig[0] == "authentication-required" for sig in emitted)
    assert not any(sig[0] == "connection-error" for sig in emitted)


def test_jump_host_passphrase_key_is_preloaded_into_agent(monkeypatch, tmp_path):
    """A jump host's passphrase-protected key (passphrase stored) is unlocked into
    the ssh-agent before the hop connect, so paramiko authenticates via the agent."""
    module = _load_file_manager_module(monkeypatch)

    jump_key = tmp_path / "bastion_key"
    jump_key.write_text("dummy")

    module._fake_ssh_config.config_map["Router"] = {
        "hostname": "router.internal",
        "user": "gateway",
        "identityfile": [str(jump_key)],
    }

    added = _fake_askpass(monkeypatch, stored_keys={str(jump_key)})

    connection = _connection(proxy_jump=["Router"])
    _manager(module, connection)._connect_impl()

    # The hop key was preloaded, and both hop + target connected.
    assert str(jump_key) in added
    assert len(DummyClient.instances) == 2


def test_jump_host_key_without_stored_passphrase_is_not_preloaded(monkeypatch, tmp_path):
    """Keyring-gated: a hop key with no stored passphrase is left untouched."""
    module = _load_file_manager_module(monkeypatch)

    jump_key = tmp_path / "bastion_key"
    jump_key.write_text("dummy")

    module._fake_ssh_config.config_map["Router"] = {
        "hostname": "router.internal",
        "user": "gateway",
        "identityfile": [str(jump_key)],
    }

    added = _fake_askpass(monkeypatch, stored_keys=set())  # nothing in keyring

    connection = _connection(proxy_jump=["Router"])
    _manager(module, connection)._connect_impl()

    assert added == []


def test_target_passphrase_key_is_preloaded_before_connect(monkeypatch):
    """Target automatic mode: the connection's keyring-gated agent preload runs
    before the target connects."""
    module = _load_file_manager_module(monkeypatch)

    preload_calls: list = []

    connection = _connection()  # automatic key mode, no proxy

    def _preload(cfg=None):
        # Snapshot the target's connect history at preload time.
        target = DummyClient.instances[0] if DummyClient.instances else None
        preload_calls.append(list(target.connect_calls) if target else None)

    connection._preload_keys_into_agent = _preload

    _manager(module, connection)._connect_impl()

    assert preload_calls, "expected _preload_keys_into_agent to be called"
    # Preload happened before the target's connect() was issued.
    assert preload_calls[0] == []
