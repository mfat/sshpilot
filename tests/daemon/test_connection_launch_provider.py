"""Tests for the daemon connection launch provider."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import conftest  # noqa: F401  (installs the GI stub)

from sshpilot.daemon.connection_launch_provider import (  # noqa: E402
    DaemonConnectionLaunchProvider,
    HeadlessConnectionView,
)
from sshpilot.core.connections.models import ConnectionRecord  # noqa: E402


def _record(nickname="web", **kwargs) -> ConnectionRecord:
    return ConnectionRecord(
        id=kwargs.pop("id", nickname),
        nickname=nickname,
        hostname=kwargs.pop("hostname", "example.com"),
        username=kwargs.pop("username", "alice"),
        port=kwargs.pop("port", 22),
        protocol=kwargs.pop("protocol", "ssh"),
        source=kwargs.pop("source", ""),
        data=kwargs.pop("data", {}),
    )


@pytest.fixture
def provider():
    records = {"web": _record()}

    def resolver(cid: str) -> Optional[ConnectionRecord]:
        return records.get(cid)

    return DaemonConnectionLaunchProvider(
        resolver,
        secret_provider=None,
        app_config=None,
    ), records


def test_resolver_required():
    with pytest.raises(ValueError):
        DaemonConnectionLaunchProvider(None)


def test_missing_connection_raises(provider):
    prov, _records = provider
    with pytest.raises(Exception) as exc:
        prov.prepare_terminal_launch("missing")
    assert "does not exist" in str(exc.value).lower()


def test_builtin_plugin_failure_survives_provider_and_session_runtime(monkeypatch):
    from sshpilot.api.errors import ErrorCode
    from sshpilot.api.models.common import ClientId, ConnectionId
    from sshpilot.api.models.connections import ConnectionDetails
    from sshpilot.api.models.sessions import (
        OpenSessionRequest,
        PluginSessionFailure,
        PluginSessionFailureCode,
        SessionState,
    )
    from sshpilot.daemon.session_runtime import SessionRuntime
    import sshpilot.platform_utils as platform_utils

    record = _record(
        nickname="serial-demo",
        protocol="serial",
        data={
            "device": "/dev/ttyUSB0",
            "baud": "9600",
            "flow": "hard",
            "databits": "5",
        },
    )
    provider = DaemonConnectionLaunchProvider(
        lambda connection_id: record if connection_id == record.id else None,
        secret_provider=None,
        app_config=None,
    )
    monkeypatch.setattr(
        platform_utils.shutil,
        "which",
        lambda name, path=None: "/usr/bin/screen" if name == "screen" else None,
    )

    class Core:
        @staticmethod
        def get_connection(connection_id):
            assert connection_id == ConnectionId("serial-demo")
            return ConnectionDetails(
                id=ConnectionId("serial-demo"),
                nickname="serial-demo",
                host="",
                hostname="",
                username="",
                port=22,
                protocol="serial",
                plugin_data=record.data,
            )

    class PreparingRunner:
        terminal_capable = False

        @staticmethod
        def start(spec, on_exit):
            del on_exit
            provider.prepare_terminal_launch(
                spec.connection_id,
                interaction_policy="none",
            )
            raise AssertionError("the unsupported Serial launch must not start")

        @staticmethod
        def close():
            return None

    runtime = SessionRuntime(Core(), runner=PreparingRunner())
    try:
        summary = runtime.open_session(
            OpenSessionRequest(connection_id=ConnectionId("serial-demo")),
            client_id=ClientId("client:test"),
        )
    finally:
        runtime.shutdown()

    assert summary.state is SessionState.FAILED
    assert type(summary.failure) is PluginSessionFailure
    assert summary.failure.code is (
        PluginSessionFailureCode.SERIAL_SCREEN_HARDWARE_FLOW_AND_DATABITS_UNSUPPORTED
    )
    assert summary.failure.error_code is ErrorCode.SESSION_STARTUP_FAILED
    assert dict(summary.failure.parameters) == {
        "fallback_program": "screen",
        "preferred_program": "picocom",
        "flow": "RTS/CTS",
        "databits": "5",
    }
    assert summary.failure.diagnostic == ""


def test_view_exposes_connection_fields():
    view = HeadlessConnectionView(_record())
    assert view.nickname == "web"
    assert view.hostname == "example.com"
    assert view.username == "alice"
    assert view.port == 22
    assert view.protocol == "ssh"
    assert view.get_effective_host() == "example.com"
    assert view.resolve_host_identifier() == "web"


def test_view_identity_candidates_from_data():
    view = HeadlessConnectionView(
        _record(data={"identity_files": ["~/.ssh/id_ed25519"], "auth_method": 0})
    )
    assert view.identity_files == ["~/.ssh/id_ed25519"]
    assert view.auth_method == 0


def test_sftp_launch_rejects_non_ssh_protocol(provider):
    prov, records = provider
    records["web"] = _record(protocol="docker")
    with pytest.raises(Exception):
        prov.prepare_sftp_launch("web")


def test_forward_launch_rejects_unknown_type(provider):
    prov, _records = provider
    with pytest.raises(Exception) as exc:
        prov.prepare_forward_launch(
            "web", forward_type="sideways", bind_port=8080
        )
    assert "not supported" in str(exc.value).lower()


def test_no_client_path_authority(provider):
    """The provider never accepts a filesystem path from the client."""
    prov, records = provider
    records["web"] = _record(
        source="/tmp/evil-config",
        data={"config_root": "/tmp/evil-root"},
    )
    # The resolver is the only source of records; the view reads daemon-owned
    # metadata, never a client-supplied path.
    view = HeadlessConnectionView(records["web"])
    assert view.source == "/tmp/evil-config"


def test_local_command_injected_only_when_present(provider):
    prov, records = provider
    records["web"] = _record(data={"local_command": "echo hi"})
    command, _environment = prov.prepare_terminal_launch(
        "web", interaction_policy="none"
    )
    assert "PermitLocalCommand=yes" in command


def test_terminal_launch_appends_remote_command_after_host(provider):
    prov, _records = provider
    command, _environment = prov.prepare_terminal_launch(
        "web", interaction_policy="none",
        remote_command="docker exec -it web sh",
        force_tty=True,
    )
    # canonical shape: <ssh-binary> ... -t web docker exec -it web sh
    assert command[-1] == "docker exec -it web sh"
    assert command[-2] == "web"
    assert command[-3] == "-t"


def test_terminal_launch_without_remote_command_has_host_last(provider):
    prov, _records = provider
    command, _environment = prov.prepare_terminal_launch(
        "web", interaction_policy="none"
    )
    assert command[-1] == "web"
    assert "-t" not in command


def test_remote_command_launch_ends_with_target_then_command(provider):
    prov, _records = provider
    command, _environment = prov.prepare_remote_command_launch("web", "uptime")
    # Canonical shape everything else depends on: options, target, command.
    assert command[-2] == "web"
    assert command[-1] == "uptime"


def test_require_master_is_off_unless_asked(provider):
    prov, _records = provider
    command, _environment = prov.prepare_remote_command_launch("web", "uptime")
    assert not any("controlmaster" in str(token).lower() for token in command)


def test_require_master_places_multiplex_before_target_not_after_command(provider):
    """Regression: multiplex options appended after the remote command are
    read by OpenSSH as remote shell text -- host-info probes failed with
    ``remote_command_failed`` one second after authentication."""

    prov, _records = provider
    command, _environment = prov.prepare_remote_command_launch(
        "web", "uptime", require_master=True
    )
    tokens = [str(token) for token in command]
    master = tokens.index("ControlMaster=auto")
    target = tokens.index("web")
    assert tokens[-1] == "uptime"
    assert target == len(tokens) - 2
    assert master < target
    assert tokens[master - 1] == "-o"
    assert "ControlPersist=60" in tokens
    assert any(token.startswith("ControlPath=") for token in tokens)


def test_require_master_coexists_with_preference_multiplex(provider):
    """When the global preference already provided the identical fragment,
    the forced copy is inert: OpenSSH takes the first value and both come
    from the same source. What matters is position, not count."""
    from types import SimpleNamespace

    from sshpilot.ssh_multiplex import controlmaster_args

    prov, _records = provider
    multiplexed = DaemonConnectionLaunchProvider(
        prov._resolver,
        secret_provider=None,
        app_config=SimpleNamespace(
            get_ssh_config=lambda: {"ssh_overrides": list(controlmaster_args())}
        ),
    )
    command, _environment = multiplexed.prepare_remote_command_launch(
        "web", "uptime", require_master=True
    )
    tokens = [str(token) for token in command]
    first_master = tokens.index("ControlMaster=auto")
    target = tokens.index("web")
    assert tokens[-1] == "uptime"
    assert first_master < target
    assert {t for t in tokens if "ControlMaster" in t} == {"ControlMaster=auto"}


def test_require_master_overrides_authored_controlmaster_no(provider):
    """Host Info must hold a master even when the Host block disabled mux.

    OpenSSH is first-value-wins: the forced fragment is emitted before
    authored Advanced-tab options, so ControlMaster=auto beats an authored
    ControlMaster no (and the forced ControlPath beats ControlPath none).
    """
    prov, records = provider
    records["web"] = _record(
        data={
            "__authored_directives": ["hostname", "user"],
            "hostname": "example.com",
            "username": "alice",
            "extra_ssh_config": "ControlMaster no\nControlPath none",
        }
    )
    command, _environment = prov.prepare_remote_command_launch(
        "web", "uptime", require_master=True
    )
    tokens = [str(token) for token in command]
    first_master = tokens.index("ControlMaster=auto")
    authored_no = tokens.index("ControlMaster=no")
    assert first_master < authored_no
    assert tokens[-1] == "uptime"
    assert first_master < len(tokens) - 2
    assert tokens[first_master - 1] == "-o"
    # Forced path is the first ControlPath; authored none comes later and loses.
    control_paths = [t for t in tokens if t.startswith("ControlPath=")]
    assert control_paths[0] != "ControlPath=none"
    assert "ControlPath=none" in control_paths[1:]
    assert any(token.startswith("ControlPath=") and "/%C" in token for token in tokens)


def test_remote_command_overrides_authored_session_type_none(provider):
    """Host Info must run a command even when the Host is forwarding-only.

    ``SessionType none`` (ssh -N) makes OpenSSH ignore the remote command and
    exit 0 with empty stdout — Host Info then "succeeds" with a blank
    snapshot. Forced ``SessionType=default`` is first-value-wins before the
    authored Advanced-tab option.
    """
    prov, records = provider
    records["web"] = _record(
        data={
            "__authored_directives": ["hostname", "user"],
            "hostname": "example.com",
            "username": "alice",
            "extra_ssh_config": "SessionType none",
        }
    )
    command, _environment = prov.prepare_remote_command_launch("web", "uptime")
    tokens = [str(token) for token in command]
    forced = tokens.index("SessionType=default")
    authored_none = tokens.index("SessionType=none")
    assert tokens[forced - 1] == "-o"
    assert forced < authored_none
    assert forced < len(tokens) - 2
    assert tokens[-2]  # destination alias / host
    assert tokens[-1] == "uptime"


def test_terminal_forwarding_only_disables_preference_multiplex(provider):
    """SessionType none terminal launches must not inherit ControlMaster=auto.

    Preference multiplexing injects ControlMaster=auto + ControlPersist via
    ssh_overrides (last). Combined with SessionType none, OpenSSH backgrounds
    the master and the watched process exits 0 — the tab closes as a clean
    exit. Forced ControlMaster=no is first-value-wins before those overrides.
    """
    from types import SimpleNamespace

    from sshpilot.ssh_multiplex import controlmaster_args

    prov, records = provider
    records["web"] = _record(
        data={
            "__authored_directives": ["hostname", "user"],
            "hostname": "example.com",
            "username": "alice",
            "extra_ssh_config": "SessionType none",
        }
    )
    multiplexed = DaemonConnectionLaunchProvider(
        prov._resolver,
        secret_provider=None,
        app_config=SimpleNamespace(
            get_ssh_config=lambda: {"ssh_overrides": list(controlmaster_args())}
        ),
    )
    command, _environment = multiplexed.prepare_terminal_launch(
        "web", interaction_policy="none"
    )
    tokens = [str(token) for token in command]
    forced_no = tokens.index("ControlMaster=no")
    assert tokens[forced_no - 1] == "-o"
    # Preference auto may still appear later; first value wins.
    if "ControlMaster=auto" in tokens:
        assert forced_no < tokens.index("ControlMaster=auto")
    assert forced_no < len(tokens) - 1
    assert tokens[-1]  # destination host / alias


def test_terminal_shell_host_keeps_preference_multiplex(provider):
    """Ordinary shell tabs still receive ControlMaster=auto when preferred."""
    from types import SimpleNamespace

    from sshpilot.ssh_multiplex import controlmaster_args

    prov, _records = provider
    multiplexed = DaemonConnectionLaunchProvider(
        prov._resolver,
        secret_provider=None,
        app_config=SimpleNamespace(
            get_ssh_config=lambda: {"ssh_overrides": list(controlmaster_args())}
        ),
    )
    command, _environment = multiplexed.prepare_terminal_launch(
        "web", interaction_policy="none"
    )
    tokens = [str(token) for token in command]
    assert "ControlMaster=auto" in tokens
    assert "ControlMaster=no" not in tokens
    assert "ControlPersist=60" in tokens


def test_forward_launch_disables_preference_multiplex(provider):
    """Daemon ``ssh -N`` forwards must not inherit ControlMaster=auto.

    Preference multiplexing injects ControlMaster=auto + ControlPersist via
    ssh_overrides (last). Combined with ``ssh -N``, OpenSSH backgrounds the
    master and the watched process exits 0 — Host Info web-UI opens fail with
    ``forward_not_active``. Forced ControlMaster=no is first-value-wins before
    those overrides.
    """
    from types import SimpleNamespace

    from sshpilot.ssh_multiplex import controlmaster_args

    prov, _records = provider
    multiplexed = DaemonConnectionLaunchProvider(
        prov._resolver,
        secret_provider=None,
        app_config=SimpleNamespace(
            get_ssh_config=lambda: {"ssh_overrides": list(controlmaster_args())}
        ),
    )
    command, _environment = multiplexed.prepare_forward_launch(
        "web",
        forward_type="local",
        bind_host="127.0.0.1",
        bind_port=18080,
        destination_host="127.0.0.1",
        destination_port=80,
    )
    tokens = [str(token) for token in command]
    forced_no = tokens.index("ControlMaster=no")
    assert tokens[forced_no - 1] == "-o"
    if "ControlMaster=auto" in tokens:
        assert forced_no < tokens.index("ControlMaster=auto")
    assert "-N" in tokens
    assert "ExitOnForwardFailure=yes" in tokens
    assert forced_no < tokens.index("-N")


@pytest.mark.parametrize(
    ("term", "expected"),
    [
        (None, "xterm-256color"),
        ("", "xterm-256color"),
        ("dumb", "xterm-256color"),
        ("DUMB", "xterm-256color"),
        ("foot", "foot"),
        ("kitty", "kitty"),
        ("xterm-256color", "xterm-256color"),
    ],
)
def test_terminal_launch_normalizes_unusable_term_at_provider_boundary(
    provider, monkeypatch, term, expected
):
    """Interactive SSH launches always provide a usable terminal type."""
    import sshpilot.ssh_connection_builder as builder

    class Prepared:
        command = ("ssh", "web")
        env = {} if term is None else {"TERM": term}
        use_askpass = False

    monkeypatch.setattr(builder, "build_ssh_connection", lambda _context: Prepared())
    monkeypatch.setattr(
        "sshpilot.daemon.connection_launch_provider.shutil.which",
        lambda _name, **_kwargs: "/usr/bin/ssh",
    )

    prov, _records = provider
    _command, environment = prov.prepare_terminal_launch(
        "web", interaction_policy="normal"
    )

    assert environment["TERM"] == expected


def test_non_terminal_ssh_launch_does_not_get_terminal_term_default(provider, monkeypatch):
    import sshpilot.ssh_connection_builder as builder

    class Prepared:
        command = ("ssh", "web")
        env = {}
        use_askpass = False

    monkeypatch.setattr(builder, "build_ssh_connection", lambda _context: Prepared())
    monkeypatch.setattr(
        "sshpilot.daemon.connection_launch_provider.shutil.which",
        lambda _name, **_kwargs: "/usr/bin/ssh",
    )

    prov, _records = provider
    _command, environment = prov.prepare_scp_launch("web")

    assert "TERM" not in environment


def test_production_settings_view_is_not_called_as_a_function(provider, monkeypatch):
    class SettingsView:
        def get_setting(self, _key, default=None):
            return default

    class Prepared:
        command = ("ssh", "web")
        env = {}
        use_askpass = False

    import sshpilot.ssh_connection_builder as builder

    monkeypatch.setattr(builder, "build_ssh_connection", lambda _context: Prepared())
    prov, _records = provider
    prov = DaemonConnectionLaunchProvider(
        lambda cid: _record(cid),
        app_config=SettingsView(),
    )
    command, _environment = prov.prepare_terminal_launch("web")
    assert command[0].endswith("/ssh")


def test_preload_uses_identities_discovered_by_resolver_when_cache_empty(
    monkeypatch,
):
    """Fresh callers can resolve identities without caching them.

    The provider-level flow: the view starts with an empty candidate cache,
    ``collect_identity_file_candidates()`` discovers one key, the builder
    receives that candidate, and the post-build preload path preloads the
    SAME key through the shared credential surface using its stored passphrase.
    """
    import sshpilot.ssh_connection_builder as builder
    from sshpilot.daemon import connection_launch_provider as provider_mod

    key = "/home/u/.ssh/id_ed25519"

    class _Credentials:
        def __init__(self):
            self.prepared = []

        def get_key_passphrase(self, key_path):
            return "secretpass" if key_path == key else None

        def prepare_key_for_connection(self, key_path, *, force=True, lifetime=0):
            self.prepared.append((key_path, force, lifetime))
            return True

    credentials = _Credentials()
    seen_by_builder = []

    class Prepared:
        command = ("ssh", "web")
        env = {}
        use_askpass = False
        password = None

    def _fake_build(context):
        seen_by_builder.append(list(context.connection.resolved_identity_files))
        return Prepared()

    monkeypatch.setattr(
        provider_mod.HeadlessConnectionView,
        "collect_identity_file_candidates",
        lambda self: [key],
    )
    monkeypatch.setattr(builder, "build_ssh_connection", _fake_build)
    monkeypatch.setattr(
        provider_mod.shutil, "which", lambda _name, **_kwargs: "/usr/bin/ssh"
    )

    class SettingsView:
        def get_setting(self, _key, default=None):
            return default

    prov = DaemonConnectionLaunchProvider(
        lambda cid: _record(data={"auth_method": 0}),
        secret_provider=None,
        app_config=SettingsView(),
    )
    prov._manager_shim = lambda _connection: credentials

    _argv, _env = prov.prepare_terminal_launch("web", interaction_policy="none")

    # The builder sees the resolved candidate, and the preload path shares it.
    assert seen_by_builder == [[key]]
    assert credentials.prepared == [(key, True, 0)]


def test_remote_identity_supplies_the_account_prompts_and_secrets_need(provider):
    """Headless operations cannot derive this from argv, so it is exposed.

    A remote-command argv ends with the command rather than the target, so the
    interaction broker's argv fallback finds no account. An empty username
    makes a prompt read "unknown@host" and makes the stored-secret lookup miss
    the key the session path saved under.
    """

    prov, _records = provider

    hostname, username, port = prov.remote_identity("web")

    assert hostname == "example.com"
    assert username == "alice"
    assert port == 22


def test_remote_identity_reports_a_non_default_port():
    records = {"web": _record(port=2222)}
    prov = DaemonConnectionLaunchProvider(
        lambda cid: records.get(cid), secret_provider=None, app_config=None
    )

    assert prov.remote_identity("web")[2] == 2222


def test_remote_identity_rejects_an_unknown_connection():
    prov = DaemonConnectionLaunchProvider(
        lambda _cid: None, secret_provider=None, app_config=None
    )

    with pytest.raises(Exception):
        prov.remote_identity("missing")
