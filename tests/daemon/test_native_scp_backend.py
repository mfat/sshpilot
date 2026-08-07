import threading
from types import SimpleNamespace

import pytest

from sshpilot.api import ErrorCode
from sshpilot.api.models import ConnectionId, StartScpTransferRequest, TransferDirection
from sshpilot.daemon.native_scp_backend import NativeScpBackend


class _Broker:
    def __init__(self):
        self.prepared = []
        self.cancelled = []

    def prepare_operation_launch(self, argv, env, **kwargs):
        self.prepared.append((tuple(argv), dict(env), kwargs))
        result = dict(env)
        result["SSH_ASKPASS"] = "/private/helper"
        result["SSHPILOT_DAEMON_ASKPASS_TOKEN"] = "secret-token"
        return tuple(argv), result

    def cancel_session(self, scope_id):
        self.cancelled.append(scope_id)


class _Provider:
    def __init__(self):
        self.calls = []

    def prepare_daemon_scp_target(self, connection_id):
        return "alice@[2001:db8::1]"

    def prepare_daemon_scp_launch(self, connection_id, **kwargs):
        self.calls.append((connection_id, kwargs))
        return (
            (
                "/usr/bin/scp",
                "-F",
                "/tmp/ssh config",
                *kwargs.get("extra_args", ()),
                kwargs["target_override"],
            ),
            {"PATH": "/usr/bin", "SENTINEL": "not-secret"},
        )


class _Process:
    def __init__(self, returncode=0, stderr=b""):
        self.pid = 1234
        self.returncode = returncode
        self.stderr = SimpleNamespace(read=lambda _limit: stderr)
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode if not self.terminated else -15

    def wait(self, timeout=None):
        if self.returncode is None and not self.terminated:
            raise __import__("subprocess").TimeoutExpired("scp", timeout)
        return self.returncode if not self.terminated else -15

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.terminated = True


class _Popen:
    def __init__(self, processes):
        self.processes = list(processes)
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append((tuple(argv), kwargs))
        return self.processes.pop(0)


def _request(**overrides):
    values = {
        "connection_id": ConnectionId("demo"),
        "direction": TransferDirection.UPLOAD,
        "sources": ("/tmp/a file", "/tmp/b;literal"),
        "destination": "/remote/drop",
    }
    values.update(overrides)
    return StartScpTransferRequest(**values)


def test_native_backend_builds_literal_multi_source_argv_without_shell():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([_Process()])
    backend = NativeScpBackend(provider, broker, popen=popen)

    backend.run(
        _request(),
        connection_target="alice@[2001:db8::1]",
        connection_id=ConnectionId("demo"),
        cancel_event=threading.Event(),
    )

    argv, kwargs = popen.calls[0]
    assert argv == (
        "/usr/bin/scp",
        "-F",
        "/tmp/ssh config",
        "/tmp/a file",
        "/tmp/b;literal",
        "alice@[2001:db8::1]:/remote/drop",
    )
    assert kwargs["shell"] is False
    assert kwargs["env"]["SENTINEL"] == "not-secret"
    assert "secret-token" in kwargs["env"]["SSHPILOT_DAEMON_ASKPASS_TOKEN"]
    assert broker.cancelled
    assert "secret-token" not in repr(_request())


def test_native_backend_retries_once_with_legacy_protocol_for_sftp_failure():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([
        _Process(returncode=1, stderr=b"subsystem request failed"),
        _Process(returncode=0),
    ])
    backend = NativeScpBackend(provider, broker, popen=popen)

    result = backend.run(
        _request(sources=("/tmp/source",)),
        connection_target="alice@example.test",
        connection_id=ConnectionId("demo"),
        cancel_event=threading.Event(),
    )

    assert result.returncode == 0
    assert popen.calls[1][0][1] == "-O"


def test_native_backend_does_not_retry_authentication_failure():
    provider = _Provider()
    broker = _Broker()
    popen = _Popen([_Process(returncode=1, stderr=b"Permission denied (publickey)")])
    backend = NativeScpBackend(provider, broker, popen=popen)

    with pytest.raises(Exception) as exc_info:
        backend.run(
            _request(sources=("/tmp/source",)),
            connection_target="alice@example.test",
            connection_id=ConnectionId("demo"),
            cancel_event=threading.Event(),
        )

    assert getattr(exc_info.value, "code", None) is ErrorCode.TRANSFER_IO_FAILED
    assert len(popen.calls) == 1


def test_typed_client_dispatches_to_native_scp_backend(tmp_path):
    from sshpilot.api import DaemonClient
    from sshpilot.core.connection_application_service import ConnectionApplicationService
    from sshpilot.daemon import DaemonServer
    from sshpilot.daemon.server import CoreServices
    from tests.helpers.fake_connection_repository import make_test_repository
    from tests.daemon.test_transfer_runtime import (
        _FakeScpBackend as RuntimeScpBackend,
        _scp_request,
    )
    base_service = ConnectionApplicationService(make_test_repository())
    backend = RuntimeScpBackend()
    from sshpilot.daemon.operation_runtime import OperationRuntime
    services = CoreServices(
        connections=base_service,
        scp_backend=backend,
        operations=OperationRuntime(),
    )
    socket_path = tmp_path / "daemon" / "sshpilotd.sock"
    socket_path.parent.mkdir(mode=0o700, parents=True)
    server = DaemonServer(
        lambda: services,
        socket_path=socket_path,
    )
    server.start_in_thread()
    client = DaemonClient(socket_path=server.socket_path)
    try:
        request = _scp_request()
        summary = client.start_scp_transfer(request)
        assert summary.backend.value == "native_scp"
        assert summary.sftp_service_id is None
    finally:
        client.close()
        server.shutdown()
        server.wait_stopped()


def test_native_backend_cancellation_terminates_process():
    provider = _Provider()
    broker = _Broker()
    process = _Process(returncode=None)
    popen = _Popen([process])
    backend = NativeScpBackend(provider, broker, popen=popen)
    cancelled = threading.Event()
    cancelled.set()

    with pytest.raises(Exception) as exc_info:
        backend.run(
            _request(sources=("/tmp/source",)),
            connection_target="alice@example.test",
            connection_id=ConnectionId("demo"),
            cancel_event=cancelled,
        )

    assert getattr(exc_info.value, "code", None) is ErrorCode.OPERATION_CANCELLED
    assert process.terminated or process.killed
