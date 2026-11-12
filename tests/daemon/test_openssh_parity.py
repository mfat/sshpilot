"""A saved host is launched exactly as ``ssh <alias>`` resolves it.

The SSH configuration is the source of truth. With nothing set in Settings ▸
SSH Options, the command the daemon launches for a saved host must resolve,
through OpenSSH itself, to the same configuration as ``ssh <alias>`` typed in a
terminal: the same ``Host *`` and ``Match`` blocks, the same precedence between
them and the host's own block, the same included files.

Each case writes a real configuration, asks the production launch provider
for its argv, and compares ``ssh -G`` of that argv with ``ssh -G`` of the
plain command. The app's only additions are named in ``APP_OWNED``.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.core import ssh_config_effective  # noqa: E402
from sshpilot.core.connections.ssh_config_loader import (  # noqa: E402
    load_ssh_configuration,
)
from sshpilot.core.ssh_config_effective import (  # noqa: E402
    earlier_directives,
    launch_config_file,
    openssh_reachable_files,
)
from sshpilot.daemon.connection_launch_provider import (  # noqa: E402
    DaemonConnectionLaunchProvider,
)
from sshpilot.platform import paths  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")

#: Options the app sets for its own operation, per launch kind. Everything
#: else has to come out of the configuration unchanged.
APP_OWNED = {
    "terminal": set(),
    # One askpass cancel ends a file operation; the sftp subsystem cannot run
    # next to a configured RemoteCommand.
    "sftp": {"numberofpasswordprompts", "remotecommand"},
    "command": {"sessiontype", "remotecommand", "requesttty"},
}

STAR = """Host *
    User globaluser
    Port 2222
    ServerAliveInterval 60
    ServerAliveCountMax 10
    Compression yes
    ForwardAgent yes
    ConnectTimeout 7
    StrictHostKeyChecking accept-new
    ControlMaster no
    LogLevel ERROR
    SetEnv FOO=bar
    SendEnv MYVAR
    LocalForward 18080 localhost:80
"""

HOSTS = """Host plain
    HostName 192.0.2.10

Host own
    HostName 192.0.2.11
    User ownuser
    Port 2200
    ServerAliveInterval 5
    Compression no
    ForwardAgent no
    ConnectTimeout 30
    StrictHostKeyChecking yes

Host web.lab
    HostName 192.0.2.12

Host bare.example.com

Host *.lab
    User labuser
    ProxyJump plain

Match originalhost own
    AddressFamily inet
