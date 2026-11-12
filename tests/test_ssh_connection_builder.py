"""Tests for unified (native-only) SSH command construction.

The connection builder is NATIVE-ONLY: per-host settings live in ~/.ssh/config
and OpenSSH resolves them. The builder owns runtime concerns: app-level
ssh_overrides, batch mode, the host token, an optional raw remote command, and
the auth env/options (askpass + optional agent bypass, or sshpass for a stored
password).

One exception, and it is deliberate: directives the Host block *authored* are
re-emitted as command-line options. OpenSSH resolves with first-obtained-value
-wins, so a `Host *` block earlier in the file would otherwise beat a specific
block's explicit values and the editor would show one thing while the session
used another. Only authored directives are emitted — never defaults, which
would override a global the user relies on — and never accumulating directives
(IdentityFile, CertificateFile, the forwards), which the command line cannot
override anyway. A record with no authorship evidence emits nothing.
"""

import asyncio

import shutil

import pytest

from sshpilot.connection_model import Connection
from sshpilot.core.connections.models import ConnectionRecord
from sshpilot.daemon.connection_launch_provider import HeadlessConnectionView
from sshpilot.ssh_connection_builder import (
    ConnectionContext,
    build_ssh_connection,
    build_native_command,
)


asyncio.set_event_loop(asyncio.new_event_loop())


def runtime_connection(
    *,
    nickname="demo",
    hostname="demo.example",
    username="alice",
    auth_method=0,
    data=None,
):
    """Daemon launch-compatible connection view for a fresh record.

    The ``Connection`` presentation value drops persisted SSH-policy fields
    such as ``auth_method``, so tests that need password auth use the view the
    daemon launch provider builds instead.
    """
    record_data = dict(data or {})
    record_data["auth_method"] = auth_method

    record = ConnectionRecord(
        id=nickname,
        nickname=nickname,
        host=nickname,
        hostname=hostname,
        username=username,
        port=22,
        protocol="ssh",
        data=record_data,
    )
    return HeadlessConnectionView(record)


class CredentialLookup:
    """Narrow secret-backend fake: passwords, key passphrases, agent preload.

    Contains no repository, group, Config, GTK, or persistence behavior.
    """

    secret_lookup_authoritative = True

    def __init__(
        self,
        *,
        password=None,
        passphrases=None,
        preload_result=True,
    ):
        self.password = password
        self.passphrases = dict(passphrases or {})
        self.preload_result = preload_result
        self.prepared_keys = []
        self.password_lookups = []

    def get_connection_password(self, connection):
        self.password_lookups.append(connection.id)
        return self.password

    def get_key_passphrase(self, key_path):
        return self.passphrases.get(key_path)

    def prepare_key_for_connection(self, key_path):
        self.prepared_keys.append(key_path)
        return self.preload_result


def _host_index(cmd):
    """Index of the SSH target host token (first non-option after ssh binary)."""
    idx = 1
    takes_value = {'-o', '-i', '-F', '-p', '-P', '-D', '-L', '-R'}
    while idx < len(cmd):
        token = cmd[idx]
        if token in takes_value and idx + 1 < len(cmd):
            idx += 2
            continue
        if token.startswith('-'):
            idx += 1
            continue
        return idx
    return -1


def test_ssh_options_precede_host_and_raw_remote_command():
    conn = Connection(
        {
            'host': 'example.com',
            'hostname': 'example.com',
            'username': 'alice',
            'remote_command': 'uptime',
        }
    )
    ctx = ConnectionContext(
        connection=conn,
        connection_manager=None,
        config=None,
        command_type='ssh',
        remote_command='uptime',
    )
    result = build_ssh_connection(ctx)
    cmd = result.command
    host_idx = _host_index(cmd)
    assert host_idx >= 0
    # Host token comes from resolve_host_identifier() (the SSH config alias).
    assert cmd[host_idx] == 'example.com'
    # Remote command is appended RAW after the host: no "; exec $SHELL -l".
    assert cmd[host_idx + 1] == 'uptime'
    assert host_idx + 1 == len(cmd) - 1
    # Native mode never injects -t/-tt; that lives in ~/.ssh/config if wanted.
    assert '-t' not in cmd and '-tt' not in cmd


