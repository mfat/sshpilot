"""GUI regressions for Docker Console SSH password preflight."""

import types

import pytest

from tests._gui_harness import requires_gui


requires_gui()

pytestmark = pytest.mark.gui


class _Result:
    exit_code = 0
    stdout = ""
    stderr = ""


def _password_auth_ctx(*, stored_password=None):
    class Connection:
        nickname = "web"
        protocol = "ssh"
        auth_method = 1
        hostname = "example.test"
        username = "alice"
        password = None

    connection = Connection()
    connection_info = types.SimpleNamespace(
        nickname="web",
        protocol="ssh",
        host="example.test",
        username="alice",
    )
    manager = types.SimpleNamespace(
        find_connection_by_nickname=lambda _nick: connection,
        get_connection_password=lambda _connection: stored_password,
    )
    ctx = types.SimpleNamespace(
        connection_manager=manager,
        run_command=lambda *args, **kwargs: _Result(),
        run_on_ui_thread=lambda fn, *args: fn(*args),
        settings=types.SimpleNamespace(
            get=lambda key, default=None: default,
            set=lambda key, value: None,
        ),
        # Plugin APIs return a ConnectionInfo snapshot without auth_method;
        # password preflight must resolve the authoritative saved Connection.
        list_connections=lambda: [connection_info],
        open_command_terminal=lambda *args, **kwargs: True,
        ui=types.SimpleNamespace(notify=lambda *args, **kwargs: None),
    )
    return ctx, connection


def test_password_auth_prompts_before_docker_probe(gui, monkeypatch):
    from sshpilot import window
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, connection = _password_auth_ctx()
    page = DockerConsolePage(ctx, initial_host="web")
    prompts = []
    monkeypatch.setattr(
        window,
        "show_ssh_password_dialog",
        lambda **kwargs: prompts.append(kwargs) or "login-secret",
    )

    assert page._ensure_ssh_password("web") is True
    assert connection.password == "login-secret"
    assert prompts[0]["connection"] is connection
    assert prompts[0]["connection_manager"] is ctx.connection_manager


def test_saved_password_skips_docker_prompt(gui, monkeypatch):
    from sshpilot import window
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, connection = _password_auth_ctx(stored_password="saved-secret")
    page = DockerConsolePage(ctx, initial_host="web")
    monkeypatch.setattr(
        window,
        "show_ssh_password_dialog",
        lambda **kwargs: pytest.fail("saved password should skip the prompt"),
    )

    assert page._ensure_ssh_password("web") is True
    assert connection.password is None


def test_cancelled_password_stops_probe_and_polling(gui, monkeypatch):
    from sshpilot import window
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, _connection = _password_auth_ctx()
    page = DockerConsolePage(ctx, initial_host="web")
    monkeypatch.setattr(
        window, "show_ssh_password_dialog", lambda **kwargs: None)
    probes = []
    monkeypatch.setattr(
        page, "_run_async", lambda *args: probes.append(args))

    page._on_host_changed()

    assert probes == []
    assert "web" in page._ssh_auth_blocked
    assert page._tick() is True
    assert probes == []
    assert "SSH password required" in (
        page._containers_placeholder._label.get_text())
