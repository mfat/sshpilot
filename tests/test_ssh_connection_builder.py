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


# --- authored directives become argv ---------------------------------------


def _config_connection(**data):
    """A Connection carrying parser authorship evidence, as the daemon supplies."""
    authored = data.pop("authored", ())
    payload = {"host": "web", "nickname": "web", **data}
    payload["__authored_directives"] = tuple(authored)
    return Connection(payload)


def _argv(conn):
    return build_ssh_connection(
        ConnectionContext(connection=conn, command_type="ssh")
    ).command


def test_authored_user_and_port_are_emitted_so_a_global_cannot_win():
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            username="alice",
            port=2200,
            authored=("hostname", "port", "user"),
        )
    )
    assert "-l" in argv and argv[argv.index("-l") + 1] == "alice"
    assert "-p" in argv and argv[argv.index("-p") + 1] == "2200"
    assert argv[-1] == "web"


def test_unauthored_user_and_port_are_never_forced_onto_the_command():
    # This block authors only HostName, so User/Port are inherited. Emitting the
    # parser defaults here would override a `Host *` the user depends on.
    argv = _argv(
        _config_connection(
            hostname="bare.example.com",
            username="",
            port=22,
            authored=("hostname",),
        )
    )
    assert "-l" not in argv
    assert "-p" not in argv
    assert "User=" not in " ".join(argv)


def test_accumulating_directives_are_left_to_openssh():
    # `-i` appends rather than replaces and no argv spelling clears an inherited
    # IdentityFile, so emitting these would duplicate entries without changing
    # precedence.
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            identity_files=["~/.ssh/id_web"],
            certificate_files=["~/.ssh/id_web-cert.pub"],
            authored=("hostname", "identityfile", "certificatefile"),
        )
    )
    assert "-i" not in argv
    assert "CertificateFile" not in " ".join(argv)


def test_record_without_authorship_evidence_keeps_the_plain_shape():
    # Empty evidence means "unknown", never "authored nothing" — guessing here
    # would force values onto connections that never came from a Host block.
    argv = _argv(
        _config_connection(hostname="web.example.com", username="alice", port=2200)
    )
    assert "-l" not in argv
    assert "-p" not in argv


def test_authored_proxy_jump_and_forward_agent_are_emitted():
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            proxy_jump=["jump.example.com"],
            forward_agent=True,
            authored=("hostname", "proxyjump", "forwardagent"),
        )
    )
    assert "-J" in argv and argv[argv.index("-J") + 1] == "jump.example.com"
    assert "ForwardAgent=yes" in argv


def test_authored_forward_agent_no_is_emitted_so_a_global_yes_cannot_win():
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            forward_agent=False,
            authored=("hostname", "forwardagent"),
        )
    )
    assert "ForwardAgent=no" in argv


# --- Advanced tab (extra_ssh_config) ---------------------------------------


def test_authored_advanced_options_are_emitted():
    # These reached OpenSSH only by being written into the Host block, which an
    # earlier `Host *` beats — the tab showed `Compression no` while the
    # session ran with `Compression yes`.
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            extra_ssh_config="compression no\nstricthostkeychecking yes",
            authored=("hostname", "compression", "stricthostkeychecking"),
        )
    )
    assert "compression=no" in argv
    assert "stricthostkeychecking=yes" in argv


def test_structural_and_accumulating_advanced_options_are_not_emitted():
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            extra_ssh_config=(
                "include other.conf\n"
                "sendenv LC_ONE\n"
                "remotecommand tail -f /var/log/syslog"
            ),
            authored=("hostname", "include", "sendenv", "remotecommand"),
        )
    )
    joined = " ".join(argv)
    # `-o Include=` is meaningless, SendEnv appends rather than replaces, and
    # RemoteCommand is composed from a typed field.
    assert "Include=" not in joined
    assert "sendenv=" not in joined.lower()
    assert "remotecommand=" not in joined.lower()


def test_repeated_advanced_directive_is_left_to_the_config():
    # Only the first `-o` for a keyword takes effect, so emitting would silently
    # drop the second SetEnv line.
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            extra_ssh_config="setenv A=1\nsetenv B=2",
            authored=("hostname", "setenv"),
        )
    )
    assert "setenv=" not in " ".join(argv).lower()


def test_authored_keepalive_suppresses_the_app_default():
    # ssh_overrides precede authored options in argv and the command line is
    # first-obtained-value-wins, so an injected default would beat the value the
    # user set on the Advanced tab.
    argv = _argv(
        _config_connection(
            hostname="web.example.com",
            extra_ssh_config="serveraliveinterval 30",
            authored=("hostname", "serveraliveinterval"),
        )
    )
    assert "serveraliveinterval=30" in argv
    assert "ServerAliveInterval=15" not in argv


def test_default_keepalive_still_applies_without_an_authored_one():
    argv = _argv(_config_connection(hostname="web.example.com", authored=("hostname",)))
    assert "ServerAliveInterval=15" in argv
