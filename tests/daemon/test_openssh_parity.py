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


# --- the other launches: scp, ssh-copy-id, dedicated forwards -----------------

#: What each of these operations sets for itself, on top of APP_OWNED.
OPERATION_OWNED = {
    # One prompt, no forwards, no login-only directives on a file copy.
    "scp": {"numberofpasswordprompts", "clearallforwardings", "forwardagent",
            "forwardx11", "permitlocalcommand", "remotecommand", "requesttty",
            "controlmaster", "localforward", "remoteforward", "dynamicforward",
            "sessiontype"},
    # A key install is no login and must stay interactive.
    "copy-id": {"numberofpasswordprompts", "clearallforwardings",
                "permitlocalcommand", "remotecommand", "localforward",
                "remoteforward", "dynamicforward"},
    # A tunnel of its own: the host's forwards are stripped, its own -L added.
    "forward": {"controlmaster", "clearallforwardings", "sessiontype",
                "requesttty", "exitonforwardfailure", "permitlocalcommand",
                "localforward", "remoteforward", "dynamicforward"},
}


def _destination_args(argv, kind):
    """OpenSSH arguments of an scp / ssh-copy-id / forward argv, for ``ssh -G``."""
    tokens = list(argv[1:])
    args, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        if token in ("-o", "-F"):
            args += [token, tokens[index + 1]]
            index += 2
        elif token == "-P" and kind == "scp":
            args += ["-p", tokens[index + 1]]
            index += 2
        elif token in ("-L", "-R", "-D", "-p", "-l"):
            args += [token, tokens[index + 1]]
            index += 2
        elif token == "-i" and kind == "copy-id":
            index += 2  # the key to install, not an identity to log in with
        elif token.startswith("-"):
            index += 1
        else:
            break
    target = tokens[-1].split(":", 1)[0] if kind == "scp" else tokens[-1]
    return args + [target]


@pytest.mark.parametrize("layout", ["star_first", "star_last"])
def test_scp_copy_id_and_forwards_resolve_as_plain_ssh(tmp_path, layout):
    root = tmp_path / "config"
    root.write_text(
        STAR + "\n" + HOSTS if layout == "star_first" else HOSTS + "\n" + STAR
    )
    key = tmp_path / "k"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                   check=True)
    records, provider = _provider(root)
    for alias in records:
        want = _resolved(["-F", str(root), alias])
        launches = {
            "scp": provider.prepare_scp_launch(
                alias, extra_args=[str(root)],
                target_override=f"{provider.scp_target(alias)}:/tmp/x",
            )[0],
            "copy-id": provider.prepare_copy_id_launch(alias, f"{key}.pub")[0],
            "forward": provider.prepare_forward_launch(
                alias, bind_port=18999, destination_host="localhost",
                destination_port=80,
            )[0],
        }
        for kind, argv in launches.items():
            got = _resolved(_destination_args(argv, kind))
            different = {
                k: (want.get(k), got.get(k))
                for k in set(want) | set(got)
                if want.get(k) != got.get(k) and k not in OPERATION_OWNED[kind]
            }
            assert not different, (alias, kind, list(argv), different)


# --- LocalCommand --------------------------------------------------------------


def test_local_command_stays_in_the_file_and_never_follows_the_destination(tmp_path):
    """Options appended after the build used to land behind the destination,
    inside the remote command of every Host Info and dashboard probe."""
    root = tmp_path / "config"
    root.write_text(
        "Host lc\n    HostName 192.0.2.9\n"
        "    PermitLocalCommand yes\n    LocalCommand echo connected\n"
    )
    _records, provider = _provider(root)

    terminal, _env = provider.prepare_terminal_launch("lc", interaction_policy="broker")
    probe, _env = provider.prepare_remote_command_launch("lc", "uptime")
    sftp, _env = provider.prepare_sftp_launch("lc")

    assert terminal[-1] == "lc" and terminal[1:3] == ("-F", str(root))
    assert list(probe[-2:]) == ["lc", "uptime"]
    for argv in (terminal, probe, sftp):
        assert not any(token.startswith("LocalCommand=") for token in argv)
    # A probe is no login: the "connected" command must not fire for it.
    for argv in (probe, sftp):
        assert "PermitLocalCommand=no" in argv
        assert argv.index("PermitLocalCommand=no") < argv.index("lc")
    # The terminal is the login: OpenSSH runs it from the file.
    assert _resolved(_ssh_g_args(terminal))["localcommand"] == ["echo connected"]


