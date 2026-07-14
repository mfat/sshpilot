"""GUI regressions for Docker Console's connection-free Local target."""

import types

import pytest

from tests._gui_harness import requires_gui


requires_gui()

pytestmark = pytest.mark.gui


class _Result:
    exit_code = 0
    stdout = ""
    stderr = ""


def _local_ctx():
    calls = []
    terminal_calls = []
    ctx = types.SimpleNamespace(
        connection_manager=types.SimpleNamespace(
            find_connection_by_nickname=lambda nickname: None),
        run_command=lambda *args, **kwargs:
            pytest.fail("Local target must not use remote SSH command API"),
        run_local_command=lambda command, **kwargs:
            calls.append((command, kwargs)) or _Result(),
        run_on_ui_thread=lambda fn, *args: fn(*args),
        settings=types.SimpleNamespace(
            get=lambda key, default=None: default,
            set=lambda key, value: None,
        ),
        list_connections=list,
        open_command_terminal=lambda *args, **kwargs:
            pytest.fail("Local target must not open a remote SSH terminal"),
        open_local_command_terminal=lambda command, **kwargs:
            terminal_calls.append((command, kwargs)) or True,
        ui=types.SimpleNamespace(notify=lambda *args, **kwargs: None),
    )
    return ctx, calls, terminal_calls


def test_local_target_exists_without_ssh_connections(gui):
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, calls, _terminal_calls = _local_ctx()
    page = DockerConsolePage(ctx)

    assert page._current_nickname() == "__local__"
    assert page._host_label.get_label() == "Local"
    assert page._ensure_ssh_password("__local__") is True
    assert page._client().ping().exit_code == 0
    assert calls == [("docker ps -q", {"timeout": 30})]


def test_local_interactive_action_uses_local_terminal(gui):
    from sshpilot.plugins.builtin.docker_manager.page import DockerConsolePage

    ctx, _calls, terminal_calls = _local_ctx()
    page = DockerConsolePage(ctx)

    assert page._open_command_terminal(
        "__local__", "docker logs -f web", title="Web logs")
    assert terminal_calls == [(
        "docker logs -f web",
        {"title": "Web logs", "pty_prompt": None, "pty_response": None},
    )]
