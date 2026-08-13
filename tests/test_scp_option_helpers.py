from sshpilot.api.models import ConnectionId, StartScpTransferRequest, TransferDirection
from sshpilot.daemon.native_scp_backend import NativeScpBackend


def _request(**overrides):
    values = {
        "connection_id": ConnectionId("demo"),
        "direction": TransferDirection.UPLOAD,
        "sources": ("/tmp/a file", "/tmp/b;literal"),
        "destination": "/remote/drop",
    }
    values.update(overrides)
    return StartScpTransferRequest(**values)


def test_backend_build_operands_preserves_literal_paths():
    backend = NativeScpBackend.__new__(NativeScpBackend)
    sources, destination = backend.build_operands(
        _request(), "alice@[2001:db8::1]"
    )
    assert sources == ("/tmp/a file", "/tmp/b;literal")
    assert destination == "alice@[2001:db8::1]:/remote/drop"


def test_backend_build_argv_keeps_sources_before_builder_destination():
    backend = NativeScpBackend.__new__(NativeScpBackend)
    argv = backend.build_argv(
        _request(sources=("/tmp/source",)),
        "alice@example.test",
        ("/usr/bin/scp", "-F", "/tmp/ssh config", "alice@example.test:/remote/drop"),
    )
    assert argv == (
        "/usr/bin/scp",
        "-F",
        "/tmp/ssh config",
        "/tmp/source",
        "alice@example.test:/remote/drop",
    )


def test_gtk_controller_has_no_process_or_argv_builder():
    from sshpilot.scp_window import ScpWindowController

    assert not hasattr(ScpWindowController, "_build_scp_argv")
    assert not hasattr(ScpWindowController, "_prepare_scp_spawn_env")
    assert not hasattr(ScpWindowController, "_show_scp_terminal_window")