"""


def _resolved(args):
    out = subprocess.run(
        ["ssh", "-G", *args], capture_output=True, text=True, check=True
    ).stdout
    found = {}
    for line in out.splitlines():
        key, _sep, value = line.partition(" ")
        found.setdefault(key, []).append(value)
    return found


def _ssh_g_args(argv):
    """The launch argv as ``ssh -G`` arguments: options and the destination."""
    args = []
    tokens = list(argv[1:])
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-o", "-F", "-p", "-l", "-J", "-E", "-i"):
            if token != "-E":
                args += [token, tokens[index + 1]]
            index += 2
        elif token.startswith("-"):
            if token != "-s":  # needs its subsystem argument; not an option
                args.append(token)
            index += 1
        else:
            args.append(token)  # the destination; a command may follow
            break
    return args


def _provider(root):
    records = {
        record.id: record
        for record in load_ssh_configuration(root, isolated=False).connections
    }
    return records, DaemonConnectionLaunchProvider(
        records.get, config_root=lambda: root
    )


def _launches(provider, alias):
    yield "terminal", provider.prepare_terminal_launch(
        alias, interaction_policy="broker"
    )[0]
    yield "sftp", provider.prepare_sftp_launch(alias)[0]
    yield "command", provider.prepare_remote_command_launch(alias, "uptime")[0]


def _assert_parity(root, plain_args=None):
    records, provider = _provider(root)
    assert records, "the fixture declares hosts"
    for alias in records:
        want = _resolved((plain_args or ["-F", str(root)]) + [alias])
        for kind, argv in _launches(provider, alias):
            got = _resolved(_ssh_g_args(argv))
            different = {
                key: (want.get(key), got.get(key))
                for key in set(want) | set(got)
                if want.get(key) != got.get(key) and key not in APP_OWNED[kind]
            }
            assert not different, (alias, kind, list(argv), different)


@pytest.mark.parametrize("layout", ["star_first", "star_last"])
def test_launch_resolves_as_plain_ssh(tmp_path, layout):
    root = tmp_path / "config"
    root.write_text(
        STAR + "\n" + HOSTS if layout == "star_first" else HOSTS + "\n" + STAR
    )
    _assert_parity(root)


def test_host_in_an_included_file_gets_the_roots_blocks(tmp_path):
    """The whole root is resolved, not the file that declares the host."""
    fragments = tmp_path / "conf.d"
    fragments.mkdir()
    (fragments / "a.conf").write_text("Host inc\n    HostName 192.0.2.20\n")
    root = tmp_path / "config"
    root.write_text(f"Include {fragments}/*.conf\n\n" + HOSTS + "\n" + STAR)

    _assert_parity(root)

    records, provider = _provider(root)
    argv, _env = provider.prepare_terminal_launch("inc", interaction_policy="broker")
    assert list(argv[1:]) == ["-F", str(root), "inc"]
    assert provider.remote_identity("inc") == ("192.0.2.20", "globaluser", 2222)


def test_host_star_first_beats_the_hosts_own_block_as_in_openssh(tmp_path):
    root = tmp_path / "config"
    root.write_text(STAR + "\n" + HOSTS)
    _records, provider = _provider(root)

    argv, _env = provider.prepare_terminal_launch("own", interaction_policy="broker")

    assert list(argv[1:]) == ["-F", str(root), "own"]
    got = _resolved(_ssh_g_args(argv))
    assert got["user"] == ["globaluser"] and got["port"] == ["2222"]
    assert provider.remote_identity("own") == ("192.0.2.11", "globaluser", 2222)
    assert provider.scp_target("own") == "own"


def test_keepalive_from_the_configuration_is_kept_and_the_default_fills_in(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host quiet\n    HostName 192.0.2.30\n\n"
        "Host chatty\n    HostName 192.0.2.31\n    ServerAliveInterval 60\n"
    )
    _records, provider = _provider(root)

    chatty, _env = provider.prepare_terminal_launch("chatty", interaction_policy="broker")
    quiet, _env = provider.prepare_terminal_launch("quiet", interaction_policy="broker")

    assert not any("ServerAlive" in token for token in chatty)
    assert "ServerAliveInterval=15" in quiet and "ServerAliveCountMax=3" in quiet


def test_users_own_config_is_launched_without_dash_f(tmp_path, monkeypatch):
    """`-F` makes OpenSSH skip /etc/ssh/ssh_config; `ssh host` reads it."""
    home_ssh = tmp_path / ".ssh"
    home_ssh.mkdir()
    root = home_ssh / "config"
    root.write_text("Host plain\n    HostName 192.0.2.10\n")
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: home_ssh)

    assert launch_config_file(root) is None
    assert launch_config_file(tmp_path / "elsewhere" / "config") is None  # missing
    records, provider = _provider(root)
    view = provider._view(records["plain"])
    assert view._resolve_config_override_path() is None


# --- Include discovery -------------------------------------------------------


def test_include_keyword_accepts_tab_and_equals(tmp_path):
    for name in ("tab", "eq"):
        (tmp_path / f"{name}.conf").write_text(f"Host {name}-host\n    HostName 192.0.2.40\n")
    root = tmp_path / "config"
    root.write_text(
        f"Include\t{tmp_path}/tab.conf\nInclude={tmp_path}/eq.conf\n"
    )

    loaded = load_ssh_configuration(root, isolated=False)

    assert {record.id for record in loaded.connections} == {"tab-host", "eq-host"}
    assert _resolved(["-F", str(root), "tab-host"])["hostname"] == ["192.0.2.40"]
    assert _resolved(["-F", str(root), "eq-host"])["hostname"] == ["192.0.2.40"]


def _nested_layout(base):
    (base / "conf.d").mkdir(parents=True)
    (base / "config").write_text("Include conf.d/a.conf\n")
    (base / "conf.d" / "a.conf").write_text(
        "Host a-host\n    HostName 192.0.2.50\nInclude nested.conf\n"
    )
    (base / "conf.d" / "nested.conf").write_text("Host beside-fragment\n")
    (base / "nested.conf").write_text("Host in-ssh-dir\n")
    return base / "config"


def test_relative_include_in_the_users_config_resolves_in_dot_ssh(tmp_path, monkeypatch):
    """OpenSSH resolves a relative Include in ~/.ssh, whichever file holds it."""
    home_ssh = tmp_path / ".ssh"
    root = _nested_layout(home_ssh)
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: home_ssh)

    loaded = load_ssh_configuration(root, isolated=False)

    assert {record.id for record in loaded.connections} == {"a-host", "in-ssh-dir"}


def test_relative_include_under_another_root_resolves_beside_the_file(tmp_path):
    """A root outside ~/.ssh is the app's own notion of an SSH folder."""
    root = _nested_layout(tmp_path / "custom")

    loaded = load_ssh_configuration(root, isolated=False)

    assert {record.id for record in loaded.connections} == {"a-host", "beside-fragment"}