def test_askpass_when_password_present_for_key_and_password_auth():
    """Stored login password is delivered via askpass (never sshpass)."""
    # Key-auth scenario: saved password with no saved key passphrase. The
    # minimal presentation Connection needs no SSH-policy fields here.
    key_conn = Connection(
        {
            'host': 'example.com',
            'hostname': 'example.com',
            'username': 'alice',
            'auth_method': 0,
        }
    )
    key_conn.resolved_identity_files = []  # no key -> no saved passphrase
    key_result = build_ssh_connection(
        ConnectionContext(
            connection=key_conn,
            connection_manager=CredentialLookup(password='secret'),
            command_type='ssh',
        )
    )
    assert key_result.use_sshpass is False
    assert key_result.password == 'secret'
    assert key_result.use_askpass is True
    assert key_result.env.get('SSH_ASKPASS')
    assert key_result.env.get('SSH_ASKPASS_REQUIRE') == 'prefer'
    assert key_result.env.get('SSHPILOT_PASSWORD_USER') == 'alice'

    # Password-auth scenario (auth_method=1): same stored password via the
    # secret backend, delivered through askpass. Kept outside connection data.
    pw_conn = runtime_connection(auth_method=1)
    pw_result = build_ssh_connection(
        ConnectionContext(
            connection=pw_conn,
            connection_manager=CredentialLookup(password='secret'),
            command_type='ssh',
        )
    )
    assert pw_result.use_sshpass is False
    assert pw_result.use_askpass is True
    assert pw_result.password == 'secret'
    assert 'password' not in pw_conn.data


def test_key_auth_without_anything_saved_uses_native_prompts():
    # Key auth, no saved passphrase and no saved password -> neither askpass nor
    # sshpass; SSH prompts on the TTY (and can fall back to password naturally).
    conn = Connection(
        {
            'host': 'example.com',
            'hostname': 'example.com',
            'username': 'alice',
            'auth_method': 0,
        }
    )
    conn.resolved_identity_files = []
    result = build_ssh_connection(ConnectionContext(connection=conn))
    assert result.use_sshpass is False
    assert result.password is None
    assert result.use_askpass is False
    assert 'SSH_ASKPASS' not in result.env


def _forwarding_conn():
    return Connection({'host': 'srv', 'hostname': 'srv', 'username': 'u'})


def _build_forwarding(rules=None, extra_args=None):
    ctx = ConnectionContext(
        connection=_forwarding_conn(),
        connection_manager=None,
        config=None,
        command_type='ssh',
        port_forwarding_rules=rules or [],
        extra_args=extra_args or [],
    )
    return build_ssh_connection(ctx).command


def test_port_forwarding_rules_are_not_emitted_to_command():
    """port_forwarding_rules is vestigial in native mode; forwarding lives in
    ~/.ssh/config, so no -D/-L/-R is added from rules."""
    cmd = _build_forwarding([
        {'type': 'dynamic', 'listen_addr': 'localhost', 'listen_port': 1080, 'enabled': True},
        {'type': 'local', 'listen_addr': 'localhost', 'listen_port': 8080,
         'remote_host': 'db', 'remote_port': 5432, 'enabled': True},
        {'type': 'remote', 'listen_addr': 'localhost', 'listen_port': 9090,
         'local_host': 'localhost', 'local_port': 9090, 'enabled': True},
    ])
    assert '-D' not in cmd
    assert '-L' not in cmd
    assert '-R' not in cmd


