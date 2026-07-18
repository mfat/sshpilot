"""Presence reminder auto-dismisses when the askpass helper goes away.

OpenSSH SIGTERMs the helper once the security key is touched; the helper's
socket EOF must close the routed main-app dialog instead of leaving it stuck.
"""
import json

from sshpilot.askpass_server import AskpassPromptServer


class FakeStream:
    def write_all(self, payload, _cancellable):
        self.payload = payload

    def flush(self, _cancellable):
        pass


class FakeConnection:
    def __init__(self):
        self.stream = FakeStream()

    def get_output_stream(self):
        return self.stream

    def close(self, _cancellable):
        pass


class FakeDataIn:
    """Captures the peer-EOF watcher instead of doing real Gio I/O."""

    def __init__(self):
        self.callback = None

    def read_line_async(self, _priority, _cancellable, callback, _data):
        self.callback = callback

    def read_line_finish_utf8(self, _result):
        return None, 0  # EOF


class FakeWindow:
    """prompt_ssh_presence normally blocks in a nested loop; here we hold the
    registered closer so the test can fire the EOF callback 'mid-dialog'."""

    def __init__(self):
        self.closer = None
        self.closed = False

    def prompt_ssh_presence(self, prompt, register_close=None):
        if register_close is not None:
            register_close(lambda: setattr(self, "closed", True))
        return True


def test_presence_peer_eof_closes_dialog():
    window = FakeWindow()
    server = AskpassPromptServer(window)
    server._token = "tok"
    connection = FakeConnection()
    data_in = FakeDataIn()

    request = json.dumps({"token": "tok", "type": "presence", "prompt": "touch"})
    server._handle_request(request, connection, data_in)

    # The EOF watcher was armed while the dialog was up.
    assert data_in.callback is not None
    # Reply is ok+"" (acknowledged), not a cancel.
    assert json.loads(connection.stream.payload.decode()) == {
        "ok": True,
        "value": "",
    }


def test_presence_eof_callback_invokes_registered_closer():
    window = FakeWindow()
    server = AskpassPromptServer(window)
    server._token = "tok"
    data_in = FakeDataIn()

    captured = {}

    def _prompt(prompt, register_close=None):
        register_close(lambda: captured.setdefault("closed", True))
        # Simulate the helper dying while the dialog is still open.
        data_in.callback(data_in, None)
        return True

    window.prompt_ssh_presence = _prompt
    request = json.dumps({"token": "tok", "type": "presence", "prompt": "touch"})
    server._handle_request(request, FakeConnection(), data_in)

    assert captured.get("closed") is True