def test_file_openssh_cannot_reach_from_the_root_is_resolved_alone(tmp_path):
    """`ssh -F root` looks for a relative Include in ~/.ssh and misses a file
    next to a root elsewhere. Passing the root would lose the host's own
    block, so the declaring file is passed for that host instead."""
    root = _nested_layout(tmp_path / "custom")
    fragment = tmp_path / "custom" / "conf.d" / "a.conf"
    absolute = tmp_path / "abs.conf"
    absolute.write_text("Host abs-host\n")
    with open(root, "a") as handle:
        handle.write(f"Include {absolute}\n")

    assert str(absolute) in openssh_reachable_files(str(root))
    assert str(fragment) not in openssh_reachable_files(str(root))
    assert launch_config_file(root, absolute) == str(root)
    assert launch_config_file(root, fragment) == str(fragment)


# --- what an earlier block decides -------------------------------------------


def test_earlier_directives_follow_openssh_reading_order(tmp_path):
    included = tmp_path / "early.conf"
    included.write_text("Host o*\n    Compression yes\n")
    root = tmp_path / "config"
    root.write_text(
        "ConnectTimeout 9\n"                      # 1: above every block
        f"Include {included}\n"                  # 2
        "Host *\n"                                # 3
        "    User deploy\n"                       # 4
        "    IdentityFile ~/.ssh/shared\n"        # 5: accumulates, never beaten
        "Host other !own\n"                       # 6: does not apply
        "    Port 1\n"
        "Match originalhost own\n"                # 8
        "    Port 2222\n"                         # 9
        "Match user nobody\n"                     # 10: undecided, not in effect
        "    ForwardAgent yes\n"
        "Host own\n"
        "    User admin\n"
        "    Port 2200\n"
        "Host *\n"
        "    LogLevel ERROR\n"                    # after the own block
    )
    effective = ssh_config_effective.get_effective_ssh_config("own", str(root))

    found = {item["key"]: item for item in earlier_directives("own", str(root), effective)}

    assert set(found) == {"connecttimeout", "compression", "user", "port"}
    assert (found["connecttimeout"]["block"], found["connecttimeout"]["line"]) == ("", 1)
    assert found["compression"]["file"] == str(included)
    assert found["compression"]["block"] == "Host o*"
    assert (found["user"]["value"], found["user"]["block"], found["user"]["line"]) == (
        "deploy", "Host *", 4,
    )
    assert (found["port"]["value"], found["port"]["block"]) == (
        "2222", "Match originalhost own",
    )
    # OpenSSH agrees on every one of them.
    for key, item in found.items():
        assert str(effective[key]).lower() in (item["value"].lower(), "9", "yes")


def test_nothing_is_earlier_for_a_host_without_a_block_or_with_the_first_one(tmp_path):
    root = tmp_path / "config"
    root.write_text("Host own\n    User admin\nHost *\n    User deploy\n")

    assert earlier_directives("own", str(root)) == []
    assert earlier_directives("unknown", str(root)) == []
