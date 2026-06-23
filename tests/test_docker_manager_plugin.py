"""Tests for the built-in Docker Manager plugin.

Fully offline: ``DockerClient`` is pure logic over an injected ``run_command``,
so we drive it with a fake (no Docker, no SSH, no GTK). ``page.py`` is not
imported here — it pulls in GTK and is only built lazily in the real app.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from sshpilot.plugins import registry as registry_mod
from sshpilot.plugins.loader import load_plugins
from sshpilot.plugins.builtin.docker_manager import Plugin
from sshpilot.plugins.builtin.docker_manager.client import DockerClient, DockerError


class FakeConfig:
    def get_setting(self, key, default=None):
        return default


class FakeResult:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def fresh_registry(monkeypatch, tmp_path):
    monkeypatch.setattr(registry_mod, "_registry", None)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))


def _recording_client(responder, *, runtime="docker"):
    """Build a DockerClient whose run_command records calls and returns the
    FakeResult that ``responder(command)`` yields (or empty success)."""
    calls = []

    def run_command(nickname, command, *, timeout=None):
        calls.append(command)
        result = responder(command) if responder else None
        return result if result is not None else FakeResult()

    return DockerClient(run_command, "srv", runtime), calls


# --- discovery / activation -------------------------------------------------

def test_loader_discovers_docker_manager():
    loaded = load_plugins(app_config=FakeConfig(), connection_manager=None)
    assert any(p.plugin_id == "docker-manager" and p.builtin for p in loaded)


class _FakeUI:
    def __init__(self):
        self.pages = []    # (page_id, title, icon, factory, kwargs)
        self.actions = []  # (action_id, label, icon, callback)
        self.opened = []   # page_ids passed to open_page
        self.toasts = []

    def register_page(self, page_id, title, icon, factory, **kwargs):
        self.pages.append((page_id, title, icon, factory, kwargs))

    def register_connection_action(self, action_id, label, icon, callback):
        self.actions.append((action_id, label, icon, callback))

    def open_page(self, page_id):
        self.opened.append(page_id)

    def notify(self, message, *a, **k):
        self.toasts.append(message)


def _fake_ctx(last_host=None, connections=()):
    ui = _FakeUI()
    return types.SimpleNamespace(
        ui=ui,
        settings=types.SimpleNamespace(get=lambda k, d=None: last_host if k == "last_host" else d,
                                       set=lambda k, v: None),
        list_connections=lambda: list(connections),
    )


def test_activate_registers_tools_menu_and_connection_action():
    ctx = _fake_ctx()
    Plugin().activate(ctx)
    # Tools-menu "Docker Manager" entry opens via an on_activate callback (not a
    # fixed page) so it can target the last-used host.
    manager = [p for p in ctx.ui.pages if p[0] == "manager"]
    assert manager and manager[0][1] == "Docker Manager"
    assert callable(manager[0][4].get("on_activate"))
    # Connection right-click action.
    assert ctx.ui.actions and ctx.ui.actions[0][1] == "Docker Manager"


def test_connection_action_opens_per_host_tab_and_reuses_it():
    ctx = _fake_ctx()
    plugin = Plugin()
    plugin.activate(ctx)
    open_cb = ctx.ui.actions[0][3]

    open_cb("web")
    host_pages = [p for p in ctx.ui.pages if p[0] == "host-web"]
    assert host_pages, "expected a per-host page id 'host-web'"
    assert host_pages[0][1] == "Docker — web"
    assert host_pages[0][4].get("add_menu_item") is False  # no Tools-menu clutter
    assert ctx.ui.opened == ["host-web"]

    # Reopening the same host registers once but opens (focuses) again.
    open_cb("web")
    assert sum(1 for p in ctx.ui.pages if p[0] == "host-web") == 1
    assert ctx.ui.opened == ["host-web", "host-web"]

    # A different host gets its own tab.
    open_cb("db")
    assert any(p[0] == "host-db" for p in ctx.ui.pages)
    assert ctx.ui.opened[-1] == "host-db"


def test_tools_menu_opens_last_used_host():
    ctx = _fake_ctx(last_host="beta")
    plugin = Plugin()
    plugin.activate(ctx)
    on_activate = next(p[4]["on_activate"] for p in ctx.ui.pages if p[0] == "manager")
    on_activate()
    assert "host-beta" in ctx.ui.opened


def test_tools_menu_no_connections_notifies():
    ctx = _fake_ctx(last_host=None, connections=())
    plugin = Plugin()
    plugin.activate(ctx)
    on_activate = next(p[4]["on_activate"] for p in ctx.ui.pages if p[0] == "manager")
    on_activate()
    assert not ctx.ui.opened and ctx.ui.toasts


def test_activate_without_register_connection_action_is_safe():
    # Older cores (API < 1.7) lack register_connection_action; activate must not
    # crash — it just skips the context-menu item.
    class _UI:
        def register_page(self, *a, **k):
            pass

    class _Ctx:
        ui = _UI()

    Plugin().activate(_Ctx())  # no exception


# --- DockerClient: queries + parsing ---------------------------------------

def test_ps_builds_command_and_parses_ndjson():
    line = '{"ID":"abc","Names":"web","Image":"nginx","Status":"Up 2h","State":"running","Ports":"80/tcp"}'
    client, calls = _recording_client(lambda c: FakeResult(stdout=line + "\n"))
    rows = client.ps()
    assert calls[-1] == "docker ps -a --format '{{json .}}'"
    assert rows == [{"ID": "abc", "Names": "web", "Image": "nginx",
                     "Status": "Up 2h", "State": "running", "Ports": "80/tcp"}]


def test_ps_running_only_omits_all_flag():
    client, calls = _recording_client(lambda c: FakeResult(stdout=""))
    client.ps(all=False)
    assert calls[-1] == "docker ps --format '{{json .}}'"


def test_stats_and_images_commands():
    client, calls = _recording_client(lambda c: FakeResult(stdout="{}"))
    client.stats()
    assert calls[-1] == "docker stats --no-stream --format '{{json .}}'"
    client.images()
    assert calls[-1] == "docker images --format '{{json .}}'"


def test_ndjson_skips_blank_and_invalid_lines():
    out = '{"ID":"1"}\n\nnot-json\n{"ID":"2"}\n'
    client, _ = _recording_client(lambda c: FakeResult(stdout=out))
    assert client.ps() == [{"ID": "1"}, {"ID": "2"}]


def test_parses_json_array_for_podman():
    out = '[{"Id":"1"},{"Id":"2"}]'
    client, _ = _recording_client(lambda c: FakeResult(stdout=out), runtime="podman")
    rows = client.ps()
    assert rows == [{"Id": "1"}, {"Id": "2"}]


def test_nonzero_exit_raises_docker_error():
    client, _ = _recording_client(
        lambda c: FakeResult(exit_code=1, stderr="Cannot connect to the Docker daemon")
    )
    with pytest.raises(DockerError, match="Cannot connect"):
        client.ps()


# --- DockerClient: actions / arg building ----------------------------------

@pytest.mark.parametrize("action", ["start", "stop", "restart", "kill"])
def test_lifecycle_simple_actions(action):
    client, calls = _recording_client(None)
    client.lifecycle(action, "c1")
    assert calls[-1] == f"docker {action} c1"


def test_rm_force_and_plain():
    client, calls = _recording_client(None)
    client.lifecycle("rm", "c1")
    assert calls[-1] == "docker rm c1"
    client.lifecycle("rm", "c1", force=True)
    assert calls[-1] == "docker rm -f c1"


def test_lifecycle_rejects_unknown_action():
    client, _ = _recording_client(None)
    with pytest.raises(ValueError):
        client.lifecycle("nuke", "c1")


def test_remove_image_and_prune():
    client, calls = _recording_client(None)
    client.remove_image("img1")
    assert calls[-1] == "docker rmi img1"
    client.remove_image("img1", force=True)
    assert calls[-1] == "docker rmi -f img1"
    client.system_prune()
    assert calls[-1] == "docker system prune -f"
    client.volume_prune()
    assert calls[-1] == "docker volume prune -f"


def test_container_id_is_shell_quoted():
    client, calls = _recording_client(None)
    client.lifecycle("stop", "weird; rm -rf /")
    assert calls[-1] == "docker stop 'weird; rm -rf /'"


# --- DockerClient: logs snapshot + streamed command strings ----------------

def test_logs_snapshot_combines_streams_and_flags():
    client, calls = _recording_client(lambda c: FakeResult(stdout="out\n", stderr="err\n"))
    text = client.logs_snapshot("c1", tail=200, timestamps=True)
    assert calls[-1] == "docker logs -t --tail 200 c1"
    assert "out" in text and "err" in text


def test_logs_follow_command_string():
    client, _ = _recording_client(None)
    assert client.logs_follow_command("c1", tail=50, timestamps=True) == \
        "docker logs -f -t --tail 50 c1"
    assert client.logs_follow_command("c1", tail=100, timestamps=False) == \
        "docker logs -f --tail 100 c1"


def test_exec_shell_command_prefers_bash_with_sh_fallback():
    client, _ = _recording_client(None)
    cmd = client.exec_shell_command("c1")
    # PATH-resolved (no hard-coded /bin), single exec, bash preferred.
    assert cmd == (
        "docker exec -it c1 sh -c "
        "'command -v bash >/dev/null 2>&1 && exec bash || exec sh'"
    )


def test_runtime_podman_is_used_in_commands():
    client, calls = _recording_client(lambda c: FakeResult(stdout="{}"), runtime="podman")
    client.stats()
    assert calls[-1] == "podman stats --no-stream --format '{{json .}}'"
    assert client.stats_stream_command() == "podman stats"


# --- DockerClient: runtime detection ---------------------------------------

@pytest.mark.parametrize("stdout,expected", [
    ("docker", "docker"),
    ("podman", "podman"),
    ("", None),
])
def test_detect_runtime(stdout, expected):
    client, _ = _recording_client(lambda c: FakeResult(stdout=stdout))
    assert client.detect_runtime() == expected


# --- sudo support -----------------------------------------------------------

def test_sudo_prefixes_captured_commands_with_sudo_n():
    calls = []

    def run_command(nickname, command, *, timeout=None):
        calls.append(command)
        return FakeResult(stdout="")

    client = DockerClient(run_command, "srv", "docker", use_sudo=True)
    client.ps()
    assert calls[-1] == "sudo -n docker ps -a --format '{{json .}}'"
    client.ping()
    assert calls[-1] == "sudo -n docker ps -q"


def test_sudo_uses_plain_sudo_for_interactive_commands():
    client, _ = _recording_client(None, runtime="docker")
    client.use_sudo = True
    assert client.logs_follow_command("c1", tail=10) == "sudo docker logs -f --tail 10 c1"
    assert client.stats_stream_command() == "sudo docker stats"
    assert client.exec_shell_command("c1") == (
        "sudo docker exec -it c1 sh -c "
        "'command -v bash >/dev/null 2>&1 && exec bash || exec sh'"
    )


def test_no_sudo_when_disabled():
    client, calls = _recording_client(None)
    assert client.use_sudo is False
    client.ping()
    assert calls[-1] == "docker ps -q"


@pytest.mark.parametrize("text,expected", [
    ("Got permission denied while trying to connect to the Docker daemon socket", True),
    ("dial unix /var/run/docker.sock: connect: permission denied", True),
    ("Cannot connect to the Docker daemon", False),
    ("", False),
])
def test_is_permission_error(text, expected):
    assert DockerClient.is_permission_error(text) is expected
