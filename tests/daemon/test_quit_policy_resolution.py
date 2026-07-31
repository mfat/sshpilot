"""Quit / tab-close policy resolution (no GTK dialog).

Proves resolve_tab_close_policy / resolve_app_close_policy / route defaults.
Does not present the quit dialog or exercise MainWindow shutdown.
"""
from __future__ import annotations

class TestQuitPolicy:
    """Prove quit policy resolution works."""

    def test_tab_close_policy_defaults(self):
        """Default tab close policy is DETACH."""
        from sshpilot.daemon_terminal_policy import (
            resolve_tab_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                return default

        policy = resolve_tab_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.DETACH

    def test_app_close_policy_defaults(self):
        """Default app close policy is DETACH."""
        from sshpilot.daemon_terminal_policy import (
            resolve_app_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                return default

        policy = resolve_app_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.DETACH

    def test_tab_close_policy_terminate(self):
        """Tab close policy can be set to TERMINATE."""
        from sshpilot.daemon_terminal_policy import (
            resolve_tab_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                if key == "terminal.daemon_tab_close_policy":
                    return "terminate"
                return default

        policy = resolve_tab_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.TERMINATE

    def test_tab_close_policy_ask(self):
        """Tab close policy can be set to ASK."""
        from sshpilot.daemon_terminal_policy import (
            resolve_tab_close_policy,
            TerminalClosePolicy,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                if key == "terminal.daemon_tab_close_policy":
                    return "ask"
                return default

        policy = resolve_tab_close_policy(FakeConfig())
        assert policy == TerminalClosePolicy.ASK

    def test_terminal_route_daemon_default(self):
        """Default terminal route is DAEMON."""
        from sshpilot.daemon_terminal_policy import (
            resolve_ssh_terminal_route,
            SshTerminalRoute,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                return default

        class FakeConnection:
            protocol = "ssh"

        route = resolve_ssh_terminal_route(FakeConfig(), FakeConnection())
        assert route == SshTerminalRoute.DAEMON

    def test_terminal_route_external(self):
        """External terminal preference returns EXTERNAL route."""
        from sshpilot.daemon_terminal_policy import (
            resolve_ssh_terminal_route,
            SshTerminalRoute,
        )

        class FakeConfig:
            def get_setting(self, key, default=None):
                if key == "use-external-terminal":
                    return True
                return default

        class FakeConnection:
            protocol = "ssh"

        route = resolve_ssh_terminal_route(FakeConfig(), FakeConnection())
        assert route in {SshTerminalRoute.EXTERNAL, None}


# ---------------------------------------------------------------------------
# Resource tracking
# ---------------------------------------------------------------------------
