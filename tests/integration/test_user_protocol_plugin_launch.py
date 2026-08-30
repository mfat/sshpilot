"""Integration: open a session whose protocol comes from a third-party plugin.

A protocol backend only exists in the process that ran ``activate()``. The GTK
process does that at startup, but session launch is daemon-owned and runs
elsewhere, so the daemon has to activate user plugins itself or every
third-party protocol fails as unsupported.

Covers that seam with a real user plugin on disk: create via
``CreateConnectionRequest`` → resolve through ``DaemonConnectionLaunchProvider``
→ run the argv the plugin's ``build_spawn`` returned. A protocol backend is just
argv, so this needs no server — only ``/bin/sh``.
"""

from __future__ import annotations

import json
import os
import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytestmark = pytest.mark.integration

pexpect = pytest.importorskip("pexpect")

from sshpilot.api.errors import ErrorCode, SshPilotError
from sshpilot.api.models.connections import CreateConnectionRequest
from sshpilot.core.connection_application_service import ConnectionApplicationService
from sshpilot.core.connections.repository import ConnectionRepository
from sshpilot.core.connections.ssh_config_store import SshConfigStore
from sshpilot.daemon.connection_launch_provider import DaemonConnectionLaunchProvider
from sshpilot.plugins import registry as registry_mod


MARKER = "SSHPILOT-USER-PROTOCOL-OK"
PLUGIN_ID = "rig-probe"
PROTOCOL_ID = "rigprobe"

PLUGIN_SOURCE = textwrap.dedent(f'''
    """A third-party protocol backend: its connection is a command."""
    from sshpilot.plugins.api import (
        FieldSpec, ProtocolBackend, SpawnSpec, SshPilotPlugin,
    )


    class _Backend(ProtocolBackend):
        protocol_id = "{PROTOCOL_ID}"
        display_name = "Rig Probe"
        default_port = None

        def capabilities(self):
            return frozenset()

        def connection_fields(self):
            return [FieldSpec(key="marker", label="Marker", kind="text")]

        def validate(self, data):
            return []

        def build_spawn(self, connection, ctx):
            data = getattr(connection, "data", None) or {{}}
            marker = data.get("marker") or "NO-MARKER"
            return SpawnSpec(
                argv=["/bin/sh", "-c", f"echo {{marker}}; sleep 5"],
                env={{"PATH": "/usr/bin:/bin"}},
            )


    class Plugin(SshPilotPlugin):
        def activate(self, ctx):
            # A page registration alongside the backend: in the daemon there is
            # no UI to take it, and that must not abort activate() before
            # register_protocol runs.
            ctx.ui.register_page("p", "Rig Probe", "icon", lambda: None)
            ctx.register_protocol(_Backend())
''')


class _FakeConfig:
    def __init__(self, settings=None):
        self._settings = dict(settings or {})

    def get_setting(self, key, default=None):
        return self._settings.get(key, default)