# --- keepalive: an explicit 0 is a choice -------------------------------------


def test_an_explicit_serveraliveinterval_0_turns_the_app_keepalive_off(tmp_path):
    """`ssh -G` prints 0 for both, so the configuration lines decide."""
    root = tmp_path / "config"
    root.write_text(
        "Host quiet\n    HostName 192.0.2.1\n    ServerAliveInterval 0\n\n"
        "Host plain\n    HostName 192.0.2.2\n\n"
        "Host counted\n    HostName 192.0.2.3\n    ServerAliveCountMax 3\n"
    )
    _records, provider = _provider(root)

    def launch(alias):
        return provider.prepare_terminal_launch(alias, interaction_policy="broker")[0]

    assert not any("ServerAlive" in token for token in launch("quiet"))
    assert "ServerAliveInterval=15" in launch("plain")
    counted = launch("counted")
    assert "ServerAliveInterval=15" in counted
    assert not any("ServerAliveCountMax" in token for token in counted)


# --- pre-connection: knock, wake and %h/%p/%u aim where OpenSSH connects -------


def _service(root, records):
    from sshpilot.core.connection_application_service import (
        ConnectionApplicationService,
    )
    from tests.core.test_connection_application_service import FakeRepository

    repo = FakeRepository(list(records.values()))
    repo.root_config_path = root
    repo.ssh_config_isolated = False
    for record in records.values():
        repo._metadata[record.id] = {
            "pre_command": "knock %h %p %u", "pre_command_mode": "command",
        }
    repo.get_connection_metadata = lambda cid: repo._metadata.get(cid, {})
    return ConnectionApplicationService(repo)


def test_pre_connection_targets_the_host_openssh_connects_to(tmp_path):
    root = tmp_path / "config"
    root.write_text(
        "Host *.lab\n    ProxyJump bastion\n\n"
        "Host *\n    HostName gateway.example.com\n    Port 2222\n    User deploy\n\n"
        "Host server\n    HostName server.internal\n\n"
        "Host db.lab\n    HostName 192.0.2.7\n"
    )
    records, _provider_ = _provider(root)
    service = _service(root, records)

    server = service.get_pre_connection_settings("server")
    plain = _resolved(["-F", str(root), "server"])
    assert (server.hostname, server.target_port) == (
        plain["hostname"][0], int(plain["port"][0]),
    ) == ("gateway.example.com", 2222)
    assert "gateway.example.com" in server.command and "2222" in server.command
    assert "deploy" in server.command
    assert server.direct is True

    # A ProxyJump from a pattern block: not reachable from here, no wake.
    assert service.get_pre_connection_settings("db.lab").direct is False


# --- dedicated forwards keep the system-wide file -----------------------------


def test_forward_config_copy_reads_the_system_file_last(tmp_path, monkeypatch):
    from sshpilot.core import ssh_config_forward_strip as strip

    home_ssh = tmp_path / ".ssh"
    (home_ssh / "conf.d").mkdir(parents=True)
    user = home_ssh / "config"
    user.write_text(
        "Include conf.d/a.conf\n"
        "Host web\n    HostName 192.0.2.30\n    Compression no\n"
        "    LocalForward 7000 localhost:80\n"
    )
    # Relative to ~/.ssh, as OpenSSH reads it -- not to conf.d.
    (home_ssh / "conf.d" / "a.conf").write_text("Include conf.d/b.conf\n")
    (home_ssh / "conf.d" / "b.conf").write_text("Host web\n    User fromb\n")
    system = tmp_path / "etc" / "ssh_config"
    system.parent.mkdir()
    system.write_text("Host *\n    Compression yes\n    SendEnv SYSTEMWIDE\n")
    monkeypatch.setattr(paths, "openssh_user_dir", lambda: home_ssh)
    monkeypatch.setattr(ssh_config_effective, "openssh_system_config", lambda: str(system))

    copy = strip.materialize_ssh_config_without_port_forwards(str(user))
    got = _resolved(["-F", copy, "web"])

    assert got["sendenv"] == ["SYSTEMWIDE"]      # the system file is read
    assert got["compression"] == ["no"]           # after the user's, which wins
    assert got["user"] == ["fromb"]               # nested relative Include found
    assert "localforward" not in got              # the host's forwards stripped

    # Any other root is passed with -F by the session too: no system file.
    other = tmp_path / "custom" / "config"
    other.parent.mkdir()
    other.write_text("Host web\n    HostName 192.0.2.30\n")
    got = _resolved(["-F", strip.materialize_ssh_config_without_port_forwards(str(other)), "web"])
    assert "sendenv" not in got


