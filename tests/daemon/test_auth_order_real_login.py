"""A saved authentication order is what a real login attempts.

``test_auth_policy_matrix.py`` asks ``ssh -G`` what a saved edit resolves to;
``test_openssh_parity.py`` pins that the daemon launches a host as plain
``ssh <alias>`` resolves it. This closes the loop with a real handshake: the
editor's model makes the edit, the store saves it, the production launch
provider builds the command, and that command logs in to a throwaway
``sshd``. The methods OpenSSH then tries, in order, are read from its own
debug output and must be the ones the editor's "OpenSSH will try" line
states.

The server accepts the test key and rejects every password, so a login that
reaches ``publickey`` succeeds and one that may not use it fails.
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.connection_dialog_auth import (  # noqa: E402
    AuthPolicy,
    join_extra,
    split_policy_lines,
)
from sshpilot.core.connections.repository import ConnectionRepository  # noqa: E402
from sshpilot.core.connections.ssh_config_loader import (  # noqa: E402
    load_ssh_configuration,
)
from sshpilot.core.connections.ssh_config_store import SshConfigStore  # noqa: E402
from sshpilot.daemon.connection_launch_provider import (  # noqa: E402
    DaemonConnectionLaunchProvider,
)

pytestmark = pytest.mark.integration

TOOLS = ("ssh", "sshd", "ssh-keygen")
MARK = "AUTH_ORDER_LOGIN_OK"


def _free_port() -> int:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]
    finally:
        listener.close()


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """A private sshd for the current user: the test key logs in, no
    password does (it cannot read any password database)."""
    tools = {name: shutil.which(name) for name in TOOLS}
    if not all(tools.values()):
        pytest.skip("needs the OpenSSH client, server and ssh-keygen")
    if os.name != "posix":
        pytest.skip("needs a POSIX sshd")
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if not username:
        pytest.skip("the current user is not known")
    base = tmp_path_factory.mktemp("auth-order-sshd")
    host_key, client_key = base / "host_key", base / "client_key"
    for key in (host_key, client_key):
        subprocess.run(
            (tools["ssh-keygen"], "-q", "-t", "ed25519", "-N", "", "-f", str(key)),
            check=True,
        )
    authorized = base / "authorized_keys"
    authorized.write_bytes(client_key.with_suffix(".pub").read_bytes())
    authorized.chmod(0o600)
    port = _free_port()
    sshd_config = base / "sshd_config"
    sshd_config.write_text("\n".join((
        f"Port {port}",
        "ListenAddress 127.0.0.1",
        f"HostKey {host_key}",
        f"PidFile {base / 'sshd.pid'}",
        f"AuthorizedKeysFile {authorized}",
        f"AllowUsers {username}",
        "PubkeyAuthentication yes",
        "PasswordAuthentication yes",
        "KbdInteractiveAuthentication no",
        "UsePAM no",
        "StrictModes no",
        "MaxAuthTries 6",
        "LogLevel ERROR",
    )) + "\n")
    daemon = subprocess.Popen(
        (tools["sshd"], "-D", "-e", "-f", str(sshd_config)),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE, text=True,
    )
    deadline = time.time() + 10
    while time.time() < deadline:
        if daemon.poll() is not None:
            pytest.skip(f"sshd cannot run here: {daemon.stderr.read().strip()[:200]}")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        daemon.kill()
        pytest.skip("sshd did not start listening")
    # Stands in for the app's askpass: answers every prompt with a password
    # the server refuses, and notes that it was asked.
    asked = base / "asked"
    askpass = base / "askpass.sh"
    askpass.write_text(f"#!/bin/sh\necho x >> '{asked}'\necho wrong-password\n")
    askpass.chmod(0o700)
    try:
        yield {
            "port": port, "user": username, "key": client_key,
            "known_hosts": base / "known_hosts", "askpass": askpass, "asked": asked,
        }
    finally:
        daemon.terminate()
        try:
            daemon.wait(timeout=5)
        except subprocess.TimeoutExpired:
            daemon.kill()


def _host_block(server, *auth_lines):
    return "\n".join((
        "Host demo",
        "    HostName 127.0.0.1",
        f"    Port {server['port']}",
        f"    User {server['user']}",
        f"    IdentityFile {server['key']}",
        "    IdentitiesOnly yes",
        "    IdentityAgent none",
        f"    UserKnownHostsFile {server['known_hosts']}",
        "    StrictHostKeyChecking no",
        "    NumberOfPasswordPrompts 1",
        "    ConnectTimeout 10",
        *(f"    {line}" for line in auth_lines),
    )) + "\n"


def _repository(directory, config):
    return ConnectionRepository(
        ssh_store=SshConfigStore(config),
        state_path=directory / "connections.json",
        legacy_config_path=directory / "config.json",
        isolated=False,
    )


def _edit_and_save(directory, config, edit, *, earlier=None):
    """Open the host as the editor does, apply *edit* to the section's
    model, save through the store. Returns the model as saved."""
    repo = _repository(directory, config)
    record = repo.get_record("demo")
    lines, other = split_policy_lines(record.data.get("extra_ssh_config") or "")
    policy = AuthPolicy.from_lines(lines)
    # What ``ssh -G`` answers for this host without lines of its own.
    policy.set_inherited(_ssh_g(config))
    policy.earlier = dict(earlier or {})
    edit(policy)
    repo.update_connection(
        "demo", {**record.data, "extra_ssh_config": join_extra(other, policy.to_lines())}
    )
    return policy


def _ssh_g(config):
    out = subprocess.run(
        ["ssh", "-G", "-F", str(config), "demo"],
        capture_output=True, text=True, timeout=20,
    )
    assert out.returncode == 0, out.stderr
    return dict(line.split(" ", 1) for line in out.stdout.splitlines() if " " in line)


def _login(server, config):
    """Log in with the command the daemon builds for the saved host.

    Returns ``(methods tried in order, logged in, times a password was
    asked for)``.
    """
    records = {
        record.id: record
        for record in load_ssh_configuration(config, isolated=False).connections
    }
    provider = DaemonConnectionLaunchProvider(records.get, config_root=lambda: config)
    argv = list(provider.prepare_remote_command_launch("demo", f"echo {MARK}")[0])
    server["asked"].unlink(missing_ok=True)
    env = dict(os.environ)
    env.pop("SSH_AUTH_SOCK", None)
    env.update(
        SSH_ASKPASS=str(server["askpass"]), SSH_ASKPASS_REQUIRE="force",
        DISPLAY=env.get("DISPLAY") or ":0", LC_ALL="C",
    )
    # -v after the app's options: debug output is the record of what was tried.
    done = subprocess.run(
        [argv[0], "-v", *argv[1:]],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, env=env,
    )
    tried = re.findall(r"Next authentication method: (\S+)", done.stderr)
    asked = (
        len(server["asked"].read_text().splitlines()) if server["asked"].exists() else 0
    )
    return tried, MARK in done.stdout, asked, done


@pytest.fixture
def host(tmp_path, server):
    def make(*auth_lines, before=""):
        config = tmp_path / "config"
        config.write_text(before + _host_block(server, *auth_lines), encoding="utf-8")
        return config

    return make


def test_with_nothing_set_the_key_logs_in_and_no_password_is_asked(server, host):
    config = host()
    tried, logged_in, asked, done = _login(server, config)
    assert tried == ["publickey"], done.stderr[-2000:]
    assert logged_in and asked == 0


def test_password_moved_to_the_top_is_attempted_first(tmp_path, server, host):
    config = host()
    policy = _edit_and_save(
        tmp_path, config, lambda p: p.move_to_top("password")
    )
    assert policy.checked()[0] == "password"
    tried, logged_in, asked, done = _login(server, config)
    # The password comes first now, is refused, and the key still logs in:
    # the other methods stay available behind it.
    assert tried[:2] == ["password", "publickey"], done.stderr[-2000:]
    assert asked == 1 and logged_in
    # What the editor says OpenSSH will try is what it tried, in that order
    # (methods the server does not offer are never reached).
    will_try = policy.will_try()
    assert [m for m in will_try if m in tried] == tried


def test_a_method_unchecked_here_is_never_attempted(tmp_path, server, host):
    config = host("PreferredAuthentications password,publickey")
    policy = _edit_and_save(
        tmp_path, config, lambda p: p.set_tried("publickey", False)
    )
    assert "publickey" not in policy.will_try()
    tried, logged_in, asked, done = _login(server, config)
    assert tried == ["password"], done.stderr[-2000:]
    # The key would have worked; unchecked, it is not offered, so the login
    # fails rather than quietly using it.
    assert asked == 1 and not logged_in
    assert done.returncode != 0


def test_reset_gives_the_order_back_to_openssh(tmp_path, server, host):
    config = host("PreferredAuthentications password")
    tried, logged_in, _asked, _done = _login(server, config)
    assert tried == ["password"] and not logged_in
    _edit_and_save(tmp_path, config, lambda p: p.reset_order())
    assert "PreferredAuthentications" not in config.read_text()
    tried, logged_in, asked, done = _login(server, config)
    assert tried == ["publickey"], done.stderr[-2000:]
    assert logged_in and asked == 0


def test_an_earlier_block_wins_exactly_as_the_editor_warns(tmp_path, server, host):
    config = host(before="Host *\n    PreferredAuthentications publickey\n\n")
    policy = _edit_and_save(
        tmp_path, config,
        lambda p: p.set_tried("password", True) and p.move_to_top("password"),
        earlier={"preferredauthentications": "publickey"},
    )
    # The host's own line is saved as the user set it...
    assert "password" in policy.order
    assert any(
        line.strip().startswith("PreferredAuthentications") and "password" in line
        for line in config.read_text().split("Host demo", 1)[1].splitlines()
    )
    # ...the editor says it is not what will happen (the red line)...
    assert policy.preview_conflict()
    assert policy.will_try() == ["publickey"]
    # ...and it is not: the earlier block's order is the one used.
    tried, logged_in, asked, done = _login(server, config)
    assert tried == ["publickey"], done.stderr[-2000:]
    assert logged_in and asked == 0


def test_a_method_switched_off_here_is_not_attempted_though_listed(
    tmp_path, server, host
):
    config = host(
        "PreferredAuthentications password,publickey", "PasswordAuthentication no",
    )
    lines, _other = split_policy_lines(
        _repository(tmp_path, config).get_record("demo").data.get("extra_ssh_config")
        or ""
    )
    policy = AuthPolicy.from_lines(lines)
    policy.set_inherited(_ssh_g(config))
    # Listed but switched off: shown unchecked, and left out of the line below.
    assert policy.shown_checked("password") is False
    assert policy.will_try() == ["publickey"]
    tried, logged_in, asked, done = _login(server, config)
    assert tried == ["publickey"], done.stderr[-2000:]
    assert logged_in and asked == 0