def test_forwarding_via_extra_args_before_host():
    """Flags passed via extra_args (as start_local/remote_forwarding does) land before host."""
    for flag, spec in [('-L', 'localhost:8080:db:5432'), ('-R', 'localhost:9090:localhost:9090')]:
        cmd = _build_forwarding(extra_args=['-N', flag, spec, '-f'])
        host_idx = _host_index(cmd)
        flag_positions = [i for i, t in enumerate(cmd) if t == flag]
        assert flag_positions, f"no {flag} in command: {cmd}"
        assert all(p < host_idx for p in flag_positions), f"{flag} after host in: {cmd}"
        n_positions = [i for i, t in enumerate(cmd) if t == '-N']
        assert n_positions and all(p < host_idx for p in n_positions), f"-N after host in: {cmd}"


def test_build_ssh_connection_reads_password_via_manager():
    credentials = CredentialLookup(password='from-vault')
    connection = runtime_connection(auth_method=1)

    built = build_ssh_connection(
        ConnectionContext(
            connection=connection,
            connection_manager=credentials,
            command_type='ssh',
        )
    )
    # The saved password is resolved through the manager's
    # get_connection_password() lookup, never embedded in the record.
    assert credentials.password_lookups == [connection.id]
    assert built.use_sshpass is False
    assert built.use_askpass is True
    assert built.password == 'from-vault'
    assert 'password' not in connection.data


# --- new surface: native command shape + auth resolution ---


def test_native_command_minimal_shape():
    """build_native_command yields a plain `ssh <host>` with no auth options."""
    conn = Connection({'host': 'plain.example', 'hostname': 'plain.example', 'username': 'u'})
    cmd = build_native_command(conn)
    assert cmd == ['ssh', 'plain.example']
    # Plain command applies NO auth: no agent bypass.
    assert 'IdentityAgent=none' not in cmd


def test_key_auth_does_not_add_identity_agent_bypass():
    conn = Connection({'host': 'k.example', 'hostname': 'k.example', 'auth_method': 0})
    cmd = build_ssh_connection(ConnectionContext(connection=conn)).command
    assert 'IdentityAgent=none' not in cmd


# --- the Host block is never repeated on the command line -------------------
#
# The configuration file is the source of truth and OpenSSH resolves it with
# its own first-value-wins precedence. Anything the builder repeated as an
# option would beat every file, including a ``Host *`` block placed before
# the host's own block, and the session would differ from `ssh <alias>`.


def _config_connection(**data):
    """A Connection carrying parser authorship evidence, as the daemon supplies."""
    authored = data.pop("authored", ())
    effective = data.pop("effective", None)
    payload = {"host": "web", "nickname": "web", **data}
    payload["__authored_directives"] = tuple(authored)
    conn = Connection(payload)
    if effective is not None:
        # What the daemon's launch view answers from `ssh -G`.
        conn.effective_ssh_config = lambda: dict(effective)
    return conn


def _argv(conn):
    return build_ssh_connection(
        ConnectionContext(connection=conn, command_type="ssh")
    ).command


def test_a_host_blocks_own_settings_are_not_repeated_as_options():
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            username="alice",
            port=2200,
            proxy_jump=["bastion"],
            forward_agent=False,
            x11_forwarding=True,
            identity_files=["~/.ssh/id_web"],
            local_command="echo connected",
            remote_command="tmux attach",
            request_tty="force",
            extra_ssh_config="compression no\nserveraliveinterval 30",
            authored=(
                "hostname", "user", "port", "proxyjump", "forwardagent",
                "forwardx11", "identityfile", "localcommand", "remotecommand",
                "requesttty", "compression", "serveraliveinterval",
            ),
        )
    )
    assert argv == ["ssh", "web"]


def test_one_shot_remote_command_stays_positional():
    conn = _config_connection(
        hostname="web.example.com",
        remote_command="tmux attach",
        authored=("hostname", "remotecommand"),
    )
    argv = build_ssh_connection(
        ConnectionContext(
            connection=conn, command_type="ssh", remote_command="uptime"
        )
    ).command
    assert argv[-2:] == ["web", "uptime"]
    # ssh refuses a command next to a configured RemoteCommand.
    assert "RemoteCommand=none" in argv
    assert "RemoteCommand=tmux attach" not in argv


