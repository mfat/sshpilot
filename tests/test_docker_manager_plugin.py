"""Tests for the built-in Docker Console plugin.

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
from sshpilot.plugins.builtin.docker_manager.client import (
    DockerClient, DockerError, parse_published_ports)


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

    def run_command(nickname, command, *, timeout=None, **_kwargs):
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
    # Tools-menu "Docker Console" entry opens via an on_activate callback (not a
    # fixed page) so it can target the last-used host.
    manager = [p for p in ctx.ui.pages if p[0] == "manager"]
    assert manager and manager[0][1] == "Docker Console"
    assert callable(manager[0][4].get("on_activate"))
    # Connection right-click action.
    assert ctx.ui.actions and ctx.ui.actions[0][1] == "Docker Console"


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
    connection = types.SimpleNamespace(nickname="beta", protocol="ssh")
    ctx = _fake_ctx(last_host="beta", connections=(connection,))
    plugin = Plugin()
    plugin.activate(ctx)
    on_activate = next(p[4]["on_activate"] for p in ctx.ui.pages if p[0] == "manager")
    on_activate()
    assert "host-beta" in ctx.ui.opened


def test_tools_menu_no_connections_opens_local_console():
    ctx = _fake_ctx(last_host=None, connections=())
    plugin = Plugin()
    plugin.activate(ctx)
    on_activate = next(p[4]["on_activate"] for p in ctx.ui.pages if p[0] == "manager")
    on_activate()
    assert ctx.ui.opened == ["host-__local__"]
    local = next(p for p in ctx.ui.pages if p[0] == "host-__local__")
    assert local[1] == "Docker — Local"


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
    with_opts = client.exec_shell_command("c1", user="root", workdir="/app")
    assert "-u root" in with_opts and "-w /app" in with_opts


def test_runtime_podman_is_used_in_commands():
    client, calls = _recording_client(lambda c: FakeResult(stdout="{}"), runtime="podman")
    client.stats()
    assert calls[-1] == "podman stats --no-stream --format '{{json .}}'"
    assert client.stats_stream_command() == "podman stats"


def test_stats_one_quotes_container_id():
    client, calls = _recording_client(lambda c: FakeResult(
        stdout='{"Name":"web","CPUPerc":"1.2%","MemUsage":"10MiB / 1GiB"}\n'))
    row = client.stats_one("weird;id")
    assert calls[-1] == "docker stats --no-stream --format '{{json .}}' 'weird;id'"
    assert row["Name"] == "web" and row["CPUPerc"] == "1.2%"


def test_events_command_string():
    client, _ = _recording_client(None)
    assert client.events_command() == (
        "docker events --filter type=container --format '{{json .}}'"
    )
    sudo_client, _ = _recording_client(None)
    sudo_client.use_sudo = True
    assert sudo_client.events_command() == (
        "sudo -n docker events --filter type=container --format '{{json .}}'"
    )


def test_logs_follow_stream_command_uses_captured_runtime():
    client, _ = _recording_client(None)
    assert client.logs_follow_stream_command("c1", tail=50) == \
        "docker logs -f --tail 50 c1"
    client.use_sudo = True
    client.sudo_password = "x"
    assert client.logs_follow_stream_command("c1", tail=10).startswith(
        "sudo -S -p '' docker logs -f")


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


def test_create_run_args_command_is_not_shell_injectable():
    client, _ = _recording_client(None)
    # A malicious one-liner must not break out of its argument: every token is
    # shell-quoted, so the ';' and 'rm' become inert literal args.
    args = client.create_run_args("alpine", command="; rm -rf /")
    assert args == "run -d alpine ';' rm -rf /"
    # An argv list is quoted token-by-token too.
    assert client.create_run_args("alpine", command=["sh", "-c", "echo hi; rm x"]) == \
        "run -d alpine sh -c 'echo hi; rm x'"


def test_create_run_args_command_unbalanced_quotes_raises():
    client, _ = _recording_client(None)
    with pytest.raises(ValueError):
        client.create_run_args("alpine", command="sh -c 'unbalanced")


# --- DockerClient: volumes / networks / compose ps -------------------------

def test_volume_and_network_commands():
    client, calls = _recording_client(lambda c: FakeResult(stdout='{"Name":"v1"}'))
    client.remove_volume("v1")
    assert calls[-1] == "docker volume rm v1"
    client.remove_volume("v1", force=True)
    assert calls[-1] == "docker volume rm -f v1"
    assert client.volume_inspect("v1") == {"Name": "v1"}
    assert calls[-1] == "docker volume inspect v1 --format '{{json .}}'"
    client.remove_network("n1")
    assert calls[-1] == "docker network rm n1"
    client.network_inspect("n1")
    assert calls[-1] == "docker network inspect n1 --format '{{json .}}'"


def test_compose_ps_command_and_table_fallback():
    rows = '[{"Service":"web","State":"running"}]'
    client, calls = _recording_client(lambda c: FakeResult(stdout=rows))
    assert client.compose_ps("proj") == [{"Service": "web", "State": "running"}]
    assert calls[-1] == "docker compose -p proj ps --format json"

    table = ("NAME      STATUS\nweb       running\n")

    def responder(cmd):
        if "--format" in cmd:
            return FakeResult(exit_code=1, stderr="unknown flag: --format")
        return FakeResult(stdout=table)

    client2, calls2 = _recording_client(responder)
    out = client2.compose_ps("proj")
    assert calls2[-1] == "docker compose -p proj ps"
    assert out and out[0]["Name"] == "web"


def test_parse_ndjson_logs_skipped_lines(caplog):
    import logging
    client, _ = _recording_client(lambda c: FakeResult(stdout='{"ID":"1"}\nbroken\n'))
    with caplog.at_level(logging.DEBUG,
                         logger="sshpilot.plugins.builtin.docker_manager.client"):
        assert client.ps() == [{"ID": "1"}]
    assert any("failed to parse" in r.message or "unparseable" in r.message
               for r in caplog.records)


def test_client_logs_commands_and_results(caplog):
    """``--verbose`` should surface each docker CLI call and its exit status."""
    import logging

    def responder(command):
        if "ps" in command:
            return FakeResult(exit_code=1, stderr="permission denied")
        return FakeResult(stdout='{"ID":"abc"}\n')

    client, _ = _recording_client(responder)
    with caplog.at_level(logging.DEBUG,
                         logger="sshpilot.plugins.builtin.docker_manager.client"):
        client.images()
        client.ping()
    messages = [r.getMessage() for r in caplog.records]
    assert any("run: docker images" in m for m in messages)
    assert any("exit=0" in m for m in messages)
    assert any("parsed 1 JSON" in m for m in messages)
    assert any("run: docker ps -q" in m for m in messages)
    assert any("exit=1" in m for m in messages)
    assert any("permission denied" in m for m in messages)


def test_detect_runtime_logs_result(caplog):
    import logging
    client, _ = _recording_client(lambda c: FakeResult(stdout="podman\n"))
    with caplog.at_level(logging.DEBUG,
                         logger="sshpilot.plugins.builtin.docker_manager.client"):
        assert client.detect_runtime() == "podman"
    assert any("detected runtime: podman" in r.getMessage()
               for r in caplog.records)


def test_detect_runtime_uses_non_login_shell():
    """Login shells (``sh -lc``) source profiles that hang on non-interactive SSH
    and burn the full 30s timeout before the container list can load."""
    client, calls = _recording_client(lambda c: FakeResult(stdout="docker\n"))
    assert client.detect_runtime() == "docker"
    assert len(calls) == 1
    assert calls[0].startswith("sh -c ")
    assert "sh -lc" not in calls[0]


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


def test_sudo_password_mode_feeds_password_to_captured_sudo_s():
    """Password-required sudo: captured commands use ``sudo -S`` and the password
    is fed over stdin (never on the command line)."""
    calls = []

    def run_command(nickname, command, *, timeout=None, input=None):
        calls.append((command, input))
        return FakeResult(stdout="")

    client = DockerClient(run_command, "srv", "docker",
                          use_sudo=True, sudo_password="s3cret")
    client.ping()
    command, stdin = calls[-1]
    assert command == "sudo -S -p '' docker ps -q"
    assert stdin == "s3cret\n"
    # The password must not leak onto the command line.
    assert "s3cret" not in command


def test_sudo_password_mode_read_file_uses_sudo_s():
    calls = []

    def run_command(nickname, command, *, timeout=None, input=None):
        calls.append((command, input))
        return FakeResult(stdout="data")

    client = DockerClient(run_command, "srv", "docker",
                          use_sudo=True, sudo_password="pw")
    assert client.read_file("/etc/x.yml") == "data"
    command, stdin = calls[-1]
    assert command == "sudo -S -p '' cat /etc/x.yml"
    assert stdin == "pw\n"


def test_sudo_password_mode_interactive_uses_dash_p_sentinel():
    """Interactive commands use ``sudo -p <sentinel>`` so the terminal can detect
    the prompt and auto-type the password — the password is NOT in the string."""
    client = DockerClient(lambda *a, **k: FakeResult(), "srv", "docker",
                          use_sudo=True, sudo_password="pw")
    sentinel = DockerClient.SUDO_PROMPT
    assert client.exec_shell_command("c1") == (
        f"sudo -p '{sentinel}' docker exec -it c1 sh -c "
        "'command -v bash >/dev/null 2>&1 && exec bash || exec sh'"
    )
    assert client.logs_follow_command("c1", tail=5) == (
        f"sudo -p '{sentinel}' docker logs -f --tail 5 c1"
    )
    assert client.stats_stream_command() == f"sudo -p '{sentinel}' docker stats"
    assert "pw" not in client.exec_shell_command("c1")


def test_passwordless_sudo_still_uses_sudo_n_without_stdin():
    """use_sudo without a password keeps the existing ``sudo -n`` path and feeds
    no stdin (so run_command implementations without ``input`` keep working)."""
    calls = []

    def run_command(nickname, command, *, timeout=None):  # no ``input`` kwarg
        calls.append(command)
        return FakeResult(stdout="")

    client = DockerClient(run_command, "srv", "docker", use_sudo=True)
    client.ping()  # would raise TypeError if input= were passed
    assert calls[-1] == "sudo -n docker ps -q"


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
        run_local_command=lambda *a, **k: FakeResult(),
        run_command_stream=lambda *a, **k: _fake_stream_handle(),
        run_local_command_stream=lambda *a, **k: _fake_stream_handle(),
        run_on_ui_thread=lambda fn, *a: fn(*a),
        settings=types.SimpleNamespace(get=lambda k, d=None: d, set=lambda k, v: None),
        list_connections=lambda: [Conn("web"), Conn("db")],
        open_command_terminal=lambda *a, **k: True,
        open_local_command_terminal=lambda *a, **k: True,
        ui=types.SimpleNamespace(notify=lambda *a, **k: None),
    )


def _fake_stream_handle():
    class _H:
        def stop(self):
            pass

        @property
        def running(self):
            return False

    return _H()


def test_page_builds_all_tabs():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    names = []
    child = page._stack.get_first_child()
    while child is not None:
        names.append(page._stack.get_page(child).get_name())
        child = child.get_next_sibling()
    assert names == ["containers", "logs", "stats", "images",
                     "volumes", "networks", "compose"]


def test_page_hardening_widgets_and_helpers():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    # Pause toggle defaults off; toggling sets the paused flag.
    assert page._pause_btn.get_active() is False
    page._pause_btn.set_active(True)
    assert page._paused is True
    # Default refresh interval is the safer 10s (not the old 3s).
    assert page._refresh_interval() == 10
    # Runtime override dropdown present, defaults to Auto.
    assert page._RUNTIME_MODES[page._runtime_drop.get_selected()] == "Auto"
    # Health parsing from the ps Status string.
    assert page._health_of("Up 2 hours (healthy)") == "healthy"
    assert page._health_of("Up (unhealthy)") == "unhealthy"
    assert page._health_of("Up 3 hours") is None
    # Container search filters the cached list (non-matching rows stay hidden).
    page._containers = [{"Names": "web", "Image": "nginx", "ID": "a1",
                         "Status": "Up", "State": "running"},
                        {"Names": "db", "Image": "postgres", "ID": "b2",
                         "Status": "Up", "State": "running"}]
    page._container_query = "postgres"
    page._render_containers()
    n = 0
    ch = page._containers_list.get_first_child()
    while ch is not None:
        if ch.get_visible():
            n += 1
        ch = ch.get_next_sibling()
    assert n == 1


def test_dismiss_active_confirm_bumps_generation():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    gen0 = page._confirm_gen
    page._active_confirm_dialog = object()
    page._dismiss_active_confirm()
    assert page._confirm_gen == gen0 + 1
    assert page._active_confirm_dialog is None


def test_confirm_handler_ignored_after_dismiss():
    """Generation guard used by ``_confirm`` — no GTK needed."""
    confirm_gen = 1
    gen = 1
    called = []

    def on_response(_d, response):
        nonlocal confirm_gen
        if gen != confirm_gen:
            return
        if response == "ok":
            called.append(True)

    on_response(None, "ok")
    assert called == [True]
    confirm_gen += 1  # simulates _dismiss_active_confirm / host switch
    on_response(None, "ok")
    assert called == [True]


def test_settings_dialog_exposes_refresh_interval():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.dialogs import DockerConsoleSettingsDialog

    seen = {}
    dlg = DockerConsoleSettingsDialog(
        None, reuse_ssh=True, on_reuse_ssh_changed=lambda v: None,
        refresh_interval=12, on_refresh_interval_changed=lambda n: seen.__setitem__("n", n))
    assert dlg._interval_row.get_value() == 12


def test_create_dialog_rejects_unbalanced_command():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.dialogs import CreateContainerDialog

    called = []
    dlg = CreateContainerDialog(None, ["nginx"], ["bridge"], lambda s: called.append(s))
    dlg._image_row.set_text("nginx")
    dlg._command_row.set_text("sh -c 'unbalanced")
    dlg._on_create_clicked(None)
    assert not called  # dialog stays open, on_create not invoked
    assert dlg._command_row.has_css_class("error")


def test_page_host_button_shows_initial_host():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    class Conn:
        def __init__(self, n):
            self.nickname = n
            self.protocol = "ssh"

    ctx = types.SimpleNamespace(
        run_command=lambda *a, **k: FakeResult(),
        run_on_ui_thread=lambda fn, *a: fn(*a),
        settings=types.SimpleNamespace(get=lambda k, d=None: d, set=lambda k, v: None),
        list_connections=lambda: [Conn("alpha"), Conn("beta")],
        open_command_terminal=lambda *a, **k: True,
        ui=types.SimpleNamespace(notify=lambda *a, **k: None),
    )
    page = DockerConsolePage(ctx, initial_host="beta")
    assert page._selected_nick == "beta"
    assert page._host_label.get_label() == "beta"


def test_page_reuse_ssh_toggle_defaults_on_and_persists():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

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
    page = DockerConsolePage(ctx, initial_host="web")
    # Default ON.
    assert page._multiplex_enabled() is True
    # Acquire keeps the master for the current host.
    page._acquire_multiplex("web")
    assert acquired == ["web"] and page._mux_nick == "web"
    # Toggling off persists the setting and releases.
    page._set_multiplex_enabled(False)
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
    groups = ContainerDetailsDialog._build_overview_groups(data)
    titles = [g.get_title() for g in groups]
    assert titles == ["General", "State", "Ports", "Networks",
                      "Mounts / volumes", "Labels"]
    assert ContainerDetailsDialog._env_pairs(data) == [("TZ", "UTC")]


def test_settings_dialog_reuse_ssh_switch():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.dialogs import (
        DockerConsoleSettingsDialog)

    changes = []
    dlg = DockerConsoleSettingsDialog(
        None, reuse_ssh=True, on_reuse_ssh_changed=changes.append)
    assert dlg._reuse_row.get_active() is True
    assert dlg._reuse_row.get_title() == "Reuse SSH connection"
    dlg._reuse_row.set_active(False)
    assert changes == [False]


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


def _sudo_ctx(settings_store):
    """A page ctx whose settings are backed by a real dict (so ``sudo:<nick>``
    sticks) — used to exercise the sudo probe routing."""
    class Conn:
        def __init__(self, n):
            self.nickname = n
            self.protocol = "ssh"

    return types.SimpleNamespace(
        run_command=lambda *a, **k: FakeResult(),
        run_on_ui_thread=lambda fn, *a: fn(*a),
        settings=types.SimpleNamespace(
            get=lambda k, d=None: settings_store.get(k, d),
            set=lambda k, v: settings_store.__setitem__(k, v)),
        list_connections=lambda: [Conn("web"), Conn("db")],
        open_command_terminal=lambda *a, **k: True,
        ui=types.SimpleNamespace(notify=lambda *a, **k: None),
    )


def test_resolve_sudo_prompts_when_password_required():
    """The probe must ask for a password whenever `sudo -n` is denied — even on
    a host where plain docker works — instead of silently running `sudo -n`."""
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_sudo_ctx({}), initial_host="web")

    def rc(nick, command, *, timeout=None, input=None):
        if command.startswith("sudo -n"):
            return FakeResult(exit_code=1, stderr="sudo: a password is required")
        if command.startswith("sudo -S"):
            return FakeResult() if input == "secret\n" else FakeResult(exit_code=1)
        return FakeResult()

    # No cached/stored password yet -> prompt the user.
    assert page._resolve_sudo(rc, "web", "docker") == (True, "needs_password", None)
    # A cached password that verifies via `sudo -S` is reported back for caching.
    page._sudo_passwords["web"] = "secret"
    assert page._resolve_sudo(
        rc, "web", "docker", session_pw="secret") == (True, "password", "secret")


def test_probe_pending_blocks_auto_refresh_until_sudo_ready(monkeypatch):
    """Map-time timer must not list containers with ``sudo -n`` before the
    host probe has resolved a required sudo password (first-load race)."""
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    settings = {"sudo:web": True}
    page = DockerConsolePage(_sudo_ctx(settings), initial_host="web")
    refreshes = []
    monkeypatch.setattr(
        page, "_refresh_containers", lambda: refreshes.append("containers"))

    # Probe in flight (as after map → _on_host_changed): ticks must no-op.
    page._probe_pending = True
    assert page._tick() is True
    page._refresh_visible()
    assert refreshes == []

    # After the probe caches the password and clears the gate, listing runs
    # with ``sudo -S`` (via _client reading _sudo_passwords).
    page._sudo_passwords["web"] = "secret"
    page._finish_probe_and_refresh()
    assert page._probe_pending is False
    assert refreshes == ["containers"]
    client = page._client()
    assert client is not None
    assert client.use_sudo is True
    assert client.sudo_password == "secret"


def test_resolve_sudo_passwordless():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_sudo_ctx({}), initial_host="web")
    # `sudo -n` succeeds -> passwordless, no prompt, no cached password.
    rc = lambda *a, **k: FakeResult()
    assert page._resolve_sudo(rc, "web", "docker") == (True, None, None)


def test_resolve_sudo_not_sudoers():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_sudo_ctx({}), initial_host="web")

    def rc(nick, command, *, timeout=None, input=None):
        if command.startswith("sudo -n"):
            return FakeResult(
                exit_code=1,
                stderr="webuser is not in the sudoers file.  This incident will be reported.",
            )
        return FakeResult()

    assert page._resolve_sudo(rc, "web", "docker") == (True, "not_sudoers", None)


def test_resolve_sudo_clears_stale_keyring_on_failed_verify(monkeypatch):
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_sudo_ctx({}), initial_host="web")
    cleared = []

    def rc(nick, command, *, timeout=None, input=None):
        if command.startswith("sudo -n"):
            return FakeResult(exit_code=1, stderr="sudo: a password is required")
        if command.startswith("sudo -S"):
            return FakeResult(exit_code=1, stderr="Sorry, try again.")
        return FakeResult()

    monkeypatch.setattr(
        page, "_lookup_stored_sudo", lambda nick: "stale")
    monkeypatch.setattr(
        page, "_clear_stored_sudo", lambda nick: cleared.append(nick))

    assert page._resolve_sudo(rc, "web", "docker") == (True, "needs_password", None)
    assert cleared == ["web"]


@pytest.mark.parametrize("text,expected", [
    ("user is not in the sudoers file", True),
    ("is not allowed to execute", True),
    ("sudo: a password is required", False),
    ("Sorry, try again.", False),
])
def test_is_sudo_denied_error(text, expected):
    assert DockerClient.is_sudo_denied_error(text) is expected


def test_check_sudo():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    def rc_ok(nick, command, *, timeout=None, input=None):
        return FakeResult()

    def rc_wrong(nick, command, *, timeout=None, input=None):
        return FakeResult(exit_code=1, stderr="Sorry, try again.")

    def rc_denied(nick, command, *, timeout=None, input=None):
        return FakeResult(exit_code=1, stderr="user is not in the sudoers file")

    assert DockerConsolePage._check_sudo(
        rc_ok, "web", "docker", "good") == (True, None)
    assert DockerConsolePage._check_sudo(
        rc_wrong, "web", "docker", "bad") == (False, "wrong_password")
    assert DockerConsolePage._check_sudo(
        rc_denied, "web", "docker", "bad") == (False, "not_sudoers")


# --- ANSI stripping (Logs tab TextView cannot render escapes) --------------

def test_strip_ansi_removes_color_codes():
    from sshpilot.plugins.builtin.docker_manager import widgets as w

    raw = (
        "\x1b[36mwebpack_version=\x1b[0m5.105.0\n"
        "\x1b[90m2026/07/17 02:13PM\x1b[0m \x1b[32mINF\x1b[0m "
        "\x1b[1mgithub.com/portainer/portainer/api/http/server.go:371\x1b[0m"
        "\x1b[36m >\x1b[0m starting HTTPS server | "
        "\x1b[36mbind_address=\x1b[0m:9443"
    )
    clean = w.strip_ansi(raw)
    assert "\x1b" not in clean
    assert "webpack_version=5.105.0" in clean
    assert "starting HTTPS server" in clean
    assert "bind_address=:9443" in clean
    assert "INF" in clean


def test_strip_ansi_empty_and_plain():
    from sshpilot.plugins.builtin.docker_manager import widgets as w

    assert w.strip_ansi("") == ""
    assert w.strip_ansi(None) == ""  # type: ignore[arg-type]
    assert w.strip_ansi("plain log line") == "plain log line"


# --- failure placeholder / toast truncation --------------------------------

def test_truncate_message_keeps_short_text():
    from sshpilot.plugins.builtin.docker_manager import widgets as w

    display, truncated = w.truncate_message("permission denied", 480)
    assert display == "permission denied"
    assert truncated is False


def test_truncate_message_clips_long_log():
    from sshpilot.plugins.builtin.docker_manager import widgets as w

    log = "\n".join(f"error line {i}: boom" for i in range(80))
    display, truncated = w.truncate_message(log, 480)
    assert truncated is True
    assert display.endswith("…")
    assert len(display) < len(log)


def test_truncate_toast_collapses_newlines():
    from sshpilot.plugins.builtin.docker_manager import widgets as w

    toast = w.truncate_toast("failed:\n" + ("x" * 400))
    assert "\n" not in toast
    assert toast.endswith("…")


def test_placeholder_error_shows_human_summary():
    """Failures show a parsed human summary (plain selectable label, no framed
    log box); the raw output expands inline via the "Show detailed log" toggle."""
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    ph = page._containers_placeholder
    assert ph.has_css_class("docker-console-placeholder")
    assert ph.get_halign().value_nick == "fill"
    assert ph.get_valign().value_nick == "fill"

    raw = ("debug1: OpenSSH_10.2p1 Ubuntu\n"
           "debug1: Reading configuration data /home/u/.ssh/config\n"
           "ash: docker: not found")
    page._set_placeholder_idle(ph, raw, error=True)
    assert ph.get_visible()
    assert ph.get_title() == "Docker isn't installed on this host"
    assert ph.has_css_class("docker-error")
    assert ph._full_text == raw          # raw log behind the toggle
    assert ph._details_btn.get_visible()
    assert ph._details_btn.get_label() == "Show detailed log"
    assert ph.get_can_target() is True

    # Toggling expands the raw log inline (no separate window, no framed
    # box — a plain selectable label like the status feed).
    assert not ph._detail_frame.get_visible()
    ph._details_btn.set_active(True)
    assert ph._detail_frame.get_visible()
    assert ph._details_btn.get_label() == "Hide detailed log"
    assert "ash: docker: not found" in ph._detail_lbl.get_text()
    assert ph._detail_lbl.get_selectable() is True

    # Auto-refresh repainting the SAME failure is a no-op: the expanded log
    # must stay open instead of collapsing every tick.
    page._set_placeholder_idle(ph, raw, error=True)
    assert ph._detail_frame.get_visible()
    assert ph._details_btn.get_active() is True

    # A message that IS already the summary needs no details toggle; the
    # previous expansion is collapsed again.
    page._set_placeholder_idle(ph, "Connection timed out", error=True)
    assert ph._details_btn.get_visible() is False
    assert ph._details_btn.get_active() is False
    assert not ph._detail_frame.get_visible()
    assert ph.get_can_target() is True

    page._set_placeholder_idle(ph, "No containers")
    assert ph._details_btn.get_visible() is False
    assert not ph.has_css_class("docker-error")
    assert ph.get_title() == "No containers"
    assert ph.get_can_target() is False

    # The crossfade revealer spans the overlay even when unrevealed — it must
    # never intercept scroll/clicks meant for the list (scrollability bug).
    page._hide_placeholder(ph)
    assert ph._revealer.get_can_target() is False
    page._set_placeholder_idle(ph, "boom", error=True)
    assert ph._revealer.get_can_target() is True


def test_text_view_dialog_is_selectable():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.dialogs import TextViewDialog

    dlg = TextViewDialog(None, "Error details", "copy me\nline 2")
    assert dlg._view.get_editable() is False
    assert dlg._view.get_cursor_visible() is True
    buf = dlg._view.get_buffer()
    start, end = buf.get_bounds()
    assert "copy me" in buf.get_text(start, end, False)


# --- loading status feed under the Docker mark ------------------------------

def test_describe_docker_failure_parses_real_output():
    from sshpilot.plugins.builtin.docker_manager import widgets as w

    cases = [
        ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock. "
         "Is the docker daemon running?", "daemon isn't running"),
        ("bash: docker: command not found", "isn't installed"),
        ("dial unix /var/run/docker.sock: connect: permission denied",
         "sudo or the docker group"),
        ("ssh: Could not resolve hostname web: Name or service not known",
         "resolve the host name"),
        ("connect to host web port 22: Connection refused", "refused"),
        ("connect to host web port 22: Connection timed out", "timed out"),
    ]
    for raw, expected_fragment in cases:
        assert expected_fragment in w.describe_docker_failure(raw)
    # Unknown output falls back to its first line, never invented text.
    assert w.describe_docker_failure("some odd error\nmore") == "some odd error"
    assert w.describe_docker_failure("") == "Command failed"
    # SSH -v chatter is never mistaken for the failure itself.
    noisy = ("debug1: OpenSSH_10.2p1 Ubuntu\n"
             "debug1: auto-mux: Trying existing master\n"
             "ash: podman: not found")
    assert w.describe_docker_failure(noisy) == "Podman isn't installed on this host"


def test_status_feed_shows_under_mark_while_loading():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    ph = page._containers_placeholder
    lbl = ph._status_lbl
    assert lbl.get_selectable() is True   # mouse-selectable
    assert not lbl.get_visible()          # hidden until a load starts

    page._status("Connecting to web…")
    page._status("Docker found")
    page._set_placeholder_loading(ph, "Loading containers…")
    assert lbl.get_visible()
    assert ph.get_can_target() is True    # selectable while loading
    assert "Connecting to web…" in lbl.get_text()
    assert "Docker found" in lbl.get_text()

    # Feed is shared: every placeholder mirrors the same lines.
    assert "Docker found" in page._images_placeholder._status_lbl.get_text()

    # Capped to the last few stage messages.
    for i in range(20):
        page._status(f"stage {i}")
    assert "stage 19" in lbl.get_text()
    assert "Connecting to web…" not in lbl.get_text()

    page._set_placeholder_idle(ph, "No containers")
    assert not lbl.get_visible()

    page._clear_status()
    assert lbl.get_text() == ""


def test_containers_sync_updates_in_place():
    """Polling refresh should reuse list rows when state/actions are unchanged."""
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    page._containers = [
        {"Names": "web", "Image": "nginx", "ID": "a1",
         "Status": "Up 1 second", "State": "running"},
    ]
    page._sync_containers_list()
    first = page._containers_list.get_first_child()
    page._containers[0]["Status"] = "Up 2 minutes"
    page._sync_containers_list()
    assert page._containers_list.get_first_child() is first
    box = first.get_child()
    assert box._sub_lbl.get_text().startswith("nginx · Up 2 minutes")


def test_loaded_flags_suppress_repulse():
    """Auto-refresh of an already-loaded (even empty) view must not re-pulse —
    otherwise the logo flaps every tick."""
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    assert page._containers_loaded is False
    page._on_containers([], None, page._load_gen)  # empty-but-successful load
    assert page._containers_loaded is True
    page._on_stats([], None, page._load_gen)
    assert page._stats_loaded is True


def test_logs_dropdown_selection_loads_logs():
    """Picking a container in the Logs dropdown loads its logs without the
    Load button; poll-driven model refreshes neither re-trigger nor lose
    the selection."""
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    page._containers = [{"Names": "web", "ID": "a1"}, {"Names": "db", "ID": "b2"}]
    calls = []
    page._reload_logs = lambda: calls.append(page._selected_container_id())

    page._refresh_logs_targets()      # programmatic model swap — must not load
    assert calls == []

    page._logs_combo.set_selected(1)  # user picks "db"
    assert calls == ["b2"]
    assert page._logs_raw == ""

    page._refresh_logs_targets()      # containers poll: keeps pick, no reload
    assert calls == ["b2"]
    assert page._logs_combo.get_selected() == 1


def test_shared_selection_drives_bar_and_logs_combo():
    """Containers list selection updates the selection bar and Logs combo."""
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    page._containers = [
        {"Names": "web", "ID": "a1", "Image": "nginx", "Status": "Up", "State": "running"},
        {"Names": "db", "ID": "b2", "Image": "postgres", "Status": "Up", "State": "running"},
    ]
    page._refresh_logs_targets()
    page._set_selected_container("b2", "db", source="containers")
    assert page._selected_cid == "b2"
    assert "db" in page._sel_label.get_label()
    assert page._logs_combo.get_selected() == 1


def test_on_docker_event_line_triggers_refresh():
    _gtk_or_skip()
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    page = DockerConsolePage(_gtk_ctx(), initial_host="web")
    calls = []
    page._refresh_containers = lambda: calls.append("refresh")
    page._containers_busy = False
    page._on_docker_event_line(
        '{"Type":"container","status":"start","Actor":{"Attributes":{"name":"web"}}}')
    assert calls == ["refresh"]
    page._on_docker_event_line('{"Type":"image","status":"pull"}')
    assert calls == ["refresh"]  # image events ignored


# --- published-port discovery (web UIs) --------------------------------------

def test_parse_published_ports():
    # IPv4/IPv6 duplicates collapse to one entry.
    assert parse_published_ports(
        "0.0.0.0:8080->80/tcp, :::8080->80/tcp") == [(8080, 80, "http")]
    # Conventional TLS ports get an https scheme.
    assert parse_published_ports("0.0.0.0:443->8443/tcp") == [(443, 8443, "https")]
    # UDP and unpublished (exposed-only) entries are skipped.
    assert parse_published_ports("0.0.0.0:53->53/udp, 80/tcp") == []
    # Podman-style entry without a host address still counts; sorted by host port.
    assert parse_published_ports("9000->9000/tcp, 0.0.0.0:3000->3000/tcp") == [
        (3000, 3000, "http"), (9000, 9000, "http")]
    # Garbage and empty input are tolerated.
    assert parse_published_ports("") == []
    assert parse_published_ports(None) == []
    assert parse_published_ports("not ports at all") == []
