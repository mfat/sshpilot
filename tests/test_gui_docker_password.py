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
    stored = stored_password is not None
    store_calls = []
    class Connection:
        nickname = "web"
        protocol = "ssh"
        auth_method = 1
        hostname = "example.test"
        username = "alice"

    connection = Connection()
    details = types.SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        nickname="web",
        display_name="web",
        host="example.test",
        hostname="example.test",
        username="alice",
        authentication_method=types.SimpleNamespace(value="password"),
    )
    connection_info = types.SimpleNamespace(
        id=details.id,
        nickname="web",
        protocol="ssh",
        host="example.test",
        username="alice",
    )
    manager = types.SimpleNamespace(
        find_connection_by_nickname=lambda _nick: connection,
    )
    client = types.SimpleNamespace(
        get_capabilities=lambda: types.SimpleNamespace(supports=lambda _cap: True),
        list_connections=lambda: [connection_info],
        get_connection=lambda _id: details,
        has_connection_password=lambda _id: stored,
        store_connection_password=lambda request: store_calls.append(request) or True,
    )
    ctx = types.SimpleNamespace(
        connection_manager=manager,
        daemon_client=lambda: client,
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
    return ctx, connection, store_calls


def test_password_auth_prompts_before_docker_probe(gui, monkeypatch):
    from sshpilot import window
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, _connection, store_calls = _password_auth_ctx()
    page = DockerConsolePage(ctx, initial_host="web")
    prompts = []
    monkeypatch.setattr(
        window,
        "show_ssh_password_dialog",
        lambda **kwargs: prompts.append(kwargs) or "login-secret",
    )

    assert page._ensure_ssh_password("web") is True
    assert store_calls[0].password == "login-secret"
    assert "connection" not in prompts[0]
    assert prompts[0]["allow_store"] is False


def test_saved_password_skips_docker_prompt(gui, monkeypatch):
    from sshpilot import window
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, _connection, store_calls = _password_auth_ctx(stored_password="saved-secret")
    page = DockerConsolePage(ctx, initial_host="web")
    monkeypatch.setattr(
        window,
        "show_ssh_password_dialog",
        lambda **kwargs: pytest.fail("saved password should skip the prompt"),
    )

    assert page._ensure_ssh_password("web") is True
    assert store_calls == []


def test_cancelled_password_stops_probe_and_polling(gui, monkeypatch):
    from sshpilot import window
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, _connection, _store_calls = _password_auth_ctx()
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
    # The placeholder is an Adw.StatusPage: its user-visible text is the title.
    # (It was a Gtk.Box with a `_label` child when this test was written.)
    assert "SSH password required" in page._containers_placeholder.get_title()