# --- keepalive is a fallback ------------------------------------------------


def test_default_keepalive_applies_when_the_configuration_sets_none():
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            authored=("hostname",),
            effective={"serveraliveinterval": "0", "serveralivecountmax": "3"},
        )
    )
    assert "ServerAliveInterval=15" in argv
    assert "ServerAliveCountMax=3" in argv


def test_a_configured_keepalive_is_left_alone():
    # Wherever it comes from -- the Host block, `Host *`, an included file --
    # `ssh -G` shows it, and a command-line option would replace it.
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            authored=("hostname",),
            effective={"serveraliveinterval": "60", "serveralivecountmax": "10"},
        )
    )
    assert not any("ServerAlive" in token for token in argv)


def test_a_configured_keepalive_count_survives_the_fallback_interval():
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            authored=("hostname",),
            effective={"serveraliveinterval": "0", "serveralivecountmax": "10"},
        )
    )
    assert "ServerAliveInterval=15" in argv
    assert not any("ServerAliveCountMax" in token for token in argv)


def test_no_keepalive_is_imposed_when_nothing_could_be_resolved():
    argv = _argv(_config_connection(hostname="web.example.com", authored=("hostname",)))
    assert not any("ServerAlive" in token for token in argv)


def test_batch_mode_only_appears_under_the_none_interaction_policy():
    """Guards what "Copy SSH Command" hands the user.

    A copied command carrying BatchMode=yes cannot prompt for a password or an
    unknown host key, so the copy path must not use the external-terminal
    (`none`) policy that adds it.
    """
    conn = _config_connection(
        hostname="web.example.com", username="alice", port=2200,
        authored=("hostname", "user", "port"),
    )

    def _argv_for(policy):
        return build_ssh_connection(
            ConnectionContext(
                connection=conn,
                command_type="ssh",
                native_mode=True,
                interaction_policy=policy,
            )
        ).command

    assert "BatchMode=yes" in _argv_for("none")
    for policy in ("normal", "broker"):
        argv = _argv_for(policy)
        assert "BatchMode=yes" not in argv
        # Still the plain command: the configuration supplies the rest.
        assert argv[-1] == "web"


# --- Preferences act as defaults, not overrides -----------------------------


def _prefs_config(prefs):
    import types

    from sshpilot.core.settings.ssh_overrides import compose_ssh_overrides

    return types.SimpleNamespace(
        get_ssh_config=lambda: {**prefs, "ssh_overrides": compose_ssh_overrides(prefs)}
    )


def test_settings_overrides_are_sent_and_the_connections_own_are_not():
    """Settings ▸ SSH Options are app-level overrides.

    A command-line option beats every configuration file, so each override
    wins over the connection's own value, which stays in the file.
    """
    app_config = _prefs_config({
        "compression": True,
        "strict_host_key_checking": "accept-new",
        "default_keepalive_interval": 60,
        "connection_timeout": 15,
        "extra_options": ["Ciphers=aes128-ctr", "User=forced"],
    })
    conn = _config_connection(
        hostname="web.example.com",
        username="alice",
        extra_ssh_config=(
            "stricthostkeychecking yes\ncompression no\n"
            "serveraliveinterval 30\nciphers aes256-ctr"
        ),
        authored=(
            "hostname", "user", "stricthostkeychecking", "compression",
            "serveraliveinterval", "ciphers",
        ),
    )
    argv = build_ssh_connection(
        ConnectionContext(connection=conn, config=app_config, command_type="ssh")
    ).command

    for override in (
        "StrictHostKeyChecking=accept-new", "Compression=yes",
        "Ciphers=aes128-ctr", "User=forced", "ConnectTimeout=15",
    ):
        assert override in argv, argv
    lowered = " ".join(argv).lower()
    for own in ("stricthostkeychecking=yes", "compression=no", "ciphers=aes256-ctr"):
        assert own not in lowered
    assert "-l" not in argv
    # The keepalive is no override: this connection could not be resolved,
    # so it is not sent at all.
    assert not any("ServerAlive" in token for token in argv)


