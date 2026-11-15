"""Pin the production terminal runners to ``TerminalSessionProcessRunner``.

``SessionRuntime`` calls a terminal-capable runner with four arguments and
then drives the handle's terminal I/O. These checks keep the declared
contract in ``session_runtime`` and the PTY/ConPTY runners from drifting
apart again. Behaviour (callback ordering, exactly-once delivery) is covered
by the runner and runtime test modules; this file pins only the shape.
"""

from __future__ import annotations

import inspect

import pytest

from sshpilot.daemon import conpty_runner, pty_runner
from sshpilot.daemon.session_runtime import (
    SessionRuntime,
    TerminalSessionProcessHandle,
    TerminalSessionProcessRunner,
    UnsupportedSessionProcessRunner,
    runner_is_terminal_capable,
)


RUNNERS = [
    pytest.param(pty_runner.PtySessionProcessRunner, id="pty"),
    pytest.param(conpty_runner.ConPtySessionProcessRunner, id="conpty"),
]
HANDLES = [
    pytest.param(pty_runner.PtyProcessHandle, id="pty"),
    pytest.param(conpty_runner.ConPtyProcessHandle, id="conpty"),
]


def _parameter_names(function) -> list[str]:
    return list(inspect.signature(function).parameters)


@pytest.mark.parametrize("runner_class", RUNNERS)
def test_terminal_runner_start_matches_protocol(runner_class) -> None:
    assert _parameter_names(runner_class.start) == _parameter_names(
        TerminalSessionProcessRunner.start
    )


@pytest.mark.parametrize("runner_class", RUNNERS)
def test_terminal_runner_declares_terminal_capable(runner_class) -> None:
    assert runner_class.terminal_capable is True
    assert callable(getattr(runner_class, "close", None))


@pytest.mark.parametrize("handle_class", HANDLES)
@pytest.mark.parametrize(
    "method",
    ["write", "input_status", "resize", "terminate", "kill", "wait"],
)
def test_terminal_handle_implements_protocol_method(handle_class, method) -> None:
    assert hasattr(TerminalSessionProcessHandle, method)
    implementation = getattr(handle_class, method, None)
    assert callable(implementation)
    assert _parameter_names(implementation) == _parameter_names(
        getattr(TerminalSessionProcessHandle, method)
    )


def test_plain_runner_is_not_terminal_capable() -> None:
    assert runner_is_terminal_capable(UnsupportedSessionProcessRunner()) is False


def test_runtime_reports_terminal_support_from_the_runner() -> None:
    class _TerminalRunner:
        terminal_capable = True

        def start(self, spec, on_exit, on_output, on_eof):  # pragma: no cover
            raise AssertionError("not started in this test")

        def close(self) -> None:
            return

    assert SessionRuntime(object(), runner=_TerminalRunner()).terminal_supported
    assert not SessionRuntime(object()).terminal_supported
