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


# --- DockerClient: details / images / compose / create ----------------------

def test_inspect_returns_first_object():
    obj = '{"Id":"abc","Name":"/web","Config":{"Image":"nginx"}}'
    client, calls = _recording_client(lambda c: FakeResult(stdout=obj))
    data = client.inspect("abc")
    assert calls[-1] == "docker inspect abc --format '{{json .}}'"
    assert data["Name"] == "/web" and data["Config"]["Image"] == "nginx"


def test_inspect_empty_on_no_output():
    client, _ = _recording_client(lambda c: FakeResult(stdout=""))
    assert client.inspect("abc") == {}


def test_image_history_command_and_parse():
    out = '{"CreatedBy":"RUN apk add","Size":"5MB"}\n{"CreatedBy":"CMD","Size":"0B"}'
    client, calls = _recording_client(lambda c: FakeResult(stdout=out))
    rows = client.image_history("nginx:latest")
    assert calls[-1] == "docker history --no-trunc --format '{{json .}}' nginx:latest"
    assert rows[0]["Size"] == "5MB" and len(rows) == 2


def test_image_prune_command():
    client, calls = _recording_client(None)
    client.image_prune()
    assert calls[-1] == "docker image prune -f"


def test_pull_command_string():
    client, _ = _recording_client(None)
    assert client.pull_command("nginx:latest") == "docker pull nginx:latest"
    assert client.pull_command("weird;rm") == "docker pull 'weird;rm'"


def test_compose_ls_command_and_parse():
    out = '[{"Name":"stack1","Status":"running(2)","ConfigFiles":"/srv/dc.yml"}]'
    client, calls = _recording_client(lambda c: FakeResult(stdout=out))
    rows = client.compose_ls()
    assert calls[-1] == "docker compose ls --all --format json"
    assert rows == [{"Name": "stack1", "Status": "running(2)", "ConfigFiles": "/srv/dc.yml"}]


def test_compose_ls_falls_back_without_all_flag():
    out = '[{"Name":"stack1","Status":"running"}]'

    def responder(cmd):
        if "--all" in cmd:
            return FakeResult(exit_code=1, stderr="unknown flag: --all")
        return FakeResult(stdout=out)

    client, calls = _recording_client(responder)
    rows = client.compose_ls()
    assert calls[-2] == "docker compose ls --all --format json"
    assert calls[-1] == "docker compose ls --format json"  # retried without --all
    assert rows == [{"Name": "stack1", "Status": "running"}]


def test_compose_ls_falls_back_to_table_when_no_format_flag():
    table = ("NAME       STATUS              CONFIG FILES\n"
             "stack1     running(2)          /srv/dc.yml\n"
             "stack2     exited(1)           /opt/app/compose.yml\n")

    def responder(cmd):
        if "--format" in cmd:
            return FakeResult(exit_code=1, stderr="unknown flag: --format")
        if "--all" in cmd:
            return FakeResult(exit_code=1, stderr="unknown flag: --all")
        return FakeResult(stdout=table)

    client, calls = _recording_client(responder)
    rows = client.compose_ls()
    assert calls[-1] == "docker compose ls"  # degraded all the way to plain table
    assert rows == [
        {"Name": "stack1", "Status": "running(2)", "ConfigFiles": "/srv/dc.yml"},
        {"Name": "stack2", "Status": "exited(1)", "ConfigFiles": "/opt/app/compose.yml"},
    ]


def test_compose_ls_raises_on_real_error():
    client, _ = _recording_client(
        lambda c: FakeResult(exit_code=1, stderr="Cannot connect to the Docker daemon"))
    with pytest.raises(DockerError, match="Cannot connect"):
        client.compose_ls()


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_compose_action_commands(action):
    client, calls = _recording_client(None)
    client.compose("my proj", action)
    assert calls[-1] == f"docker compose -p 'my proj' {action}"


def test_compose_rejects_unknown_action():
    client, _ = _recording_client(None)
    with pytest.raises(ValueError):
        client.compose("p", "up")


def test_compose_up_and_down_command_strings():
    client, _ = _recording_client(None)
    assert client.compose_up_command("/srv/dc.yml") == \
        "docker compose -f /srv/dc.yml up -d"
    assert client.compose_down_command("stack1") == \
        "docker compose -p stack1 down"