# --- review of a537e0fb: conditional blocks, user commands, idle resolves ------


def test_a_conditional_block_openssh_skips_does_not_count_as_configured(tmp_path):
    """`Match exec "false"` never applies, so its `ServerAliveInterval 0` is
    not the user's choice for this host and the app default still fills in."""
    root = tmp_path / "config"
    root.write_text(
        'Match exec "false"\n    ServerAliveInterval 0\n\n'
        "Match host other\n    ServerAliveInterval 0\n\n"
        "Host server\n    HostName 192.0.2.4\n\n"
        "Host quiet\n    HostName 192.0.2.5\n\n"
        "Host aliased\n    HostName 192.0.2.7\n\n"
        # `Match host` compares the HostName, not the alias.
        "Match host 192.0.2.5\n    ServerAliveInterval 0\n\n"
        "Match host aliased\n    ServerAliveInterval 0\n"
    )
    _records, provider = _provider(root)

    def launch(alias):
        return provider.prepare_terminal_launch(alias, interaction_policy="broker")[0]

    assert "ServerAliveInterval=15" in launch("server")
    assert "ServerAliveInterval=15" in launch("aliased")
    # A Match block that does apply is still the user's choice.
    assert not any("ServerAlive" in token for token in launch("quiet"))


def test_a_terminal_running_a_users_command_keeps_local_command(tmp_path):
    """`ssh lc uptime` runs LocalCommand; so does a terminal tab opened with a
    command (CLI `sshpilot ssh lc uptime`, a plugin's command tab)."""
    root = tmp_path / "config"
    root.write_text(
        "Host lc\n    HostName 192.0.2.9\n"
        "    PermitLocalCommand yes\n    LocalCommand echo connected\n"
    )
    _records, provider = _provider(root)

    argv, _env = provider.prepare_terminal_launch(
        "lc", interaction_policy="broker", remote_command="uptime"
    )
    assert "PermitLocalCommand=no" not in argv
    assert list(argv[-2:]) == ["lc", "uptime"]
    resolved = _resolved(_ssh_g_args(argv))
    assert resolved["permitlocalcommand"] == ["yes"]
    assert resolved == _resolved(["-F", str(root), "lc"]) | {
        key: resolved[key] for key in ("serveraliveinterval", "serveralivecountmax")
        if key in resolved
    }


def test_a_launch_with_nothing_to_run_first_does_not_resolve_the_host(
    tmp_path, monkeypatch
):
    """Every launch reads these settings; `ssh -G` also runs `Match exec`
    commands, so resolving when there is no knock, command or wake costs a
    subprocess and can fire the user's own scripts."""
    from sshpilot.core import connection_application_service as cas

    root = tmp_path / "config"
    root.write_text("Host plain\n    HostName 192.0.2.6\n")
    records, _provider_ = _provider(root)
    service = _service(root, records)
    service._repository._metadata.clear()
    calls = []
    real = ssh_config_effective.get_effective_ssh_config
    monkeypatch.setattr(
        ssh_config_effective, "get_effective_ssh_config",
        lambda *a, **k: calls.append(a) or real(*a, **k),
    )

    settings = service.get_pre_connection_settings("plain")
    assert not settings.configured and not settings.runs_wake
    assert calls == []

    service._repository._metadata["plain"] = {"wol_mac": "00:11:22:33:44:55"}
    assert service.get_pre_connection_settings("plain").hostname == "192.0.2.6"
    assert calls