def test_app_runtime_options_still_beat_settings_overrides():
    """A caller's forced option (forwarding-only tab) outranks the overrides."""
    app_config = _prefs_config({"extra_options": ["SessionType=default"]})
    argv = build_ssh_connection(
        ConnectionContext(
            connection=_config_connection(
                hostname="web.example.com", authored=("hostname",)
            ),
            config=app_config,
            command_type="ssh",
            extra_args=["-o", "SessionType=none"],
        )
    ).command
    assert argv.index("SessionType=none") < argv.index("SessionType=default")


@pytest.mark.skipif(shutil.which("ssh") is None, reason="needs OpenSSH")
@pytest.mark.parametrize(
    "override, expected",
    [
        ("ProxyJump=global-jump", "proxyjump global-jump"),
        ("ProxyCommand=nc %h %p", "proxycommand nc %h %p"),
        ("Port=2022", "port 2022"),
        ("User=forced", "user forced"),
    ],
)
def test_openssh_resolves_an_override_over_the_connections_value(override, expected):
    """Ask ssh itself: the override wins, and the command still parses.

    A global proxy with a connection that has its own jump host used to be a
    hard failure (``-J`` after ``-o ProxyJump=``/``-o ProxyCommand=``).
    """
    import subprocess

    app_config = _prefs_config({"extra_options": [override]})
    conn = _config_connection(
        hostname="web.example.com",
        username="alice",
        port=2200,
        proxy_jump=["jump.example.com"],
        authored=("hostname", "user", "port", "proxyjump"),
    )
    argv = build_ssh_connection(
        ConnectionContext(connection=conn, config=app_config, command_type="ssh")
    ).command
    result = subprocess.run(
        [argv[0], "-F", "/dev/null", "-G", *argv[1:]],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert expected in result.stdout.lower().splitlines()


def test_preferences_still_apply_when_the_connection_sets_nothing():
    import types

    from sshpilot.core.settings.ssh_overrides import compose_ssh_overrides

    prefs = {"compression": True, "connection_timeout": 15}
    app_config = types.SimpleNamespace(
        get_ssh_config=lambda: {**prefs, "ssh_overrides": compose_ssh_overrides(prefs)}
    )
    argv = build_ssh_connection(
        ConnectionContext(
            connection=_config_connection(hostname="web.example.com", authored=("hostname",)),
            config=app_config,
            command_type="ssh",
        )
    ).command

    assert "Compression=yes" in argv
    assert "ConnectTimeout=15" in argv


def test_scp_preference_overrides_stay_options_when_path_operands_follow():
    """SCP path sources must not ride ``extra_args`` ahead of Preferences.

    After Preferences were moved behind ``extra_options`` (so they lose to
    connection options for ssh), putting transfer paths in ``extra_args`` made
    argv ``scp [sources…] -o … -v dest``. OpenSSH then treated ``-o``/``-v`` as
    more source names. Production only passes scp flags as ``extra_args`` and
    inserts path operands after the prepared command (before destination).
    """
    import types

    from sshpilot.core.settings.ssh_overrides import compose_ssh_overrides

    prefs = {
        "verbosity": 1,
        "default_keepalive_interval": 25,
    }
    app_config = types.SimpleNamespace(
        get_ssh_config=lambda: {**prefs, "ssh_overrides": compose_ssh_overrides(prefs)}
    )
    prepared = build_ssh_connection(
        ConnectionContext(
            connection=_config_connection(
                hostname="host.example.com",
                authored=("hostname",),
                effective={"serveraliveinterval": "0"},
            ),
            config=app_config,
            command_type="scp",
            extra_args=["-r"],
            target_override="alice@host.example.com:/remote/drop",
            interaction_policy="broker",
        )
    ).command

    assert prepared[0] == "scp"
    assert prepared[-1] == "alice@host.example.com:/remote/drop"
    assert "-r" in prepared
    assert "-v" in prepared
    assert "ServerAliveInterval=25" in prepared
    # Flags and Preference overrides only — no path operands yet.
    assert "/tmp/payload" not in prepared

    argv = prepared[:-1] + ["/tmp/payload"] + [prepared[-1]]
    payload_i = argv.index("/tmp/payload")
    assert argv.index("-r") < payload_i
    assert argv.index("-v") < payload_i
    assert argv.index("ServerAliveInterval=25") < payload_i
    assert argv[-1] == "alice@host.example.com:/remote/drop"


def test_scp_none_policy_sends_no_port_or_user():
    """The alias in the target carries both: scp resolves them from the config."""

    argv = build_ssh_connection(
        ConnectionContext(
            connection=_config_connection(
                hostname="host.example.com",
                username="alice",
                port=2200,
                authored=("hostname", "user", "port"),
            ),
            command_type="scp",
            extra_args=["-r"],
            target_override="web:/remote/drop",
            interaction_policy="none",
        )
    ).command

    assert argv[0] == "scp"
    assert "BatchMode=yes" in argv
    for flag in ("-P", "-l", "-p"):
        assert flag not in argv
    assert not any(token.startswith("User=") for token in argv)


def test_ssh_copy_id_leaves_the_host_block_to_the_config():
    """ssh-copy-id is given the alias, so OpenSSH applies the Host block.

    Repeating User or Port as options would beat a `Host *` block placed
    before the host's own, and install the key somewhere `ssh <alias>` does
    not log in.
    """
    import types

    from sshpilot.ssh_connection_builder import _build_base_ssh_command

    conn = _config_connection(
        hostname="web.example.com",
        username="alice",
        port=2200,
        authored=("hostname", "user", "port"),
    )
    argv = _build_base_ssh_command(
        conn, {}, types.SimpleNamespace(get_ssh_config=dict), "ssh-copy-id"
    )

    assert not any(token.startswith("User=") for token in argv)
    # ssh-copy-id rejects the long flags ssh accepts.
    for rejected in ("-p", "-l", "-J", "-A", "-C", "-v"):
        assert rejected not in argv


def test_ssh_copy_id_sends_no_host_key_option_when_not_configured():
    """"Do not configure" must not fall back to accept-new on this path."""
    import types

    from sshpilot.ssh_connection_builder import _build_base_ssh_command

    conn = _config_connection(hostname="web.example.com", authored=("hostname",))
    for settings in ({}, {"strict_host_key_checking": ""},
                     {"strict_host_key_checking": "", "auto_add_host_keys": True}):
        argv = _build_base_ssh_command(
            conn, {}, types.SimpleNamespace(get_ssh_config=lambda s=settings: dict(s)),
            "ssh-copy-id",
        )
        assert not any("StrictHostKeyChecking" in token for token in argv)

    argv = _build_base_ssh_command(
        conn, {},
        types.SimpleNamespace(get_ssh_config=lambda: {"strict_host_key_checking": "yes"}),
        "ssh-copy-id",
    )
    assert "StrictHostKeyChecking=yes" in argv


def test_ssh_copy_id_does_not_emit_session_directives():
    # A config RemoteCommand must not replace ssh-copy-id's own remote script.
    import types

    from sshpilot.ssh_connection_builder import _build_base_ssh_command

    conn = _config_connection(
        hostname="web.example.com",
        local_command="echo connected",
        remote_command="tmux attach",
        request_tty="force",
        authored=("hostname", "localcommand", "remotecommand", "requesttty"),
    )
    argv = _build_base_ssh_command(
        conn, {}, types.SimpleNamespace(get_ssh_config=dict), "ssh-copy-id"
    )
    joined = " ".join(argv)
    assert "LocalCommand=echo" not in joined
    # A key install is no login: the block's LocalCommand is refused.
    assert "PermitLocalCommand=no" in argv
    assert "RemoteCommand=tmux" not in joined
    assert "RequestTTY=" not in joined
    # The alias is the destination, so the block's RemoteCommand is cancelled.
    assert "RemoteCommand=none" in argv