def test_read_file_is_raw_cat_not_runtime_prefixed():
    client, calls = _recording_client(lambda c: FakeResult(stdout="services: {}\n"))
    text = client.read_file("/srv/dc.yml")
    assert calls[-1] == "cat /srv/dc.yml"  # no 'docker' prefix
    assert text == "services: {}\n"


def test_read_file_raises_on_error():
    client, _ = _recording_client(lambda c: FakeResult(exit_code=1, stderr="No such file"))
    with pytest.raises(DockerError, match="No such file"):
        client.read_file("/nope")


def test_create_run_args_quotes_all_fields():
    client, _ = _recording_client(None)
    args = client.create_run_args(
        "nginx:latest", name="my web", ports=[("8080", "80")],
        volumes=[("/host data", "/var/www")], envs=[("TZ", "Europe/Berlin")],
        restart="always", command="nginx -g 'daemon off;'")
    assert args == (
        "run -d --name 'my web' -p 8080:80 -v '/host data:/var/www' "
        "-e TZ=Europe/Berlin --restart always nginx:latest nginx -g 'daemon off;'")


def test_create_run_args_minimal_and_restart_no_omitted():
    client, _ = _recording_client(None)
    assert client.create_run_args("alpine", restart="no") == "run -d alpine"
    assert client.create_run_args("alpine") == "run -d alpine"


def test_create_container_runs_via_exec():
    client, calls = _recording_client(None)
    client.create_container("nginx", name="web", ports=[("8080", "80")])
    assert calls[-1] == "docker run -d --name web -p 8080:80 nginx"


def test_create_container_with_sudo():
    client, calls = _recording_client(None)
    client.use_sudo = True
    client.create_container("nginx")
    assert calls[-1] == "sudo -n docker run -d nginx"


def test_networks_command_and_parse():
    out = '{"Name":"bridge","Driver":"bridge"}\n{"Name":"mynet","Driver":"bridge"}'
    client, calls = _recording_client(lambda c: FakeResult(stdout=out))
    rows = client.networks()
    assert calls[-1] == "docker network ls --format '{{json .}}'"
    assert [n["Name"] for n in rows] == ["bridge", "mynet"]


def test_create_run_args_interactive_tty():
    client, _ = _recording_client(None)
    assert client.create_run_args("alpine", interactive=True, tty=True) == \
        "run -d -i -t alpine"


def test_create_run_args_network_skips_default_bridge():
    client, _ = _recording_client(None)
    # The default bridge network is omitted (kept minimal)…
    assert client.create_run_args("nginx", network="bridge") == "run -d nginx"
    # …but host/none/custom are passed through.
    assert client.create_run_args("nginx", network="host") == \
        "run -d --network host nginx"


def test_create_run_args_user_memory_cpus_quoted():
    client, _ = _recording_client(None)
    assert client.create_run_args(
        "nginx", user="1000:1000", memory="512m", cpus="1.5") == \
        "run -d --user 1000:1000 --memory 512m --cpus 1.5 nginx"


def test_create_run_args_advanced_flag_order():
    client, _ = _recording_client(None)
    args = client.create_run_args(
        "nginx:latest", name="web", network="mynet", interactive=True, tty=True,
        user="app", memory="1g", cpus="2", ports=[("8080", "80")],
        volumes=[("/data", "/var/www")], envs=[("TZ", "UTC")], restart="always",
        command="nginx -g 'daemon off;'")
    assert args == (
        "run -d -i -t --name web --network mynet --user app --memory 1g --cpus 2 "
        "-p 8080:80 -v /data:/var/www -e TZ=UTC --restart always nginx:latest "
        "nginx -g 'daemon off;'")


def test_create_container_passes_advanced_kwargs():
    client, calls = _recording_client(None)
    client.create_container("alpine", interactive=True, tty=True, memory="256m")
    assert calls[-1] == "docker run -d -i -t --memory 256m alpine"


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


# --- real-GTK page + dialogs (skipped if GTK can't init headless) -----------

