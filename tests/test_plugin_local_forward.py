"""``ctx.ensure_local_forward`` rides the single SSH/auth path: the forward is
added onto the shared ControlMaster with ``ssh -O forward`` when multiplexing
is active, falls back to a background ``ssh -N`` otherwise, and reuses a live
forward per (nickname, remote_port). Everything is monkeypatched — no SSH."""

import os
import subprocess
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import api as api_mod
from sshpilot.plugins.api import PluginContext
from sshpilot.plugins.registry import ProtocolRegistry
from sshpilot import port_utils, ssh_connection_builder, ssh_multiplex

LOCAL_PORT = 55123


class FakeManager:
    def find_connection_by_nickname(self, nickname):
        return types.SimpleNamespace(nickname=nickname) if nickname == "web" else None


@pytest.fixture(autouse=True)
def clean_forwards():
    api_mod._FORWARDS.clear()
    yield
    api_mod._FORWARDS.clear()


@pytest.fixture
def ctx(monkeypatch):
    monkeypatch.setattr(port_utils, "find_available_port",
                        lambda preferred, *a, **k: LOCAL_PORT)
    monkeypatch.setattr(ssh_multiplex, "is_active", lambda nick: True)
    monkeypatch.setattr(ssh_multiplex, "control_path", lambda: "/tmp/ctl-sock")
    return PluginContext(plugin_id="test", app_config=None,
                         connection_manager=FakeManager(),
                         protocol_registry=ProtocolRegistry())


def _capture_builds(monkeypatch):
    """Record each ConnectionContext handed to build_ssh_connection and return
    a stub prepared command (no sshpass, empty env)."""
    builds = []

    def fake_build(conn_ctx):
        builds.append(list(conn_ctx.extra_args or []))
        return types.SimpleNamespace(command=["ssh", "web"], env={},
                                     use_sshpass=False, password=None)

    monkeypatch.setattr(ssh_connection_builder, "build_ssh_connection", fake_build)
    return builds


def test_mux_forward_builds_control_command_and_reuses(ctx, monkeypatch):
    builds = _capture_builds(monkeypatch)
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(port_utils, "is_port_available", lambda p, *a, **k: False)

    assert ctx.ensure_local_forward("web", 8080) == LOCAL_PORT
    assert builds == [["-O", "forward", "-o", "ControlPath=/tmp/ctl-sock",
                       "-L", f"{LOCAL_PORT}:localhost:8080"]]

    # Second call for the same target reuses the live forward — no new build.
    assert ctx.ensure_local_forward("web", 8080) == LOCAL_PORT
    assert len(builds) == 1


def test_fallback_spawns_background_ssh_n(ctx, monkeypatch):
    builds = _capture_builds(monkeypatch)
    # -O forward fails (e.g. master not created yet) -> fall through.
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **k: types.SimpleNamespace(returncode=1))
    spawned = []

    def fake_popen(argv, **kwargs):
        spawned.append(argv)
        return types.SimpleNamespace(poll=lambda: None, terminate=lambda: None)

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    # The forward's local port reads as bound immediately.
    monkeypatch.setattr(port_utils, "is_port_available", lambda p, *a, **k: False)

    assert ctx.ensure_local_forward("web", 8080) == LOCAL_PORT
    assert builds[-1] == ["-N", "-o", "ExitOnForwardFailure=yes",
                          "-L", f"{LOCAL_PORT}:localhost:8080"]
    assert spawned == [["ssh", "web"]]


def test_unknown_connection_raises(ctx):
    with pytest.raises(RuntimeError, match="No connection named"):
        ctx.ensure_local_forward("nope", 8080)
