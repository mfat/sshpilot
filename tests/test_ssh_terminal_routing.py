from types import SimpleNamespace
from sshpilot.daemon_terminal_policy import SshTerminalRoute, resolve_ssh_terminal_route

class Config:
    def __init__(self, external=False): self.external = external
    def get_setting(self, key, default=None):
        return self.external if key == "use-external-terminal" else default

def test_internal_protocols_use_daemon(monkeypatch):
    monkeypatch.setattr("sshpilot.daemon_terminal_policy.should_hide_external_terminal_options", lambda: False)
    for protocol in ("ssh", "telnet", "mosh", "local_shell", "plugin"):
        assert resolve_ssh_terminal_route(Config(), SimpleNamespace(protocol=protocol)) is SshTerminalRoute.DAEMON

def test_explicit_external_terminal_remains_external(monkeypatch):
    monkeypatch.setattr("sshpilot.daemon_terminal_policy.should_hide_external_terminal_options", lambda: False)
    assert resolve_ssh_terminal_route(Config(True), SimpleNamespace(protocol="ssh")) is SshTerminalRoute.EXTERNAL