def _gtk_or_skip():
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"GTK unavailable: {exc}")
    # tests/conftest.py stubs `gi` with dummy modules when real GTK isn't present
    # (CI's slim image). Detect that — a real binding exposes MAJOR_VERSION as an
    # int — and skip rather than construct dummy widgets that lack methods.
    if not isinstance(getattr(Gtk, "MAJOR_VERSION", None), int):
        pytest.skip("gi is stubbed in this environment (no real GTK)")
    if not Gtk.init_check():
        pytest.skip("GTK cannot initialise (no display)")
    Adw.init()  # register Adw widget types (the app does this via Adw.Application)


def _gtk_ctx():
    class Conn:
        def __init__(self, n):
            self.nickname = n
            self.protocol = "ssh"

    return types.SimpleNamespace(
        run_command=lambda *a, **k: FakeResult(),
        run_on_ui_thread=lambda fn, *a: fn(*a),
        settings=types.SimpleNamespace(get=lambda k, d=None: d, set=lambda k, v: None),
        list_connections=lambda: [Conn("web"), Conn("db")],
        open_command_terminal=lambda *a, **k: True,
        ui=types.SimpleNamespace(notify=lambda *a, **k: None),
    )


def test_page_builds_five_tabs():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerManagerPage

    page = DockerManagerPage(_gtk_ctx(), initial_host="web")
    names = []
    child = page._stack.get_first_child()
    while child is not None:
        names.append(page._stack.get_page(child).get_name())
        child = child.get_next_sibling()
    assert names == ["containers", "logs", "stats", "images", "compose"]


def test_page_reuse_ssh_toggle_defaults_on_and_persists():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerManagerPage

    store = {}
    acquired, released = [], []

    class Conn:
        def __init__(self, n):
            self.nickname = n
            self.protocol = "ssh"

    ctx = types.SimpleNamespace(
        run_command=lambda *a, **k: FakeResult(),
        run_on_ui_thread=lambda fn, *a: fn(*a),
        settings=types.SimpleNamespace(
            get=lambda k, d=None: store.get(k, d), set=store.__setitem__),
        list_connections=lambda: [Conn("web")],
        open_command_terminal=lambda *a, **k: True,
        acquire_multiplex=lambda n: acquired.append(n),
        release_multiplex=lambda n: released.append(n),
        ui=types.SimpleNamespace(notify=lambda *a, **k: None),
    )
    page = DockerManagerPage(ctx, initial_host="web")
    # Default ON.
    assert page._mux_check.get_active() is True
    # Acquire keeps the master for the current host.
    page._acquire_multiplex("web")
    assert acquired == ["web"] and page._mux_nick == "web"
    # Toggling off persists the setting and releases.
    page._mux_check.set_active(False)
    assert store.get("controlmaster") is False
    assert released == ["web"] and page._mux_nick is None


def test_details_dialog_renders_inspect_data():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.dialogs import ContainerDetailsDialog

    data = {
        "Name": "/web", "Id": "abc123def456",
        "Config": {"Image": "nginx", "Env": ["TZ=UTC"], "Labels": {"role": "web"}},
        "State": {"Status": "running"},
        "HostConfig": {"RestartPolicy": {"Name": "always"},
                       "PortBindings": {"80/tcp": [{"HostIp": "0.0.0.0", "HostPort": "8080"}]}},
        "NetworkSettings": {"Networks": {"bridge": {}}},
        "Mounts": [{"Source": "/data", "Destination": "/var/www"}],
    }
    groups = ContainerDetailsDialog._build_groups(data)
    titles = [g.get_title() for g in groups]
    assert titles == ["General", "State", "Ports", "Networks",
                      "Mounts / volumes", "Environment", "Labels"]


def test_create_and_textview_dialogs_construct():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.dialogs import (
        CreateContainerDialog, TextViewDialog)

    captured = {}
    dlg = CreateContainerDialog(None, ["nginx:latest"], ["bridge", "host", "mynet"],
                                lambda spec: captured.update(spec))
    # The image row is prefilled from the supplied image list.
    assert dlg._image_row.get_text() == "nginx:latest"
    # The advanced Network picker is populated and defaults to bridge.
    assert dlg._networks == ["bridge", "host", "mynet"]
    assert dlg._network_row.get_selected() == 0
    TextViewDialog(None, "title", "body text")  # constructs without error
