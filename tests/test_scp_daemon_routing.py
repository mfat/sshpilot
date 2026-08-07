from types import SimpleNamespace

from sshpilot.api import Capability
from sshpilot.scp_window import ScpWindowController


class _Bridge:
    def __init__(self):
        self.calls = []

    def submit(self, operation, *, on_success, on_error, **_kwargs):
        self.calls.append((operation, on_success, on_error))
        return SimpleNamespace(cancel=lambda: None)


class _Client:
    def __init__(self, capabilities):
        self.capabilities = capabilities
        self.started = []
        self.cancelled = []

    def get_capabilities(self):
        return self.capabilities

    def start_scp_transfer(self, request):
        self.started.append(request)
        return SimpleNamespace(id="transfer-1")

    def cancel_transfer(self, request):
        self.cancelled.append(request)


def _controller(client):
    controller = ScpWindowController.__new__(ScpWindowController)
    controller.window = SimpleNamespace(client=client, client_bridge=_Bridge())
    controller._show_transfer_error = lambda message: setattr(controller, "error", message)
    return controller


def test_scp_start_uses_typed_client_and_never_local_process(monkeypatch):
    class Label:
        def set_wrap(self, _value):
            return None

        def set_halign(self, _value):
            return None

        def set_text(self, _value):
            return None

    class Dialog:
        def __init__(self, _title):
            self.content_box = SimpleNamespace(append=lambda _item: None)
            self.cancel_btn = SimpleNamespace()

        def connect(self, *_args):
            return None

        def present(self, *_args):
            return None

    monkeypatch.setattr("sshpilot.scp_window.ScpTransferDialog", Dialog)
    monkeypatch.setattr("sshpilot.scp_window.Gtk.Label", Label)
    client = _Client(SimpleNamespace(supports=lambda capability: capability is Capability.TRANSFERS_SCP))
    controller = _controller(client)
    controller.start_scp_transfer(
        SimpleNamespace(id="demo", nickname="demo"),
        ["/tmp/a file"],
        "/remote/drop",
        direction="upload",
    )

    operation, on_success, _on_error = controller.window.client_bridge.calls[0]
    summary = operation()
    on_success(summary)
    assert len(client.started) == 1
    assert client.started[0].sources == ("/tmp/a file",)
    assert client.started[0].destination == "/remote/drop"
    assert not hasattr(controller, "_show_scp_terminal_window")


def test_scp_start_rejects_missing_capability_without_fallback():
    client = _Client(SimpleNamespace(supports=lambda _capability: False))
    controller = _controller(client)
    controller.start_scp_transfer(
        SimpleNamespace(id="demo", nickname="demo"),
        ["/tmp/file"],
        "/remote/drop",
        direction="upload",
    )

    assert "unavailable" in controller.error.lower()
    assert controller.window.client_bridge.calls == []


def test_scp_controller_has_no_subprocess_or_vte_ownership():
    source = open("src/sshpilot/scp_window.py", encoding="utf-8").read()
    for forbidden in (
        "TerminalWidget",
        "spawn_async",
        "bash",
        "subprocess",
        "list_remote_files",
        "resolve_native_auth",
        "_build_scp_argv",
    ):
        assert forbidden not in source
