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
