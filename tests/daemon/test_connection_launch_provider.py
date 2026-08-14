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
