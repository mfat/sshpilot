from sshpilot.api.models import ConnectionId, StartScpTransferRequest, TransferDirection
from sshpilot.daemon.native_scp_backend import NativeScpBackend


class _Provider:
    def prepare_daemon_scp_target(self, connection_id):
        return "alice@testbox"

    def prepare_scp_launch(self, connection_id, **kwargs):
        return (
            (
                "/usr/bin/scp",
                "-F",
                "/tmp/ssh config",
                *kwargs.get("extra_args", ()),
                kwargs["target_override"],
            ),
            {"PATH": "/usr/bin"},
        )


def test_native_backend_preserves_alias_and_literal_proxy_config_boundary():
    request = StartScpTransferRequest(
        connection_id=ConnectionId("demo"),
        direction=TransferDirection.UPLOAD,
        sources=("local.txt",),
        destination="/remote/path",
    )
    backend = NativeScpBackend.__new__(NativeScpBackend)
    sources, destination = backend.build_operands(request, "alice@testbox")
    assert sources == ("local.txt",)
    assert destination == "alice@testbox:/remote/path"


def test_native_backend_places_recursive_flag_in_daemon_launch_args():
    request = StartScpTransferRequest(
        connection_id=ConnectionId("demo"),
        direction=TransferDirection.UPLOAD,
        sources=("/tmp/folder",),
        destination="/remote/path",
        recursive=True,
    )
    backend = NativeScpBackend(_Provider(), object())
    argv = backend.build_argv(
        request,
        "alice@testbox",
        (
            "/usr/bin/scp",
            "-r",
            "/tmp/folder",
            "alice@testbox:/remote/path",
        ),
    )
    assert "-r" in argv
    assert argv[-1] == "alice@testbox:/remote/path"