def _write_user_plugin(tmp_path: Path, *, declare_protocols: bool) -> Path:
    plugin_dir = tmp_path / "xdg-data" / "sshpilot" / "plugins" / PLUGIN_ID
    plugin_dir.mkdir(parents=True)
    manifest = {"id": PLUGIN_ID, "name": "Rig Probe", "api_version": 1}
    if declare_protocols:
        manifest["protocols"] = [PROTOCOL_ID]
    (plugin_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    (plugin_dir / "__init__.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    return plugin_dir


def _repo(tmp_path: Path) -> ConnectionRepository:
    root = tmp_path / "ssh_config"
    root.write_text("# empty\n", encoding="utf-8")
    return ConnectionRepository(
        ssh_store=SshConfigStore(root),
        state_path=tmp_path / "connections.json",
        legacy_config_path=tmp_path / "config.json",
        isolated=False,
    )


@pytest.fixture
def empty_registry(monkeypatch, tmp_path):
    """A daemon-like process: nothing registered, no user plugins activated."""
    monkeypatch.setattr(registry_mod, "_registry", None)
    import sshpilot.plugins.loader as loader_mod

    monkeypatch.setattr(loader_mod, "_builtins_ensured_for", None)
    monkeypatch.setattr(loader_mod, "_headless_user_plugins_for", None)
    monkeypatch.setattr(loader_mod, "_headless_user_plugin_ids", set())
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    assert registry_mod.protocol_registry().get_or_none(PROTOCOL_ID) is None
    return registry_mod.protocol_registry()


def _launch(tmp_path, app_config):
    repository = _repo(tmp_path)
    launch_provider = DaemonConnectionLaunchProvider(
        repository.get_record,
        secret_provider=None,
        app_config=app_config,
    )
    core = ConnectionApplicationService(
        repository,
        launch_provider=launch_provider,
        client_name="user-protocol-launch",
        allow_cross_thread_commands=True,
    )
    core.create_connection(
        CreateConnectionRequest(
            nickname="rig-probe-host",
            hostname="127.0.0.1",
            port=22,  # unused by this protocol; the model requires 1-65535
            protocol=PROTOCOL_ID,
            plugin_data={"marker": MARKER},
        )
    )
    return core, launch_provider


@pytest.mark.parametrize("declare_protocols", [True, False],
                         ids=["manifest-declares-protocol", "manifest-silent"])
def test_user_plugin_protocol_launches(tmp_path, empty_registry, declare_protocols):
    """The daemon resolves a user protocol and runs the argv build_spawn gave it.

    Both manifest shapes must work: declaring ``protocols`` lets the daemon load
    this plugin directly, and staying silent falls back to the id-order sweep.
    """
    _write_user_plugin(tmp_path, declare_protocols=declare_protocols)
    app_config = _FakeConfig({"plugins.enabled": [PLUGIN_ID]})
    core, launch_provider = _launch(tmp_path, app_config)
    try:
        argv, env = launch_provider.prepare_terminal_launch(
            "rig-probe-host", interaction_policy="none"
        )
        assert registry_mod.protocol_registry().get_or_none(PROTOCOL_ID) is not None
        assert os.path.basename(argv[0]) == "sh"
        assert MARKER in argv[2]

        child = pexpect.spawn(argv[0], list(argv[1:]), env=env,
                              timeout=15, encoding="utf-8")
        try:
            idx = child.expect([MARKER, pexpect.EOF, pexpect.TIMEOUT])
            assert idx == 0, f"user protocol did not run: {child.before!r}"
        finally:
            if child.isalive():
                child.terminate(force=True)
            child.close(force=True)
    finally:
        core.close()


def test_user_plugin_protocol_refused_when_not_enabled(tmp_path, empty_registry):
    """Plugins stay opt-in in the daemon too: present on disk but not enabled
    means the protocol never resolves. Also proves the test above is not
    passing on some pre-registered backend."""
    _write_user_plugin(tmp_path, declare_protocols=True)
    core, launch_provider = _launch(tmp_path, _FakeConfig())
    try:
        with pytest.raises(SshPilotError) as excinfo:
            launch_provider.prepare_terminal_launch(
                "rig-probe-host", interaction_policy="none"
            )
        assert excinfo.value.code is ErrorCode.UNSUPPORTED_SESSION_PROTOCOL
    finally:
        core.close()


def test_daemon_does_not_import_plugins_for_a_builtin_protocol(tmp_path, empty_registry):
    """An ordinary SSH launch must not pull third-party code into the daemon:
    user plugins are only reached for a protocol the built-ins cannot resolve."""
    plugin_dir = _write_user_plugin(tmp_path, declare_protocols=False)
    (plugin_dir / "__init__.py").write_text(
        "open(__file__ + '.executed', 'w').close()\n", encoding="utf-8")
    repository = _repo(tmp_path)
    launch_provider = DaemonConnectionLaunchProvider(
        repository.get_record,
        secret_provider=None,
        app_config=_FakeConfig({"plugins.enabled": [PLUGIN_ID]}),
    )
    core = ConnectionApplicationService(
        repository,
        launch_provider=launch_provider,
        client_name="user-protocol-launch",
        allow_cross_thread_commands=True,
    )
    try:
        core.create_connection(
            CreateConnectionRequest(
                nickname="plain-telnet",
                hostname="127.0.0.1",
                port=23,
                protocol="telnet",
                plugin_data={},
            )
        )
        launch_provider.prepare_terminal_launch(
            "plain-telnet", interaction_policy="none"
        )
    finally:
        core.close()
    assert not (plugin_dir / "__init__.py.executed").exists()
